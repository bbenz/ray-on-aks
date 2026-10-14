# Demo dashboard

A read-only web view that runs in the same AKS cluster as the samples. It walks
through the ten stages the articles build, shows each stage's live results, and
adds Ray metrics and GPU metrics, so an audience can follow a command-line demo
without reading terminal output.

It doesn't replace or change any command-line step. Run the scripts and the
articles' commands as usual; the dashboard watches the cluster and follows along.

## What it shows

| # | Stage | What you see | Article section |
| --- | --- | --- | --- |
| 1 | Cluster | Kubernetes version, Ready nodes, the network dataplane, and each pool's node OS (the container host), kernel, and runtime | Quickstart: prerequisites |
| 2 | GPU node | Ready versus allocatable `nvidia.com/gpu`, GPU health conditions, who holds each GPU, and a live DCGM panel | Quickstart: add a GPU node pool |
| 3 | KubeRay | Operator version and health, the `ray.io/v1` resources, and the Ray clusters it manages | Quickstart: install KubeRay |
| 4 | RayCluster | Head and worker pods on their nodes, the GPU declared to Kubernetes and to Ray, and Ray's own view from metrics | Quickstart: create the Ray cluster |
| 5 | GPU check | A live banner with the RayJob's status timeline and timer, then the verified result: the GPU, CUDA, PyTorch, and `tensor_sum` | Quickstart: run the GPU check |
| 6 | Inference | The four records, with words that weren't in the input highlighted, and a review note on DLV-002 | Quickstart: run batch inference |
| 7 | Security | Default deny, the policies, pinned ports, token auth, the egress proxy, submitter labels, and the probes' allowed and denied results | Secure Ray clusters on AKS |
| 8 | Metrics | Scrape targets, data delay, and charts for GPU utilization, GPU memory, Ray's GPUs, and Ray tasks | Monitor Ray workloads on AKS |
| 9 | Autoscaling | The autoscaler's settings, workers and GPU nodes, charts, events, and a replay of a recorded scale-out | Autoscale Ray workloads on AKS |
| 10 | Cost | Compute cost per hour, day, and month at list price, and each GPU's state: busy, held but idle, or free but still billing | Quickstart: clean up |

Each stage also shows the command from the article and what to look for. A rail
at the top shows every stage's status at a glance, and a strip at the bottom
keeps GPU nodes, held GPUs, the live GPU reading, the latest RayJob, and the cost
per hour on screen.

## Where the data comes from

| Source | Used for | Freshness |
| --- | --- | --- |
| Kubernetes API | Nodes, pods, RayClusters, RayJobs, policies, events | Every 2.5 seconds |
| Submitter pod logs | Job results while the pods exist | Every 2.5 seconds |
| DCGM exporter on each AKS-managed GPU node (port 19400) | The live GPU panels: utilization, memory, power, temperature | Polled every 2 seconds; the exporter samples every 30 seconds by default, so a GPU burst shorter than that can fall between samples |
| Azure Monitor managed Prometheus | Charts, scrape targets, Ray's logical resources | 30-second scrapes plus ingestion: usually 30 to 90 seconds behind |
| Container insights (Log Analytics) | Output of jobs and probes whose pods are gone | Minutes after the fact, for the workspace's retention period |

Status chips in the top bar show each source's health: green when working,
amber when some calls fail, red when failing. Hover for details. If a call
fails, the page keeps showing the last good data and says so. The
dashboard works without Azure Monitor, with no charts and no history for
finished jobs.

Results are recognized for RayJobs named `ray-gpu-check*` and `ray-inference*`,
the samples' names with any `-NameSuffix`. A pool created with the autoscaling
sample's driver-only setup has no DCGM exporter; its GPUs appear in the charts
through Ray's own `ray_node_gpus_utilization`.

## Security model

The dashboard reads; it never changes the cluster.

