# Known issues

Failures that a live run produced, what caused them, and what the samples do
about them. Most of these look like something else when you first hit them.

## Runtime environment disappears from a rejoining worker

**Symptom.** An actor never starts. The RayJob fails with
`ActorUnschedulableError` after five attempts, and the worker's `raylet.err`
repeats:

```text
.../runtime_resources/pip/<hash>/virtualenv/bin/activate: No such file or directory
```

The node has plenty of free disk, the GPU is healthy, and a GPU check submitted
minutes earlier succeeded.

**Cause.** Two runtime environments that differ only in `env_vars` produce the
**same pip URI**, because the URI hashes the package set. Ray 2.58's
`URICache.add` adds that URI's size once per environment variant without
deduplicating. The sample's environment is about 6.8 GB, so two variants account
for roughly 13.6 GB against a default 10 GiB per-plugin cache budget. Releasing
the first variant then evicts the shared directory while the second variant
still holds a "created successfully" cache entry pointing at it. A worker that
rejoins and tries to activate that environment finds nothing there.

**What the samples do.** `gpu_check.py` and `batch_inference.py` pass identical
`runtime_env` definitions, `env_vars` included.
`tests/test_python_samples.py::test_gpu_check_and_inference_share_one_runtime_environment`
compares their ASTs so they can't drift apart again.

**What not to do.** Raising `RAY_RUNTIME_ENV_PIP_CACHE_SIZE_GB` hides it until
you add a third variant. Patching installed Ray code moves the problem into your
image.

**Verified.** After the fix, sequential GPU and inference jobs succeeded on a
fresh cluster, then again after deleting the GPU worker so a new one rejoined
the running head. Each worker recorded exactly one cache addition for the shared
URI, with the activation file present.

## `pip check` fails on a dependency the image already had

**Symptom.** The autoscaling burst fails during runtime environment setup,
before any inference starts, complaining about `pydantic`.

**Cause.** `pip_check: true` validates the whole environment, not just what you
asked for. The Ray image ships `conda-lockfiles 0.2.1`, which requires
`pydantic>=2.12.5`, and the image has `pydantic 2.12.3`.

**What the samples do.** `worker-runtime-env.json` pins `pydantic==2.12.5` and
keeps `pip_check: true`, because the check is what surfaced the conflict.

## A Ready GPU node that has no GPU yet

**Symptom.** A GPU task fails to schedule seconds after `kubectl wait
--for=condition=Ready` returned successfully on a new node.

**Cause.** The device plugin registers `nvidia.com/gpu` with kubelet after the
node becomes Ready. Work submitted in that gap sees zero allocatable GPUs.

**What the samples do.** `Assert-AllocatableGpu` in `scripts/Common.ps1` waits
for allocatable `nvidia.com/gpu`, and both the GPU pool and quickstart scripts
call it. A Running device-plugin pod isn't the signal to wait for.

## Recreating a RayCluster with the same name looks ready too early

**Symptom.** After deleting and reapplying `ray-t4`, a readiness check passes
immediately, then commands hit pods that vanish.

**Cause.** Background deletion leaves terminating pods from the old cluster.
They still match the new cluster's label selectors.

**What the samples do.** `scripts/Protect-RayCluster.ps1` deletes with
`--cascade=foreground --wait=true` before recreating, and checks the new owner
UID before trusting readiness.

## GPU metadata that drags PyTorch onto the CPU head

**Symptom.** The GPU task itself succeeds, but the driver fails while
deserializing its result, trying to import `torch` on a CPU-only head.

**Cause.** `torch.__version__` is a `TorchVersion` instance, not a plain `str`.
Unpickling it requires torch.

**What the samples do.** `gpu_check.py` returns `str(torch.__version__)`. A test
asserts that conversion stays.

## Metric-name discovery returns HTTP 501

**Symptom.** A Prometheus query like `{__name__=~"ray_.*"}` fails against the
Azure Monitor managed endpoint.

**Cause.** That endpoint doesn't support the regex-on-`__name__` form. The
September 29 run got the exact reason: "Metric name only support equality(=)
filter".

