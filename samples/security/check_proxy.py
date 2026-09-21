import json
import urllib.error
import urllib.request


proxy = "http://ray-egress-proxy.ray-egress.svc.cluster.local:3128"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy}))
for url in (
    "https://pypi.org/simple/torch/",
    "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct/resolve/7ae557604adf67be50417f59c2c2f167def9a775/config.json",
):
    with opener.open(url, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"Approved download path returned HTTP {response.status}.")
        print(json.dumps({"check": "approved_proxy_destination", "url": url, "status": response.status}), flush=True)

try:
    with opener.open("https://example.com/", timeout=20):
        raise RuntimeError("The proxy unexpectedly allowed an unapproved destination.")
except urllib.error.URLError as error:
    if "Tunnel connection failed: 403" not in str(error.reason):
        raise
    print(json.dumps({"check": "unapproved_proxy_destination", "status": "explicit_403_denial"}), flush=True)