- **Reachable only through `kubectl port-forward`.** The server listens on the
  pod's loopback interface. There's no Service, load balancer, or ingress, and a
  default-deny ingress policy covers the namespace. Opening it requires
  Kubernetes RBAC permission to port-forward in `ray-dashboard`. A second
  listener on the pod IP answers only `/healthz`, for the kubelet's probes.
- **Read-only RBAC.** Cluster-wide: get and list on nodes, pods, deployments,
  RayClusters, RayJobs, network policies, and PodMonitors. In the Ray namespaces
  only: pod logs, services, and events. No Secrets, no ConfigMaps, no exec, no
  proxy. The Ray token stays in its Secret; the dashboard only sees that token
  authentication is on.
- **Browser hardening.** GET only. A fixed list of static files. Requests whose
  `Host` isn't `localhost`, `127.0.0.1`, or `[::1]` are refused, which blocks DNS
  rebinding against the forwarded port. A strict Content Security Policy with no
  inline script. The browser picks a time window; the server builds every
  PromQL and KQL query from fixed templates and validated names.
- **Azure access without secrets.** A user-assigned managed identity with
  Monitoring Data Reader on the Azure Monitor workspace and Log Analytics Reader
  on the Container insights workspace. The pod proves its identity with a
  projected service account token (audience `api://AzureADTokenExchange`)
  through a federated credential on the cluster's OIDC issuer. The workload
  identity webhook isn't needed, so the cluster's configuration doesn't change.
- **Minimal runtime.** The Azure Linux distroless Python image, pinned by digest:
  no shell or package manager. The app is Python standard library only and ships
  as a ConfigMap, so nothing is built or pulled at startup. Non-root, read-only
  root filesystem, all capabilities dropped, `RuntimeDefault` seccomp.

## Deploy

You need the cluster's OIDC issuer enabled (`az aks update --enable-oidc-issuer`)
for the charts and job history, and permission to create a managed identity and
role assignments in the resource group. Use `-SkipAzureMonitor` without them.

```powershell
./dashboard/Deploy-Dashboard.ps1 -ResourceGroup <rg> -ClusterName <aks> -WhatIf
./dashboard/Deploy-Dashboard.ps1 -ResourceGroup <rg> -ClusterName <aks>
```

The script finds the Azure Monitor workspace in the resource group and the
Container insights workspace from the cluster's monitoring add-on. Pass
`-MonitorWorkspaceId` or `-LogAnalyticsWorkspaceId` to choose others. It refuses
to run when the kubectl context doesn't point at `-ClusterName`; pass `-Context`
to choose one. Rerunning it is safe: it changes only what differs, and a code or
settings change rolls the pod. Rerun it after creating a Ray namespace, so the
dashboard can read that namespace's logs.

To offer a replay of a recorded run on the autoscaling stage, add its window:

```powershell
./dashboard/Deploy-Dashboard.ps1 -ResourceGroup <rg> -ClusterName <aks> `
  -ReplayStart 2026-09-29T22:20:00Z -ReplayEnd 2026-09-29T23:30:00Z -ReplayLabel 'the September 29 burst'
```

Open it:

```powershell
kubectl -n ray-dashboard port-forward deployment/ray-dashboard 8090:8080
```

Then browse to <http://localhost:8090/>. New role assignments can take a few
minutes to apply; until then the Prometheus and Container insights chips show
an error.

## Present with it

| Key | Action |
| --- | --- |
| ← → (or a clicker's Page Up and Page Down) | Previous or next stage |
| 1-9, 0 | Jump to a stage; 0 is stage 10 |
| F | Follow live jobs: jump to a RayJob's stage when it's submitted or changes status (on by default) |
| T | Next chart window: 15 minutes, 1 hour, 6 hours, 24 hours |
| R | Replay the recorded autoscaling run |
| ? | Keyboard help |

Every stage has its own address, such as `http://localhost:8090/#gpucheck`, and
`?window=replay` opens with the replay selected:
`http://localhost:8090/?window=replay#autoscaling`.

For a live demo:

1. Start the port-forward and open the dashboard before the session, on the
   screen you'll share. Press F11 for full screen. The layout is sized for 1080p.
