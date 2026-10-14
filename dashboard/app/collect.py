"""Turns raw Kubernetes objects, job logs, and query results into the dashboard's view model.

Every function here is a pure function of its inputs, so the dashboard's logic can be
tested offline with recorded fixtures. Network access lives in clients.py and server.py.
"""

import json
import re
from collections import Counter
from datetime import datetime, timezone

GPU_RESOURCE = "nvidia.com/gpu"
TERMINAL_JOB_STATES = {"SUCCEEDED", "FAILED", "STOPPED"}
TERMINAL_DEPLOYMENT_STATES = {"Complete", "Failed"}
JOB_KINDS = (("gpu-check", "ray-gpu-check"), ("inference", "ray-inference"))
EXPECTED_RECORD_IDS = ("DLV-001", "DLV-002", "DLV-003", "DLV-004")

# Linux pay-as-you-go list prices in USD per hour, October 2026 (East US and West US 3).
# Compute only: disks, networking, and monitoring are extra. Override with PRICES_JSON.
DEFAULT_PRICES = {
    "Standard_D4s_v5": 0.192,
    "Standard_NC4as_T4_v3": 0.526,
    "Standard_NC24ads_A100_v4": 3.673,
    "Standard_ND96amsr_A100_v4": 32.77,
}

# Outputs a person reviewed during the September 29, 2026 POC. The note applies only when
# the summary matches exactly, so a different generation is never mislabeled.
REVIEWED_OUTPUTS = {
    "DLV-002": {
        "summary": "Driver's access to building has been denied, but will be available tomorrow.",
        "note": "Reviewed in the POC: the input only says the driver will retry tomorrow. "
                "Nothing says access was denied or that it will be available.",
    },
}

PROBE_CHECKS = {
    "dns": ("DNS resolves the head service", "resolved"),
    "submitter_network_allowed_auth_required": ("Labeled submitter reaches the Jobs API", "401: token required"),
    "unapproved_pod_to_head": ("Unlabeled pod reaches the head pod IP", "blocked"),
    "external_positive_control": ("Control: the same pod reaches the internet", "connected"),
}
PROBE_TITLES = {
    "probe-allowed-submitter": "Submitter with the ray-submit label",
    "probe-denied-same-ns": "Unlabeled pod, same namespace",
    "probe-denied-cross-ns": "Pod in another namespace",
}

STOPWORDS = set(
    "a an the to of for by at in on and or but is are was were be been being has have had will would "
    "can could should may might must not no it its this that these those with as from into than then "
    "there their they he she we you i his her our your up out over under about after before due".split()
)
WORD_RE = re.compile(r"[A-Za-z0-9:']+")


# ── Small helpers ────────────────────────────────────────────────────────────

def dig(obj, *path, default=None):
    """Follows dict keys and list indexes, returning default when anything is missing."""
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and isinstance(key, int) and -len(cur) <= key < len(cur):
            cur = cur[key]
        else:
            return default
        if cur is None:
            return default
    return cur


def parse_time(value):
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def to_int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def labels_of(obj):
    return dig(obj, "metadata", "labels", default={}) or {}


def short_node(name):
    """aks-raygpu-43828327-vmss000000 -> raygpu-0 (pool plus the scale set's instance suffix)."""
    m = re.match(r"^aks-([a-z0-9]+)-\d+-vmss([0-9a-z]+)$", name or "")
    if m:
        return f"{m.group(1)}-{m.group(2).lstrip('0') or '0'}"
    return name


def pool_of_node_name(name):
    m = re.match(r"^aks-([a-z0-9]+)-", name or "")
    return m.group(1) if m else None


def pod_ready(pod):
    statuses = dig(pod, "status", "containerStatuses", default=[]) or []
    return bool(statuses) and all(s.get("ready") for s in statuses)


def pod_view(pod):
    statuses = dig(pod, "status", "containerStatuses", default=[]) or []
    waiting = next((dig(s, "state", "waiting", "reason") for s in statuses if dig(s, "state", "waiting")), None)
    return {
        "name": dig(pod, "metadata", "name"),
        "namespace": dig(pod, "metadata", "namespace"),
        "node": dig(pod, "spec", "nodeName"),
        "phase": dig(pod, "status", "phase", default="Unknown"),
        "ready": pod_ready(pod),
        "restarts": sum(to_int(s.get("restartCount")) for s in statuses),
        "waitingReason": waiting,
        "created": dig(pod, "metadata", "creationTimestamp"),
    }


def pod_gpu_request(pod):
    total = 0
    for c in dig(pod, "spec", "containers", default=[]) or []:
        res = c.get("resources") or {}
        total += max(to_int(dig(res, "limits", GPU_RESOURCE)), to_int(dig(res, "requests", GPU_RESOURCE)))
    return total