**What to do instead.** Query exact metric names, or use the label-values API to
discover what a workspace holds. The label API lists DCGM names in lowercase,
for example `dcgm_fi_dev_gpu_util`, but queries matched both that and
`DCGM_FI_DEV_GPU_UTIL`. Querying the endpoint directly needs **Monitoring Data
Reader** on the Azure Monitor workspace; subscription Owner doesn't include it.

## The Squid image has no `squid` executable

**Symptom.** The egress proxy pod crash-loops with a command-not-found error.

**Cause.** The Canonical image's binary is `/usr/sbin/squid-gnutls`, and it runs
as `_daemon_` with UID/GID 584792.

**What the samples do.** `egress-proxy.yaml` sets that command and matching
`runAsUser`/`runAsGroup`, with a read-only root filesystem and a writable
`/tmp`.

## CRLF line endings break shell scripts in containers

**Symptom.** A shell script edited on Windows fails immediately inside a Linux
container, often on `set -o pipefail`.

**What the repository does.** `.gitattributes` normalizes all text files to LF.

## Managed GPU pools and the cluster autoscaler

AKS-managed GPU pools install the driver, device plugin, and DCGM exporter, and
the preview doesn't support the cluster autoscaler on a manually created managed
pool. The autoscaling sample therefore uses a separate driver-only pool with the
cluster autoscaler enabled, and installs the NVIDIA device plugin scoped to that
pool only with `samples/autoscaling/nvidia-device-plugin.yaml`. Don't add a
second device plugin to a managed pool.

That separation has a visible consequence: the driver-only pool exports Ray's
`ray_node_gpus_utilization` but no DCGM series.

## Results only exist on the head pod

`batch_inference.py` writes JSON Lines to the head pod's `/tmp`. Deleting or
recreating the cluster destroys them. `Invoke-RayQuickstartJobs.ps1` copies the
file out as soon as the job succeeds.

## Found by the September 29 native run

The September 18 POC drove the cluster through AKS Run Command. The first run
from a workstation with direct `kubectl` and Helm access found these, all in
paths that run hadn't exercised.

### The scripts crashed on status fields that didn't exist yet

**Symptom.** `Invoke-RayQuickstartJobs.ps1` stopped a second after submitting,
with `The property 'jobStatus' cannot be found on this object`.

**Cause.** The scripts run under `Set-StrictMode -Version Latest`, which throws
on a missing property. KubeRay hasn't written `status.jobStatus` on a RayJob it
has just received. `Assert-AllocatableGpu` had the same latent bug: a GPU node
that hasn't registered `nvidia.com/gpu` yet has no such allocatable property,
which is exactly the state that function waits through.

**What the scripts do.** `Get-PropertyValue` in `Common.ps1` reads optional
paths and returns `$null` for anything not reported yet. A test fails if a script
reads those status fields directly.

### `kubectl cp` can't take a Windows drive-letter destination

**Symptom.** `error: one of src or dest must be a local file specification`
while copying the inference results, after both jobs succeeded.

**Cause.** `kubectl cp` parses `C:\...` as `pod:path`. The script's default
output folder is an absolute path, so the copy failed on every Windows run.

**What the scripts do.** Copy into the output folder by relative file name. The
articles already use a relative destination and now say why.

### `helm uninstall` leaves CRDs, and the install script refused to reinstall

**Symptom.** After following the articles' cleanup, `Install-KubeRay.ps1`
refused with "ray.io resource types already exist without a Helm release".

**Cause.** Helm never deletes CRDs, and the articles tell readers not to delete
them. The guard treated leftover CRDs as a foreign installation.

**What the scripts do.** Refuse only when a KubeRay operator deployment is
actually running without a release; otherwise warn that the existing CRDs are
reused and that Helm doesn't upgrade them.

### `-WhatIf` broke rendering and leaked temp files

**Symptom.** `Protect-RayCluster.ps1 -WithProxy -WithTokenAuth -WhatIf` failed
on the second patch with "the path ... does not exist". Every script printed
"What if: Output to File" and "Remove File" lines about temp files.

**Cause.** `-WhatIf` sets `$WhatIfPreference`, which the file redirection and
`Set-Content` honor. The rendered manifest was never written, and the stderr
capture and its cleanup were skipped.

