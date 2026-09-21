# Validation status

Two different things get called "tested". This page keeps them apart.

- **Live**: ran against a real AKS cluster and produced the recorded result.
- **Offline**: checked by `pytest` in this repository, which parses files and
  compares values. Useful, but it can't prove a cluster works.
- **Not validated**: neither of the above.

## The environment that produced the live results

| Component | Value |
| --- | --- |
| AKS | 1.35.7, Ubuntu 24.04.4 LTS, Free tier, West US 3 |
| Networking | Azure CNI Overlay, Cilium dataplane and network policy |
| GPU node | One Spot `Standard_NC4as_T4_v3`, AKS-managed GPU pool, NVIDIA driver 580.159.04, 16384 MiB |
| CPU nodes | Two on-demand `Standard_D4s_v5` |
| KubeRay / Ray | 1.7.0 / 2.58.0 on Python 3.11 |
| Runtime | PyTorch 2.8.0+cu128, CUDA 12.8, Transformers 4.57.1 |
| Model | `Qwen/Qwen2.5-0.5B-Instruct` at revision `7ae557604adf67be50417f59c2c2f167def9a775` |

One cluster, one region, one driver version. That's a working configuration, not
a compatibility matrix.

## Live results

| What ran | Result |
| --- | --- |
| GPU task, interactive and as a RayJob | Tesla T4, CUDA 12.8, PyTorch 2.8.0+cu128, `tensor_sum` 10.0 |
| Batch inference, as a RayJob | Four records with the expected IDs and the pinned model revision |
| The same two jobs under isolation and token auth | Both `SUCCEEDED`, identical output |
| Ray token authentication | Unauthenticated Jobs API request returned 401; the authenticated SDK succeeded |
| Network policies | Labeled submitter reached the head and got an auth challenge; unlabeled same-namespace and cross-namespace pods were blocked, tested against the head **pod IP** so DNS failure couldn't be mistaken for isolation |
| Egress proxy | Approved PyPI and Hugging Face destinations returned 200; an unapproved destination got an explicit 403; direct external TCP from the Ray head was blocked while a control pod in another namespace reached the same IP |
| Managed Prometheus | Head and worker scrape targets reported `up=1`; DCGM GPU series present |
| Container Insights | The actual tensor result and all four inference records appeared in `ContainerLogV2` |
| Ray autoscaling | Four tasks ran on four distinct workers; Ready GPU nodes and Ready Ray workers went 1 -> 4 -> 1; Azure independently reported one remaining VM instance |
| Worker replacement | After deleting the GPU worker pod, the replacement rejoined and both jobs succeeded again |

The autoscaling run is the input to `samples/autoscaling/verify_scaling.py`. The
fixtures in `tests/fixtures` keep its event shapes with identifiers replaced.

## Offline only

- Every manifest and Python sample in this repository parses, and the values
  that have to agree across files do agree. See `tests/`.
- Every PowerShell script parses under PowerShell 7.

## Not validated

**The scripts in `scripts/` have never been run against a cluster.** The live
POC drove the cluster through AKS Run Command, because that workstation's direct
connections to the Kubernetes API server were reset before TLS completed. The
scripts issue the same `az` and `kubectl` commands the articles document, but
that documented native path is unproven. Run them with `-WhatIf` first, and
expect to fix something.

Also not validated here:

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
- Any AKS, driver, Ray, or KubeRay version other than the ones above.

## About the generated output

The inference sample's `DLV-002` summary says building access "will be available
tomorrow". The input only promises a retry. A 0.5B model states things the input
didn't say; the sample keeps the real output rather than a corrected one.
Functional success is not output quality.