def os_family(os_image, os_sku=None):
    text = f"{os_image or ''} {os_sku or ''}".lower()
    if "azure linux" in text or "azurelinux" in text or "cbl-mariner" in text or "mariner" in text:
        return "Azure Linux"
    if "ubuntu" in text:
        return "Ubuntu"
    if "windows" in text:
        return "Windows"
    return os_image or "Unknown"


# ── Nodes, pools, and the cluster ────────────────────────────────────────────

def node_views(nodes, pods):
    active = [p for p in pods if dig(p, "status", "phase") in ("Pending", "Running") and dig(p, "spec", "nodeName")]
    by_node = {}
    for p in active:
        by_node.setdefault(p["spec"]["nodeName"], []).append(p)
    views = []
    for node in nodes:
        labels = labels_of(node)
        name = dig(node, "metadata", "name")
        conds = dig(node, "status", "conditions", default=[]) or []
        ready_cond = next((c for c in conds if c.get("type") == "Ready"), {})
        holders = [
            {"namespace": dig(p, "metadata", "namespace"), "pod": dig(p, "metadata", "name"),
             "gpus": pod_gpu_request(p), "phase": dig(p, "status", "phase")}
            for p in by_node.get(name, []) if pod_gpu_request(p) > 0
        ]
        gpu_conditions = [
            {"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason")}
            for c in conds
            if any(h in (c.get("type") or "").lower() for h in ("nvidia", "gpu", "xid", "dcgm"))
        ]
        info = dig(node, "status", "nodeInfo", default={}) or {}
        views.append({
            "name": name,
            "short": short_node(name),
            "pool": labels.get("kubernetes.azure.com/agentpool") or labels.get("agentpool") or pool_of_node_name(name) or "?",
            "vmSize": labels.get("node.kubernetes.io/instance-type") or labels.get("beta.kubernetes.io/instance-type"),
            "zone": labels.get("topology.kubernetes.io/zone"),
            "region": labels.get("topology.kubernetes.io/region"),
            "ready": ready_cond.get("status") == "True",
            "readySince": ready_cond.get("lastTransitionTime"),
            "created": dig(node, "metadata", "creationTimestamp"),
            "osImage": info.get("osImage"),
            "osSku": labels.get("kubernetes.azure.com/os-sku-effective") or labels.get("kubernetes.azure.com/os-sku"),
            "osFamily": os_family(info.get("osImage"), labels.get("kubernetes.azure.com/os-sku")),
            "kernel": info.get("kernelVersion"),
            "containerRuntime": info.get("containerRuntimeVersion"),
            "kubelet": info.get("kubeletVersion"),
            "nodeImage": labels.get("kubernetes.azure.com/node-image-version"),
            "internalIP": next((a.get("address") for a in dig(node, "status", "addresses", default=[]) or []
                                if a.get("type") == "InternalIP"), None),
            "gpuCapacity": to_int(dig(node, "status", "capacity", GPU_RESOURCE)),
            "gpuAllocatable": to_int(dig(node, "status", "allocatable", GPU_RESOURCE)),
            "gpuAllocated": sum(h["gpus"] for h in holders),
            "gpuHolders": holders,
            "gpuConditions": gpu_conditions,
            "accelerator": labels.get("kubernetes.azure.com/accelerator") or labels.get("accelerator"),
            "managedGpu": labels.get("kubernetes.azure.com/dcgm-exporter") == "enabled"
                          or any(c["type"] == "UnhealthyNvidiaDevicePlugin" for c in gpu_conditions),
            "dcgmExporter": labels.get("kubernetes.azure.com/dcgm-exporter") == "enabled",
            "taints": [f"{t.get('key')}={t.get('value', '')}:{t.get('effect')}"
                       for t in dig(node, "spec", "taints", default=[]) or []],
            "unschedulable": bool(dig(node, "spec", "unschedulable")),
        })
    views.sort(key=lambda n: (n["pool"], n["name"]))
    return views


def is_gpu_node(node_view):
    return node_view["gpuCapacity"] > 0 or bool(node_view["accelerator"])


def pool_views(nodes, prices):
    pools = {}
    for n in nodes:
        p = pools.setdefault(n["pool"], {
            "name": n["pool"], "vmSize": n["vmSize"], "nodes": 0, "ready": 0, "osImages": set(),
            "osFamilies": set(), "nodeImages": set(), "gpuAllocatable": 0, "gpuAllocated": 0, "gpuNodes": 0,
            "managedGpu": False, "dcgm": False,
        })
        p["nodes"] += 1
        p["ready"] += 1 if n["ready"] else 0
        p["osImages"].add(n["osImage"] or "?")
        p["osFamilies"].add(n["osFamily"])
        if n["nodeImage"]:
            p["nodeImages"].add(n["nodeImage"])
        p["gpuAllocatable"] += n["gpuAllocatable"]
        p["gpuAllocated"] += n["gpuAllocated"]
        p["gpuNodes"] += 1 if is_gpu_node(n) else 0
        p["managedGpu"] = p["managedGpu"] or n["managedGpu"]
        p["dcgm"] = p["dcgm"] or n["dcgmExporter"]
    out = []
    for p in pools.values():
        price = prices.get(p["vmSize"])
        out.append({
            **p,
            "osImages": sorted(p["osImages"]),
            "osFamilies": sorted(p["osFamilies"]),
            "nodeImages": sorted(p["nodeImages"]),
            "gpu": p["gpuNodes"] > 0,
            "pricePerHour": price,
            "costPerHour": round(price * p["nodes"], 3) if price is not None else None,
        })
    out.sort(key=lambda p: (p["gpu"], p["name"]))
    return out


