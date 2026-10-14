"""Ray on AKS demo dashboard: a read-only, presentation-friendly view of the samples on a cluster.

Run in-cluster (see ../deploy and ../Deploy-Dashboard.ps1), where it listens on the pod's
loopback interface and is reachable only through `kubectl port-forward`. Or run it locally
against `kubectl proxy` for development:

    KUBE_API_URL=http://127.0.0.1:8001 DASHBOARD_AZ_CLI=1 python server.py

Data sources, freshest first:
  * Kubernetes API, every few seconds: nodes, pods, RayClusters, RayJobs, policies, events.
  * The GPU node's DCGM exporter (port 19400 on AKS-managed GPU nodes), every 2 seconds.
  * Submitter pod logs for job results while the pods exist.
  * Azure Monitor managed Prometheus for charts (30-second scrapes, about a minute behind).
  * Container insights (Log Analytics) for the output of jobs whose pods are gone.
"""

import collections
import concurrent.futures
import hashlib
import json
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import clients
import collect
import metrics

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/ui.js": ("ui.js", "text/javascript; charset=utf-8"),
    "/stages.js": ("stages.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
DNS_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def log(message):
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {message}", flush=True)


def _split(ref, default_ns, default_name):
    if not ref:
        return default_ns, default_name
    ns, _, name = ref.partition("/")
    return ns, name or default_name


def _epoch(text):
    dt = collect.parse_time(text)
    return dt.timestamp() if dt else None


def load_settings(env=os.environ):
    primary_ns, primary = _split(env.get("RAY_PRIMARY"), "ray-learn", "ray-t4")
    scale_ns, scale = _split(env.get("RAY_SCALE"), "ray-scale", "ray-scale")
    prices = {}
    if env.get("PRICES_JSON"):
        prices = {k: float(v) for k, v in json.loads(env["PRICES_JSON"]).items()}
    allowed = {"localhost", "127.0.0.1", "[::1]"}
    allowed.update(h.strip().lower() for h in env.get("ALLOWED_HOSTS", "").split(",") if h.strip())
    return {
        "port": int(env.get("PORT", "8080")),
        "bind": env.get("BIND_ADDRESS", "127.0.0.1"),
        "healthPort": int(env["HEALTH_PORT"]) if env.get("HEALTH_PORT") else None,
        "clusterName": env.get("CLUSTER_NAME", ""),
        "primaryNamespace": primary_ns,
        "primaryCluster": primary,
        "scaleNamespace": scale_ns,
        "scaleCluster": scale,
        "egressNamespace": env.get("EGRESS_NAMESPACE", "ray-egress"),
        "probeNamespaces": [n.strip() for n in env.get("PROBE_NAMESPACES", f"{primary_ns},ray-probe").split(",") if n.strip()],
        "pollSeconds": float(env.get("POLL_SECONDS", "2.5")),
        "dcgmPort": int(env.get("DCGM_PORT", "19400")),
        "dcgmViaApiProxy": env.get("DCGM_VIA_API_PROXY") == "1",
        "replayStart": _epoch(env.get("REPLAY_START")),
        "replayEnd": _epoch(env.get("REPLAY_END")),
        "replayLabel": env.get("REPLAY_LABEL", "Recorded run"),
        "prices": prices,
        "allowedHosts": allowed,
        "devReload": env.get("DASHBOARD_DEV") == "1",
    }


def host_allowed(host_header, allowed):
    """Rejects requests whose Host isn't localhost (a DNS-rebinding defense for the port-forward)."""
    if not host_header:
        return False
    host = host_header.strip().lower()
    if host.startswith("["):
        hostname = host[: host.find("]") + 1]
    elif host.count(":") == 1:
        hostname = host.split(":", 1)[0]
    else:
        hostname = host
    return hostname in allowed


def make_credential(env=os.environ):
    if env.get("AZURE_FEDERATED_TOKEN_FILE") and env.get("AZURE_CLIENT_ID") and env.get("AZURE_TENANT_ID"):
        return clients.WorkloadIdentityCredential(
            env["AZURE_CLIENT_ID"], env["AZURE_TENANT_ID"], env["AZURE_FEDERATED_TOKEN_FILE"],
            env.get("AZURE_AUTHORITY_HOST")), "workload identity"
    if env.get("DASHBOARD_AZ_CLI") == "1":
        return clients.AzureCliCredential(), "Azure CLI (local)"
    return None, None


def build_id(app_dir=APP_DIR):
    digest = hashlib.sha256()
    for name in sorted(os.listdir(app_dir)):
        if name.endswith((".py", ".js", ".css", ".html", ".svg")):
            with open(os.path.join(app_dir, name), "rb") as stream:
                digest.update(name.encode() + b"\0" + stream.read())
    return digest.hexdigest()[:7]


def history_kql(candidates):
    """KQL for the output of finished RayJobs whose submitter pods are gone.

    `candidates` are (uid, namespace, job name, kind, created) tuples whose names passed
    DNS_NAME, so they need no escaping: a '-' outside a character class is literal.
    """
    for _, ns, name, *_ in candidates:
        if not (DNS_NAME.match(ns or "") and DNS_NAME.match(name or "")):
            raise ValueError(f"not a DNS label: {ns}/{name}")
    namespaces = ", ".join(f"'{ns}'" for ns in sorted({c[1] for c in candidates}))
    names = "|".join(sorted({c[2] for c in candidates}))
    return (
        "ContainerLogV2\n"
        "| where TimeGenerated > ago(30d)\n"
        f"| where PodNamespace in ({namespaces})\n"
        f"| where PodName matches regex @'^({names})-[a-z0-9]{{5}}$'\n"
        "| extend msg = tostring(LogMessage)\n"
        "| where msg contains 'tensor_sum' or msg contains '\"DLV-'\n"
        "| project TimeGenerated, PodNamespace, PodName, msg\n"
        "| order by TimeGenerated asc"
    )


class Hub:
    """Collects data in background threads and serves cached snapshots to every viewer."""

    def __init__(self, settings, kube, prom=None, la=None, credential_kind=None):
        self.settings = settings
        self.kube = kube
        self.prom = prom
        self.la = la
        self.lock = threading.Lock()
        self.raw = {}
        self.results = {}
        self.final = set()
        self.timelines = {}
        self.history_attempts = {}
        self.probes_live = []
        self.probes_live_at = 0
        self.probes_history = []
        self.discovery = (0, None)
        self.gpu_live = {}
        self.prom_instant = {}
        self.metrics_cache = {}
        self.build = build_id()
        now = time.time()
        self.sources = {
            "kubernetes": {"ok": None, "error": None, "lastSuccess": None, "latencyMs": None},
            "dcgm": {"ok": None, "error": None, "nodes": 0},
            "prometheus": {"configured": prom is not None, "ok": None, "error": None, "lastSuccess": None,
                           "delaySeconds": None, "auth": credential_kind if prom else None},
            "logAnalytics": {"configured": la is not None, "ok": None, "error": None, "lastSuccess": None,
                             "auth": credential_kind if la else None},
        }
        self.started = now
        self.state_bytes = json.dumps({"loading": True}).encode()

    # ── Kubernetes ───────────────────────────────────────────────────────────

    def _list(self, path, errors, key, previous):
        """Lists objects. A 404 means the API (or CRD) isn't installed: nothing to show. Any other
        error keeps the last good objects, so one failed call can't make jobs or stages vanish."""
        try:
            return self.kube.items(path)
        except clients.ApiError as err:
            if err.status == 404:
                return []
            errors[key] = str(err)[:240]
            return previous

    def collect_kubernetes(self):
        s = self.settings
        errors = {}
        t0 = time.time()
        with self.lock:
            prev = self.raw

        def listed(key, path, error_key):
            return self._list(path, errors, error_key, prev.get(key, []))

        services = []
        for ns in sorted({s["primaryNamespace"], s["scaleNamespace"]}):
            services += self._list(f"/api/v1/namespaces/{ns}/services", errors, "services",
                                   [x for x in prev.get("services", []) if collect.dig(x, "metadata", "namespace") == ns])
        raw = {
            "nodes": listed("nodes", "/api/v1/nodes", "nodes"),
            "pods": listed("pods", "/api/v1/pods", "pods"),
            "deployments": listed("deployments", "/apis/apps/v1/deployments", "deployments"),
            "rayClusters": listed("rayClusters", "/apis/ray.io/v1/rayclusters", "rayclusters"),
            "rayJobs": listed("rayJobs", "/apis/ray.io/v1/rayjobs", "rayjobs"),
            "networkPolicies": listed("networkPolicies", "/apis/networking.k8s.io/v1/networkpolicies", "networkpolicies"),
            "podMonitors": listed("podMonitors", "/apis/azmonitoring.coreos.com/v1/podmonitors", "podmonitors"),
            "scaleEvents": listed("scaleEvents", f"/api/v1/namespaces/{s['scaleNamespace']}/events", "events"),
            "services": services,
        }
        if time.time() - self.discovery[0] > 60:
            try:
                self.discovery = (time.time(), self.kube.get("/apis/ray.io/v1"))
            except clients.ApiError:
                self.discovery = (time.time(), None)
        raw["rayDiscovery"] = self.discovery[1]
        ok = "nodes" not in errors and "pods" not in errors
        self.sources["kubernetes"].update(
            ok=ok, error="; ".join(f"{k}: {v}" for k, v in errors.items()) or None,
            latencyMs=int((time.time() - t0) * 1000))
        if ok:
            self.sources["kubernetes"]["lastSuccess"] = collect.iso(datetime.now(timezone.utc))
        return raw, ok

    def update_timelines(self, raw, now_iso):
        for rj in raw["rayJobs"]:
            uid = collect.dig(rj, "metadata", "uid")
            status = rj.get("status") or {}
            key = (status.get("jobStatus") or "", status.get("jobDeploymentStatus") or "")
            if uid not in self.timelines:
                terminal = key[0] in collect.TERMINAL_JOB_STATES or key[1] in collect.TERMINAL_DEPLOYMENT_STATES
                # Only jobs seen while still running get a timeline; older jobs keep start and end times.
                self.timelines[uid] = None if terminal else []
            timeline = self.timelines[uid]
            if timeline is not None and (not timeline or (timeline[-1]["jobStatus"], timeline[-1]["deploymentStatus"]) != key):
                timeline.append({"jobStatus": key[0], "deploymentStatus": key[1], "at": now_iso})

    def _submitter_pod(self, pods, namespace, job_name, created):
        pattern = collect.submitter_pod_re(job_name)
        candidates = [p for p in pods if collect.dig(p, "metadata", "namespace") == namespace
                      and (collect.labels_of(p).get("job-name") == job_name
                           or pattern.match(collect.dig(p, "metadata", "name") or ""))
                      and (collect.dig(p, "metadata", "creationTimestamp") or "") >= (created or "")]
        candidates.sort(key=lambda p: collect.dig(p, "metadata", "creationTimestamp") or "")
        return candidates[-1] if candidates else None

    def update_results(self, raw, now_iso):
        for rj in raw["rayJobs"]:
            meta = rj.get("metadata") or {}
            uid, name, ns = meta.get("uid"), meta.get("name"), meta.get("namespace")
            kind, _ = collect.job_kind(name or "")
            if kind == "other" or uid in self.final:
                continue
            status = rj.get("status") or {}
            terminal = (status.get("jobStatus") in collect.TERMINAL_JOB_STATES
                        or status.get("jobDeploymentStatus") in collect.TERMINAL_DEPLOYMENT_STATES)
            pod = self._submitter_pod(raw["pods"], ns, name, meta.get("creationTimestamp"))
            if not pod:
                continue
            containers = collect.dig(pod, "spec", "containers", default=[]) or []
            container = None if len(containers) == 1 else next(
                (c["name"] for c in containers if c["name"] == "ray-job-submitter"), None)
            try:
                text = self.kube.logs(ns, pod["metadata"]["name"], container=container, tail=600)
            except clients.ApiError as err:
                if err.status not in (400, 404):
                    log(f"logs {ns}/{pod['metadata']['name']}: {err}")
                continue
            parsed = collect.result_from_lines(kind, text.splitlines())
            if parsed:
                self.results[uid] = {"result": parsed, "source": "pod log", "at": now_iso,
                                     "note": f"pod {pod['metadata']['name']}"}
            phase = collect.dig(pod, "status", "phase")
            if terminal and (parsed or phase in ("Succeeded", "Failed")):
                self.final.add(uid)

    def update_live_probes(self, raw):
        probes = []
        for pod in raw["pods"]:
            ns = collect.dig(pod, "metadata", "namespace")
            name = collect.dig(pod, "metadata", "name") or ""
            if ns not in self.settings["probeNamespaces"] or not name.startswith("probe-"):
                continue
            if collect.dig(pod, "status", "phase") not in ("Succeeded", "Failed", "Running"):
                continue
            try:
                text = self.kube.logs(ns, name, tail=50)
            except clients.ApiError:
                continue
            view = collect.probe_view(name, ns, collect.dig(pod, "metadata", "creationTimestamp"), text.splitlines())
            if view["checks"]:
                view["source"] = "pod log"
                probes.append(view)
        self.probes_live = probes

    def kubernetes_loop(self):
        while True:
            started = time.time()
            try:
                raw, ok = self.collect_kubernetes()
                now = datetime.now(timezone.utc)
                if ok:
                    now_iso = collect.iso(now)
                    self.update_timelines(raw, now_iso)
                    self.update_results(raw, now_iso)
                    if started - self.probes_live_at > 15:
                        self.update_live_probes(raw)
                        self.probes_live_at = started
                    with self.lock:
                        self.raw = raw
                # Publish even when the API failed, so the page shows the error over the last good data.
                self.publish(now)
            except Exception as err:  # pragma: no cover - keep the loop alive whatever happens
                self.sources["kubernetes"].update(ok=False, error=f"{type(err).__name__}: {err}"[:240])
                log("kubernetes loop: " + traceback.format_exc(limit=3))
            time.sleep(max(0.5, self.settings["pollSeconds"] - (time.time() - started)))

    # ── DCGM ────────────────────────────────────────────────────────────────

    def dcgm_loop(self):
        while True:
            started = time.time()
            try:
                self.poll_dcgm(started)
            except Exception as err:  # pragma: no cover - keep the loop alive whatever happens
                self.sources["dcgm"].update(ok=False, error=f"{type(err).__name__}: {err}"[:240])
                log("dcgm loop: " + traceback.format_exc(limit=3))
            time.sleep(max(0.5, 2.0 - (time.time() - started)))

    def poll_dcgm(self, started):
        """Reads every AKS-managed GPU node's DCGM exporter once. A node that fails keeps its last
        reading, whose age the page shows as stale."""
        with self.lock:
            nodes = [n for n in self.raw.get("nodes", [])
                     if collect.labels_of(n).get("kubernetes.azure.com/dcgm-exporter") == "enabled"]
        errors = []
        for node in nodes:
            name = node["metadata"]["name"]
            ip = next((a["address"] for a in collect.dig(node, "status", "addresses", default=[])
                       if a.get("type") == "InternalIP"), None)
            try:
                if self.settings["dcgmViaApiProxy"]:
                    text = self.kube.proxy_text(f"/api/v1/nodes/{name}:{self.settings['dcgmPort']}/proxy/metrics")
                else:
                    text = clients.scrape_dcgm(f"http://{ip}:{self.settings['dcgmPort']}/metrics")
            except clients.ApiError as err:
                errors.append(f"{name}: {err}"[:200])
                continue
            gpus = clients.parse_dcgm(text)
            with self.lock:
                entry = self.gpu_live.setdefault(name, {"history": {}})
                entry["gpus"] = gpus
                entry["at"] = started
                for idx, g in gpus.items():
                    hist = entry["history"].setdefault(idx, collections.deque(maxlen=450))
                    hist.append([round(started, 1), g.get("util"), g.get("fbUsedMiB"), g.get("powerW")])
        known = {n["metadata"]["name"] for n in nodes}
        with self.lock:
            for gone in [k for k in self.gpu_live if k not in known]:
                del self.gpu_live[gone]
        self.sources["dcgm"].update(ok=(not errors) if nodes else None, error="; ".join(errors) or None,
                                    nodes=len(nodes))

    def live_gpus(self, node_views):
        by_name = {n["name"]: n for n in node_views}
        out = []
        now = time.time()
        with self.lock:
            for name, entry in self.gpu_live.items():
                for idx, g in sorted((entry.get("gpus") or {}).items()):
                    hist = list(entry["history"].get(idx, []))[-180:]
                    used, free = g.get("fbUsedMiB"), g.get("fbFreeMiB")
                    node = by_name.get(name, {})
                    out.append({
                        "node": name, "short": node.get("short", name), "pool": node.get("pool"), "gpu": idx,
                        "model": g.get("model"), "util": g.get("util"), "fbUsedMiB": used,
                        "fbTotalMiB": (used + free) if used is not None and free is not None else None,
                        "powerW": g.get("powerW"), "tempC": g.get("tempC"), "smClockMHz": g.get("smClockMHz"),
                        "ageSeconds": round(now - entry.get("at", now), 1), "history": hist,
                    })
        return out

    # ── Container insights (job output after the pods are gone) ─────────────

    def _history_candidates(self, raw):
        now = time.time()
        pods = raw.get("pods", [])
        out = []
        for rj in raw.get("rayJobs", []):
            meta = rj.get("metadata") or {}
            uid, name, ns = meta.get("uid"), meta.get("name") or "", meta.get("namespace") or ""
            kind, _ = collect.job_kind(name)
            status = rj.get("status") or {}
            terminal = (status.get("jobStatus") in collect.TERMINAL_JOB_STATES
                        or status.get("jobDeploymentStatus") in collect.TERMINAL_DEPLOYMENT_STATES)
            if (kind == "other" or not terminal or uid in self.final or uid in self.results
                    or not DNS_NAME.match(name) or not DNS_NAME.match(ns)
                    or now - self.history_attempts.get(uid, 0) < 600
                    or self._submitter_pod(pods, ns, name, meta.get("creationTimestamp"))):
                continue
            out.append((uid, ns, name, kind, meta.get("creationTimestamp")))
        return out

    def update_history(self):
        with self.lock:
            raw = self.raw
        candidates = self._history_candidates(raw)[:20]
        if not candidates:
            return
        for uid, *_ in candidates:
            self.history_attempts[uid] = time.time()
        kql = history_kql(candidates)
        rows = None
        try:
            rows = self.la.query(kql)
        finally:
            if rows is None:
                # The query failed, which says nothing about these jobs: retry in a minute, not ten.
                for uid, *_ in candidates:
                    self.history_attempts[uid] = time.time() - 540
        for uid, ns, name, kind, created in candidates:
            pattern = collect.submitter_pod_re(name)
            mine = [r for r in rows if r["PodNamespace"] == ns and pattern.match(r["PodName"] or "")
                    and (r["TimeGenerated"] or "") >= (created or "")]
            parsed = collect.result_from_lines(kind, [r["msg"] or "" for r in mine])
            if parsed:
                self.results[uid] = {"result": parsed, "source": "Container insights", "at": mine[-1]["TimeGenerated"],
                                     "note": f"pod {mine[-1]['PodName']}, which no longer exists"}
                self.final.add(uid)

    def update_probe_history(self):
        namespaces = ", ".join(f"'{ns}'" for ns in self.settings["probeNamespaces"] if DNS_NAME.match(ns))
        if not namespaces:
            return
        rows = self.la.query(
            "ContainerLogV2\n| where TimeGenerated > ago(30d)\n"
            f"| where PodNamespace in ({namespaces}) and PodName startswith 'probe-'\n"
            "| extend msg = tostring(LogMessage)\n| where msg contains '\"check\"'\n"
            "| project TimeGenerated, PodNamespace, PodName, msg\n| order by TimeGenerated asc")
        by_probe = collections.defaultdict(lambda: collections.defaultdict(list))
        for r in rows:
            pod_name = r["PodName"] or ""
            by_probe[re.sub(r"-[a-z0-9]{5}$", "", pod_name)][pod_name].append(r)
        probes = []
        for probe, by_pod in sorted(by_probe.items()):
            # A probe Job rerun gets a new pod; show the newest run.
            pod_name, pod_rows = max(by_pod.items(), key=lambda kv: kv[1][-1]["TimeGenerated"] or "")
            view = collect.probe_view(pod_name, pod_rows[0]["PodNamespace"], pod_rows[0]["TimeGenerated"],
                                      [r["msg"] or "" for r in pod_rows])
            view["source"] = "Container insights"
            probes.append(view)
        self.probes_history = probes

    def log_analytics_loop(self):
        probes_at = 0
        while True:
            try:
                if self.raw:
                    self.update_history()
                    if time.time() - probes_at > 600:
                        self.update_probe_history()
                        probes_at = time.time()
                    self.sources["logAnalytics"].update(ok=True, error=None,
                                                        lastSuccess=collect.iso(datetime.now(timezone.utc)))
            except Exception as err:  # pragma: no cover
                self.sources["logAnalytics"].update(ok=False, error=f"{err}"[:300])
                log(f"log analytics: {err}")
            time.sleep(20)

    # ── Prometheus ──────────────────────────────────────────────────────────

    def prometheus_loop(self):
        queries = metrics.instant_queries(self.settings)
        while True:
            try:
                out = {}
                for key, promql in queries.items():
                    out[key] = self.prom.query(promql)
                fresh = out.get("freshness") or []
                delay = None
                if fresh:
                    try:
                        delay = max(0, round(time.time() - float(fresh[0]["value"][1])))
                    except (KeyError, ValueError, TypeError, IndexError):
                        delay = None
                self.prom_instant = {
                    "targets": metrics.to_instant(out.get("targets", []), ["namespace", "pod", "ray_node_type", "ray_io_cluster"]),
                    "rayResources": metrics.to_instant(out.get("rayResources", []), ["ray_io_cluster", "name", "state"]),
                    "gpuUtil": metrics.to_instant(out.get("gpuUtilNow", []), ["kubernetes_node"]),
                    "at": collect.iso(datetime.now(timezone.utc)),
                }
                self.sources["prometheus"].update(ok=True, error=None, delaySeconds=delay,
                                                  lastSuccess=collect.iso(datetime.now(timezone.utc)))
            except Exception as err:  # pragma: no cover
                self.sources["prometheus"].update(ok=False, error=f"{err}"[:300])
                log(f"prometheus: {err}")
            time.sleep(30)

    def chart_data(self, window):
        if not self.prom:
            return {"configured": False}
        bounds = metrics.window_bounds(window, self.settings)
        if not bounds:
            return {"configured": True, "error": f"Unknown window '{window}'."}
        cached = self.metrics_cache.get(window)
        if cached and cached[0] > time.time():
            return cached[1]
        start, end, step = bounds
        with self.lock:
            nodes = collect.node_views(self.raw.get("nodes", []), [])
        gpu_pools = {n["pool"] for n in nodes if collect.is_gpu_node(n)}
        catalog = metrics.chart_queries(self.settings, gpu_pools)
        charts = {}

        def run(item):
            key, spec = item
            try:
                series = metrics.to_series(self.prom.query_range(spec["query"], start, end, step), spec["by"])
                return key, {**{k: v for k, v in spec.items() if k != "query"}, "series": series, "error": None}
            except Exception as err:  # one slow query mustn't blank the others
                return key, {**{k: v for k, v in spec.items() if k != "query"}, "series": [], "error": str(err)[:200]}

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            for key, chart in pool.map(run, catalog.items()):
                charts[key] = chart
        data = {"configured": True, "window": window, "start": start, "end": end, "step": step, "charts": charts,
                "replayLabel": self.settings["replayLabel"] if window == metrics.REPLAY else None}
        ttl = 600 if window == metrics.REPLAY else 20
        self.metrics_cache[window] = (time.time() + ttl, data)
        return data

    # ── The snapshot ────────────────────────────────────────────────────────

    def publish(self, now):
        with self.lock:
            raw = self.raw
        sources = {k: dict(v) for k, v in self.sources.items()}
        if not raw:
            # No good snapshot yet: tell the page why it's still waiting.
            body = json.dumps({"loading": True, "sources": sources}, separators=(",", ":"), default=str).encode()
            with self.lock:
                self.state_bytes = body
            return
        probes = self.probes_live or self.probes_history
        state = collect.build_state(raw, self.results, self.timelines, probes, self.settings, now)
        state.update({
            "build": self.build,
            "sources": sources,
            "liveGpus": self.live_gpus(state["nodes"]),
            "prometheus": self.prom_instant,
            "replay": {"available": bool(self.settings["replayStart"] and self.settings["replayEnd"]),
                       "label": self.settings["replayLabel"],
                       "start": self.settings["replayStart"], "end": self.settings["replayEnd"]},
        })
        body = json.dumps(state, separators=(",", ":"), default=str).encode()
        with self.lock:
            self.state_bytes = body

    def start(self):
        threading.Thread(target=self.kubernetes_loop, name="kubernetes", daemon=True).start()
        threading.Thread(target=self.dcgm_loop, name="dcgm", daemon=True).start()
        if self.la:
            threading.Thread(target=self.log_analytics_loop, name="log-analytics", daemon=True).start()
        if self.prom:
            threading.Thread(target=self.prometheus_loop, name="prometheus", daemon=True).start()


def make_handler(hub, settings):
    static_cache = {}

    def static(name):
        if settings["devReload"] or name not in static_cache:
            with open(os.path.join(APP_DIR, name), "rb") as stream:
                static_cache[name] = stream.read()
        return static_cache[name]

    class Handler(BaseHTTPRequestHandler):
        server_version = "ray-on-aks-dashboard"
        sys_version = ""

        def _send(self, code, body, content_type, cache="no-store"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code, value):
            self._send(code, json.dumps(value, separators=(",", ":"), default=str).encode(), "application/json")

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/healthz":
                return self._send(200, b"ok", "text/plain")
            if not host_allowed(self.headers.get("Host"), settings["allowedHosts"]):
                return self._send(403, b"Use kubectl port-forward and open http://127.0.0.1:<port>/", "text/plain")
            if url.path == "/api/state":
                with hub.lock:
                    body = hub.state_bytes
                return self._send(200, body, "application/json")
            if url.path == "/api/metrics":
                window = (parse_qs(url.query).get("window") or ["1h"])[0]
                return self._json(200, hub.chart_data(window))
            if url.path in STATIC:
                name, content_type = STATIC[url.path]
                try:
                    return self._send(200, static(name), content_type, "no-cache")
                except OSError:
                    return self._send(404, b"not found", "text/plain")
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):
            self._send(405, b"read-only", "text/plain")

        do_PUT = do_DELETE = do_PATCH = do_POST

        def log_message(self, fmt, *args):
            if settings["devReload"]:
                sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    return Handler


