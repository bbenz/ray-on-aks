"""Checks on the Kubernetes manifests the articles tell readers to apply."""

import re

import pytest
from conftest import (
    MODEL_REVISION,
    RAY_CLUSTER,
    RAY_NAMESPACE,
    RAY_VERSION,
    SAMPLES,
    iter_containers,
    load_yaml_documents,
)

DIGEST = re.compile(r"^(?P<repo>[^:@]+):(?P<tag>[^@]+)@sha256:[0-9a-f]{64}$")

# Node-level agents that need root to serve a socket in a kubelet host directory.
# They still drop every capability and can't escalate.
NODE_AGENTS = {("nvidia-device-plugin.yaml", "nvidia-device-plugin-ctr")}


def pod_specs(document):
    """Every pod spec in a document, whatever resource kind wraps it."""
    kind = document.get("kind")
    spec = document.get("spec", {})
    if kind == "RayCluster":
        yield spec["headGroupSpec"]["template"]["spec"]
        for group in spec.get("workerGroupSpecs", []):
            yield group["template"]["spec"]
    elif kind == "RayJob":
        if "submitterPodTemplate" in spec:
            yield spec["submitterPodTemplate"]["spec"]
    elif kind in {"Deployment", "Job", "DaemonSet", "StatefulSet"}:
        yield spec["template"]["spec"]


def test_every_manifest_parses(manifest_paths):
    assert manifest_paths, "No manifests were found."
    for path in manifest_paths:
        documents = load_yaml_documents(path)
        assert documents, f"{path.name} contains no documents."
        for document in documents:
            assert document.get("apiVersion"), f"{path.name} has a document without apiVersion."
            assert document.get("kind"), f"{path.name} has a document without kind."
            assert document.get("metadata", {}).get("name"), f"{path.name} has an unnamed object."


def test_container_images_are_digest_pinned(manifest_paths):
    for path in manifest_paths:
        for document in load_yaml_documents(path):
            for spec in pod_specs(document):
                for container in iter_containers(spec):
                    image = container["image"]
                    assert DIGEST.match(image), f"{path.name}: {container['name']} image isn't digest-pinned: {image}"


def test_ray_images_match_the_declared_ray_version(manifest_paths):
    for path in manifest_paths:
        for document in load_yaml_documents(path):
            for spec in pod_specs(document):
                for container in iter_containers(spec):
                    match = DIGEST.match(container["image"])
                    if match and match["repo"] == "rayproject/ray":
                        assert match["tag"].startswith(f"{RAY_VERSION}-py311-"), (
                            f"{path.name}: {container['name']} uses {match['tag']}, "
                            f"which doesn't match Ray {RAY_VERSION} on Python 3.11."
                        )
            if document.get("kind") == "RayCluster":
                assert document["spec"]["rayVersion"] == RAY_VERSION


def test_head_uses_cpu_image_and_worker_uses_cuda_image(manifest_paths):
    for path in manifest_paths:
        for document in load_yaml_documents(path):
            if document.get("kind") != "RayCluster":
                continue
            head = document["spec"]["headGroupSpec"]["template"]["spec"]["containers"][0]["image"]
            assert "-cpu@" in head, f"{path.name}: the head should use the CPU image, not {head}."
            for group in document["spec"]["workerGroupSpecs"]:
                worker = group["template"]["spec"]["containers"][0]["image"]
                assert "-cu" in worker, f"{path.name}: GPU worker group '{group['groupName']}' isn't using a CUDA image."


def test_gpu_workers_request_and_limit_the_same_device_count(manifest_paths):
    seen = False
    for path in manifest_paths:
        for document in load_yaml_documents(path):
            if document.get("kind") != "RayCluster":
                continue
            for group in document["spec"]["workerGroupSpecs"]:
                container = group["template"]["spec"]["containers"][0]
                resources = container["resources"]
                requested = resources["requests"].get("nvidia.com/gpu")
                limited = resources["limits"].get("nvidia.com/gpu")
                assert requested is not None and limited is not None, (
                    f"{path.name}: worker group '{group['groupName']}' must request and limit nvidia.com/gpu."
                )
                assert str(requested) == str(limited), f"{path.name}: GPU request and limit differ."
                assert str(group["rayStartParams"]["num-gpus"]) == str(requested), (
                    f"{path.name}: Ray's num-gpus doesn't match the Kubernetes device count."
                )
                seen = True
    assert seen, "No GPU worker group was checked."


def test_head_advertises_no_logical_cpus(manifest_paths):
    """The head keeps Kubernetes CPU for its own processes but shouldn't attract tasks."""
    for path in manifest_paths:
        for document in load_yaml_documents(path):
            if document.get("kind") != "RayCluster":
                continue
            params = document["spec"]["headGroupSpec"]["rayStartParams"]
            assert str(params.get("num-cpus")) == "0", f"{path.name}: the head advertises logical CPUs."
            assert str(params.get("num-gpus", "0")) == "0", f"{path.name}: the head advertises GPUs."