def cluster_view(nodes, pods, settings):
    kubelets = Counter(n["kubelet"] for n in nodes if n["kubelet"])
    regions = Counter(n["region"] for n in nodes if n["region"])
    cilium = any((dig(p, "metadata", "name") or "").startswith("cilium-")
                 and dig(p, "metadata", "namespace") == "kube-system" for p in pods)
    return {
        "name": settings.get("clusterName") or None,
        "kubernetesVersion": kubelets.most_common(1)[0][0] if kubelets else None,
        "region": regions.most_common(1)[0][0] if regions else None,
        "nodes": len(nodes),
        "readyNodes": sum(1 for n in nodes if n["ready"]),
        "dataplane": "Cilium" if cilium else None,
        "osFamilies": sorted({n["osFamily"] for n in nodes}),
    }


# ── KubeRay and Ray clusters ─────────────────────────────────────────────────

def image_tag(image):
    if not image:
        return None
    name = image.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    return last.split(":", 1)[1] if ":" in last else None


def kuberay_view(deployments, pods, discovery, ray_clusters, ray_jobs):
    dep = next((d for d in deployments
                if labels_of(d).get("app.kubernetes.io/name") == "kuberay-operator"
                or dig(d, "metadata", "name") == "kuberay-operator"), None)
    kinds = sorted({r.get("kind") for r in (discovery or {}).get("resources", [])
                    if r.get("kind") and "/" not in (r.get("name") or "")})
    view = {
        "found": dep is not None,
        "kinds": kinds,
        "rayClusters": len(ray_clusters),
        "rayJobs": len(ray_jobs),
    }
    if dep is None:
        return view
    ns = dig(dep, "metadata", "namespace")
    image = dig(dep, "spec", "template", "spec", "containers", 0, "image")
    op_pods = [p for p in pods if dig(p, "metadata", "namespace") == ns
               and (labels_of(p).get("app.kubernetes.io/name") == "kuberay-operator"
                    or (dig(p, "metadata", "name") or "").startswith("kuberay-operator"))]
    view.update({
        "namespace": ns,
        "name": dig(dep, "metadata", "name"),
        "image": image,
        "version": image_tag(image),
        "chart": labels_of(dep).get("helm.sh/chart"),
        "desired": to_int(dig(dep, "spec", "replicas"), 1),
        "ready": to_int(dig(dep, "status", "readyReplicas")),
        "restarts": sum(pod_view(p)["restarts"] for p in op_pods),
        "node": dig(op_pods, 0, "spec", "nodeName"),
        "created": dig(dep, "metadata", "creationTimestamp"),
    })
    return view


PINNED_PORT_KEYS = ("port", "node-manager-port", "object-manager-port", "runtime-env-agent-port",
                    "dashboard-agent-grpc-port", "dashboard-agent-listen-port", "min-worker-port", "max-worker-port")


