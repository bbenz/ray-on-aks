# Autoscaling samples

Companion code for **Autoscale Ray workloads on AKS**. These run in their own
`ray-scale` namespace and their own GPU node pool, separate from the quickstart.

| File | Purpose | Live-tested |
| --- | --- | --- |
| `raycluster.yaml` | RayCluster with in-tree Ray autoscaling (v2), token auth, and a worker group bounded at 1-4 T4 workers. | Yes |
| `burst.py` | Submits up to four bounded GPU inference tasks so Ray reports real pending demand. | Yes |
| `worker-runtime-env.json` | Per-task runtime environment: hashed CUDA wheel, pinned packages, `pip_check: true`. | Yes |
| `observe_scaling.py` | Read-only in-cluster observer that samples Ready GPU nodes, Ready Ray workers, and Kubernetes events. | Yes |
| `scaling-observer.yaml` | ServiceAccount, scoped RBAC, and Job that run the observer. | Yes |
| `verify_scaling.py` | Offline checker: reads the burst log and observer log and asserts a real 1 -> N -> 1 cycle with distinct workers. | Yes |

## Why a burst script instead of a load generator

**Ray scales on logical resource demand, not GPU utilization.** Each task in
`burst.py` requests `num_cpus=2, num_gpus=1`, so four queued tasks create demand
for four workers. A busy GPU with an empty queue produces no scale-out.

## Run order

```powershell
kubectl create namespace ray-scale
kubectl -n ray-scale create configmap ray-scale-code `
  --from-file=burst.py=.\burst.py `
  --from-file=worker-runtime-env.json=.\worker-runtime-env.json
kubectl apply -f .\raycluster.yaml

kubectl -n ray-scale create configmap scaling-observer-code --from-file=.\observe_scaling.py
kubectl apply -f .\scaling-observer.yaml
```

Then submit the burst from the head pod, for example:

```text
python /home/ray/autoscaling/burst.py --tasks 4 --duration-seconds 1800 --timeout-seconds 5400
```

Afterwards, verify the run offline instead of trusting a screenshot:

```powershell
python .\verify_scaling.py --burst-log .\burst.log --observer-log .\observer.jsonl --tasks 4 --duration-seconds 1800
```

## Prerequisites the sample does not create

- A GPU node pool with the cluster autoscaler enabled and a maximum that matches
  `maxReplicas`. Ray asks for pods; only the AKS autoscaler adds nodes.
- A GPU device plugin on that pool. **Wait for the node to advertise allocatable
  `nvidia.com/gpu` before submitting work.** A Running plugin pod is not the same
  as a registered device, and a GPU check that runs in that gap fails.
- Don't install a second device plugin on an AKS-managed GPU pool.

## Timing observations, not guarantees

In a single Spot T4 run with four tasks at 1800 seconds each: four Ready GPU
workers about 15 minutes after submission, all tasks complete about 48 minutes
after submission, and a stable return to one worker about 62 minutes after
submission. The first worker's caches were warm; the three added nodes were
cold. Those numbers include node provisioning, image pulls, dependency
installation, and the configured idle timeout. Measure your own.