**What the scripts do.** Local rendering and the helper's own temp files ignore
`-WhatIf`; only the cluster-changing steps are gated.

### `-NameSuffix '-sec'` bound as `-Secured`

**Cause.** Through `pwsh -File`, an argument that starts with `-` is read as a
parameter name, and `-sec` is an unambiguous prefix of `-Secured`.

**What the scripts do.** Accept the suffix without the leading dash
(`-NameSuffix sec`) and add it.

### `Remove-RayQuickstart.ps1 -RemoveGpuPool -WhatIf` failed

**Symptom.** "Node pool 'raygpu' still exists. Confirm the deletion before
assuming charges stopped", from a run that was only asked to show its plan.

**Cause.** The post-deletion check ran even though `-WhatIf` had skipped the
deletion.

**What the scripts do.** Check that the pool is gone only after actually
deleting it.

### `--gpu-driver-mode` needs `aks-preview` 22.0.0b6

**Symptom.** `az aks nodepool add` fails with `unrecognized arguments:
--gpu-driver-mode DevicePlugin`.

**Cause.** The option (an alias of `--managed-gpu-driver-mode`) arrived in
`aks-preview` 22.0.0b6. The managed GPU article still lists 19.0.0b29 as the
minimum, and the workstation had 21.0.0b7.

**What the scripts do.** `Add-RayGpuNodePool.ps1` checks the extension version
and the `ManagedGPUExperiencePreview` registration before creating anything.

### An unregistered provider looks like missing quota

**Symptom.** `az vm list-usage` returns an empty list with exit code 0.

**Cause.** `Microsoft.Compute` isn't registered in a subscription that has never
hosted VMs.

**What to do.** `az provider register --namespace Microsoft.Compute --wait`.
The pool script's warning now says so.

### Queries with parentheses and no spaces break `az` on Windows

**Symptom.** `} was unexpected at this time` or `].name was unexpected at this
time`, and nothing runs.

**Cause.** On Windows, `az` is a batch file. PowerShell passes an argument
without spaces unquoted, and `cmd.exe` then parses the parentheses in a JMESPath
function such as `to_string(...)` or `contains(...)`.

**What to do.** Put a space in the query so PowerShell quotes it, for example
`"[].{Name: name, Taints: to_string(nodeTaints)}"`. Separately, `--output
table` silently drops array-valued columns such as `nodeTaints`; convert them
with `to_string()`.

### The upstream device plugin also lands on managed GPU nodes

**Cause.** NVIDIA's static DaemonSet, which the AKS self-managed GPU article
applies, tolerates the `sku=gpu` taint and has no node selector.

**What the samples do.** `samples/autoscaling/nvidia-device-plugin.yaml` adds a
`scalet4` pool selector. A test checks that it targets the same pool and
tolerates the same taints as the autoscaling workers.

### One CPU node isn't enough for the autoscaling head as well

**Cause.** The autoscaling head needs 1.5 CPUs and 4.5 GiB with its sidecar. On
the quickstart's single `Standard_D4s_v5`, the quickstart head, KubeRay, and
managed Prometheus left about 1.3 CPUs.

**What to do.** Use a separate lab cluster, as the article says, or add a
second CPU node first.

### Azure reports the peak node count after scale-in

**Symptom.** After the cluster autoscaler removed three GPU nodes, `az aks
nodepool list` and `az aks show` still reported `count: 4` for the pool, minutes
later.

**What to do.** Confirm scale-in with `az aks nodepool show`, which reported 1,
or the VM scale set's `sku.capacity`, and with `kubectl get nodes`.

### Idle scale-in shows up as "Recent failures"

**Symptom.** After a successful burst, `ray status` lists every removed worker
under **Recent failures** as `NodeTerminated`.

**Cause.** That's how Ray's v2 autoscaler reports nodes it terminated, including
its own idle removals. The sidecar log shows the normal sequence:
`RAY_RUNNING->RAY_STOP_REQUESTED->RAY_STOPPED->TERMINATING->TERMINATED`.

**What to do.** Check the autoscaler log before treating those entries as
crashes.