def ray_cluster_view(rc, pods, services):
    ns = dig(rc, "metadata", "namespace")
    name = dig(rc, "metadata", "name")
    spec = rc.get("spec") or {}
    status = rc.get("status") or {}
    mine = [p for p in pods if dig(p, "metadata", "namespace") == ns and labels_of(p).get("ray.io/cluster") == name]
    head_pods = [p for p in mine if labels_of(p).get("ray.io/node-type") == "head"]
    worker_pods = [p for p in mine if labels_of(p).get("ray.io/node-type") == "worker"]
    head_params = dig(spec, "headGroupSpec", "rayStartParams", default={}) or {}
    head_container = dig(spec, "headGroupSpec", "template", "spec", "containers", 0, default={}) or {}
    env_names = {e.get("name") for e in head_container.get("env") or []}
    groups = []
    for g in spec.get("workerGroupSpecs") or []:
        c = dig(g, "template", "spec", "containers", 0, default={}) or {}
        limits = dig(c, "resources", "limits", default={}) or {}
        selector = dig(g, "template", "spec", "nodeSelector", default={}) or {}
        groups.append({
            "name": g.get("groupName"),
            "replicas": g.get("replicas"),
            "minReplicas": g.get("minReplicas"),
            "maxReplicas": g.get("maxReplicas"),
            "rayStartParams": {k: v for k, v in (g.get("rayStartParams") or {}).items()
                               if k in ("num-cpus", "num-gpus", "object-store-memory")},
            "gpuLimit": to_int(limits.get(GPU_RESOURCE)),
            "cpuLimit": limits.get("cpu"),
            "memoryLimit": limits.get("memory"),
            "image": c.get("image"),
            "pool": selector.get("kubernetes.azure.com/agentpool") or selector.get("agentpool"),
            "tolerations": [f"{t.get('key')}={t.get('value', '')}:{t.get('effect', '')}"
                            for t in dig(g, "template", "spec", "tolerations", default=[]) or []],
        })
    head_svc = next((s for s in services if dig(s, "metadata", "namespace") == ns
                     and dig(s, "metadata", "name") == f"{name}-head-svc"), None)
    autoscaler = spec.get("autoscalerOptions") or {}
    return {
        "namespace": ns,
        "name": name,
        "state": status.get("state") or "unknown",
        "rayVersion": spec.get("rayVersion"),
        "authMode": dig(spec, "authOptions", "mode") or "none",
        "autoscaling": bool(spec.get("enableInTreeAutoscaling")),
        "autoscaler": {
            "version": autoscaler.get("version"),
            "upscalingMode": autoscaler.get("upscalingMode"),
            "idleTimeoutSeconds": autoscaler.get("idleTimeoutSeconds"),
        } if spec.get("enableInTreeAutoscaling") else None,
        "headParams": {k: head_params[k] for k in ("num-cpus", "num-gpus", "object-store-memory") if k in head_params},
        "headImage": head_container.get("image"),
        "workerGroups": groups,
        "head": [pod_view(p) for p in head_pods],
        "workers": sorted((pod_view(p) for p in worker_pods), key=lambda w: w["created"] or ""),
        "desiredWorkers": status.get("desiredWorkerReplicas"),
        "availableWorkers": status.get("availableWorkerReplicas"),
        "readyWorkers": sum(1 for p in worker_pods if pod_ready(p)),
        "pinnedPorts": all(k in head_params for k in ("node-manager-port", "min-worker-port", "max-worker-port")),
        "pinnedPortValues": {k: head_params[k] for k in PINNED_PORT_KEYS if k in head_params},
        "headServiceType": dig(head_svc, "spec", "type") if head_svc else None,
        "proxyEnv": "HTTPS_PROXY" in env_names,
        "created": dig(rc, "metadata", "creationTimestamp"),
    }


# ── RayJobs and their results ────────────────────────────────────────────────

def job_kind(name):
    for kind, prefix in JOB_KINDS:
        if name == prefix:
            return kind, ""
        if name.startswith(prefix + "-"):
            return kind, name[len(prefix) + 1:]
    return "other", ""


def submitter_pod_re(job_name):
    """Kubernetes names a Job's pods <job>-<5 random characters>."""
    return re.compile("^" + re.escape(job_name) + "-[a-z0-9]{5}$")


def _json_tail(line):
    start = line.find("{")
    if start < 0:
        return None
    try:
        value = json.loads(line[start:])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def parse_gpu_result(lines):
    """Returns the GPU check's JSON record from submitter log lines, or None."""
    found = None
    for line in lines:
        if "tensor_sum" not in line:
            continue
        value = _json_tail(line)
        if value and "tensor_sum" in value:
            found = value
    return found


def verify_gpu_result(result):
    if not result:
        return {"ok": False, "checks": []}
    try:
        tensor_ok = abs(float(result.get("tensor_sum")) - 10.0) < 1e-6
    except (TypeError, ValueError):
        tensor_ok = False
    checks = [
        {"label": "A CUDA GPU answered", "ok": bool(result.get("gpu"))},
        {"label": "tensor_sum is 10.0", "ok": tensor_ok},
        {"label": "CUDA runtime reported", "ok": bool(result.get("cuda_runtime"))},
    ]
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def parse_inference_records(lines):
    """Returns the batch inference records (one per delivery event), last one wins per ID."""
    records = {}
    for line in lines:
        if '"DLV-' not in line or "summary" not in line:
            continue
        value = _json_tail(line)
        if value and value.get("id") and "summary" in value:
            records[value["id"]] = value
    return [records[k] for k in sorted(records)]


def _stem(word):
    w = word.lower()
    w = re.sub(r"'s$", "", w)
    for suffix in ("ing", "ed", "es", "s"):
        if len(w) > 4 and w.endswith(suffix):
            return w[: -len(suffix)]
    return w


