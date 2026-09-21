"""Shared fixtures for the Ray on AKS sample checks.

These tests are offline: they parse the samples, cross-check the values that
have to agree across files, and exercise the offline verifier. They can't prove
that a cluster works. `docs/validation-status.md` records what ran live.
"""

import json
import os
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = REPO_ROOT / "samples"
SCRIPTS = REPO_ROOT / "scripts"

RAY_VERSION = "2.58.0"
KUBERAY_VERSION = "1.7.0"
MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"
MODEL_REVISION = "7ae557604adf67be50417f59c2c2f167def9a775"
RAY_NAMESPACE = "ray-learn"
RAY_CLUSTER = "ray-t4"


def load_yaml_documents(path):
    """Every YAML document in a file, skipping empty ones."""
    with path.open(encoding="utf-8") as stream:
        return [document for document in yaml.safe_load_all(stream) if document]


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def iter_containers(pod_spec):
    yield from pod_spec.get("containers", [])
    yield from pod_spec.get("initContainers", [])


def apply_json_patch(document, operations):
    """Apply the JSON Patch subset the samples use: `test` and `add`.

    Raises AssertionError when a `test` operation doesn't match, which is the
    behavior the patches rely on to refuse the wrong input document.
    """
    result = json.loads(json.dumps(document))
    for operation in operations:
        kind = operation["op"]
        tokens = [token.replace("~1", "/").replace("~0", "~") for token in operation["path"].split("/")[1:]]
        target = result
        for token in tokens[:-1]:
            target = target[int(token)] if isinstance(target, list) else target[token]
        leaf = tokens[-1]
        if kind == "test":
            actual = target[int(leaf)] if isinstance(target, list) else target[leaf]
            assert actual == operation["value"], f"test failed at {operation['path']}: {actual!r}"
        elif kind == "add":
            if isinstance(target, list):
                if leaf == "-":
                    target.append(operation["value"])
                else:
                    target.insert(int(leaf), operation["value"])
            else:
                target[leaf] = operation["value"]
        else:
            raise AssertionError(f"Unsupported patch operation in the samples: {kind}")
    return result


@pytest.fixture(scope="session")
def repo_root():
    return REPO_ROOT


@pytest.fixture(scope="session")
def samples():
    return SAMPLES


@pytest.fixture(scope="session")
def manifest_paths():
    return sorted(SAMPLES.rglob("*.yaml"))


@pytest.fixture(scope="session")
def python_paths():
    return sorted(SAMPLES.rglob("*.py"))


@pytest.fixture(scope="session")
def quickstart_cluster():
    documents = load_yaml_documents(SAMPLES / "quickstart" / "raycluster-t4.yaml")
    assert len(documents) == 1
    return documents[0]


@pytest.fixture(scope="session")
def docs_root():
    """The draft articles, when they're available on this machine.

    Set RAY_ON_AKS_DOCS to the folder holding the article Markdown files to
    enable the doc synchronization checks.
    """
    configured = os.environ.get("RAY_ON_AKS_DOCS")
    if not configured:
        return None
    path = Path(configured)
    return path if path.is_dir() else None
