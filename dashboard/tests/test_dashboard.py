"""Offline checks for the demo dashboard: log parsing, the view model, the PromQL and KQL it
builds, HTTP hardening, the manifest's security posture, and the deploy script.

Run from the repository root: python -m pytest dashboard/tests
"""

import http.client
import json
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

DASHBOARD = Path(__file__).resolve().parents[1]
APP = DASHBOARD / "app"
MANIFEST = DASHBOARD / "deploy" / "dashboard.yaml"
DEPLOY_SCRIPT = DASHBOARD / "Deploy-Dashboard.ps1"
sys.path.insert(0, str(APP))

import clients  # noqa: E402
import collect  # noqa: E402
import metrics  # noqa: E402
import server  # noqa: E402

NOW = datetime(2026, 9, 29, 23, 0, tzinfo=timezone.utc)
GUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
GPU_LINE = '{"gpu": "Tesla T4", "cuda_runtime": "12.8", "torch_version": "2.8.0+cu128", "tensor_sum": 10.0}'
REVISION = "0123456789abcdef0123456789abcdef01234567"


# ── Builders for the Kubernetes objects the dashboard reads ─────────────────

def node(name, pool, vm, gpu=0, ready=True, allocatable=None, managed=False, conditions=()):
    labels = {"kubernetes.azure.com/agentpool": pool, "node.kubernetes.io/instance-type": vm,
              "topology.kubernetes.io/region": "eastus"}
    if gpu:
        labels["kubernetes.azure.com/accelerator"] = "nvidia"
    if managed:
        labels["kubernetes.azure.com/dcgm-exporter"] = "enabled"
    alloc = gpu if allocatable is None else allocatable
    return {
        "metadata": {"name": name, "labels": labels, "creationTimestamp": "2026-09-29T21:00:00Z"},
        "spec": {"taints": [{"key": "sku", "value": "gpu", "effect": "NoSchedule"}] if gpu else []},
        "status": {
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}, *conditions],
            "capacity": {"nvidia.com/gpu": str(gpu)} if gpu else {},
            "allocatable": {"nvidia.com/gpu": str(alloc)} if gpu else {},
            "nodeInfo": {"osImage": "Ubuntu 24.04.5 LTS", "kernelVersion": "6.8.0-1067-azure",
                         "kubeletVersion": "v1.36.3", "containerRuntimeVersion": "containerd://2.3.3-2"},
            "addresses": [{"type": "InternalIP", "address": "10.224.0.5"}],
        },
    }


def pod(name, ns, node_name=None, phase="Running", labels=None, gpus=0, ready=True, created="2026-09-29T22:00:00Z"):
    resources = {"limits": {"nvidia.com/gpu": str(gpus)}} if gpus else {}
    return {
        "metadata": {"name": name, "namespace": ns, "labels": labels or {}, "creationTimestamp": created},
        "spec": {"nodeName": node_name, "containers": [{"name": "main", "resources": resources}]},
        "status": {"phase": phase, "containerStatuses": [{"ready": ready, "restartCount": 0}]},
    }


def ray_cluster(name, ns, pool, autoscale=False):
    spec = {
        "rayVersion": "2.58.0",
        "headGroupSpec": {"rayStartParams": {"num-cpus": "0"},
                          "template": {"spec": {"containers": [{"image": "rayproject/ray:2.58.0", "env": []}]}}},
        "workerGroupSpecs": [{
            "groupName": "t4", "replicas": 1, "minReplicas": 1, "maxReplicas": 4 if autoscale else 1,
            "rayStartParams": {"num-gpus": "1", "num-cpus": "2"},
            "template": {"spec": {
                "nodeSelector": {"kubernetes.azure.com/agentpool": pool},
                "containers": [{"image": "rayproject/ray:2.58.0-gpu",
                                "resources": {"limits": {"nvidia.com/gpu": "1", "cpu": "2", "memory": "12Gi"}}}],
            }},
        }],
    }
    if autoscale:
        spec["enableInTreeAutoscaling"] = True
        spec["autoscalerOptions"] = {"version": "v2", "idleTimeoutSeconds": 180, "upscalingMode": "Conservative"}
    return {"metadata": {"name": name, "namespace": ns, "creationTimestamp": "2026-09-29T21:30:00Z"},
            "spec": spec, "status": {"state": "ready", "desiredWorkerReplicas": 1}}