def novel_words(source, generated):
    """Content words in the generated text that don't appear in the source: a reading aid."""
    seen = {_stem(w) for w in WORD_RE.findall(source or "")}
    out = []
    for w in WORD_RE.findall(generated or ""):
        stem = _stem(w)
        if stem in STOPWORDS or w.lower() in STOPWORDS or stem in seen or len(stem) < 3:
            continue
        if w not in out:
            out.append(w)
    return out


def annotate_records(records):
    out = []
    for r in records:
        reviewed = REVIEWED_OUTPUTS.get(r.get("id"))
        out.append({
            "id": r.get("id"),
            "event": r.get("event"),
            "summary": r.get("summary"),
            "gpu": r.get("gpu"),
            "model": r.get("model"),
            "modelRevision": r.get("model_revision"),
            "novelWords": novel_words(r.get("event"), r.get("summary")),
            "reviewNote": reviewed["note"] if reviewed and reviewed["summary"] == r.get("summary") else None,
        })
    return out


def verify_inference(records):
    ids = [r.get("id") for r in records]
    revisions = {r.get("model_revision") or r.get("modelRevision") for r in records}
    checks = [
        {"label": "Four records with the expected IDs", "ok": sorted(ids) == list(EXPECTED_RECORD_IDS)},
        {"label": "One pinned model revision", "ok": len(revisions) == 1 and None not in revisions},
        {"label": "Every record names a GPU", "ok": bool(records) and all(r.get("gpu") for r in records)},
    ]
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def result_from_lines(kind, lines):
    """Parses a job's output. Returns None until the expected output has appeared."""
    if kind == "gpu-check":
        r = parse_gpu_result(lines)
        return {"gpu": r, "verified": verify_gpu_result(r)} if r else None
    if kind == "inference":
        recs = parse_inference_records(lines)
        return {"records": annotate_records(recs), "verified": verify_inference(recs)} if recs else None
    return None


def job_view(rj, entry, timeline, now):
    meta = rj.get("metadata") or {}
    spec = rj.get("spec") or {}
    status = rj.get("status") or {}
    name = meta.get("name")
    kind, suffix = job_kind(name)
    job_status = status.get("jobStatus") or ""
    deployment = status.get("jobDeploymentStatus") or ""
    start = parse_time(status.get("startTime"))
    end = parse_time(status.get("endTime"))
    terminal = job_status in TERMINAL_JOB_STATES or deployment in TERMINAL_DEPLOYMENT_STATES
    if start and end:
        duration = (end - start).total_seconds()
    elif start and not terminal:
        duration = (now - start).total_seconds()
    else:
        duration = None
    view = {
        "namespace": meta.get("namespace"),
        "name": name,
        "uid": meta.get("uid"),
        "kind": kind,
        "suffix": suffix,
        "created": meta.get("creationTimestamp"),
        "jobStatus": job_status,
        "deploymentStatus": deployment,
        "startTime": iso(start),
        "endTime": iso(end),
        "durationSeconds": round(duration, 1) if duration is not None else None,
        "terminal": terminal,
        "running": not terminal,
        "cluster": dig(spec, "clusterSelector", "ray.io/cluster") or status.get("rayClusterName"),
        "submissionMode": spec.get("submissionMode"),
        "entrypoint": spec.get("entrypoint"),
        "submitterLabel": dig(spec, "submitterPodTemplate", "metadata", "labels", "ray-submit"),
        "message": status.get("message"),
        "timeline": timeline or [],
        "result": None,
        "resultSource": None,
        "resultAt": None,
        "resultNote": None,
    }
    if entry:
        view["result"] = entry.get("result")
        view["resultSource"] = entry.get("source")
        view["resultAt"] = entry.get("at")
        view["resultNote"] = entry.get("note")
    return view


def job_views(ray_jobs, results, timelines, now):
    views = [job_view(rj, results.get(dig(rj, "metadata", "uid")), timelines.get(dig(rj, "metadata", "uid")), now)
             for rj in ray_jobs]
    views.sort(key=lambda j: j["created"] or "", reverse=True)
    return views


# ── Security, monitoring, autoscaling, and cost ─────────────────────────────

def default_deny_types(policy):
    """Returns the policy types a select-everything, no-rules policy denies, or an empty list."""
    spec = policy.get("spec") or {}
    selector = spec.get("podSelector") or {}
    if selector.get("matchLabels") or selector.get("matchExpressions"):
        return []
    types = spec.get("policyTypes") or []
    denied = []
    if "Ingress" in types and not spec.get("ingress"):
        denied.append("Ingress")
    if "Egress" in types and not spec.get("egress"):
        denied.append("Egress")
    return denied


