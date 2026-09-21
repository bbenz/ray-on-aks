"""Checks for the offline autoscaling verifier, using recorded event fixtures.

The fixtures keep the event shapes from a real four-worker run; node names, pod
names, Ray node IDs, and Azure resource IDs are replaced with placeholders.
"""

import importlib.util
import json

import pytest
from conftest import SAMPLES

FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"
BURST = FIXTURES / "burst-pass.log"
OBSERVER = FIXTURES / "observer-pass.jsonl"


def load_verifier():
    path = SAMPLES / "autoscaling" / "verify_scaling.py"
    spec = importlib.util.spec_from_file_location("verify_scaling", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def verifier():
    return load_verifier()


@pytest.fixture
def burst_events(verifier):
    return verifier.read_events(BURST)


@pytest.fixture
def observer_events(verifier):
    return verifier.read_events(OBSERVER)


def write_events(tmp_path, name, events):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n", encoding="utf-8")
    return path


def test_recorded_run_passes(verifier, capsys):
    assert verifier.main(
        ["--burst-log", str(BURST), "--observer-log", str(OBSERVER), "--tasks", "4", "--duration-seconds", "1800"]
    ) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["verified"] is True
    assert len(summary["workers"]) == 4
    assert summary["maximum_observed_gpu_nodes"] == 4
    assert summary["observed_cycle"]["peak_ready_ray_workers"] == 4
    assert summary["model"]["revision"] == "7ae557604adf67be50417f59c2c2f167def9a775"


def test_expected_versions_come_from_the_runtime_environment_sample(verifier):
    versions = verifier.expected_versions(SAMPLES / "autoscaling" / "worker-runtime-env.json")
    assert versions["torch"].endswith("+cu128"), "The CUDA build should be visible in the pinned wheel."
    assert versions["transformers"]
    assert versions["pydantic"], "The pydantic pin that fixed the pip_check failure is missing."


def test_verifier_reads_decorated_and_plain_lines(burst_events):
    """The driver log interleaves plain JSON with Ray CLI prefixes."""
    assert len(burst_events) >= 14
    assert {event["event"] for event in burst_events} >= {
        "burst_submitted",
        "runtime_ready",
        "model_load_started",
        "result_received",
        "burst_succeeded",
    }


def test_tasks_sharing_a_worker_are_rejected(verifier, burst_events, tmp_path):
    """Four results on three pods is the failure mode that looked like success."""
    events = [dict(event) for event in burst_events]
    pods = [event for event in events if event["event"] == "result_received"]
    pods[1]["pod"] = pods[0]["pod"]
    path = write_events(tmp_path, "burst.log", events)
    with pytest.raises(verifier.VerificationError, match="distinct worker pods"):
        verifier.check_burst(
            verifier.read_events(path), 4, 1800,
            verifier.expected_versions(SAMPLES / "autoscaling" / "worker-runtime-env.json"),
            verifier.expected_model(SAMPLES / "autoscaling" / "burst.py"),
        )


def test_short_inference_window_is_rejected(verifier, burst_events, tmp_path):
    events = [dict(event) for event in burst_events]
    for event in events:
        if event["event"] == "result_received":
            event["inference_window_seconds"] = 12.5
            break
    path = write_events(tmp_path, "burst.log", events)
    with pytest.raises(verifier.VerificationError, match="short of the requested"):
        verifier.check_burst(
            verifier.read_events(path), 4, 1800,
            verifier.expected_versions(SAMPLES / "autoscaling" / "worker-runtime-env.json"),
            verifier.expected_model(SAMPLES / "autoscaling" / "burst.py"),
        )


def test_unpinned_package_version_is_rejected(verifier, burst_events, tmp_path):
    events = [dict(event) for event in burst_events]
    for event in events:
        if event["event"] == "runtime_ready":
            event["transformers_version"] = "4.99.0"
            break
    path = write_events(tmp_path, "burst.log", events)
    with pytest.raises(verifier.VerificationError, match="the sample pins"):
        verifier.check_burst(
            verifier.read_events(path), 4, 1800,
            verifier.expected_versions(SAMPLES / "autoscaling" / "worker-runtime-env.json"),
            verifier.expected_model(SAMPLES / "autoscaling" / "burst.py"),
        )


def test_missing_scale_in_is_rejected(verifier, observer_events, tmp_path):
    """Scaling out without scaling back in is a cost problem, not a success."""
    events = [event for event in observer_events if event["event"] != "scale_cycle_observed"]
    path = write_events(tmp_path, "observer.jsonl", events)
    with pytest.raises(verifier.VerificationError, match="one complete scale cycle"):
        verifier.check_observer(verifier.read_events(path), 4)


def test_exceeding_the_node_bound_is_rejected(verifier, observer_events, tmp_path):
    events = [dict(event) for event in observer_events]
    for event in events:
        if event["event"] == "scale_snapshot":
            event["gpu_nodes"] = 9
            break
    path = write_events(tmp_path, "observer.jsonl", events)
    with pytest.raises(verifier.VerificationError, match="exceeded its 4-node bound"):
        verifier.check_observer(verifier.read_events(path), 4)


def test_empty_log_is_rejected(verifier, tmp_path):
    path = tmp_path / "empty.log"
    path.write_text("no events here\n", encoding="utf-8")
    with pytest.raises(verifier.VerificationError, match="no events"):
        verifier.read_events(path)


def test_fixtures_carry_no_real_identifiers():
    for path in (BURST, OBSERVER):
        text = path.read_text(encoding="utf-8")
        for needle in ("bbenz", "4498459e", "westus3", "a18443c2", "hcp.", "azmk8s.io"):
            assert needle not in text, f"{path.name} leaks '{needle}'."