def ray_job(name, uid, status="SUCCEEDED", deployment="Complete", start="2026-09-29T22:45:00Z",
            end="2026-09-29T22:48:23Z", created="2026-09-29T22:44:50Z", ns="ray-learn"):
    st = {"jobStatus": status, "jobDeploymentStatus": deployment, "startTime": start}
    if end:
        st["endTime"] = end
    return {"metadata": {"name": name, "namespace": ns, "uid": uid, "creationTimestamp": created},
            "spec": {"clusterSelector": {"ray.io/cluster": "ray-t4"},
                     "submitterPodTemplate": {"metadata": {"labels": {"ray-submit": "ray-t4"}}}},
            "status": st}


def raw_cluster(gpu_allocatable=1, jobs=()):
    nodes = [node("aks-nodepool1-1234-vmss000000", "nodepool1", "Standard_D4s_v5"),
             node("aks-raygpu-5678-vmss000000", "raygpu", "Standard_NC4as_T4_v3", gpu=1,
                  allocatable=gpu_allocatable, managed=True)]
    pods = [
        pod("ray-t4-head-abcde", "ray-learn", nodes[0]["metadata"]["name"],
            labels={"ray.io/cluster": "ray-t4", "ray.io/node-type": "head"}),
        pod("ray-t4-t4-worker-fghij", "ray-learn", nodes[1]["metadata"]["name"], gpus=1,
            labels={"ray.io/cluster": "ray-t4", "ray.io/node-type": "worker"}),
        pod("cilium-xyz12", "kube-system", nodes[0]["metadata"]["name"]),
    ]
    policy = {"metadata": {"name": "ray-learn-default-deny", "namespace": "ray-learn"},
              "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}}
    return {"nodes": nodes, "pods": pods, "rayClusters": [ray_cluster("ray-t4", "ray-learn", "raygpu")],
            "rayJobs": list(jobs), "networkPolicies": [policy], "deployments": [], "podMonitors": [],
            "services": [], "scaleEvents": []}


def build(raw, results=None, timelines=None):
    return collect.build_state(raw, results or {}, timelines or {}, [], server.load_settings({}), NOW)


def stage(state, sid):
    return next(s for s in state["stages"] if s["id"] == sid)


# ── Job output parsing ──────────────────────────────────────────────────────

def test_gpu_result_parses_pod_log_and_container_insights_formats():
    pod_log = ["Job submitted", f"(gpu_check pid=312, ip=10.244.1.7) {GPU_LINE}", "Job succeeded"]
    # Container insights re-serializes the JSON compactly, with its keys reordered.
    insights = ['{"cuda_runtime":"12.8","gpu":"Tesla T4","tensor_sum":10,"torch_version":"2.8.0+cu128"}']
    for lines in (pod_log, insights):
        result = collect.parse_gpu_result(lines)
        assert result["gpu"] == "Tesla T4"
        assert collect.verify_gpu_result(result)["ok"]


def test_gpu_result_ignores_prose_and_fails_a_wrong_sum():
    assert collect.parse_gpu_result(["expect tensor_sum to be 10.0"]) is None
    wrong = collect.parse_gpu_result([GPU_LINE.replace("10.0", "9.0")])
    verified = collect.verify_gpu_result(wrong)
    assert not verified["ok"]
    assert [c["ok"] for c in verified["checks"]] == [True, False, True]


def inference_line(rid, summary, event="The driver could not access the building and will retry tomorrow."):
    return json.dumps({"id": rid, "event": event, "summary": summary, "gpu": "Tesla T4",
                       "model": "Qwen/Qwen2.5-0.5B-Instruct", "model_revision": REVISION})


def test_inference_records_last_one_wins_and_verify():
    lines = [inference_line(f"DLV-00{i}", f"summary {i}") for i in range(1, 5)]
    lines.append(inference_line("DLV-001", "rerun"))
    records = collect.parse_inference_records(lines)
    assert [r["id"] for r in records] == ["DLV-001", "DLV-002", "DLV-003", "DLV-004"]
    assert records[0]["summary"] == "rerun"
    assert collect.verify_inference(records)["ok"]
    assert not collect.verify_inference(records[:3])["ok"]


def test_review_note_applies_only_to_the_reviewed_generation():
    reviewed = collect.REVIEWED_OUTPUTS["DLV-002"]["summary"]
    same, different = collect.annotate_records([json.loads(inference_line("DLV-002", reviewed)),
                                                json.loads(inference_line("DLV-002", "The driver will retry tomorrow."))])
    assert same["reviewNote"] and "denied" in same["novelWords"]
    assert different["reviewNote"] is None


def test_novel_words_are_content_words_missing_from_the_input():
    words = collect.novel_words("The parcel arrived at the local depot this morning.",
                                "The package was delivered to the local depot today.")
    assert words == ["package", "delivered", "today"]
    assert collect.novel_words("The truck was delayed.", "The truck was delayed for two hours.") == ["two", "hours"]


def test_job_kind_and_submitter_pod_names():
    assert collect.job_kind("ray-gpu-check") == ("gpu-check", "")
    assert collect.job_kind("ray-gpu-check-live") == ("gpu-check", "live")
    assert collect.job_kind("ray-inference-sec") == ("inference", "sec")
    assert collect.job_kind("burst") == ("other", "")
    pattern = collect.submitter_pod_re("ray-gpu-check")
    assert pattern.match("ray-gpu-check-5dvgz")
    assert not pattern.match("ray-gpu-check-sec-5dvgz")


def test_probe_results_are_judged_against_the_expected_outcome():
    lines = ['{"check": "dns", "status": "resolved"}',
             '{"check": "submitter_network_allowed_auth_required", "status": 401}',
             '{"check": "unapproved_pod_to_head", "target_ip": "10.244.0.9", "status": "connected"}']
    view = collect.probe_view("probe-denied-same-ns-ab12c", "ray-learn", None, lines)
    assert view["name"] == "probe-denied-same-ns"
    assert [c["ok"] for c in view["checks"]] == [True, True, False]


# ── The view model and stages ───────────────────────────────────────────────

def test_gpu_stage_is_done_only_when_ready_and_allocatable():
    joining = raw_cluster(gpu_allocatable=0)
    joining["nodes"][1]["metadata"]["creationTimestamp"] = "2026-09-29T22:55:00Z"
    assert stage(build(joining), "gpu") == {"id": "gpu", "status": "live", "caption": "1 Ready, GPU not yet allocatable"}
    # Registered two hours ago and still no GPU: something's wrong, not starting.
    assert stage(build(raw_cluster(gpu_allocatable=0)), "gpu")["status"] == "warn"

    not_ready = raw_cluster()
    not_ready["nodes"][1]["status"]["conditions"][0]["status"] = "False"
    assert stage(build(not_ready), "gpu") == {"id": "gpu", "status": "warn", "caption": "1 GPU node not Ready"}
    assert stage(build(not_ready), "cluster")["status"] == "warn"
    not_ready["nodes"][1]["metadata"]["creationTimestamp"] = "2026-09-29T22:58:00Z"
    assert stage(build(not_ready), "gpu")["status"] == "live"

    assert stage(build(raw_cluster()), "gpu") == {"id": "gpu", "status": "done", "caption": "1 GPU allocatable"}


def test_stage_ids_match_the_front_end():
    ids = [s["id"] for s in build(raw_cluster())["stages"]]
    js = (APP / "stages.js").read_text(encoding="utf-8")
    assert re.findall(r"^\s+id: '([a-z]+)'", js, re.MULTILINE) == ids
    assert len(ids) == 10


def test_running_job_is_live_and_a_verified_result_is_done():
    running = ray_job("ray-gpu-check-live", "u1", status="RUNNING", deployment="Running", end=None,
                      start="2026-09-29T22:55:00Z", created="2026-09-29T22:54:00Z")
    state = build(raw_cluster(jobs=[running]))
    assert stage(state, "gpucheck")["status"] == "live"
    assert state["rayJobs"][0]["durationSeconds"] == 300

    done = ray_job("ray-gpu-check", "u2")
    result = {"u2": {"result": collect.result_from_lines("gpu-check", [GPU_LINE]), "source": "pod log",
                     "at": "2026-09-29T22:48:30Z", "note": None}}
    state = build(raw_cluster(jobs=[done]), results=result)
    assert stage(state, "gpucheck") == {"id": "gpucheck", "status": "done", "caption": "Tesla T4 · 3 min 23 s"}


def test_security_view_reads_default_deny_and_labeled_submitters():
    state = build(raw_cluster(jobs=[ray_job("ray-gpu-check", "u2")]))
    checks = {c["id"]: c for c in state["security"]["checks"]}
    assert checks["default-deny"]["ok"] and checks["default-deny"]["detail"] == "Egress, Ingress"
    assert checks["submitter-label"]["detail"] == "1 of 1"
    assert not checks["token-auth"]["ok"]


def test_cost_counts_every_node_at_list_price():
    state = build(raw_cluster())
    assert state["cost"]["perHour"] == pytest.approx(0.192 + 0.526)
    assert state["cost"]["gpuPerHour"] == pytest.approx(0.526)
    gpu_node = next(n for n in state["nodes"] if n["gpuCapacity"])
    assert gpu_node["gpuAllocated"] == 1 and gpu_node["gpuHolders"][0]["pod"] == "ray-t4-t4-worker-fghij"


def test_cluster_view_detects_cilium_and_the_container_host():
    cluster = build(raw_cluster())["cluster"]
    assert cluster["dataplane"] == "Cilium"
    assert cluster["osFamilies"] == ["Ubuntu"]
    assert collect.os_family("Azure Linux 3.0") == "Azure Linux"


# ── Queries: the browser picks a window, the server builds every query ──────

def test_alternation_keeps_dns_labels_and_never_escapes():
    assert metrics._alternation(["ray-scale", "ray-learn", "Bad Name", 'x"}', "", None, "ray-learn"]) == "ray-learn|ray-scale"
    settings = server.load_settings({})
    queries = list(metrics.instant_queries(settings).values())
    queries += [q["query"] for q in metrics.chart_queries(settings, {"raygpu", "scalet4"}).values()]
    for query in queries:
        assert "\\" not in query, query
        assert '=~""' not in query, query


def test_window_bounds_align_and_replay_needs_a_configured_range():
    start, end, step = metrics.window_bounds("1h", {}, now=1_000_017)
    assert (end - start, step, end % step) == (3600, 30, 0)
    assert metrics.window_bounds("replay", {}) is None
    assert metrics.window_bounds("replay", {"replayStart": 10.0, "replayEnd": 20.0}) == (10.0, 20.0, 30)
    assert metrics.window_bounds("7d", {}) is None


def test_series_conversion_drops_nan_and_names_by_label():
    series = metrics.to_series([
        {"metric": {"kubernetes_node": "b"}, "values": [[1, "1"], [2, "NaN"], [3, "x"]]},
        {"metric": {}, "values": [[1, "2.5"]]},
    ], "kubernetes_node")
    assert series == [{"name": "b", "points": [[1.0, 1.0]]}, {"name": "total", "points": [[1.0, 2.5]]}]
    assert metrics.to_instant([{"metric": {"pod": "p", "x": "y"}, "value": [0, "1"]}], ["pod"]) == [{"pod": "p", "value": 1.0}]


def test_history_kql_uses_validated_names_unescaped():
    candidates = [("u1", "ray-learn", "ray-gpu-check-sec", "gpu-check", None),
                  ("u2", "ray-learn", "ray-inference", "inference", None)]
    kql = server.history_kql(candidates)
    assert "PodNamespace in ('ray-learn')" in kql
    assert "@'^(ray-gpu-check-sec|ray-inference)-[a-z0-9]{5}$'" in kql
    with pytest.raises(ValueError):
        server.history_kql([("u3", "ray-learn", "x') | union *", "gpu-check", None)])


def test_history_skips_running_jobs_jobs_with_pods_and_bad_names():
    hub = server.Hub(server.load_settings({}), kube=None)
    jobs = [ray_job("ray-gpu-check", "gone"), ray_job("ray-inference", "live-pod"),
            ray_job("ray-gpu-check-r2", "running", status="RUNNING", deployment="Running", end=None),
            ray_job("ray-gpu-check-BAD", "bad")]
    pods = [pod("ray-inference-ab12c", "ray-learn", created="2026-09-29T22:45:00Z")]
    assert [c[0] for c in hub._history_candidates({"rayJobs": jobs, "pods": pods})] == ["gone"]


def test_timelines_start_only_for_jobs_seen_running():
    hub = server.Hub(server.load_settings({}), kube=None)
    hub.update_timelines({"rayJobs": [ray_job("ray-gpu-check", "old")]}, "t0")
    job = ray_job("ray-gpu-check-live", "new", status="", deployment="Initializing", end=None)
    for status, deployment, at in (("", "Initializing", "t1"), ("RUNNING", "Running", "t2"), ("RUNNING", "Running", "t3")):
        job["status"].update(jobStatus=status, jobDeploymentStatus=deployment)
        hub.update_timelines({"rayJobs": [job]}, at)
    assert hub.timelines["old"] is None
    assert [(e["jobStatus"], e["at"]) for e in hub.timelines["new"]] == [("", "t1"), ("RUNNING", "t2")]


# ── Resilience: a failed call mustn't change what the audience sees ─────────

class FakeKube:
    """Serves lists by resource name, and fails the resources named in `failing` with HTTP 503."""

    def __init__(self, lists=None, failing=()):
        self.lists = lists or {}
        self.failing = set(failing)

    def items(self, path):
        resource = path.rstrip("/").rsplit("/", 1)[-1]
        if resource in self.failing:
            raise clients.ApiError(f"GET {path}: HTTP 503", 503)
        if resource == "podmonitors" and "podmonitors" not in self.lists:
            raise clients.ApiError(f"GET {path}: HTTP 404", 404)
        return self.lists.get(resource, [])

    def get(self, path):
        return {"resources": []}


def test_a_failed_list_keeps_the_last_good_objects():
    job = ray_job("ray-gpu-check", "u1")
    kube = FakeKube({"nodes": raw_cluster()["nodes"], "rayjobs": [job]})
    hub = server.Hub(server.load_settings({}), kube)
    hub.raw, ok = hub.collect_kubernetes()
    assert ok and hub.raw["rayJobs"] == [job] and hub.raw["podMonitors"] == []

    kube.failing = {"rayjobs"}
    raw, ok = hub.collect_kubernetes()
    assert ok, "nodes and pods still answered"
    assert raw["rayJobs"] == [job], "one failed call must not make every job vanish"
    assert "rayjobs" in hub.sources["kubernetes"]["error"]


def test_errors_reach_the_page_before_the_first_good_snapshot():
    hub = server.Hub(server.load_settings({}), FakeKube(failing={"nodes"}))
    raw, ok = hub.collect_kubernetes()
    assert not ok
    hub.publish(NOW)
    body = json.loads(hub.state_bytes)
    assert body["loading"] is True
    assert body["sources"]["kubernetes"]["ok"] is False and "503" in body["sources"]["kubernetes"]["error"]


def test_truncated_responses_become_api_errors(monkeypatch):
    def truncated(request, timeout=None, context=None):
        raise http.client.IncompleteRead(b"DCGM_FI_DEV_GPU_UTIL{gpu=")

    monkeypatch.setattr(clients.urllib.request, "urlopen", truncated)
    with pytest.raises(clients.ApiError, match="IncompleteRead"):
        clients.scrape_dcgm("http://10.224.0.5:19400/metrics")


def test_dcgm_poll_marks_failures_and_keeps_the_last_reading(monkeypatch):
    hub = server.Hub(server.load_settings({}), kube=None)
    name = "aks-raygpu-5678-vmss000000"
    hub.raw = {"nodes": [node(name, "raygpu", "Standard_NC4as_T4_v3", gpu=1, managed=True)]}
    monkeypatch.setattr(clients, "scrape_dcgm", lambda url, timeout=2.5: 'DCGM_FI_DEV_GPU_UTIL{gpu="0",modelName="Tesla T4"} 37\n')
    hub.poll_dcgm(1000.0)
    assert hub.sources["dcgm"]["ok"] is True and hub.gpu_live[name]["gpus"]["0"]["util"] == 37.0

    def broken(url, timeout=2.5):
        raise clients.ApiError(f"GET {url}: IncompleteRead")

    monkeypatch.setattr(clients, "scrape_dcgm", broken)
    hub.poll_dcgm(1002.0)
    assert hub.sources["dcgm"]["ok"] is False and "IncompleteRead" in hub.sources["dcgm"]["error"]
    assert hub.gpu_live[name]["at"] == 1000.0, "the last reading stays, and its age shows it's stale"


# ── Live GPU feed and credentials ───────────────────────────────────────────

def test_dcgm_exposition_is_parsed_per_gpu():
    text = "\n".join([
        "# HELP DCGM_FI_DEV_GPU_UTIL GPU utilization (in %).",
        "# TYPE DCGM_FI_DEV_GPU_UTIL gauge",
        'DCGM_FI_DEV_GPU_UTIL{gpu="0",UUID="GPU-x",device="nvidia0",modelName="Tesla T4",Hostname="node"} 37',
        'DCGM_FI_DEV_FB_USED{gpu="0",modelName="Tesla T4"} 1603',
        'DCGM_FI_DEV_FB_FREE{gpu="0",modelName="Tesla T4"} 13326',
        'DCGM_FI_DEV_POWER_USAGE{gpu="0",modelName="Tesla T4"} 27.5',
        'DCGM_FI_DEV_GPU_TEMP{gpu="0",modelName="Tesla T4"} 41',
        'DCGM_FI_DEV_XID_ERRORS{gpu="0"} 0',
    ])
    assert clients.parse_dcgm(text) == {"0": {"util": 37.0, "fbUsedMiB": 1603.0, "fbFreeMiB": 13326.0,
                                              "powerW": 27.5, "tempC": 41.0, "model": "Tesla T4"}}


def test_workload_identity_exchanges_the_projected_token_and_caches(tmp_path, monkeypatch):
    token_file = tmp_path / "azure-identity-token"
    token_file.write_text("projected-sa-token\n", encoding="utf-8")
    calls = []

    def fake_open(request, timeout, context=None):
        calls.append(request)
        return json.dumps({"access_token": "entra-token", "expires_in": 3600}).encode()

    monkeypatch.setattr(clients, "_open", fake_open)
    cred = clients.WorkloadIdentityCredential("client-id", "tenant-id", str(token_file))
    assert cred.get_token("https://prometheus.monitor.azure.com/.default") == "entra-token"
    assert cred.get_token("https://prometheus.monitor.azure.com/.default") == "entra-token"
    assert len(calls) == 1
    assert calls[0].full_url == "https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token"
    form = dict(pair.split("=", 1) for pair in calls[0].data.decode().split("&"))
    assert form["client_assertion"] == "projected-sa-token"
    assert form["grant_type"] == "client_credentials"


def test_pod_logs_accept_any_media_type(monkeypatch):
    """The log subresource answers HTTP 406 to "Accept: text/plain" (seen on Kubernetes 1.36)."""
    seen = []
    monkeypatch.setattr(clients, "_open", lambda request, timeout, context=None: seen.append(request) or b"line\n")
    kube = clients.KubeClient("http://127.0.0.1:8001")
    assert kube.logs("ray-learn", "ray-gpu-check-ab12c", tail=5) == "line\n"
    assert seen[0].get_header("Accept") == "*/*"
    assert seen[0].full_url.endswith("/api/v1/namespaces/ray-learn/pods/ray-gpu-check-ab12c/log?tailLines=5")


def test_credentials_come_from_workload_identity_or_explicit_local_opt_in():
    wi = {"AZURE_FEDERATED_TOKEN_FILE": "/t", "AZURE_CLIENT_ID": "c", "AZURE_TENANT_ID": "t"}
    assert isinstance(server.make_credential(wi)[0], clients.WorkloadIdentityCredential)
    assert isinstance(server.make_credential({"DASHBOARD_AZ_CLI": "1"})[0], clients.AzureCliCredential)
    assert server.make_credential({}) == (None, None)


# ── HTTP hardening ──────────────────────────────────────────────────────────

def test_only_localhost_host_headers_are_served():
    allowed = server.load_settings({})["allowedHosts"]
    for host in ("localhost:8090", "127.0.0.1:8090", "[::1]:8090", "LOCALHOST"):
        assert server.host_allowed(host, allowed), host
    for host in ("", None, "evil.example", "localhost.evil.example:8090", "127.0.0.1.nip.io", "[::2]:8090"):
        assert not server.host_allowed(host, allowed), host


def test_settings_default_to_loopback_and_parse_overrides():
    settings = server.load_settings({})
    assert settings["bind"] == "127.0.0.1" and settings["healthPort"] is None
    settings = server.load_settings({"RAY_PRIMARY": "team-a/gpu", "PRICES_JSON": '{"Standard_X": "1.5"}',
                                     "HEALTH_PORT": "8081"})
    assert (settings["primaryNamespace"], settings["primaryCluster"]) == ("team-a", "gpu")
    assert settings["prices"] == {"Standard_X": 1.5} and settings["healthPort"] == 8081


class FakeHub:
    lock = threading.Lock()
    state_bytes = b'{"ok":true}'

    def chart_data(self, window):
        return {"configured": False, "window": window}


@pytest.fixture(scope="module")
def port():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(FakeHub(), server.load_settings({})))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def call(port, path, method="GET", host=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest(method, path, skip_host=True)
    conn.putheader("Host", host or f"localhost:{port}")
    conn.endheaders()
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response.status, {k.lower(): v for k, v in response.getheaders()}, body


def test_page_and_api_are_served_with_a_strict_policy(port):
    status, headers, body = call(port, "/")
    assert status == 200 and b'<script src="/ui.js" defer>' in body
    assert "script-src 'self'" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY" and headers["x-content-type-options"] == "nosniff"
    assert call(port, "/api/state")[2] == b'{"ok":true}'
    assert json.loads(call(port, "/api/metrics?window=replay")[2])["window"] == "replay"


def test_rebinding_writes_and_source_files_are_refused(port):
    assert call(port, "/api/state", host="evil.example")[0] == 403
    assert call(port, "/healthz", host="evil.example")[0] == 200
    assert call(port, "/api/state", method="POST")[0] == 405
    for path in ("/server.py", "/../server.py", "/app/server.py", "/%2e%2e/server.py"):
        assert call(port, path)[0] == 404, path


def test_health_listener_answers_only_healthz():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.HealthHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert call(httpd.server_address[1], "/healthz")[0] == 200
        assert call(httpd.server_address[1], "/api/state")[0] == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


# ── Front end ───────────────────────────────────────────────────────────────

def test_every_served_and_imported_file_ships_in_the_configmap():
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    shipped = set(re.findall(r"'([^']+)'", re.search(r"\$AppFiles = @\(([^)]*)\)", script).group(1)))
    served = {name for name, _ in server.STATIC.values()}
    modules = {p.name for p in APP.glob("*.py")}
    assert shipped == served | modules
    assert all((APP / name).is_file() for name in shipped)


def test_page_loads_scripts_in_order_with_nothing_inline():
    html = (APP / "index.html").read_text(encoding="utf-8")
    assert re.findall(r'<script src="(/[^"]+)" defer></script>', html) == ["/ui.js", "/stages.js", "/app.js"]
    assert len(re.findall(r"<script", html)) == 3
    assert not re.search(r"\son[a-z]+=", html)


def test_browser_only_calls_the_two_read_only_endpoints():
    js = "\n".join((APP / name).read_text(encoding="utf-8") for name in ("ui.js", "stages.js", "app.js"))
    assert set(re.findall(r"getJson\(\s*[`'](/[^?`']*)", js)) == {"/api/state", "/api/metrics"}
    assert "fetch(" in js and js.count("fetch(") == 1
    assert "eval(" not in js and "new Function" not in js


def test_javascript_parses():
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node.js isn't installed.")
    for name in ("ui.js", "stages.js", "app.js"):
        result = subprocess.run([node_bin, "--check", str(APP / name)], capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, f"{name}: {result.stderr}"


# ── Manifest and deploy script ──────────────────────────────────────────────

@pytest.fixture(scope="module")
def docs():
    return [d for d in yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")) if d]


def of_kind(docs, kind):
    return [d for d in docs if d["kind"] == kind]


def test_pod_is_pinned_non_root_read_only_and_loopback(docs):
    spec = of_kind(docs, "Deployment")[0]["spec"]["template"]["spec"]
    container = spec["containers"][0]
    assert "@sha256:" in container["image"] and "distroless" in container["image"]
    assert spec["securityContext"]["runAsNonRoot"] is True and isinstance(spec["securityContext"]["runAsUser"], int)
    sc = container["securityContext"]
    assert sc["readOnlyRootFilesystem"] and not sc["allowPrivilegeEscalation"] and sc["capabilities"]["drop"] == ["ALL"]
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env["BIND_ADDRESS"] == "127.0.0.1"
    assert container["readinessProbe"]["httpGet"]["port"] == "health"
    assert not spec.get("hostNetwork") and all("hostPort" not in p for p in container["ports"])


def test_nothing_exposes_the_dashboard(docs):
    assert not {"Service", "Ingress"} & {d["kind"] for d in docs}
    policy = of_kind(docs, "NetworkPolicy")[0]["spec"]
    assert policy["podSelector"] == {} and policy["policyTypes"] == ["Ingress"] and "ingress" not in policy


def test_rbac_is_read_only_and_never_touches_secrets(docs):
    for role in of_kind(docs, "ClusterRole"):
        for rule in role["rules"]:
            assert set(rule["verbs"]) <= {"get", "list"}, role["metadata"]["name"]
            assert not {"secrets", "configmaps", "pods/exec", "pods/portforward", "*"} & set(rule["resources"])
    namespaced = next(r for r in of_kind(docs, "ClusterRole") if r["metadata"]["name"] == "ray-dashboard-namespace-reader")
    assert {res for rule in namespaced["rules"] for res in rule["resources"]} == {"pods/log", "services", "events"}
    assert "ray-dashboard-namespace-reader" in DEPLOY_SCRIPT.read_text(encoding="utf-8")


def test_azure_token_is_projected_for_entra_only(docs):
    volumes = of_kind(docs, "Deployment")[0]["spec"]["template"]["spec"]["volumes"]
    sources = [s for v in volumes if "projected" in v for s in v["projected"]["sources"]]
    token = sources[0]["serviceAccountToken"]
    assert token["audience"] == "api://AzureADTokenExchange" and token["expirationSeconds"] <= 3600


def test_deploy_script_documents_itself_and_supports_whatif():
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    for needle in (".SYNOPSIS", ".EXAMPLE", "SupportsShouldProcess", "ShouldProcess(", "Common.ps1", "port-forward"):
        assert needle in text, needle
    assert "set-by-deploy-script" in MANIFEST.read_text(encoding="utf-8")


def test_dashboard_carries_no_environment_specific_identifiers():
    banned = ("bbenz", "westus3", "aks-ray-poc", "amw-ray-poc", "law-ray-poc", "advdevrel")
    paths = [*APP.glob("*.*"), MANIFEST, DEPLOY_SCRIPT, DASHBOARD / "README.md"]
    for path in paths:
        if path.suffix == ".pyc" or not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for needle in banned:
            assert needle not in text, f"{path.name} contains {needle}"
        assert not GUID.search(text), f"{path.name} contains a GUID"
        assert not re.search(r"(?<![A-Za-z0-9])[A-Za-z]:\\\\?[A-Za-z]", text), f"{path.name} contains an absolute Windows path"


def test_deploy_script_parses():
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 isn't installed.")
    command = ("$e=$null; $t=$null; "
               f"[System.Management.Automation.Language.Parser]::ParseFile('{DEPLOY_SCRIPT}', [ref]$t, [ref]$e) > $null; "
               "if ($e.Count) { $e[0].Message; exit 1 }")
    result = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_configmaps_carry_the_files_byte_for_byte(tmp_path):
    """The app is full of non-ASCII (· → ✓ —). Capturing `kubectl create configmap -o yaml` would
    decode it with the console code page, and a standard Windows terminal would garble it."""
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert not re.search(r"'create',\s*'configmap'", text), "build ConfigMaps in PowerShell, not from captured output"
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 isn't installed.")
    out = tmp_path / "configmap.json"
    script = f"""
$t = $null; $e = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{DEPLOY_SCRIPT}', [ref]$t, [ref]$e)
$fn = $ast.FindAll({{ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'ConvertTo-ConfigMapJson' }}, $true) | Select-Object -First 1
. ([scriptblock]::Create($fn.Extent.Text))
$utf8 = [Text.UTF8Encoding]::new($false)
$data = [ordered]@{{}}
foreach ($name in 'stages.js', 'app.js', 'index.html') {{ $data[$name] = [IO.File]::ReadAllText((Join-Path '{APP}' $name), $utf8) }}
[IO.File]::WriteAllText('{out}', (ConvertTo-ConfigMapJson -Name ray-dashboard-app -Namespace ray-dashboard -Data $data), $utf8)
"""
    result = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    configmap = json.loads(out.read_bytes().decode("utf-8"))
    assert (configmap["kind"], configmap["metadata"]["namespace"]) == ("ConfigMap", "ray-dashboard")
    for name in ("stages.js", "app.js", "index.html"):
        assert configmap["data"][name] == (APP / name).read_bytes().decode("utf-8"), name
    assert "·" in configmap["data"]["stages.js"] and "→" in configmap["data"]["stages.js"]
