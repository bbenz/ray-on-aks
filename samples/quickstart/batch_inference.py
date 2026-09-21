import argparse
import json
from pathlib import Path

import ray


MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"
MODEL_REVISION = "7ae557604adf67be50417f59c2c2f167def9a775"
DELIVERY_EVENTS = [
    {"id": "DLV-001", "event": "The parcel arrived at the local depot this morning."},
    {"id": "DLV-002", "event": "The driver could not access the building and will retry tomorrow."},
    {"id": "DLV-003", "event": "The recipient signed for the parcel at 10:15."},
    {"id": "DLV-004", "event": "A road closure delayed the truck by two hours."},
]


@ray.remote(num_cpus=1, num_gpus=1)
class DeliverySummarizer:
    def __init__(self):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if not torch.cuda.is_available():
            raise RuntimeError("The inference actor cannot access a CUDA GPU.")
        self.torch = torch
        self.device_name = torch.cuda.get_device_name(0)
        self.tokenizer = AutoTokenizer.from_pretrained(
            MODEL_ID,
            revision=MODEL_REVISION,
            padding_side="left",
            trust_remote_code=False,
        )
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        self.model = AutoModelForCausalLM.from_pretrained(
            MODEL_ID,
            revision=MODEL_REVISION,
            torch_dtype=torch.float16,
            attn_implementation="eager",
            use_safetensors=True,
            trust_remote_code=False,
        ).to("cuda")
        self.model.eval()

    def summarize(self, records):
        prompts = [
            self.tokenizer.apply_chat_template(
                [
                    {
                        "role": "system",
                        "content": "Summarize the delivery status in one short sentence. Do not invent details.",
                    },
                    {"role": "user", "content": record["event"]},
                ],
                tokenize=False,
                add_generation_prompt=True,
            )
            for record in records
        ]
        encoded = self.tokenizer(prompts, return_tensors="pt", padding=True)
        inputs = {name: tensor.to("cuda") for name, tensor in encoded.items()}
        with self.torch.inference_mode():
            generated = self.model.generate(
                **inputs,
                max_new_tokens=48,
                do_sample=False,
                pad_token_id=self.tokenizer.pad_token_id,
            )
        completions = self.tokenizer.batch_decode(
            generated[:, inputs["input_ids"].shape[1] :],
            skip_special_tokens=True,
        )
        return [
            {
                "id": record["id"],
                "event": record["event"],
                "summary": completion.strip(),
                "gpu": self.device_name,
                "model": MODEL_ID,
                "model_revision": MODEL_REVISION,
            }
            for record, completion in zip(records, completions, strict=True)
        ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="/tmp/ray-inference-results.jsonl")
    args = parser.parse_args()
    requirements = Path(__file__).with_name("requirements-inference.txt")
    ray.init(
        address="auto",
        runtime_env={
            "pip": str(requirements),
            "env_vars": {
                "HF_HUB_DISABLE_TELEMETRY": "1",
                "TOKENIZERS_PARALLELISM": "false",
            },
        },
        log_to_driver=False,
    )
    try:
        actor = DeliverySummarizer.remote()
        results = ray.get(actor.summarize.remote(DELIVERY_EVENTS), timeout=1200)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as stream:
            for result in results:
                line = json.dumps(result, ensure_ascii=True)
                stream.write(line + "\n")
                print(line, flush=True)
    finally:
        ray.shutdown()


if __name__ == "__main__":
    main()
