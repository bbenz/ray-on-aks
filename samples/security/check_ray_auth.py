import json
import os
import socket
import sys
import urllib.error
import urllib.request

from ray.job_submission import JobSubmissionClient


if os.environ.get("RAY_AUTH_MODE") != "token":
    raise RuntimeError("This pod is not running with Ray token authentication.")
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open("http://127.0.0.1:8265/api/jobs/", timeout=10):
        raise RuntimeError("An unauthenticated request unexpectedly reached the Jobs API.")
except urllib.error.HTTPError as error:
    if error.code not in (401, 403):
        raise
    print(json.dumps({"check": "unauthenticated_jobs_api", "status": error.code}), flush=True)

jobs = JobSubmissionClient("http://127.0.0.1:8265").list_jobs()
print(json.dumps({"check": "authenticated_jobs_api", "jobs_returned": len(jobs)}), flush=True)
external_ip = sys.argv[1]
socket.getaddrinfo("pypi.org", 443, family=socket.AF_INET)
try:
    with socket.create_connection((external_ip, 443), timeout=5):
        raise RuntimeError("Direct external TCP access unexpectedly bypassed the proxy-only policy.")
except TimeoutError:
    print(json.dumps({"check": "ray_direct_external_egress", "status": "blocked", "target_ip": external_ip}), flush=True)
