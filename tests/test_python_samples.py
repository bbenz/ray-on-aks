"""Checks on the Python samples the articles ask readers to run."""

import ast

import pytest
from conftest import MODEL_ID, MODEL_REVISION, SAMPLES


def parse(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def find_call(tree, attribute, value_id):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == attribute
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == value_id
        ):
            yield node


def keyword_of(call, name):
    return next((keyword.value for keyword in call.keywords if keyword.arg == name), None)


def constants(tree):
    return {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant)
    }


def test_every_python_sample_parses(python_paths):
    assert python_paths, "No Python samples were found."
    for path in python_paths:
        parse(path)


def test_gpu_check_and_inference_share_one_runtime_environment():
    """Two runtime environments that differ only in env_vars still resolve to the
    same pip URI. Ray 2.58 accounts for that URI's size once per variant, which
    can evict the shared virtual environment out from under a rejoining worker.
    Keeping the definitions identical is the fix that was validated live.
    """
    environments = {}
    for name in ("gpu_check.py", "batch_inference.py"):
        tree = parse(SAMPLES / "quickstart" / name)
        calls = list(find_call(tree, "init", "ray"))
        assert len(calls) == 1, f"{name} should call ray.init exactly once."
        runtime_env = keyword_of(calls[0], "runtime_env")
        assert runtime_env is not None, f"{name} calls ray.init without a runtime_env."
        environments[name] = ast.dump(runtime_env)

    assert environments["gpu_check.py"] == environments["batch_inference.py"], (
        "gpu_check.py and batch_inference.py must pass identical runtime_env definitions, "
        "including env_vars. See docs/known-issues.md."
    )


def test_quickstart_scripts_pin_dependencies_to_the_requirements_file():
    requirements = SAMPLES / "quickstart" / "requirements-inference.txt"
    assert requirements.exists()
    pins = [line.strip() for line in requirements.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert pins, "requirements-inference.txt is empty."
    for pin in pins:
        assert "==" in pin, f"Dependency '{pin}' isn't pinned to an exact version."

    for name in ("gpu_check.py", "batch_inference.py"):
        source = (SAMPLES / "quickstart" / name).read_text(encoding="utf-8")
        assert "requirements-inference.txt" in source, f"{name} doesn't reference the pinned requirements file."


def test_inference_sample_pins_the_model_revision_and_refuses_remote_code():
    tree = parse(SAMPLES / "quickstart" / "batch_inference.py")
    values = constants(tree)
    assert values.get("MODEL_ID") == MODEL_ID
    assert values.get("MODEL_REVISION") == MODEL_REVISION

    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "trust_remote_code":
            assert node.value.value is False, "Model loading must not execute code from the model repository."
        if isinstance(node, ast.keyword) and node.arg == "revision":
            assert isinstance(node.value, ast.Name) and node.value.id == "MODEL_REVISION"


def test_remote_waits_are_bounded():
    """A hung actor shouldn't hold a GPU node until someone notices the bill."""
    for name in ("gpu_check.py", "batch_inference.py"):
        tree = parse(SAMPLES / "quickstart" / name)
        gets = list(find_call(tree, "get", "ray"))
        assert gets, f"{name} never calls ray.get."
        for call in gets:
            timeout = keyword_of(call, "timeout")
            assert timeout is not None, f"{name} calls ray.get without a timeout."
            assert isinstance(timeout, ast.Constant) and timeout.value > 0


def test_inference_actor_requests_exactly_one_gpu():
    tree = parse(SAMPLES / "quickstart" / "batch_inference.py")
    decorators = [
        node
        for klass in ast.walk(tree)
        if isinstance(klass, ast.ClassDef)
        for node in klass.decorator_list
        if isinstance(node, ast.Call)
    ]
    assert decorators, "The inference sample defines no Ray actor."
    requested = {keyword.arg: keyword.value.value for keyword in decorators[0].keywords}
    assert requested.get("num_gpus") == 1
    assert requested.get("num_cpus", 1) >= 1


def test_gpu_check_returns_plain_types():
    """torch.__version__ is a str subclass; returning it makes a CPU-only driver
    import torch while unpickling. Returning str(...) is the validated fix."""
    source = (SAMPLES / "quickstart" / "gpu_check.py").read_text(encoding="utf-8")
    assert "str(torch.__version__)" in source, "Convert the torch version to a built-in str before returning it."


def test_burst_arguments_are_bounded():
    """The burst script must refuse to run an unbounded GPU job."""
    tree = parse(SAMPLES / "autoscaling" / "burst.py")
    source = (SAMPLES / "autoscaling" / "burst.py").read_text(encoding="utf-8")
    added = {
        call.args[0].value
        for call in find_call(tree, "add_argument", "parser")
        if call.args and isinstance(call.args[0], ast.Constant)
    }
    for option in ("--tasks", "--duration-seconds", "--timeout-seconds", "--max-new-tokens"):
        assert option in added, f"burst.py no longer accepts {option}."
    assert "parser.error" in source, "burst.py doesn't validate its arguments."
    assert "ray.cancel" in source, "burst.py doesn't cancel unfinished tasks when it exits."


def test_burst_tasks_request_a_whole_gpu():
    """Ray scales on logical demand: the request is what triggers scale-out."""
    tree = parse(SAMPLES / "autoscaling" / "burst.py")
    decorators = [
        node
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef)
        for node in function.decorator_list
        if isinstance(node, ast.Call)
    ]
    assert decorators, "burst.py defines no Ray task."
    requested = {keyword.arg: keyword.value.value for keyword in decorators[0].keywords}
    assert requested.get("num_gpus") == 1
    assert requested.get("max_calls") == 1, "Reusing a worker process across generations hides fresh-start failures."


def test_observer_and_probes_use_only_the_standard_library():
    """These run inside the cluster with no runtime environment installed."""
    for path in (SAMPLES / "autoscaling" / "observe_scaling.py", SAMPLES / "security" / "security_probe.py"):
        tree = parse(path)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        assert not imported - set(__import__("sys").stdlib_module_names), (
            f"{path.name} imports a third-party package: {sorted(imported - set(__import__('sys').stdlib_module_names))}"
        )


@pytest.mark.parametrize("name", ["check_ray_auth.py", "check_proxy.py", "security_probe.py"])
def test_security_probes_fail_when_a_control_does_not_hold(name):
    """A probe that prints a result on the denied path would be useless."""
    source = (SAMPLES / "security" / name).read_text(encoding="utf-8")
    assert "raise RuntimeError" in source, f"{name} doesn't fail when an expected denial doesn't happen."


def test_no_sample_prints_a_token_or_hardcodes_a_secret(python_paths):
    banned = ("RAY_AUTH_TOKEN", "Bearer sk-", "password=", "client_secret")
    for path in python_paths:
        text = path.read_text(encoding="utf-8")
        for needle in banned:
            assert needle not in text, f"{path.name} appears to handle a credential directly: {needle}"
