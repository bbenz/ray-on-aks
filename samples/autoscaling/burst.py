"""Run a bounded, GPU-backed Qwen inference burst on the existing Ray cluster."""

import argparse
import importlib.metadata
import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import ray


MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"
MODEL_REVISION = "7ae557604adf67be50417f59c2c2f167def9a775"
PROMPT = "Explain the difference between a Kubernetes pod and a node in two sentences."


def emit(event, **fields):
    print(
        json.dumps(
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "event": event,
                **fields,
            },
            sort_keys=True,
        ),
        flush=True,
    )


@ray.remote(
    num_cpus=2,
    num_gpus=1,
    max_calls=1,
    max_retries=1,
    retry_exceptions=False,
)
def infer_for_window(index, duration_seconds, max_new_tokens):
    attempt = uuid4().hex
    identity = {
        "task_index": index,
        "attempt_id": attempt,
        "pod": os.environ.get("HOSTNAME", "unknown"),
        "node_id": ray.get_runtime_context().get_node_id(),
    }
    emit("task_started", **identity)

    import torch
    import transformers
    from transformers import AutoModelForCausalLM, AutoTokenizer

    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError("This task requires exactly one visible, usable CUDA GPU.")
    torch.set_num_threads(2)
    torch.cuda.set_device(0)
    emit(
        "runtime_ready",
        **identity,
        ray_version=ray.__version__,
        torch_version=torch.__version__,
        cuda_runtime=torch.version.cuda,
        transformers_version=transformers.__version__,
        pydantic_version=importlib.metadata.version("pydantic"),
        gpu=torch.cuda.get_device_name(0),
        resolved_packages=sorted(
            f"{name}=={importlib.metadata.version(name)}"
            for name in {
                distribution.metadata["Name"]
                for distribution in importlib.metadata.distributions()
                if distribution.metadata["Name"]
            }
        ),
    )

    emit("model_load_started", **identity, model=MODEL_ID, revision=MODEL_REVISION)
    tokenizer = AutoTokenizer.from_pretrained(
        MODEL_ID,
        revision=MODEL_REVISION,
        trust_remote_code=False,
    )
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_ID,
        revision=MODEL_REVISION,
        torch_dtype=torch.float16,
        attn_implementation="eager",
        use_safetensors=True,
        trust_remote_code=False,
    ).to("cuda:0")
    model.eval()
    torch.cuda.synchronize()
    emit("model_ready", **identity)

    text = tokenizer.apply_chat_template(
        [{"role": "user", "content": PROMPT}],
        tokenize=False,
        add_generation_prompt=True,
    )
    inputs = tokenizer(text, return_tensors="pt").to("cuda:0")
    input_length = inputs["input_ids"].shape[1]
    started = time.monotonic()
    deadline = started + duration_seconds
    next_progress = started + 60
    generations = 0
    generated_tokens = 0
    first_text = None
    emit("generation_started", **identity)

    with torch.inference_mode():
        while generations == 0 or time.monotonic() < deadline:
            output = model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=False,
                pad_token_id=tokenizer.eos_token_id,
            )
            torch.cuda.synchronize()
            token_ids = output[0, input_length:]
            generations += 1
            generated_tokens += token_ids.numel()
            if first_text is None:
                first_text = tokenizer.decode(token_ids, skip_special_tokens=True)
                emit("first_generation_completed", **identity, text=first_text)
            now = time.monotonic()
            if now >= next_progress:
                emit("task_progress", **identity, generations=generations)
                next_progress = now + 60

    result = {
        **identity,
        "generations": generations,
        "generated_tokens": generated_tokens,
        "inference_window_seconds": time.monotonic() - started,
        "first_text": first_text,
    }
    emit("task_completed", **result)
    return result


def stop_requested(signum, frame):
    raise KeyboardInterrupt("Job termination requested.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, choices=range(1, 5), default=4)
    parser.add_argument("--duration-seconds", type=int, default=600)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--max-new-tokens", type=int, default=64)
    args = parser.parse_args()
    if not 1 <= args.duration_seconds <= 1800:
        parser.error("--duration-seconds must be between 1 and 1800.")
    if not args.duration_seconds < args.timeout_seconds <= 7200:
        parser.error("--timeout-seconds must exceed the duration and be at most 7200.")
    if not 1 <= args.max_new_tokens <= 256:
        parser.error("--max-new-tokens must be between 1 and 256.")

    runtime_env = json.loads(
        Path(__file__).with_name("worker-runtime-env.json").read_text(encoding="utf-8")
    )
    signal.signal(signal.SIGTERM, stop_requested)
    pending = []
    completed = 0
    try:
        ray.init(address="auto", log_to_driver=True)
        deadline = time.monotonic() + args.timeout_seconds
        emit(
            "burst_submitted",
            tasks=args.tasks,
            duration_seconds=args.duration_seconds,
            timeout_seconds=args.timeout_seconds,
            max_new_tokens=args.max_new_tokens,
            prompt=PROMPT,
        )
        for index in range(args.tasks):
            pending.append(
                infer_for_window.options(runtime_env=runtime_env).remote(
                    index,
                    args.duration_seconds,
                    args.max_new_tokens,
                )
            )
        next_progress = time.monotonic() + 30
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Burst deadline exceeded; cancelling unfinished tasks.")
            ready, _ = ray.wait(pending, num_returns=1, timeout=min(5, remaining))
            for reference in ready:
                result = ray.get(reference)
                pending.remove(reference)
                completed += 1
                emit("result_received", **result)
            if time.monotonic() >= next_progress:
                emit("burst_progress", completed=completed, unfinished=len(pending))
                next_progress = time.monotonic() + 30
        emit("burst_succeeded", completed=completed)
    except KeyboardInterrupt as error:
        emit("burst_stopped", error_type=type(error).__name__, error=str(error))
        raise
    except (ray.exceptions.RayError, TimeoutError, OSError, ValueError) as error:
        emit("burst_failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        for reference in pending:
            try:
                ray.cancel(reference, force=True, recursive=True)
            except (ray.exceptions.RayError, RuntimeError, ValueError) as error:
                emit("cancellation_failed", error=str(error))
        pending.clear()
        ray.shutdown()


if __name__ == "__main__":
    main()
