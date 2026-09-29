"""Keeps the repository and the companion articles in agreement.

The articles currently ship their own copy of the samples so they read
standalone. These checks fail when the two copies drift, and are skipped when
the drafts aren't on this machine. Set RAY_ON_AKS_DOCS to the folder holding the
article Markdown files to enable them.
"""

import re

import pytest
from conftest import KUBERAY_VERSION, MODEL_REVISION, RAY_VERSION, SAMPLES

ARTICLES = (
    "get-started-ray-aks.md",
    "ray-quickstart.md",
    "ray-security.md",
    "ray-monitoring.md",
    "ray-autoscaling.md",
)

# Repository path -> path used by the articles' own samples folder.
MIRRORED = {
    "quickstart/gpu_check.py": "gpu_check.py",
    "quickstart/batch_inference.py": "batch_inference.py",
    "quickstart/requirements-inference.txt": "requirements-inference.txt",
    "quickstart/raycluster-t4.yaml": "raycluster-t4.yaml",
    "quickstart/rayjob-gpu-check.yaml": "rayjob-gpu-check.yaml",
    "quickstart/rayjob-inference.yaml": "rayjob-inference.yaml",
    "security/ray-t4-fixed-ports.patch.json": "security/ray-t4-fixed-ports.patch.json",
    "security/ray-t4-network-policies.yaml": "security/ray-t4-network-policies.yaml",
    "security/ray-t4-egress-proxy.yaml": "security/ray-t4-egress-proxy.yaml",
    "security/rayjob-submitter-label.patch.json": "security/rayjob-submitter-label.patch.json",
    "security/egress-proxy.yaml": "security/egress-proxy.yaml",
    "security/ray-t4-token-auth-proxy.patch.json": "security/ray-t4-token-auth-proxy.patch.json",
    "monitoring/ray-t4-podmonitor.yaml": "monitoring/ray-t4-podmonitor.yaml",
    "autoscaling/burst.py": "autoscaling/burst.py",
    "autoscaling/raycluster.yaml": "autoscaling/raycluster.yaml",
    "autoscaling/worker-runtime-env.json": "autoscaling/worker-runtime-env.json",
    "autoscaling/nvidia-device-plugin.yaml": "autoscaling/nvidia-device-plugin.yaml",
}


@pytest.fixture(scope="module")
def docs(docs_root):
    if docs_root is None:
        pytest.skip("Set RAY_ON_AKS_DOCS to the draft article folder to run the documentation checks.")
    return docs_root


def read_articles(docs):
    return {name: (docs / name).read_text(encoding="utf-8") for name in ARTICLES if (docs / name).exists()}


def test_articles_are_present(docs):
    missing = [name for name in ARTICLES if not (docs / name).exists()]
    assert not missing, f"Articles not found in {docs}: {missing}"


def test_mirrored_samples_are_identical(docs):
    article_samples = docs / "samples"
    if not article_samples.is_dir():
        pytest.skip("The articles no longer carry their own samples folder.")
    drifted = []
    for repo_path, article_path in MIRRORED.items():
        here = (SAMPLES / repo_path).read_bytes().replace(b"\r\n", b"\n")
        there = (article_samples / article_path).read_bytes().replace(b"\r\n", b"\n")
        if here != there:
            drifted.append(repo_path)
    assert not drifted, f"These samples differ between the repository and the articles: {drifted}"


def test_article_sample_links_resolve(docs):
    """A link to ./samples/... has to point at a file that exists."""
    broken = []
    for name, text in read_articles(docs).items():
        for match in re.finditer(r"\]\(\./(samples/[A-Za-z0-9._/-]+)\)", text):
            if not (docs / match[1]).exists():
                broken.append(f"{name} -> {match[1]}")
    assert not broken, f"Broken sample links: {broken}"


def test_article_relative_links_resolve(docs):
    broken = []
    for name, text in read_articles(docs).items():
        for match in re.finditer(r"\]\(\./([A-Za-z0-9._/-]+\.md)(#[A-Za-z0-9._-]+)?\)", text):
            if not (docs / match[1]).exists():
                broken.append(f"{name} -> {match[1]}")
    assert not broken, f"Broken article links: {broken}"


def test_articles_and_samples_agree_on_versions(docs):
    texts = read_articles(docs)
    assert any(RAY_VERSION in text for text in texts.values()), "No article states the Ray version the samples pin."
    assert any(KUBERAY_VERSION in text for text in texts.values()), "No article states the KubeRay chart version."

    requirements = (SAMPLES / "quickstart" / "requirements-inference.txt").read_text(encoding="utf-8")
    for pin in (line.strip() for line in requirements.splitlines() if line.strip()):
        package, version = pin.split("==")
        mentioned = [name for name, text in texts.items() if f"{package}=={version}" in text or f"`{version}`" in text]
        assert mentioned, f"No article states the pinned {package} version {version}."


def test_articles_pin_the_model_revision(docs):
    texts = read_articles(docs)
    naming = {name for name, text in texts.items() if "Qwen2.5-0.5B-Instruct" in text}
    assert naming, "No article names the inference model."
    missing = {name for name in naming if MODEL_REVISION not in texts[name]}
    assert not missing, f"These articles name the model without its pinned revision: {sorted(missing)}"


def test_articles_do_not_publish_ray_endpoints(docs):
    """Ray endpoints allow code execution; no article should demonstrate exposing one."""
    offenders = []
    for name, text in read_articles(docs).items():
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("-", ">", "#", "|")) or "don't" in stripped.lower() or "Don't" in stripped:
                continue
            if "type: LoadBalancer" in stripped:
                offenders.append(f"{name}: {stripped}")
            if "port-forward" in stripped and "--address" in stripped and "127.0.0.1" not in stripped:
                offenders.append(f"{name}: {stripped}")
    assert not offenders, f"Articles appear to expose a Ray endpoint: {offenders}"


def test_security_article_documents_the_submitter_label(docs):
    text = (docs / "ray-security.md").read_text(encoding="utf-8")
    assert "ray-submit: ray-t4" in text, "The article no longer explains the submitter label the policies require."


def test_autoscaling_article_warns_about_gpu_registration(docs):
    text = (docs / "ray-autoscaling.md").read_text(encoding="utf-8")
    assert "nvidia.com/gpu" in text, "The article should show how to confirm allocatable GPU capacity."


def test_quickstart_explains_the_shared_runtime_environment(docs):
    text = (docs / "ray-quickstart.md").read_text(encoding="utf-8")
    assert "runtime-environment definition" in text or "runtime_env" in text, (
        "The quickstart should explain why both scripts use the same runtime environment."
    )
