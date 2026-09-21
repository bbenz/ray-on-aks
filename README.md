# Ray on AKS samples

Companion code for the Ray on Azure Kubernetes Service articles: a Learn module,
a quickstart, and how-to guides for security, autoscaling, and observability.

The samples run a small, bounded GPU workload on a single NVIDIA T4: a CUDA
check, then batch inference with a pinned Qwen2.5-0.5B-Instruct revision. They
then add network isolation, token authentication, managed Prometheus scraping,
and a real Ray autoscaling exercise.

> [!IMPORTANT]
> These are development-environment exercises for trusted users and trusted
> code. A Ray cluster isn't a security boundary between its users. GPU nodes
> bill while they idle, while dependencies download, and while you troubleshoot.
> Read [`SECURITY.md`](SECURITY.md) and
> [`docs/validation-status.md`](docs/validation-status.md) before you rely on
> anything here.

## Layout

| Path | Contents |
| --- | --- |
| [`samples/quickstart`](samples/quickstart) | RayCluster, GPU check, batch inference, and the two RayJobs. |
| [`samples/security`](samples/security) | Fixed ports, network policies, an egress proxy, token auth, and the probes that test allowed and denied paths. |
| [`samples/monitoring`](samples/monitoring) | Managed Prometheus `PodMonitor` and a Grafana dashboard. |
| [`samples/autoscaling`](samples/autoscaling) | Autoscaling RayCluster, burst workload, in-cluster observer, and an offline run verifier. |
| [`scripts`](scripts) | PowerShell helpers that run the documented commands with precondition checks. |
| [`tests`](tests) | Offline checks that keep the samples internally consistent. |
| [`docs`](docs) | Article map, validation status, known issues, and the POC runbook. |

## Prerequisites

- An AKS cluster with a schedulable Linux CPU pool, and quota for one
  `Standard_NC4as_T4_v3`. Quota isn't capacity.
- Azure CLI with the `aks-preview` extension for AKS-managed GPU node pools,
  `kubectl`, Helm 3, and PowerShell 7.
- Permission to install the KubeRay operator, or an existing one you're allowed
  to use.

Pinned versions: KubeRay `1.7.0`, Ray `2.58.0` on Python 3.11, `torch==2.8.0`,
`transformers==4.57.1`, with CUDA 12.8 worker images. Image digests are pinned
in every manifest.

## Quick start

```powershell
git clone <this-repo> ray-on-aks
cd ray-on-aks

./scripts/Add-RayGpuNodePool.ps1 -ResourceGroup <rg> -ClusterName <aks> -WhatIf
./scripts/Add-RayGpuNodePool.ps1 -ResourceGroup <rg> -ClusterName <aks>
./scripts/Install-KubeRay.ps1
./scripts/Deploy-RayQuickstart.ps1
./scripts/Invoke-RayQuickstartJobs.ps1 -OutputDirectory ./out
```

Every state-changing script supports `-WhatIf`. When you're done:

```powershell
./scripts/Remove-RayQuickstart.ps1 -RemoveGpuPool -ResourceGroup <rg> -ClusterName <aks>
```

Deleting Ray objects doesn't stop the GPU VM charge. Removing or scaling the
node pool does.

## Run the checks

```powershell
python -m venv .venv
./.venv/Scripts/python -m pip install -r tests/requirements-dev.txt
./.venv/Scripts/python -m pytest
```

The suite is offline. It parses every manifest and script, and asserts the
things that have to agree across files: pinned image digests, Ray's ports versus
the ports the policies allow, the PodMonitor port versus the container port, the
submitter label versus the policy selector, and the object store versus
`/dev/shm`. It also runs the autoscaling verifier against recorded event
fixtures.

To also compare the samples with the article drafts:

```powershell
$env:RAY_ON_AKS_DOCS = "<folder containing the article markdown>"
./.venv/Scripts/python -m pytest tests/test_docs_sync.py
```

Passing tests don't prove a cluster works. They prove the samples are
self-consistent.

## What's been run for real

A live AKS POC exercised the GPU check, batch inference, the isolation and
authentication controls, managed metrics, and a four-worker `1 -> 4 -> 1`
autoscaling cycle. It also surfaced several failures that are fixed here, and
some that remain open.
[`docs/validation-status.md`](docs/validation-status.md) lists both, with the
limits of that evidence. [`docs/known-issues.md`](docs/known-issues.md) explains
the failures worth knowing about before you hit them.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). The samples are duplicated in the
article drafts while those drafts are in review; keep the two copies identical
and let `tests/test_docs_sync.py` prove it.
