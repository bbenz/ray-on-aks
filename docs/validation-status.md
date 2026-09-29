# Validation status

Two different things get called "tested". This page keeps them apart.

- **Live**: ran against a real AKS cluster and produced the recorded result.
- **Offline**: checked by `pytest` in this repository, which parses files and
  compares values. Useful, but it can't prove a cluster works.
- **Not validated**: neither of the above.

## The environments that produced the live results

| Component | September 18 POC | September 29 POC |
| --- | --- | --- |
| Access path | AKS Run Command (the workstation couldn't reach the API server) | **Native `kubectl` and Helm from a Windows workstation** |
| Subscription | Existing | Fresh: no providers registered, zero GPU quota |
| AKS | 1.35.7, Ubuntu 24.04.4 LTS, Free tier, West US 3 | 1.36.3, node image `AKSUbuntu-2404gen2containerd-202609.03.1`, Free tier, West US 3 |
| Networking | Azure CNI Overlay, Cilium dataplane and network policy | Same |
| Quickstart GPU node | One **Spot** `Standard_NC4as_T4_v3`, AKS-managed GPU pool, NVIDIA driver 580.159.04 | One **on-demand** `Standard_NC4as_T4_v3`, AKS-managed GPU pool (`driverMode: DevicePlugin`) |
| Autoscaling GPU nodes | Spot, driver-only pool, 1-4 | On-demand, driver-only pool, 1-4, with `nvidia-device-plugin.yaml` |
| CPU nodes | Two on-demand `Standard_D4s_v5` | One, then two, on-demand `Standard_D4s_v5` |
| Tooling | Azure CLI with `aks-preview` | Azure CLI 2.88.0, `aks-preview` 22.0.0b9, kubectl 1.36.1, Helm 3.22.0, PowerShell 7.6.6 |
| KubeRay / Ray | 1.7.0 / 2.58.0 on Python 3.11 | Same |
| Runtime | PyTorch 2.8.0+cu128, CUDA 12.8, Transformers 4.57.1 | Same |
| Model | `Qwen/Qwen2.5-0.5B-Instruct` at revision `7ae557604adf67be50417f59c2c2f167def9a775` | Same |

Two clusters, one region, one GPU type. That's a working configuration, not a
compatibility matrix.

## Live results

| What ran | Result |
| --- | --- |
| GPU task, interactive and as a RayJob | Tesla T4, CUDA 12.8, PyTorch 2.8.0+cu128, `tensor_sum` 10.0 (both POCs) |
| Batch inference, as a RayJob | Four records with the expected IDs and the pinned model revision; identical summaries across both POCs (greedy decoding) |
| **Every script in `scripts/`** | September 29, natively, `-WhatIf` first and then for real: `Add-RayGpuNodePool`, `Install-KubeRay` (including over leftover CRDs, and an idempotent rerun), `Deploy-RayQuickstart`, `Invoke-RayQuickstartJobs` (plain, `-NameSuffix`, and `-Secured`), `Protect-RayCluster -WithProxy -WithTokenAuth`. `Remove-RayQuickstart` ran with `-WhatIf` only, because the environment was kept for review; the articles' manual cleanup, including deleting the GPU pool, ran for real. The run found bugs that are fixed here; see `docs/known-issues.md`. |
| The articles' own commands | September 29: the Learn module's unit 3 end to end, including cleanup; the quickstart's cluster creation and its extra checks; the security article's manual path with the development proxy; the monitoring article's queries; the autoscaling article's pool, cluster, and burst commands. |
| The same two jobs under isolation and token auth | Both `SUCCEEDED`, identical output, through the proxy on a fresh worker (both POCs) |
| Ray token authentication | Unauthenticated Jobs API request returned 401; the authenticated SDK succeeded (both POCs). KubeRay 1.7.0 created Secret `ray-t4` (`auth_token`) and injected `RAY_AUTH_TOKEN` into head, workers, and RayJob submitters. |
| Network policies | Labeled submitter reached the head and got an auth challenge; unlabeled same-namespace and cross-namespace pods were blocked, tested against the head **pod IP** so DNS failure couldn't be mistaken for isolation (both POCs) |
| Egress proxy | Approved PyPI and Hugging Face destinations returned 200; an unapproved destination got an explicit 403; direct external TCP from the Ray head was blocked while a control pod in another namespace reached the same IP (both POCs) |
| Managed Prometheus | Head and worker scrape targets reported `up=1` through the network policies; DCGM GPU series present after the settings ConfigMap; regex on `__name__` returned 501 (both POCs) |
| Container Insights | The actual tensor result and all four inference records appeared in `ContainerLogV2` (both POCs) |
| Ray autoscaling | Four tasks on four distinct workers; Ready GPU nodes and Ready Ray workers went 1 -> 4 -> 1 (both POCs; September 29 results in the POC notes) |
| Worker replacement | After deleting the GPU worker pod, the replacement rejoined and both jobs succeeded again (September 18) |

The September 18 autoscaling run is the input to
`samples/autoscaling/verify_scaling.py`'s fixtures in `tests/fixtures`, with
identifiers replaced.

## Offline only

- Every manifest and Python sample in this repository parses, and the values
  that have to agree across files do agree. See `tests/`.
- Every PowerShell script parses under PowerShell 7, and the helpers behave
  under strict mode and `-WhatIf`.

## Not validated

- Kueue admission control, quotas, or fairness.
- Microsoft Entra Workload ID, Blob Storage access, or any production data path.
- mTLS between Ray nodes, or any transport encryption for Ray traffic.
- Spot eviction and recovery. A worker pod was deleted and replaced; no Azure
  eviction was simulated.
- Multi-tenant or hostile-code isolation. Nothing here establishes that.
- Throughput, latency, quality, cost, or learner completion time. The autoscaling
  burst repeats one prompt with a fixed token cap; it's a demand generator, not
  a benchmark.
- NAP or AKS Automatic GPU provisioning.
- The Grafana dashboard import and Grafana queries were exercised on September
  18 only.
- Scripts under Windows PowerShell 5.1, or on macOS and Linux.
- Any AKS, driver, Ray, or KubeRay version other than the ones above. Helm 4.3.0
  only rendered and server-dry-ran the chart.

## About the generated output

The inference sample's `DLV-002` summary says building access "will be available
tomorrow". The input only promises a retry. A 0.5B model states things the input
didn't say; the sample keeps the real output rather than a corrected one.
Functional success is not output quality.
