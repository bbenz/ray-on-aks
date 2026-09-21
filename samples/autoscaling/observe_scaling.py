import json
import os
import ssl
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


BASE = f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ['KUBERNETES_SERVICE_PORT_HTTPS']}"
ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
CONTEXT = ssl.create_default_context(cafile=str(ACCOUNT / "ca.crt"))
NAMESPACE = "ray-scale"


def emit(event, **fields):
    print(
        json.dumps(
            {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "event": event, **fields},
            sort_keys=True,
        ),
        flush=True,
    )


def read_api(path, selector=None):
    url = BASE + path
    if selector:
        url += "?" + urllib.parse.urlencode({"labelSelector": selector})
    token = (ACCOUNT / "token").read_text(encoding="utf-8").strip()
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, context=CONTEXT, timeout=20) as response:
        return json.load(response)


def ready(resource):
    return not resource["metadata"].get("deletionTimestamp") and any(
        item["type"] == "Ready" and item["status"] == "True"
        for item in resource.get("status", {}).get("conditions", [])
    )


def main():
    cluster = read_api(f"/apis/ray.io/v1/namespaces/{NAMESPACE}/rayclusters/ray-scale")
    cluster_uid = cluster["metadata"]["uid"]
    deadline = time.monotonic() + 7200
    peak_nodes = 0
    peak_workers = 0
    initial_one_seen = False
    stable_scale_in = 0
    deletion_marker_seen = False
    previous = None
    last_emit = 0
    known_events = {}
    while time.monotonic() < deadline:
        nodes = read_api("/api/v1/nodes", "agentpool=scalet4")["items"]
        pods = read_api(
            f"/api/v1/namespaces/{NAMESPACE}/pods",
            "ray.io/cluster=ray-scale,ray.io/node-type=worker",
        )["items"]
        pods = [
            pod for pod in pods
            if any(owner["uid"] == cluster_uid for owner in pod["metadata"].get("ownerReferences", []))
        ]
        ready_nodes = sum(
            ready(node) and node.get("status", {}).get("allocatable", {}).get("nvidia.com/gpu") == "1"
            for node in nodes
        )
        ready_workers = sum(ready(pod) for pod in pods)
        peak_nodes = max(peak_nodes, ready_nodes)
        peak_workers = max(peak_workers, ready_workers)
        if not initial_one_seen and peak_nodes == 1 and ready_nodes == 1 and ready_workers == 1:
            initial_one_seen = True
            emit("initial_one_worker_observed")
        for node in nodes:
            if any("autoscaler" in item["key"].lower() for item in node["spec"].get("taints", [])):
                deletion_marker_seen = True
        snapshot = {
            "gpu_nodes": len(nodes),
            "ready_gpu_nodes": ready_nodes,
            "ready_ray_workers": ready_workers,
            "nodes": [
                {
                    "name": node["metadata"]["name"],
                    "created": node["metadata"]["creationTimestamp"],
                    "ready": ready(node),
                    "ready_transition": next(
                        (item.get("lastTransitionTime") for item in node.get("status", {}).get("conditions", []) if item["type"] == "Ready"),
                        None,
                    ),
                    "gpu": node.get("status", {}).get("allocatable", {}).get("nvidia.com/gpu"),
                    "taints": node["spec"].get("taints", []),
                    "provider_id": node["spec"].get("providerID"),
                }
                for node in sorted(nodes, key=lambda item: item["metadata"]["name"])
            ],
            "workers": [
                {
                    "name": pod["metadata"]["name"],
                    "created": pod["metadata"]["creationTimestamp"],
                    "node": pod["spec"].get("nodeName"),
                    "phase": pod.get("status", {}).get("phase"),
                    "ready": ready(pod),
                    "ready_transition": next(
                        (item.get("lastTransitionTime") for item in pod.get("status", {}).get("conditions", []) if item["type"] == "Ready"),
                        None,
                    ),
                    "containers": [
                        {"name": item["name"], "state": item.get("state"), "restarts": item["restartCount"]}
                        for item in pod.get("status", {}).get("containerStatuses", [])
                    ],
                }
                for pod in sorted(pods, key=lambda item: item["metadata"]["name"])
            ],
        }
        encoded = json.dumps(snapshot, sort_keys=True)
        if encoded != previous or time.monotonic() - last_emit >= 60:
            emit("scale_snapshot", **snapshot)
            previous = encoded
            last_emit = time.monotonic()
        if len(nodes) > 4 or ready_workers > 4:
            raise RuntimeError("Observed GPU node/worker count exceeded the four-worker experiment bound.")
        events = read_api(f"/api/v1/namespaces/{NAMESPACE}/events")["items"]
        for event in events:
            uid = event["metadata"]["uid"]
            count = event.get("count", 1)
            if known_events.get(uid) != count:
                emit(
                    "kubernetes_event",
                    reason=event.get("reason"),
                    message=event.get("message"),
                    object=event.get("involvedObject", {}).get("name"),
                    first_time=event.get("firstTimestamp"),
                    last_time=event.get("lastTimestamp"),
                    count=count,
                )
                known_events[uid] = count
        if initial_one_seen and peak_nodes == 4 and peak_workers == 4 and len(nodes) == 1 and ready_nodes == 1 and ready_workers == 1:
            stable_scale_in += 1
            if stable_scale_in >= 3:
                emit(
                    "scale_cycle_observed",
                    peak_ready_gpu_nodes=peak_nodes,
                    peak_ready_ray_workers=peak_workers,
                    initial_one_worker_observed=initial_one_seen,
                    final_ready_gpu_nodes=ready_nodes,
                    final_ready_ray_workers=ready_workers,
                    autoscaler_deletion_marker_observed=deletion_marker_seen,
                )
                return
        else:
            stable_scale_in = 0
        time.sleep(20)
    raise TimeoutError(
        f"Did not observe a complete four-node scale-out/scale-in cycle: peak nodes={peak_nodes}, workers={peak_workers}."
    )


if __name__ == "__main__":
    main()
