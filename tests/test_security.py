"""Checks that the isolation samples describe one consistent set of rules."""

import pytest
from conftest import (
    RAY_CLUSTER,
    RAY_NAMESPACE,
    SAMPLES,
    apply_json_patch,
    load_json,
    load_yaml_documents,
)

SECURITY = SAMPLES / "security"
QUICKSTART = SAMPLES / "quickstart"

PORT_PARAMETERS = (
    "port",
    "dashboard-port",
    "node-manager-port",
    "object-manager-port",
    "runtime-env-agent-port",
    "dashboard-agent-grpc-port",
    "dashboard-agent-listen-port",
    "min-worker-port",
    "max-worker-port",
    "metrics-export-port",
)


@pytest.fixture(scope="module")
def isolated_cluster():
    """The RayCluster as the article renders it: base manifest plus port patch."""
    base = load_yaml_documents(QUICKSTART / "raycluster-t4.yaml")[0]
    patch = load_json(SECURITY / "ray-t4-fixed-ports.patch.json")
    return apply_json_patch(base, patch)


@pytest.fixture(scope="module")
def policies():
    return load_yaml_documents(SECURITY / "ray-t4-network-policies.yaml")


def policy_ports(policy):
    ports = set()
    for direction in ("ingress", "egress"):
        for rule in policy["spec"].get(direction) or []:
            for port in rule.get("ports", []):
                start = port["port"]
                end = port.get("endPort", start)
                if isinstance(start, int):
                    ports.update(range(start, end + 1))
    return ports


def test_fixed_port_patch_refuses_the_wrong_document():
    patch = load_json(SECURITY / "ray-t4-fixed-ports.patch.json")
    assert any(operation["op"] == "test" for operation in patch), "The patch would apply to any document."
    wrong = load_yaml_documents(SAMPLES / "autoscaling" / "raycluster.yaml")[0]
    with pytest.raises(AssertionError):
        apply_json_patch(wrong, patch)


def test_fixed_port_patch_pins_head_and_every_worker_group(isolated_cluster):
    head = isolated_cluster["spec"]["headGroupSpec"]["rayStartParams"]
    for parameter in PORT_PARAMETERS:
        assert parameter in head, f"The head is missing {parameter}; Ray would pick a random port."
    worker_only = [item for item in PORT_PARAMETERS if item not in {"port", "dashboard-port"}]
    for group in isolated_cluster["spec"]["workerGroupSpecs"]:
        params = group["rayStartParams"]
        for parameter in worker_only:
            assert parameter in params, f"Worker group '{group['groupName']}' is missing {parameter}."


def test_patched_cluster_disables_autoscaling(isolated_cluster):
    """The isolation walkthrough uses a static cluster; autoscaling needs extra
    head-to-Kubernetes-API egress that these policies don't grant."""
    assert isolated_cluster["spec"]["enableInTreeAutoscaling"] is False


def test_pinned_ports_are_exactly_the_ports_the_policies_allow(isolated_cluster, policies):
    internal = next(item for item in policies if item["metadata"]["name"] == "ray-t4-internal")
    allowed = policy_ports(internal)
    metrics = next(item for item in policies if item["metadata"]["name"] == "ray-t4-metrics-ingress")
    allowed_metrics = policy_ports(metrics)

    head = isolated_cluster["spec"]["headGroupSpec"]["rayStartParams"]
    worker = isolated_cluster["spec"]["workerGroupSpecs"][0]["rayStartParams"]

    for params in (head, worker):
        for name, value in params.items():
            if not name.endswith("port") or name in {"min-worker-port", "max-worker-port"}:
                continue
            port = int(value)
            if name == "metrics-export-port":
                assert port in allowed_metrics, f"Metrics port {port} isn't allowed by the metrics policy."
            else:
                assert port in allowed, f"Ray listens on {port} ({name}) but no policy allows it."

    worker_range = range(int(head["min-worker-port"]), int(head["max-worker-port"]) + 1)
    assert set(worker_range) <= allowed, "The Ray worker port range isn't fully covered by the policies."


def test_client_port_is_not_allowed_by_any_policy(policies):
    """Ray Client (10001) is unused here and stays blocked."""
    for policy in policies:
        assert 10001 not in policy_ports(policy), f"{policy['metadata']['name']} allows the Ray Client port."


def test_namespace_defaults_to_deny(policies):
    default = next(item for item in policies if item["metadata"]["name"] == "ray-learn-default-deny")
    assert default["spec"]["podSelector"] == {}
    assert sorted(default["spec"]["policyTypes"]) == ["Egress", "Ingress"]
    assert default["spec"].get("ingress") == []
    assert default["spec"].get("egress") == []
    for policy in policies:
        assert policy["metadata"]["namespace"] == RAY_NAMESPACE


def test_dns_exception_covers_tcp_and_udp(policies):
    dns = next(item for item in policies if item["metadata"]["name"] == "ray-learn-dns")
    rule = dns["spec"]["egress"][0]
    protocols = {port["protocol"] for port in rule["ports"]}
    assert protocols == {"TCP", "UDP"}, "DNS needs both protocols; large responses fall back to TCP."
    assert all(port["port"] == 53 for port in rule["ports"])