def test_no_manifest_exposes_a_ray_endpoint_externally(manifest_paths):
    for path in manifest_paths:
        for document in load_yaml_documents(path):
            spec = document.get("spec", {})
            if document.get("kind") == "Service":
                assert spec.get("type", "ClusterIP") == "ClusterIP", f"{path.name} publishes a Service externally."
            if document.get("kind") == "RayCluster":
                head_service = spec["headGroupSpec"].get("serviceType", "ClusterIP")
                assert head_service == "ClusterIP", f"{path.name}: the head service type is {head_service}."


def test_workload_pods_drop_capabilities_and_run_as_non_root(manifest_paths):
    """Ray, proxy, probe, and observer pods all run application or network code."""
    for path in manifest_paths:
        for document in load_yaml_documents(path):
            for spec in pod_specs(document):
                for container in iter_containers(spec):
                    security = container.get("securityContext")
                    assert security, f"{path.name}: {container['name']} has no securityContext."
                    assert security.get("allowPrivilegeEscalation") is False
                    assert security.get("capabilities", {}).get("drop") == ["ALL"]
                    if (path.name, container["name"]) in NODE_AGENTS:
                        continue
                    non_root = security.get("runAsNonRoot", spec.get("securityContext", {}).get("runAsNonRoot"))
                    assert non_root is True, f"{path.name}: {container['name']} doesn't require a non-root user."


def test_self_managed_device_plugin_stays_off_managed_gpu_nodes():
    """The upstream DaemonSet tolerates the GPU taint and has no selector, so it
    would also land on AKS-managed GPU nodes that already run a device plugin."""
    documents = load_yaml_documents(SAMPLES / "autoscaling" / "nvidia-device-plugin.yaml")
    daemonsets = [document for document in documents if document["kind"] == "DaemonSet"]
    assert len(daemonsets) == 1
    spec = daemonsets[0]["spec"]["template"]["spec"]
    assert spec.get("nodeSelector") == {"kubernetes.azure.com/agentpool": "scalet4"}

    cluster = load_yaml_documents(SAMPLES / "autoscaling" / "raycluster.yaml")[0]
    worker = cluster["spec"]["workerGroupSpecs"][0]["template"]["spec"]
    worker_pool = worker["nodeSelector"].get("agentpool") or worker["nodeSelector"].get("kubernetes.azure.com/agentpool")
    assert worker_pool == "scalet4", "The device plugin and the autoscaling workers must target the same pool."
    assert {(t["key"], t.get("value"), t["effect"]) for t in spec["tolerations"]} >= {
        (t["key"], t.get("value"), t["effect"]) for t in worker["tolerations"]
    }, "The device plugin must tolerate every taint the autoscaling workers tolerate."


def test_service_account_tokens_are_mounted_only_where_autoscaling_needs_them(manifest_paths):
    """KubeRay runs the Ray autoscaler as a head sidecar that calls the
    Kubernetes API. Turning the token off on an autoscaling head breaks scaling;
    leaving it on elsewhere hands application code a cluster credential."""
    for path in manifest_paths:
        for document in load_yaml_documents(path):
            if document.get("kind") != "RayCluster":
                continue
            autoscaling = bool(document["spec"].get("enableInTreeAutoscaling"))
            head = document["spec"]["headGroupSpec"]["template"]["spec"]
            mounted = head.get("automountServiceAccountToken", True)
            if autoscaling:
                assert mounted is True, (
                    f"{path.name}: the autoscaling head needs its service account token for the sidecar."
                )
            else:
                assert mounted is False, (
                    f"{path.name}: this head doesn't call the Kubernetes API; don't mount its token."
                )
            for group in document["spec"]["workerGroupSpecs"]:
                worker = group["template"]["spec"]
                assert worker.get("automountServiceAccountToken") is False, (
                    f"{path.name}: worker group '{group['groupName']}' mounts a service account token."
                )


def test_node_selectors_use_a_real_aks_agent_pool_label(manifest_paths):
    known = {"agentpool", "kubernetes.azure.com/agentpool"}
    for path in manifest_paths:
        for document in load_yaml_documents(path):
            for spec in pod_specs(document):
                selector = spec.get("nodeSelector") or {}
                pool_keys = {key for key in selector if key.endswith("agentpool")}
                assert pool_keys <= known, f"{path.name} selects nodes with an unknown label: {pool_keys - known}"


