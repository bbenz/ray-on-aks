# Running these samples as an Azure POC

A runbook for taking this repository to a real subscription. It assumes you want
evidence at the end, not just a green terminal.

## Before you create anything

1. **Check the subscription is usable.** `az account show` reports a cached
   state and can be stale. Read the authoritative one:

   ```powershell
   az rest --method get --url "https://management.azure.com/subscriptions/<id>?api-version=2022-12-01" `
     --query "{name:displayName,state:state,spendingLimit:subscriptionPolicies.spendingLimit}"
   ```

   Anything other than `Enabled` blocks writes. A `Warned` or `Disabled`
   subscription rejects `az aks create`, `az aks stop`, and AKS Run Command with
   `ReadOnlyDisabledSubscription`, and there's no workaround from the CLI side:
   the billing owner has to clear it.

2. **Register what a fresh subscription is missing.** A subscription that has
   never hosted compute can have `Microsoft.Compute`, `Microsoft.Network`, and
   `Microsoft.ContainerService` unregistered, and `az vm list-usage` then
   returns an empty list rather than an error. Managed GPU pools also need a
   preview feature, and the monitoring stages need their own providers:

   ```powershell
   foreach ($provider in 'Microsoft.Compute', 'Microsoft.Network', 'Microsoft.ContainerService',
       'Microsoft.Monitor', 'Microsoft.OperationsManagement', 'Microsoft.Dashboard', 'Microsoft.AlertsManagement') {
     az provider register --namespace $provider --wait
   }
   az feature register --namespace Microsoft.ContainerService --name ManagedGPUExperiencePreview
   az provider register --namespace Microsoft.ContainerService
   az extension add --upgrade --name aks-preview   # 22.0.0b6 or later for --gpu-driver-mode
   ```

3. **Check GPU quota, and read it correctly.** Quota is per VM family and per
   region, in vCPUs, not GPUs. One `Standard_NC4as_T4_v3` is 4 vCPUs and 1 GPU.

   ```powershell
   $sku = az vm list-skus --location <region> --resource-type virtualMachines `
     --size Standard_NC4as_T4_v3 --all --query "[0]" --output json | ConvertFrom-Json
   az vm list-usage --location <region> `
     --query "[?name.value=='$($sku.family)' || name.value=='cores']" --output table
   ```

   A reported usage of `-1` means unknown, not one free vCPU. Check the regional
   `cores` row too: family headroom alone isn't enough. Check the CPU pool's
   size as well: a size can be blocked for one subscription in one region
   (`NotAvailableForSubscription`) and fine in the next.

4. **Decide on-demand or Spot.** Spot uses a **separate** quota pool
   (`lowPriorityCores`) and is often the only way to get four T4s. Spot nodes can
   be evicted at any time and have no SLA. On-demand quota increases can be
   refused outright with `QuotaNotAvailableForResource`; that's a capacity
   answer, not a paperwork delay, and it's regional. On September 29, East US
   (20 vCPUs) and Sweden Central (4) refused within minutes, and West US 3
   granted 4 and then 20 within minutes each.

5. **Check placement, not just quota.** The Spot placement score API compares
   regions and zones for a desired count
   (`az compute-recommender spot-placement-score`). `isQuotaAvailable: true`
   alongside a `RestrictedSkuNotAvailable` zone means exactly what it says: you
   have quota for a VM you can't get there.

6. **Agree the lifecycle before you start.** Decide up front whether the cluster
   is deleted, stopped, or left running afterwards, and who does it. GPU nodes
   bill during provisioning, driver installation, image pulls, dependency
   downloads, idle time, and troubleshooting.

## Order of operations

| Stage | Command | Gate before moving on |
| --- | --- | --- |
| 1. Cluster | `az aks create` (with a NetworkPolicy-enforcing dataplane such as Cilium) or an existing cluster | `provisioningState` Succeeded, nodes Ready |
| 2. GPU pool | `scripts/Add-RayGpuNodePool.ps1` | A node advertises allocatable `nvidia.com/gpu` |
| 3. Operator | `scripts/Install-KubeRay.ps1` | Operator deployment available |
| 4. Ray cluster | `scripts/Deploy-RayQuickstart.ps1` | RayCluster state `ready`, head and worker Running |
| 5. Prove the GPU | `scripts/Invoke-RayQuickstartJobs.ps1` | `tensor_sum` is 10.0 on a T4 |
| 6. Prove inference | same script | Four records, pinned revision, copied locally |
| 7. Isolation | `scripts/Protect-RayCluster.ps1 -WithProxy -WithTokenAuth`, then `Invoke-RayQuickstartJobs.ps1 -Secured -NameSuffix secured` | Both jobs still succeed **and** the probes deny what should be denied |
| 8. Monitoring | `samples/monitoring/ray-t4-podmonitor.yaml` | Head and worker scrape targets report `up=1` |
| 9. Autoscaling | `samples/autoscaling` in its own namespace and pool, with `nvidia-device-plugin.yaml` | Observer records 1 -> N -> 1; Azure agrees on the VM count |
| 10. Clean up | `scripts/Remove-RayQuickstart.ps1 -RemoveGpuPool` | `az aks nodepool list` no longer shows the pool |

Run every mutating script once with `-WhatIf` first.

## Evidence worth keeping

For each stage, keep the UTC start and end time, the exact command, its exit
status, and the real output. Specifically:

- Image digests, model revision, resolved package versions, Kubernetes version,
  and GPU driver and CUDA versions.
- The inference JSONL file, copied off the head pod.
- For autoscaling: the burst driver log and the observer log, then run
  `samples/autoscaling/verify_scaling.py` over both. It fails if the tasks
  shared a worker, ran short, used unpinned versions, or never scaled back in.
- Maximum observed GPU node count, and whether caches were warm or cold.
- Failures and what you changed. A rerun that hides a failure is worth less than
  the failure.

## Things that will cost you an afternoon

- **A long-running management command can report failure while the workload
  keeps running.** AKS Run Command returned `Failed`/`timeout` for a wait that
  outlived its limit, while the Ray job was still progressing normally. Poll
  with short status checks instead of one long blocking call, and let an
  in-cluster observer capture the detail.
- **Deleting a RayJob doesn't stop the GPU bill.** Only removing or scaling the
  node pool does.
- **Two GPU pools mean two sets of GPUs.** Park one before you start the other if
  you're bounded by quota, and confirm the parked pool reports zero VM instances
  in Azure, not just zero in Kubernetes.
- **Restricted egress needs planning before the first job.** Default-deny blocks
  the pip and Hugging Face downloads, and NetworkPolicy can't match domain
  names. Either run the proxy in `samples/security/egress-proxy.yaml` or stage
  the images and model artifacts in advance.
- **A cleanup that followed the docs isn't a clean slate.** `helm uninstall`
  keeps the `ray.io` CRDs and the `kuberay-system` namespace. The install script
  now tolerates leftover CRDs, but a different KubeRay version would reuse
  mismatched CRDs silently, because Helm doesn't upgrade them.
- **Keep management state out of synced folders.** Use a separate kubeconfig
  (`$env:KUBECONFIG`) and, if you don't want to change the workstation's CLI, a
  separate `AZURE_EXTENSION_DIR`. Never put a kubeconfig or Ray token in a
  folder that syncs to the cloud.

## What this POC won't tell you

It won't give you throughput, latency, quality, or a cost model. It won't
establish multi-tenant isolation. It exercises one workload shape on one GPU
type. See `docs/validation-status.md` for the full list.
