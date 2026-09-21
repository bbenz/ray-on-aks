"""Verify a recorded Ray autoscaling run offline.

Reads the burst driver log and the in-cluster observer log, then asserts that a
real scale-out and scale-in cycle happened: N distinct Ray workers each ran a
bounded GPU task with the pinned runtime, and the observed Ready GPU node and
Ready Ray worker counts went 1 -> N -> 1.

Expected package versions and the model revision are derived from
worker-runtime-env.json and burst.py, so this checker can't drift from the
samples it verifies.
"""

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*m")
EVENT = re.compile(r'(\{.*"event"\s*:.*\})$')
TORCH_WHEEL = re.compile(r"torch-(?P<version>[0-9][^-]*)-cp\d+")


class VerificationError(Exception):
    """A recorded run did not demonstrate what the sample claims."""


def read_events(path):
    events = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        match = EVENT.search(ANSI.sub("", line))
        if not match:
            continue
        try:
            events.append(json.loads(match[1]))
        except json.JSONDecodeError as error:
            raise VerificationError(f"{path.name}:{number} has an unreadable event: {error}") from error
    if not events:
        raise VerificationError(f"{path.name} contains no events.")
    return events


def expected_versions(runtime_env_path):
    runtime_env = json.loads(runtime_env_path.read_text(encoding="utf-8"))
    versions = {}
    for package in runtime_env["pip"]["packages"]:
        pinned = re.fullmatch(r"([A-Za-z0-9._-]+)==([^\s;]+)", package)
        if pinned:
            versions[pinned[1].lower()] = pinned[2]
            continue
        wheel = TORCH_WHEEL.search(package)
        if wheel:
            versions["torch"] = wheel["version"].replace("%2B", "+")
    missing = {"torch", "transformers"} - versions.keys()
    if missing:
        raise VerificationError(f"{runtime_env_path.name} pins no version for: {sorted(missing)}")
    return versions


def expected_model(burst_path):
    tree = ast.parse(burst_path.read_text(encoding="utf-8"))
    constants = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant)
    }
    try:
        return constants["MODEL_ID"], constants["MODEL_REVISION"]
    except KeyError as error:
        raise VerificationError(f"{burst_path.name} no longer pins {error}.") from error


def check_burst(events, tasks, duration_seconds, versions, model):
    submitted = [event for event in events if event["event"] == "burst_submitted"]
    if len(submitted) != 1:
        raise VerificationError("Expected exactly one burst_submitted event.")
    if submitted[0]["tasks"] != tasks or submitted[0]["duration_seconds"] != duration_seconds:
        raise VerificationError(
            "The recorded submission doesn't match the requested tasks and window: "
            f"{submitted[0]['tasks']} tasks, {submitted[0]['duration_seconds']}s."
        )

    succeeded = [event for event in events if event["event"] == "burst_succeeded"]
    if len(succeeded) != 1 or succeeded[0]["completed"] != tasks:
        raise VerificationError(f"The burst didn't report {tasks} successful tasks.")

    results = [event for event in events if event["event"] == "result_received"]
    if sorted(event["task_index"] for event in results) != list(range(tasks)):
        raise VerificationError("Expected exactly one successful result per task index.")
    if len({event["pod"] for event in results}) != tasks:
        raise VerificationError("The tasks didn't run on distinct worker pods.")
    if len({event["node_id"] for event in results}) != tasks:
        raise VerificationError("The tasks didn't run on distinct Ray nodes.")

    model_id, model_revision = model
    for result in results:
        attempt = result["attempt_id"]
        runtime = one(events, "runtime_ready", attempt)
        loaded = one(events, "model_load_started", attempt)
        if "T4" not in runtime["gpu"]:
            raise VerificationError(f"Task {result['task_index']} didn't run on the expected GPU model.")
        compare_versions(runtime, versions, result["task_index"])
        if loaded["model"] != model_id or loaded["revision"] != model_revision:
            raise VerificationError(f"Task {result['task_index']} didn't use the pinned model revision.")
        if result["generations"] < 1 or result["generated_tokens"] < 1:
            raise VerificationError(f"Task {result['task_index']} generated nothing.")
        if result["inference_window_seconds"] < duration_seconds:
            raise VerificationError(
                f"Task {result['task_index']} ran {result['inference_window_seconds']:.1f}s, "
                f"short of the requested {duration_seconds}s."
            )
        if not str(result.get("first_text", "")).strip():
            raise VerificationError(f"Task {result['task_index']} recorded no generated text.")
    return results