def test_rayjobs_reuse_the_quickstart_cluster(quickstart_cluster):
    for name in ("rayjob-gpu-check.yaml", "rayjob-inference.yaml"):
        document = load_yaml_documents(SAMPLES / "quickstart" / name)[0]
        spec = document["spec"]
        assert spec["clusterSelector"]["ray.io/cluster"] == RAY_CLUSTER, f"{name} doesn't select the existing cluster."
        assert spec["shutdownAfterJobFinishes"] is False, f"{name} would delete a cluster it didn't create."
        assert document["metadata"]["namespace"] == quickstart_cluster["metadata"]["namespace"]


def test_rayjob_entrypoints_run_files_that_are_mounted(quickstart_cluster):
    """The ConfigMap mount path and the entrypoint path have to agree."""
    head = quickstart_cluster["spec"]["headGroupSpec"]["template"]["spec"]
    mount = next(
        item for item in head["containers"][0]["volumeMounts"] if item["name"] == "ray-learning-code"
    )
    mount_path = mount["mountPath"].rstrip("/")
    assert mount["readOnly"] is True

    for name in ("rayjob-gpu-check.yaml", "rayjob-inference.yaml"):
        entrypoint = load_yaml_documents(SAMPLES / "quickstart" / name)[0]["spec"]["entrypoint"]
        script = entrypoint.split()[1]
        assert script.startswith(f"{mount_path}/"), f"{name}: {script} isn't under the mounted path {mount_path}."
        assert (SAMPLES / "quickstart" / script.rsplit("/", 1)[1]).exists(), (
            f"{name} runs {script}, which has no matching sample file."
        )


def test_worker_object_store_fits_in_shared_memory(manifest_paths):
    """Ray's object store lives in /dev/shm; it can't exceed the volume size."""
    units = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3}

    def to_bytes(value):
        for suffix, factor in units.items():
            if str(value).endswith(suffix):
                return int(str(value)[: -len(suffix)]) * factor
        return int(value)

    for path in manifest_paths:
        for document in load_yaml_documents(path):
            if document.get("kind") != "RayCluster":
                continue
            groups = [("head", document["spec"]["headGroupSpec"])]
            groups += [(group["groupName"], group) for group in document["spec"]["workerGroupSpecs"]]
            for name, group in groups:
                store = group["rayStartParams"].get("object-store-memory")
                if not store:
                    continue
                spec = group["template"]["spec"]
                volume = next((item for item in spec["volumes"] if item["name"] == "shared-memory"), None)
                assert volume, f"{path.name}: group '{name}' sets an object store but mounts no /dev/shm volume."
                limit = to_bytes(volume["emptyDir"]["sizeLimit"])
                assert int(store) <= limit, f"{path.name}: group '{name}' object store exceeds /dev/shm."
                memory = to_bytes(spec["containers"][0]["resources"]["limits"]["memory"])
                assert limit <= memory, (
                    f"{path.name}: group '{name}' /dev/shm is larger than the container memory limit; "
                    "shared memory counts toward that limit."
                )


def test_podmonitor_targets_a_declared_container_port(quickstart_cluster):
    monitor = load_yaml_documents(SAMPLES / "monitoring" / "ray-t4-podmonitor.yaml")[0]
    assert monitor["metadata"]["namespace"] == RAY_NAMESPACE
    assert monitor["spec"]["namespaceSelector"]["matchNames"] == [RAY_NAMESPACE]
    assert monitor["spec"]["selector"]["matchLabels"]["ray.io/cluster"] == RAY_CLUSTER

    port_name = monitor["spec"]["podMetricsEndpoints"][0]["port"]
    groups = [quickstart_cluster["spec"]["headGroupSpec"]] + quickstart_cluster["spec"]["workerGroupSpecs"]
    for group in groups:
        container = group["template"]["spec"]["containers"][0]
        ports = {item["name"]: item["containerPort"] for item in container["ports"]}
        assert port_name in ports, f"The PodMonitor scrapes '{port_name}', which {container['name']} doesn't expose."
        assert str(group["rayStartParams"]["metrics-export-port"]) == str(ports[port_name]), (
            "Ray's metrics-export-port doesn't match the named container port."
        )


@pytest.mark.parametrize("name", ["rayjob-gpu-check.yaml", "rayjob-inference.yaml"])
def test_inference_job_writes_where_the_docs_say(name):
    document = load_yaml_documents(SAMPLES / "quickstart" / name)[0]
    entrypoint = document["spec"]["entrypoint"]
    if "batch_inference" not in entrypoint:
        pytest.skip("Not the inference job.")
    assert "--output /tmp/ray-inference-results.jsonl" in entrypoint


def test_model_revision_is_pinned_in_every_sample_that_names_the_model():
    for path in list(SAMPLES.rglob("*.py")) + list(SAMPLES.rglob("*.json")):
        text = path.read_text(encoding="utf-8")
        if "Qwen2.5-0.5B-Instruct" in text:
            assert MODEL_REVISION in text, f"{path.name} names the model without pinning its revision."
