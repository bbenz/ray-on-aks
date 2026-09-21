import json
from pathlib import Path

import ray


@ray.remote(num_cpus=1, num_gpus=1)
def check_gpu():
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("The Ray worker cannot access a CUDA GPU.")
    values = torch.arange(1, 5, device="cuda", dtype=torch.float32)
    return {
        "gpu": torch.cuda.get_device_name(0),
        "cuda_runtime": torch.version.cuda,
        # TorchVersion would otherwise require torch when the CPU driver unpickles it.
        "torch_version": str(torch.__version__),
        "tensor_sum": values.sum().item(),
    }


def main():
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
        result = ray.get(check_gpu.remote(), timeout=1200)
        print(json.dumps(result), flush=True)
    finally:
        ray.shutdown()


if __name__ == "__main__":
    main()
