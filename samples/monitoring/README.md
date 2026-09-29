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

- Querying the Azure Monitor workspace's Prometheus endpoint directly needs
  **Monitoring Data Reader** on the workspace; subscription Owner doesn't include
  that data-plane permission.
- Metric-name discovery with `__name__=~"ray_.*"` was rejected by the managed
  query endpoint (HTTP 501, "Metric name only support equality(=) filter"). Use
  exact metric names, or the label-values API, to find what a workspace holds.
- The label API lists DCGM names in lowercase (`dcgm_fi_dev_gpu_util`); queries
  matched both that and `DCGM_FI_DEV_GPU_UTIL`.
- DCGM series need the `ama-metrics-settings-configmap` setting
  `dcgmexporter = true` and come only from AKS-managed GPU pools. A pool created
  with a manually installed device plugin, like the autoscaling sample's pool,
  exports Ray's `ray_node_gpus_utilization` but no DCGM series.
- A healthy scrape target says the metrics port answered. It is not evidence
  that a GPU task ran or produced correct output.
- The PodMonitor also scrapes through the security samples' network policies:
  they allow `rsName=ama-metrics` pods in `kube-system` to reach port 8080.
