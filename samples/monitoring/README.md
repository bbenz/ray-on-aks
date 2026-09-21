# Monitoring samples

Companion code for **Monitor Ray workloads on AKS**.

| File | Purpose |
| --- | --- |
| `ray-t4-podmonitor.yaml` | Azure Monitor managed Prometheus `PodMonitor` for the quickstart RayCluster, with relabeling that keeps `ray_io_cluster`, `ray_node_type`, and `kubernetes_node`. |
| `ray-on-aks-dashboard.json` | Grafana dashboard: scrape health, Ray CPU, DCGM GPU utilization, Ray GPU utilization, and healthy worker count. |

## Apply the PodMonitor

```powershell
kubectl apply -f .\ray-t4-podmonitor.yaml
kubectl -n ray-learn get podmonitor ray-t4
```

The custom resource uses the `azmonitoring.coreos.com/v1` group that the AKS
managed Prometheus add-on installs. Enable managed Prometheus on the cluster
first; applying this file without the add-on fails on the missing resource type.

## Import the dashboard

The dashboard has no baked-in data source or cluster name. On import, pick your
Prometheus data source and type the AKS cluster name into the `cluster`
variable. The `namespace` variable defaults to `ray-learn|ray-scale`.

## Query notes from live use

- Metric-name discovery with `__name__=~"ray_.*"` was rejected by the managed
  query endpoint (HTTP 501). Use exact metric names, or the label-values API, to
  find what a workspace actually holds.
- DCGM metric names were lowercase (`dcgm_fi_dev_gpu_util`) in that workspace.
- DCGM series come from AKS-managed GPU pools. A pool created with a manually
  installed device plugin, like the autoscaling sample's pool, exports Ray's
  `ray_node_gpus_utilization` but no DCGM series.
- A healthy scrape target says the metrics port answered. It is not evidence
  that a GPU task ran or produced correct output.