2. Walk stages 1 to 4 while you talk through the platform.
3. Run `Invoke-RayQuickstartJobs.ps1` in the terminal. With follow on, the
   dashboard jumps to the GPU check when the RayJob is submitted, shows its
   status timeline as it runs, then moves to inference and shows the records.
4. Use stage 7 for isolation, and stages 8 to 10 for metrics, the autoscaling
   replay, and cost.

The page reconnects on its own if the port-forward drops; restart the
port-forward and it picks up again.

## Run it locally

For development, run the server on your workstation against `kubectl proxy`.
It uses your own Kubernetes and Azure CLI permissions instead of the pod's.

```powershell
kubectl proxy --port 8001
$env:KUBE_API_URL = 'http://127.0.0.1:8001'
$env:DASHBOARD_AZ_CLI = '1'         # use your az login for Prometheus and Log Analytics
$env:DCGM_VIA_API_PROXY = '1'       # reach DCGM through the API server's node proxy
$env:DASHBOARD_DEV = '1'            # reread static files on every request
$env:PROMETHEUS_QUERY_ENDPOINT = '<Azure Monitor workspace query endpoint>'
$env:LOG_ANALYTICS_WORKSPACE_ID = '<workspace customer ID>'
$env:PORT = '8090'
python dashboard/app/server.py
```

`DCGM_VIA_API_PROXY` needs `nodes/proxy`, which your own account may have. The
in-cluster dashboard is never granted it: it reads the exporter on the node's IP.

## Configuration

`Deploy-Dashboard.ps1` writes these into the `ray-dashboard-settings` ConfigMap.

| Variable | Default | Meaning |
| --- | --- | --- |
| `RAY_PRIMARY` | `ray-learn/ray-t4` | The quickstart RayCluster, as namespace/name |
| `RAY_SCALE` | `ray-scale/ray-scale` | The autoscaling RayCluster |
| `EGRESS_NAMESPACE` | `ray-egress` | Where the security sample's proxy runs |
| `PROBE_NAMESPACES` | `<primary>,ray-probe` | Where the security probes run |
| `PROMETHEUS_QUERY_ENDPOINT` | none | Azure Monitor workspace query endpoint; charts are off without it |
| `LOG_ANALYTICS_WORKSPACE_ID` | none | Container insights workspace ID; job history is off without it |
| `REPLAY_START`, `REPLAY_END`, `REPLAY_LABEL` | none | A recorded window for the replay button |
| `PRICES_JSON` | built-in list prices | VM size to USD per hour, for example `{"Standard_NC4as_T4_v3": 0.526}` |
| `CLUSTER_NAME` | none | Shown in the top bar |
| `POLL_SECONDS` | `2.5` | Kubernetes polling interval |

## Limits

- Status timelines live in memory. A job that was already finished when the
  dashboard started shows its start time and duration, but no timeline.
- Costs are list prices for compute only, without disks, networking,
  monitoring, discounts, or Spot pricing.
- The review note on DLV-002 matches one generation word for word. Any other
  output gets highlighted words only, which are a reading aid, not a verdict.
- One replica. It's a presentation tool, not a monitoring system: use Azure
  Managed Grafana with `samples/monitoring/ray-on-aks-dashboard.json` for that.

## Tests

```powershell
./.venv/Scripts/python -m pytest dashboard/tests
```

They cover log parsing in both the pod-log and Container insights formats, the
stage logic, the queries the server builds, the HTTP hardening, the manifest's
security settings, and the deploy script. They're offline and separate from the
samples' suite in `tests/`.

## Remove it

```powershell
./dashboard/Deploy-Dashboard.ps1 -ResourceGroup <rg> -ClusterName <aks> -Remove
```

This deletes the `ray-dashboard` namespace, the dashboard's ClusterRoles and
bindings, its RoleBindings in the Ray namespaces, and the managed identity with
its role assignments. Add `-SkipAzureMonitor` to keep the identity.