class HealthHandler(BaseHTTPRequestHandler):
    """Answers the kubelet's probes on the pod IP. The dashboard itself listens on loopback only,
    so it's reachable through `kubectl port-forward` and nothing else."""
    server_version = "ray-on-aks-dashboard"
    sys_version = ""

    def do_GET(self):
        ok = urlparse(self.path).path == "/healthz"
        body = b"ok" if ok else b"not found"
        self.send_response(200 if ok else 404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def main():
    settings = load_settings()
    kube = clients.KubeClient(os.environ.get("KUBE_API_URL"))
    credential, kind = make_credential()
    prom = None
    la = None
    if credential and os.environ.get("PROMETHEUS_QUERY_ENDPOINT"):
        prom = clients.PrometheusClient(os.environ["PROMETHEUS_QUERY_ENDPOINT"], credential)
    if credential and os.environ.get("LOG_ANALYTICS_WORKSPACE_ID"):
        la = clients.LogAnalyticsClient(os.environ["LOG_ANALYTICS_WORKSPACE_ID"], credential)
    hub = Hub(settings, kube, prom, la, kind)
    hub.start()
    if settings["healthPort"]:
        health = ThreadingHTTPServer(("0.0.0.0", settings["healthPort"]), HealthHandler)
        health.daemon_threads = True
        threading.Thread(target=health.serve_forever, name="health", daemon=True).start()
    server = ThreadingHTTPServer((settings["bind"], settings["port"]), make_handler(hub, settings))
    server.daemon_threads = True
    log(f"build {hub.build} listening on {settings['bind']}:{settings['port']} "
        f"(kubernetes: {'proxy' if kube.via_proxy else 'in-cluster'}, metrics: {kind or 'off'}, "
        f"prometheus: {'on' if prom else 'off'}, container insights: {'on' if la else 'off'}"
        f"{', health on :' + str(settings['healthPort']) if settings['healthPort'] else ''})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
