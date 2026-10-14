"""The fixed set of PromQL queries the dashboard runs, and their conversion to chart series.

The browser can only pick a time window. It never sends PromQL, so the dashboard can't be
used to run arbitrary queries against the workspace.
"""

import math
import re
import time

# Window name -> (seconds, step seconds).
WINDOWS = {"15m": (900, 15), "1h": (3600, 30), "6h": (21600, 120), "24h": (86400, 600)}
REPLAY = "replay"

# Ray's job supervisor and internal tasks would drown out the sample's own tasks.
USER_TASKS = 'name!~"jobsupervisor.*|_ray_internal.*"'
DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def _alternation(names):
    """Joins DNS-label names into a PromQL regex alternation. Anything else is dropped, not escaped:
    re.escape would turn '-' into '\\-', which PromQL strings reject."""
    return "|".join(sorted({n for n in names if n and DNS_LABEL.match(n)}))


def _ns_regex(namespaces):
    return _alternation(namespaces)


def chart_queries(settings, gpu_pools):
    ns = _ns_regex([settings["primaryNamespace"], settings["scaleNamespace"]])
    pools = _alternation(gpu_pools) or "none"
    return {
        "gpuUtil": {
            "title": "GPU utilization", "unit": "%", "max": 100, "by": "kubernetes_node",
            "query": f'max by (kubernetes_node) (ray_node_gpus_utilization{{namespace=~"{ns}"}})',
        },
        "gpuMemory": {
            "title": "GPU memory used", "unit": "MiB", "by": "kubernetes_node",
            "query": f'max by (kubernetes_node) (ray_node_gram_used{{namespace=~"{ns}"}})',
        },
        "rayGpus": {
            "title": "GPUs Ray has handed out", "unit": "GPUs", "by": "ray_io_cluster", "step": True,
            "query": f'sum by (ray_io_cluster) (ray_resources{{namespace=~"{ns}", name="gpu", state="used"}})',
        },
        "rayTasks": {
            "title": "Ray tasks running (sample code)", "unit": "tasks", "by": "ray_io_cluster", "step": True,
            "query": f'sum by (ray_io_cluster) (clamp_min(ray_tasks{{namespace=~"{ns}", state="running", '
                     f'source="executor", {USER_TASKS}}}, 0))',
        },
        "gpuNodes": {
            "title": "GPU nodes", "unit": "nodes", "by": "pool", "step": True,
            "query": f'count by (pool) (label_replace(kube_node_info{{node=~"aks-({pools})-.*"}}, '
                     f'"pool", "$1", "node", "aks-([a-z0-9]+)-.*"))',
        },
        "readyWorkers": {
            "title": "Ready Ray workers", "unit": "workers", "by": "namespace", "step": True,
            "query": f'count by (namespace) (kube_pod_status_ready{{namespace=~"{ns}", '
                     f'pod=~".+-worker-.+", condition="true"}} == 1)',
        },
    }


def instant_queries(settings):
    ns = _ns_regex([settings["primaryNamespace"], settings["scaleNamespace"]])
    return {
        "targets": f'up{{namespace=~"{ns}"}}',
        "rayResources": f'sum by (ray_io_cluster, name, state) (ray_resources{{namespace=~"{ns}", '
                        f'name=~"cpu|gpu", state=~"used|available"}})',
        "gpuUtilNow": f'max by (kubernetes_node) (ray_node_gpus_utilization{{namespace=~"{ns}"}})',
        "freshness": f'max(timestamp(up{{namespace=~"{ns}"}}))',
    }


def window_bounds(window, settings, now=None):
    """Returns (start, end, step) in Unix seconds for a window name, or None if unknown."""
    now = now or time.time()
    if window == REPLAY:
        start, end = settings.get("replayStart"), settings.get("replayEnd")
        if not (start and end):
            return None
        return start, end, 30
    if window not in WINDOWS:
        return None
    seconds, step = WINDOWS[window]
    end = math.floor(now / step) * step
    return end - seconds, end, step


def to_series(result, by):
    """Converts a Prometheus matrix into [{name, points: [[t, v], ...]}], skipping NaN values."""
    series = []
    for item in result:
        name = (item.get("metric") or {}).get(by) or "total"
        points = []
        for t, v in item.get("values") or []:
            try:
                value = float(v)
            except (TypeError, ValueError):
                continue
            if math.isnan(value) or math.isinf(value):
                continue
            points.append([float(t), round(value, 3)])
        series.append({"name": name, "points": points})
    series.sort(key=lambda s: s["name"])
    return series


def to_instant(result, keys):
    out = []
    for item in result:
        metric = item.get("metric") or {}
        try:
            value = float((item.get("value") or [None, None])[1])
        except (TypeError, ValueError):
            continue
        out.append({**{k: metric.get(k) for k in keys}, "value": value})
    return out
