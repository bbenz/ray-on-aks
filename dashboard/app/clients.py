"""Small clients for the Kubernetes API, Microsoft Entra tokens, Azure Monitor managed
Prometheus, Log Analytics, and a GPU node's DCGM exporter.

Only the Python standard library is used, so the dashboard runs on a minimal distroless
image and installs nothing at startup.
"""

import http.client
import json
import os
import re
import shutil
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class ApiError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _read(path):
    with open(path, encoding="utf-8") as stream:
        return stream.read().strip()


def _open(request, timeout, context=None):
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return response.read()
    except urllib.error.HTTPError as err:
        detail = err.read()[:500].decode("utf-8", "replace")
        raise ApiError(f"{request.get_method()} {_redact(request.full_url)}: HTTP {err.code} {detail}", err.code) from None
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as err:
        # HTTPException covers a response cut short, such as an exporter restarting mid-read.
        raise ApiError(f"{request.get_method()} {_redact(request.full_url)}: {type(err).__name__} {err}") from None


def _redact(url):
    return url.split("?", 1)[0]


class KubeClient:
    """Read-only Kubernetes API access, in-cluster or through `kubectl proxy` for local runs."""

    def __init__(self, base_url=None, timeout=10):
        self.timeout = timeout
        if base_url:
            self.base = base_url.rstrip("/")
            self.context = None
            self.token_path = None
        else:
            host = os.environ["KUBERNETES_SERVICE_HOST"]
            port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            self.base = f"https://{host}:{port}"
            self.context = ssl.create_default_context(cafile=os.path.join(SA_DIR, "ca.crt"))
            self.token_path = os.path.join(SA_DIR, "token")

    @property
    def via_proxy(self):
        return self.token_path is None

    def _get(self, path, params=None, accept="application/json", timeout=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": accept}
        if self.token_path:
            # The kubelet rotates the projected token, so read it on every call.
            headers["Authorization"] = "Bearer " + _read(self.token_path)
        return _open(urllib.request.Request(url, headers=headers), timeout or self.timeout, self.context)

    def get(self, path, params=None, timeout=None):
        return json.loads(self._get(path, params, timeout=timeout))

    def items(self, path, params=None):
        return self.get(path, params).get("items", [])

    def logs(self, namespace, pod, container=None, tail=400):
        params = {"tailLines": str(tail)}
        if container:
            params["container"] = container
        # The log endpoint answers 406 to "Accept: text/plain" on current Kubernetes; kubectl sends */*.
        raw = self._get(f"/api/v1/namespaces/{namespace}/pods/{pod}/log", params, accept="*/*")
        return raw.decode("utf-8", "replace")

    def proxy_text(self, path, timeout=3):
        return self._get(path, accept="text/plain", timeout=timeout).decode("utf-8", "replace")


class WorkloadIdentityCredential:
    """Exchanges the pod's projected service account token for a Microsoft Entra token.

    Microsoft Entra trusts the token through a federated credential on a user-assigned
    managed identity. No client secret exists anywhere.
    """

    def __init__(self, client_id, tenant_id, token_file, authority_host=None):
        self.client_id = client_id
        self.tenant_id = tenant_id
        self.token_file = token_file
        self.authority = (authority_host or "https://login.microsoftonline.com/").rstrip("/") + "/"
        self._cache = {}
        self._lock = threading.Lock()

    def get_token(self, scope):
        with self._lock:
            hit = self._cache.get(scope)
            if hit and hit[1] - 300 > time.time():
                return hit[0]
        form = urllib.parse.urlencode({
            "client_id": self.client_id,
            "scope": scope,
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": _read(self.token_file),
        }).encode()
        request = urllib.request.Request(
            f"{self.authority}{self.tenant_id}/oauth2/v2.0/token", data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            payload = json.loads(_open(request, 20))
        except ApiError as err:
            raise ApiError("Microsoft Entra token request failed: " + _entra_error(str(err)), err.status) from None
        token = payload["access_token"]
        expires = time.time() + int(payload.get("expires_in", 3600))
        with self._lock:
            self._cache[scope] = (token, expires)
        return token


def _entra_error(text):
    match = re.search(r"(AADSTS\d+)[^\"]*", text)
    return match.group(0)[:300] if match else text[:300]


class AzureCliCredential:
    """Local development only: borrows the signed-in Azure CLI user's token."""

    def __init__(self):
        self._cache = {}
        self._lock = threading.Lock()

    def get_token(self, scope):
        with self._lock:
            hit = self._cache.get(scope)
            if hit and hit[1] - 300 > time.time():
                return hit[0]
        resource = scope[: -len("/.default")] if scope.endswith("/.default") else scope
        az = shutil.which("az") or shutil.which("az.cmd")
        if not az:
            raise ApiError("Azure CLI not found on PATH.")
        done = subprocess.run([az, "account", "get-access-token", "--resource", resource, "-o", "json"],
                              capture_output=True, text=True, timeout=90, check=False)
        if done.returncode != 0:
            raise ApiError("az account get-access-token failed: " + (done.stderr or "").strip()[:300])
        data = json.loads(done.stdout)
        expires = float(data.get("expires_on") or time.time() + 1800)
        with self._lock:
            self._cache[scope] = (data["accessToken"], expires)
        return data["accessToken"]


class PrometheusClient:
    """Azure Monitor managed Prometheus query API (PromQL), authenticated with Microsoft Entra."""

    SCOPE = "https://prometheus.monitor.azure.com/.default"

    def __init__(self, endpoint, credential, timeout=30):
        self.endpoint = endpoint.rstrip("/")
        self.credential = credential
        self.timeout = timeout

    def _post(self, path, form):
        request = urllib.request.Request(
            self.endpoint + path, data=urllib.parse.urlencode(form).encode(),
            headers={"Authorization": "Bearer " + self.credential.get_token(self.SCOPE),
                     "Content-Type": "application/x-www-form-urlencoded"})
        payload = json.loads(_open(request, self.timeout))
        if payload.get("status") != "success":
            raise ApiError(f"Prometheus query failed: {payload.get('error')}")
        return payload["data"]

    def query(self, promql, at=None):
        form = {"query": promql}
        if at is not None:
            form["time"] = str(at)
        return self._post("/api/v1/query", form).get("result", [])

    def query_range(self, promql, start, end, step):
        return self._post("/api/v1/query_range", {
            "query": promql, "start": str(start), "end": str(end), "step": str(step)}).get("result", [])


class LogAnalyticsClient:
    """Log Analytics query API (KQL) for Container insights data, authenticated with Microsoft Entra."""

    SCOPE = "https://api.loganalytics.io/.default"

    def __init__(self, workspace_id, credential, timeout=60):
        self.workspace_id = workspace_id
        self.credential = credential
        self.timeout = timeout

    def query(self, kql, timespan="P30D"):
        request = urllib.request.Request(
            f"https://api.loganalytics.io/v1/workspaces/{self.workspace_id}/query",
            data=json.dumps({"query": kql, "timespan": timespan}).encode(),
            headers={"Authorization": "Bearer " + self.credential.get_token(self.SCOPE),
                     "Content-Type": "application/json"})
        payload = json.loads(_open(request, self.timeout))
        tables = payload.get("tables") or []
        if not tables:
            return []
        columns = [c["name"] for c in tables[0].get("columns", [])]
        return [dict(zip(columns, row)) for row in tables[0].get("rows", [])]


# ── DCGM exporter ────────────────────────────────────────────────────────────

DCGM_FIELDS = {
    "DCGM_FI_DEV_GPU_UTIL": "util",
    "DCGM_FI_DEV_FB_USED": "fbUsedMiB",
    "DCGM_FI_DEV_FB_FREE": "fbFreeMiB",
    "DCGM_FI_DEV_POWER_USAGE": "powerW",
    "DCGM_FI_DEV_GPU_TEMP": "tempC",
    "DCGM_FI_DEV_SM_CLOCK": "smClockMHz",
    "DCGM_FI_DEV_MEM_COPY_UTIL": "memCopyUtil",
}
_LABEL_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def parse_dcgm(text):
    """Parses dcgm-exporter's Prometheus text into {gpu index: {field: value, "model": ...}}."""
    gpus = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        brace = line.find("{")
        name = line[:brace] if brace > 0 else line.split(" ", 1)[0]
        field = DCGM_FIELDS.get(name)
        if not field:
            continue
        labels = dict(_LABEL_RE.findall(line[brace:line.rfind("}")])) if brace > 0 else {}
        try:
            value = float(line[line.rfind("}") + 1:].split()[0] if brace > 0 else line.split()[1])
        except (IndexError, ValueError):
            continue
        gpu = gpus.setdefault(labels.get("gpu", "0"), {})
        gpu[field] = value
        if labels.get("modelName"):
            gpu["model"] = labels["modelName"]
    return gpus


def scrape_dcgm(url, timeout=2.5):
    request = urllib.request.Request(url, headers={"Accept": "text/plain"})
    return _open(request, timeout).decode("utf-8", "replace")
