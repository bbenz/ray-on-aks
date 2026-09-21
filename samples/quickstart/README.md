# Quickstart samples

Companion code for **Quickstart: Run a Ray workload on a single GPU in AKS** and
units 4-6 of the **Get started with Ray on AKS** module.

| File | Purpose |
| --- | --- |
| `raycluster-t4.yaml` | RayCluster with a CPU head and one T4 worker. Image digests pinned. |
| `gpu_check.py` | Ray task that requests one GPU and returns device, CUDA, PyTorch, and a tensor sum. |
| `batch_inference.py` | GPU actor that loads a pinned Qwen2.5-0.5B-Instruct revision and summarizes four synthetic delivery events. |
| `requirements-inference.txt` | Pinned `torch` and `transformers` for the Ray runtime environment used by both scripts. |
| `rayjob-gpu-check.yaml` | RayJob that runs `gpu_check.py` against the existing cluster. |
| `rayjob-inference.yaml` | RayJob that runs `batch_inference.py` and writes JSON Lines on the head pod. |
| `spot-worker.patch.json` | Optional JSON patch adding the Azure Spot toleration to the worker group. |

## Run order

```powershell
kubectl create namespace ray-learn
kubectl -n ray-learn create configmap ray-learning-code `
  --from-file=.\gpu_check.py `
  --from-file=.\batch_inference.py `
  --from-file=.\requirements-inference.txt
kubectl apply -f .\raycluster-t4.yaml
kubectl -n ray-learn wait raycluster/ray-t4 "--for=jsonpath={.status.state}=ready" --timeout=20m
kubectl apply -f .\rayjob-gpu-check.yaml
kubectl apply -f .\rayjob-inference.yaml
```

`scripts/Deploy-RayQuickstart.ps1` and `scripts/Invoke-RayQuickstartJobs.ps1` run
the same steps with precondition checks.

## Things that are easy to get wrong

- **Node pool names.** Both manifests select `kubernetes.azure.com/agentpool: nodepool1`
  for the head and submitter, and `raygpu` for the GPU worker. Change every
  occurrence together if your pools differ.
- **Keep the two runtime environments identical.** `gpu_check.py` and
  `batch_inference.py` deliberately pass the same `runtime_env`, including
  `env_vars`. Two variants that differ only by environment variables resolve to
  the same pip URI, and Ray 2.58 counts that URI's size once per variant. On a
  worker that rejoins an existing cluster, that accounting can evict the shared
  virtual environment and leave actors failing with `ActorUnschedulableError`.
  See `docs/known-issues.md`.
- **Spot nodes.** Add `spot-worker.patch.json` only when the GPU pool uses Spot
  priority; on-demand nodes don't carry that taint.

  ```powershell
  kubectl patch --local -f .\raycluster-t4.yaml --type json `
    --patch-file .\spot-worker.patch.json -o yaml | Set-Content -Encoding utf8 .\raycluster-spot.yaml
  ```

- **Results aren't durable.** `batch_inference.py` writes to the head pod's
  `/tmp`. Copy the file out before you delete or replace the cluster.