def probe_view(pod_name, namespace, time, lines):
    name = re.sub(r"-[a-z0-9]{5}$", "", pod_name or "")
    checks = []
    for line in lines:
        value = _json_tail(line)
        if not value or "check" not in value:
            continue
        label, expected = PROBE_CHECKS.get(value["check"], (value["check"], None))
        status = value.get("status")
        if value["check"] == "submitter_network_allowed_auth_required":
            ok = str(status) == "401"
        elif expected:
            ok = str(status) == expected
        else:
            ok = None
        checks.append({"check": value["check"], "label": label, "status": status, "expected": expected, "ok": ok})
    return {"name": name, "title": PROBE_TITLES.get(name, name), "namespace": namespace, "time": time, "checks": checks}


def security_view(primary_ns, primary_cluster, policies, deployments, jobs, probes, egress_ns):
    mine = [p for p in policies if dig(p, "metadata", "namespace") == primary_ns]
    deny = sorted({t for p in mine for t in default_deny_types(p)})
    proxy = next((d for d in deployments if dig(d, "metadata", "namespace") == egress_ns), None)
    recent = [j for j in jobs if j["namespace"] == primary_ns][:4]
    labeled = [j for j in recent if j["submitterLabel"]]
    checks = [
        {"id": "default-deny", "label": "Default deny in " + primary_ns,
         "ok": deny == ["Egress", "Ingress"], "detail": ", ".join(deny) if deny else "none"},
        {"id": "policies", "label": "Network policies", "ok": len(mine) > 0, "detail": str(len(mine))},
        {"id": "pinned-ports", "label": "Ray ports pinned for the policies",
         "ok": bool(primary_cluster and primary_cluster["pinnedPorts"]), "detail": None},
        {"id": "token-auth", "label": "Ray token authentication",
         "ok": bool(primary_cluster and primary_cluster["authMode"] == "token"),
         "detail": primary_cluster["authMode"] if primary_cluster else None},
        {"id": "head-service", "label": "Head service stays inside the cluster",
         "ok": bool(primary_cluster and primary_cluster["headServiceType"] in ("ClusterIP", None)),
         "detail": primary_cluster["headServiceType"] if primary_cluster else None},
        {"id": "egress-proxy", "label": "Downloads through the allowlist proxy",
         "ok": bool(proxy and to_int(dig(proxy, "status", "readyReplicas")) > 0
                    and primary_cluster and primary_cluster["proxyEnv"]),
         "detail": dig(proxy, "metadata", "name") if proxy else None},
        {"id": "submitter-label", "label": "Recent RayJobs carry the submitter label",
         "ok": bool(recent) and len(labeled) == len(recent),
         "detail": f"{len(labeled)} of {len(recent)}" if recent else "no jobs"},
    ]
    return {
        "namespace": primary_ns,
        "policies": [{"name": dig(p, "metadata", "name"),
                      "selector": dig(p, "spec", "podSelector", "matchLabels", default={}) or {},
                      "types": dig(p, "spec", "policyTypes", default=[]) or [],
                      "defaultDeny": bool(default_deny_types(p))} for p in sorted(mine, key=lambda p: dig(p, "metadata", "name") or "")],
        "checks": checks,
        "probes": probes,
        "tokenSecretNote": f"The token lives in Secret {primary_cluster['name']}. This dashboard can't read Secrets."
        if primary_cluster and primary_cluster["authMode"] == "token" else None,
    }


def monitoring_view(podmonitors, pods, nodes, ray_namespaces):
    def running(prefix):
        return sum(1 for p in pods if dig(p, "metadata", "namespace") == "kube-system"
                   and (dig(p, "metadata", "name") or "").startswith(prefix) and dig(p, "status", "phase") == "Running")
    return {
        "podMonitors": [{"namespace": dig(m, "metadata", "namespace"), "name": dig(m, "metadata", "name")}
                        for m in podmonitors],
        "podMonitorNamespaces": sorted({dig(m, "metadata", "namespace") for m in podmonitors}),
        "missingPodMonitors": [ns for ns in ray_namespaces
                               if ns not in {dig(m, "metadata", "namespace") for m in podmonitors}],
        "amaMetricsPods": running("ama-metrics"),
        "amaLogsPods": running("ama-logs"),
        "dcgmNodes": [n["name"] for n in nodes if n["dcgmExporter"]],
    }


def autoscaling_view(scale_cluster, nodes, events):
    if not scale_cluster:
        return {"found": False}
    group = (scale_cluster["workerGroups"] or [{}])[0]
    pool = group.get("pool")
    pool_nodes = [n for n in nodes if pool and n["pool"] == pool]
    pending = [w for w in scale_cluster["workers"] if w["phase"] == "Pending"]
    return {
        "found": True,
        "cluster": scale_cluster,
        "pool": pool,
        "minWorkers": group.get("minReplicas"),
        "maxWorkers": group.get("maxReplicas"),
        "desiredWorkers": scale_cluster["desiredWorkers"],
        "readyWorkers": scale_cluster["readyWorkers"],
        "pendingWorkers": len(pending),
        "poolNodes": len(pool_nodes),
        "poolReadyNodes": sum(1 for n in pool_nodes if n["ready"]),
        "poolAllocatableGpus": sum(n["gpuAllocatable"] for n in pool_nodes),
        "nodes": [{"name": n["name"], "short": n["short"], "ready": n["ready"], "gpuAllocatable": n["gpuAllocatable"],
                   "gpuAllocated": n["gpuAllocated"], "created": n["created"]} for n in pool_nodes],
        "events": events,
    }


