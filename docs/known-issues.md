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

**Cause.** That endpoint doesn't support the regex-on-`__name__` form.

**What to do instead.** Query exact metric names, or use the label-values API to
discover what a workspace holds. In the validated workspace the DCGM names were
lowercase, for example `dcgm_fi_dev_gpu_util`.

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
pool only. Don't add a second device plugin to a managed pool.

That separation has a visible consequence: the driver-only pool exports Ray's
`ray_node_gpus_utilization` but no DCGM series.

## Results only exist on the head pod

`batch_inference.py` writes JSON Lines to the head pod's `/tmp`. Deleting or
recreating the cluster destroys them. `Invoke-RayQuickstartJobs.ps1` copies the
file out as soon as the job succeeds.
