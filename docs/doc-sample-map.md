# Which sample belongs to which article

The articles are in review and currently carry their own copy of these samples
so they read standalone. `tests/test_docs_sync.py` fails if the two copies
drift. When the articles move to linking this repository, that mapping becomes
the link target list.

## Get started with Ray on AKS (Learn module)

| Unit | Samples |
| --- | --- |
| 2. What Ray adds on top of Kubernetes | Inline snippets only. |
| 3. Plan the cluster shape | `samples/quickstart/raycluster-t4.yaml` |
| 4. Exercise: run a GPU task | `samples/quickstart/gpu_check.py`, `rayjob-gpu-check.yaml` |
| 5. Exercise: batch inference | `samples/quickstart/batch_inference.py`, `rayjob-inference.yaml`, `requirements-inference.txt` |
| 6. Knowledge check | None. |
| 7. Summary | None. |

## Quickstart: Run a Ray workload on a single GPU

| Step | Samples |
| --- | --- |
| Add a GPU node pool | `scripts/Add-RayGpuNodePool.ps1` |
| Install KubeRay | `scripts/Install-KubeRay.ps1` |
| Create the Ray cluster | `samples/quickstart/raycluster-t4.yaml`, `scripts/Deploy-RayQuickstart.ps1` |
| Run the GPU check | `samples/quickstart/gpu_check.py`, `rayjob-gpu-check.yaml` |
| Run batch inference | `samples/quickstart/batch_inference.py`, `rayjob-inference.yaml` |
| Both jobs plus result export | `scripts/Invoke-RayQuickstartJobs.ps1` |
| Clean up | `scripts/Remove-RayQuickstart.ps1` |

Spot GPU nodes also need `samples/quickstart/spot-worker.patch.json`.

## Secure Ray clusters on AKS

| Section | Samples |
| --- | --- |
| Pin ports before creating pods | `samples/security/ray-t4-fixed-ports.patch.json` |
| Apply scoped network isolation | `samples/security/ray-t4-network-policies.yaml` |
| Label the RayJob submitter | `samples/security/rayjob-submitter-label.patch.json` |
| Plan downloads | `samples/security/ray-t4-egress-proxy.yaml`, `egress-proxy.yaml`, `ray-t4-token-auth-proxy.patch.json` |
| Add token authentication | `samples/security/ray-t4-token-auth-proxy.patch.json` |
| Validate allowed and denied paths | `samples/security/check_ray_auth.py`, `check_proxy.py`, `security_probe.py`, `security-probe-job.yaml` |
| Whole sequence | `scripts/Protect-RayCluster.ps1` |

## Monitor Ray workloads on AKS

| Section | Samples |
| --- | --- |
| Scrape Ray metrics | `samples/monitoring/ray-t4-podmonitor.yaml` |
| Build a dashboard | `samples/monitoring/ray-on-aks-dashboard.json` |

## Autoscale Ray workloads on AKS

| Section | Samples |
| --- | --- |
| Prepare a separate GPU pool | `samples/autoscaling/nvidia-device-plugin.yaml` |
| Configure worker autoscaling | `samples/autoscaling/raycluster.yaml` |
| Submit and observe a burst | `samples/autoscaling/burst.py`, `worker-runtime-env.json` |
| Observe node and worker counts | `samples/autoscaling/observe_scaling.py`, `scaling-observer.yaml` |
| Confirm the run afterwards | `samples/autoscaling/verify_scaling.py` |

## Repository-only files

These have no article counterpart yet. They exist because the live POC needed
them.

| File | Why it's here |
| --- | --- |
| `samples/security/check_*.py`, `security_probe.py`, `security-probe-job.yaml` | The article says to test allowed and denied paths; these are the tests that were run. |
| `samples/security/rayjob-submitter-label.patch.json` | Applies the submitter label the article asks readers to merge by hand. The article also carries it. |
| `samples/autoscaling/observe_scaling.py`, `scaling-observer.yaml` | Records Ready node and worker counts over time, rather than trusting desired replicas. |
| `samples/autoscaling/verify_scaling.py` | Checks a finished run against the sample's own pins. |
| `samples/monitoring/ray-on-aks-dashboard.json` | The article describes the panels; this is an importable dashboard. |

`samples/security/egress-proxy.yaml` and `ray-t4-token-auth-proxy.patch.json`
used to be repository-only. The security article now uses them as its working
development download path, so they're mirrored into the article samples.