def event_views(events, limit=12):
    out = []
    for e in events:
        when = e.get("lastTimestamp") or e.get("eventTime") or dig(e, "metadata", "creationTimestamp")
        out.append({
            "time": when,
            "type": e.get("type"),
            "reason": e.get("reason"),
            "message": (e.get("message") or "")[:240],
            "object": f"{dig(e, 'involvedObject', 'kind')}/{dig(e, 'involvedObject', 'name')}",
            "count": e.get("count") or dig(e, "series", "count") or 1,
        })
    out.sort(key=lambda e: e["time"] or "", reverse=True)
    return out[:limit]


def cost_view(pools):
    known = [p for p in pools if p["costPerHour"] is not None]
    per_hour = round(sum(p["costPerHour"] for p in known), 3)
    gpu_hour = round(sum(p["costPerHour"] for p in known if p["gpu"]), 3)
    return {
        "perHour": per_hour,
        "perDay": round(per_hour * 24, 2),
        "perMonth": round(per_hour * 730, 0),
        "gpuPerHour": gpu_hour,
        "unpricedPools": [p["name"] for p in pools if p["costPerHour"] is None],
        "note": "List price, Linux pay as you go, compute only (October 2026). Disks, networking, and monitoring are extra.",
    }


# ── Stages ──────────────────────────────────────────────────────────────────

def _latest(jobs, kind):
    return next((j for j in jobs if j["kind"] == kind), None)


def _joining(node, now, minutes=15):
    """A node that registered recently and isn't ready yet is most likely still starting."""
    created = parse_time(node.get("created"))
    return bool(created and now and (now - created).total_seconds() < minutes * 60)


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def _fmt_seconds(s):
    if s is None:
        return ""
    s = int(round(s))
    return f"{s // 60} min {s % 60} s" if s >= 120 else f"{s} s"


def _job_stage(job, kind):
    if job is None:
        return "todo", "not run yet"
    if job["running"]:
        return "live", f"{job['jobStatus'] or 'starting'} · {_fmt_seconds(job['durationSeconds'])}".strip(" ·")
    if job["jobStatus"] != "SUCCEEDED":
        return "warn", job["jobStatus"] or job["deploymentStatus"] or "unknown"
    result = job.get("result")
    if not result:
        return "done", f"SUCCEEDED · {_fmt_seconds(job['durationSeconds'])}"
    verified = (result.get("verified") or {}).get("ok")
    if kind == "inference":
        n = len(result.get("records") or [])
        return ("done" if verified else "warn"), f"{n} records · {_fmt_seconds(job['durationSeconds'])}"
    gpu = (result.get("gpu") or {}).get("gpu") or "GPU"
    return ("done" if verified else "warn"), f"{gpu} · {_fmt_seconds(job['durationSeconds'])}"