def one(events, name, attempt):
    matches = [event for event in events if event["event"] == name and event["attempt_id"] == attempt]
    if len(matches) != 1:
        raise VerificationError(f"Expected exactly one {name} event for attempt {attempt}.")
    return matches[0]


def compare_versions(runtime, versions, task_index):
    observed = {
        "torch": runtime.get("torch_version"),
        "transformers": runtime.get("transformers_version"),
        "pydantic": runtime.get("pydantic_version"),
    }
    for name, expected in versions.items():
        actual = observed.get(name)
        if actual is None:
            actual = next(
                (
                    item.split("==", 1)[1]
                    for item in runtime.get("resolved_packages", [])
                    if item.lower().startswith(f"{name}==")
                ),
                None,
            )
        if actual is None:
            continue
        if actual != expected:
            raise VerificationError(
                f"Task {task_index} ran {name} {actual}; the sample pins {expected}."
            )


def check_observer(events, tasks):
    cycles = [event for event in events if event["event"] == "scale_cycle_observed"]
    if len(cycles) != 1:
        raise VerificationError("The observer didn't report exactly one complete scale cycle.")
    cycle = cycles[0]
    expected = {
        "initial_one_worker_observed": True,
        "peak_ready_gpu_nodes": tasks,
        "peak_ready_ray_workers": tasks,
        "final_ready_gpu_nodes": 1,
        "final_ready_ray_workers": 1,
    }
    wrong = {key: cycle.get(key) for key, value in expected.items() if cycle.get(key) != value}
    if wrong:
        raise VerificationError(f"The observed trajectory wasn't 1 -> {tasks} -> 1: {wrong}")

    snapshots = [event for event in events if event["event"] == "scale_snapshot"]
    if not snapshots:
        raise VerificationError("The observer recorded no snapshots.")
    peak_nodes = max(event["gpu_nodes"] for event in snapshots)
    if peak_nodes > tasks:
        raise VerificationError(f"The run exceeded its {tasks}-node bound: {peak_nodes} GPU nodes.")
    return cycle, snapshots, peak_nodes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--burst-log", type=Path, required=True)
    parser.add_argument("--observer-log", type=Path, required=True)
    parser.add_argument("--tasks", type=int, default=4)
    parser.add_argument("--duration-seconds", type=int, required=True)
    parser.add_argument("--samples-dir", type=Path, default=Path(__file__).parent)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    versions = expected_versions(args.samples_dir / "worker-runtime-env.json")
    model = expected_model(args.samples_dir / "burst.py")
    results = check_burst(read_events(args.burst_log), args.tasks, args.duration_seconds, versions, model)
    cycle, snapshots, peak_nodes = check_observer(read_events(args.observer_log), args.tasks)

    summary = {
        "verified": True,
        "tasks": args.tasks,
        "requested_window_seconds": args.duration_seconds,
        "model": {"id": model[0], "revision": model[1]},
        "pinned_versions": versions,
        "workers": sorted(event["pod"] for event in results),
        "generations": sum(event["generations"] for event in results),
        "generated_tokens": sum(event["generated_tokens"] for event in results),
        "observed_cycle": cycle,
        "observer_snapshots": len(snapshots),
        "maximum_observed_gpu_nodes": peak_nodes,
        "caveat": "A repeated prompt with a fixed token cap. Not a throughput or quality benchmark.",
    }
    text = json.dumps(summary, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except VerificationError as error:
        print(f"Verification failed: {error}", file=sys.stderr)
        sys.exit(1)