def test_submitter_label_patch_matches_the_policy_selector(policies):
    patch = load_json(SECURITY / "rayjob-submitter-label.patch.json")
    job = load_yaml_documents(QUICKSTART / "rayjob-gpu-check.yaml")[0]
    patched = apply_json_patch(job, patch)
    labels = patched["spec"]["submitterPodTemplate"]["metadata"]["labels"]

    egress = next(item for item in policies if item["metadata"]["name"] == "ray-t4-submitter-egress")
    assert egress["spec"]["podSelector"]["matchLabels"] == labels, (
        "The submitter label patch doesn't match the pod selector the egress policy uses."
    )

    ingress = next(item for item in policies if item["metadata"]["name"] == "ray-t4-dashboard-ingress")
    peers = [peer for rule in ingress["spec"]["ingress"] for peer in rule["from"] if "podSelector" in peer]
    assert any(peer["podSelector"].get("matchLabels") == labels for peer in peers), (
        "Head ingress doesn't admit the labeled submitter."
    )


def test_submitter_patch_preserves_the_existing_submitter_pod_spec():
    patch = load_json(SECURITY / "rayjob-submitter-label.patch.json")
    for name in ("rayjob-gpu-check.yaml", "rayjob-inference.yaml"):
        job = load_yaml_documents(QUICKSTART / name)[0]
        patched = apply_json_patch(job, patch)
        assert patched["spec"]["submitterPodTemplate"]["spec"] == job["spec"]["submitterPodTemplate"]["spec"]
        assert patched["spec"]["entrypoint"] == job["spec"]["entrypoint"]


def test_token_auth_patch_enables_token_mode_and_proxy_variables():
    patch = load_json(SECURITY / "ray-t4-token-auth-proxy.patch.json")
    base = load_yaml_documents(QUICKSTART / "raycluster-t4.yaml")[0]
    patched = apply_json_patch(base, patch)
    assert patched["spec"]["authOptions"]["mode"] == "token"

    proxy_service = None
    for document in load_yaml_documents(SECURITY / "egress-proxy.yaml"):
        if document["kind"] == "Service":
            proxy_service = document
    assert proxy_service is not None, "The proxy sample defines no Service for the clients to reach."
    port = proxy_service["spec"]["ports"][0]["port"]
    expected = f"{proxy_service['metadata']['name']}.{proxy_service['metadata']['namespace']}.svc.cluster.local:{port}"

    groups = [patched["spec"]["headGroupSpec"]] + patched["spec"]["workerGroupSpecs"]
    for group in groups:
        variables = {item["name"]: item["value"] for item in group["template"]["spec"]["containers"][0]["env"]}
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
            assert name in variables, f"The patch didn't set {name}."
        assert expected in variables["HTTPS_PROXY"], "The proxy variables don't point at the sample proxy Service."
        for internal in (".svc", ".cluster.local", "localhost"):
            assert internal in variables["NO_PROXY"], "Internal Ray traffic would be sent through the proxy."


def test_egress_policy_points_at_the_proxy_the_sample_deploys():
    exception = load_yaml_documents(SECURITY / "ray-t4-egress-proxy.yaml")[0]
    rule = exception["spec"]["egress"][0]
    peer = rule["to"][0]
    target_namespace = peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
    target_labels = peer["podSelector"]["matchLabels"]
    port = rule["ports"][0]["port"]

    documents = load_yaml_documents(SECURITY / "egress-proxy.yaml")
    deployment = next(item for item in documents if item["kind"] == "Deployment")
    assert deployment["metadata"]["namespace"] == target_namespace
    assert deployment["spec"]["template"]["metadata"]["labels"] == target_labels
    assert deployment["spec"]["template"]["spec"]["containers"][0]["ports"][0]["containerPort"] == port


def test_proxy_denies_by_default_and_allows_only_listed_domains():
    documents = load_yaml_documents(SECURITY / "egress-proxy.yaml")
    config = next(item for item in documents if item["kind"] == "ConfigMap")
    squid = config["data"]["squid.conf"]
    assert "http_access deny all" in squid, "The proxy has no default deny."
    assert squid.index("http_access allow downloads") < squid.index("http_access deny all"), (
        "Squid applies the first matching rule; the allowlist must precede the deny."
    )
    assert "acl downloads dstdomain" in squid, "The proxy allows destinations without an allowlist."
    assert "http_access deny !CONNECT" in squid, "The proxy would serve plain HTTP requests."

    policy = next(
        item for item in documents if item["kind"] == "NetworkPolicy" and item["metadata"]["name"] == "approved-download-proxy"
    )
    external = [rule for rule in policy["spec"]["egress"] if any("ipBlock" in peer for peer in rule["to"])]
    assert external, "The proxy has no external egress rule."
    excepted = external[0]["to"][0]["ipBlock"]["except"]
    for private in ("10.0.0.0/8", "169.254.0.0/16", "168.63.129.16/32"):
        assert private in excepted, f"The proxy could reach {private} on 443."


def test_probe_job_template_is_not_directly_applyable():
    """It's a template: each probe needs its own name and PROBE_MODE."""
    job = load_yaml_documents(SECURITY / "security-probe-job.yaml")[0]
    assert job["spec"]["template"]["spec"]["containers"][0]["env"] == []
    assert job["spec"]["backoffLimit"] == 0, "A retrying probe would report a transient result."
    assert job["spec"]["activeDeadlineSeconds"] > 0
    assert job["metadata"]["namespace"] == RAY_NAMESPACE


def test_policies_select_the_cluster_the_samples_create():
    for path in (SECURITY / "ray-t4-network-policies.yaml", SECURITY / "ray-t4-egress-proxy.yaml"):
        for document in load_yaml_documents(path):
            selector = document["spec"].get("podSelector", {}).get("matchLabels", {})
            if "ray.io/cluster" in selector:
                assert selector["ray.io/cluster"] == RAY_CLUSTER