def build_stages(state, now=None):
    nodes = state["nodes"]
    gpu_nodes = [n for n in nodes if is_gpu_node(n)]
    stages = []

    ready = state["cluster"]["readyNodes"]
    total = state["cluster"]["nodes"]
    pools = len(state["pools"])
    not_ready = [n for n in nodes if not n["ready"]]
    if not total:
        cluster = ("todo", "no nodes")
    elif not_ready:
        cluster = ("live" if all(_joining(n, now) for n in not_ready) else "warn", f"{ready}/{total} nodes · {pools} pools")
    else:
        cluster = ("done", f"{ready}/{total} nodes · {pools} pools")
    stages.append({"id": "cluster", "status": cluster[0], "caption": cluster[1]})

    # A GPU node is done only when it's Ready and the device plugin has registered its GPU.
    alloc = sum(n["gpuAllocatable"] for n in gpu_nodes if n["ready"])
    gpu_not_ready = [n for n in gpu_nodes if not n["ready"]]
    waiting = [n for n in gpu_nodes if n["ready"] and n["gpuAllocatable"] == 0]
    if not gpu_nodes:
        gpu = ("todo", "no GPU nodes")
    elif gpu_not_ready:
        gpu = ("live" if all(_joining(n, now) for n in gpu_not_ready) else "warn",
               f"{_plural(len(gpu_not_ready), 'GPU node')} not Ready")
    elif waiting:
        gpu = ("live" if all(_joining(n, now) for n in waiting) else "warn",
               f"{len(waiting)} Ready, GPU not yet allocatable")
    else:
        gpu = ("done", f"{_plural(alloc, 'GPU')} allocatable")
    stages.append({"id": "gpu", "status": gpu[0], "caption": gpu[1]})

    kr = state["kuberay"]
    if not kr["found"]:
        stages.append({"id": "kuberay", "status": "todo", "caption": "not installed"})
    else:
        stages.append({"id": "kuberay", "status": "done" if kr["ready"] >= 1 else "warn",
                       "caption": f"{kr.get('version') or 'operator'} · {kr['ready']}/{kr['desired']} ready"})

    primary = state.get("primaryCluster")
    if not primary:
        stages.append({"id": "raycluster", "status": "todo", "caption": "not created"})
    else:
        st = "done" if primary["state"] == "ready" else "live"
        stages.append({"id": "raycluster", "status": st,
                       "caption": f"{primary['name']} {primary['state']} · {primary['readyWorkers']} worker"
                                  f"{'s' if primary['readyWorkers'] != 1 else ''}"})

    for kind, sid in (("gpu-check", "gpucheck"), ("inference", "inference")):
        st, cap = _job_stage(_latest(state["rayJobs"], kind), kind)
        stages.append({"id": sid, "status": st, "caption": cap})

    sec = state["security"]
    checks = {c["id"]: c["ok"] for c in sec["checks"]}
    n_pol = len(sec["policies"])
    if n_pol == 0:
        sec_stage = ("todo", "no policies")
    else:
        sec_stage = ("done" if checks.get("default-deny") and checks.get("token-auth") else "warn",
                     f"{n_pol} policies{' · token' if checks.get('token-auth') else ''}")
    stages.append({"id": "security", "status": sec_stage[0], "caption": sec_stage[1]})

    mon = state["monitoring"]
    if not mon["podMonitors"]:
        stages.append({"id": "monitoring", "status": "todo", "caption": "no PodMonitor"})
    else:
        caption = f"{len(mon['podMonitors'])} PodMonitors"
        if mon["dcgmNodes"]:
            caption += " · DCGM"
        stages.append({"id": "monitoring", "status": "done" if not mon["missingPodMonitors"] else "warn",
                       "caption": caption})

    auto = state["autoscaling"]
    if not auto["found"]:
        stages.append({"id": "autoscaling", "status": "todo", "caption": "not deployed"})
    else:
        scaling = (auto["pendingWorkers"] > 0 or (auto["desiredWorkers"] or 0) > auto["readyWorkers"])
        stages.append({"id": "autoscaling", "status": "live" if scaling else "done",
                       "caption": f"{auto['readyWorkers']} of {auto['maxWorkers']} workers · "
                                  f"{auto['poolReadyNodes']} node{'s' if auto['poolReadyNodes'] != 1 else ''}"})

    cost = state["cost"]
    stages.append({"id": "cost", "status": "info", "caption": f"${cost['perHour']:.2f}/h now"})
    return stages


# ── The whole snapshot ──────────────────────────────────────────────────────

def build_state(raw, results, timelines, probes, settings, now):
    """Builds the dashboard snapshot from raw API data. `raw` keys are lists of Kubernetes objects."""
    prices = {**DEFAULT_PRICES, **(settings.get("prices") or {})}
    pods = raw.get("pods", [])
    nodes = node_views(raw.get("nodes", []), pods)
    pools = pool_views(nodes, prices)
    clusters = [ray_cluster_view(rc, pods, raw.get("services", [])) for rc in raw.get("rayClusters", [])]
    jobs = job_views(raw.get("rayJobs", []), results, timelines, now)
    primary = next((c for c in clusters if c["namespace"] == settings["primaryNamespace"]
                    and c["name"] == settings["primaryCluster"]), None)
    scale = next((c for c in clusters if c["namespace"] == settings["scaleNamespace"]
                  and c["name"] == settings["scaleCluster"]), None)
    state = {
        "generatedAt": iso(now),
        "cluster": cluster_view(nodes, pods, settings),
        "nodes": nodes,
        "pools": pools,
        "kuberay": kuberay_view(raw.get("deployments", []), pods, raw.get("rayDiscovery"),
                                raw.get("rayClusters", []), raw.get("rayJobs", [])),
        "rayClusters": clusters,
        "primaryCluster": primary,
        "rayJobs": jobs,
        "security": security_view(settings["primaryNamespace"], primary, raw.get("networkPolicies", []),
                                  raw.get("deployments", []), jobs, probes, settings["egressNamespace"]),
        "monitoring": monitoring_view(raw.get("podMonitors", []), pods, nodes,
                                      [settings["primaryNamespace"], settings["scaleNamespace"]]),
        "autoscaling": autoscaling_view(scale, nodes, event_views(raw.get("scaleEvents", []))),
        "cost": cost_view(pools),
    }
    state["stages"] = build_stages(state, now)
    return state
