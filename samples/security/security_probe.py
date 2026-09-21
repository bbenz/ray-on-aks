import json
import os
import socket
import urllib.error
import urllib.request


mode = os.environ["PROBE_MODE"]
head_ip = os.environ["HEAD_IP"]
addresses = socket.getaddrinfo(
    "ray-t4-head-svc.ray-learn.svc.cluster.local", 8265, family=socket.AF_INET
)
if not addresses:
    raise RuntimeError("DNS did not return a service address.")
print(json.dumps({"check": "dns", "mode": mode, "status": "resolved"}), flush=True)

if os.environ.get("EXTERNAL_CONTROL") == "true":
    external_ip = os.environ["EXTERNAL_IP"]
    with socket.create_connection((external_ip, 443), timeout=10):
        print(json.dumps({"check": "external_positive_control", "target_ip": external_ip, "status": "connected"}), flush=True)

if mode == "allowed-submitter":
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://{head_ip}:8265/api/jobs/", timeout=10):
            raise RuntimeError("The reachable Jobs API unexpectedly accepted an unauthenticated request.")
    except urllib.error.HTTPError as error:
        if error.code not in (401, 403):
            raise
        print(json.dumps({"check": "submitter_network_allowed_auth_required", "status": error.code}), flush=True)
elif mode == "denied":
    try:
        with socket.create_connection((head_ip, 8265), timeout=5):
            raise RuntimeError("An unapproved pod unexpectedly connected to the head.")
    except TimeoutError:
        print(json.dumps({"check": "unapproved_pod_to_head", "target_ip": head_ip, "status": "blocked"}), flush=True)
else:
    raise ValueError(f"Unknown probe mode: {mode}")
