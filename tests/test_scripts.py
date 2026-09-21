"""Checks on the PowerShell helpers in scripts/.

The syntax check needs PowerShell 7 (`pwsh`) and is skipped when it isn't
installed. The rest are plain text checks that run anywhere.
"""

import re
import shutil
import subprocess

import pytest
from conftest import SCRIPTS

MUTATING = {
    "Add-RayGpuNodePool.ps1",
    "Deploy-RayQuickstart.ps1",
    "Install-KubeRay.ps1",
    "Invoke-RayQuickstartJobs.ps1",
    "Protect-RayCluster.ps1",
    "Remove-RayQuickstart.ps1",
}

GUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)


@pytest.fixture(scope="module")
def scripts():
    found = sorted(SCRIPTS.glob("*.ps1"))
    assert found, "No PowerShell scripts were found."
    return found


def test_scripts_parse(scripts):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 isn't installed.")
    for path in scripts:
        command = (
            "$e=$null; $t=$null; "
            f"[System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$t, [ref]$e) > $null; "
            "if ($e.Count) { $e[0].Message; exit 1 }"
        )
        result = subprocess.run(
            [pwsh, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, f"{path.name} failed to parse: {result.stdout}{result.stderr}"


def test_every_script_documents_itself(scripts):
    for path in scripts:
        text = path.read_text(encoding="utf-8")
        assert ".SYNOPSIS" in text, f"{path.name} has no comment-based help."
        if path.name in MUTATING:
            assert ".EXAMPLE" in text, f"{path.name} shows no example invocation."


def test_mutating_scripts_support_whatif(scripts):
    for path in scripts:
        if path.name not in MUTATING:
            continue
        text = path.read_text(encoding="utf-8")
        assert "SupportsShouldProcess" in text, f"{path.name} changes state without -WhatIf support."
        assert "ShouldProcess(" in text, f"{path.name} declares SupportsShouldProcess but never calls it."


def test_scripts_fail_on_native_command_errors(scripts):
    """PowerShell doesn't stop on a nonzero exit code from az or kubectl."""
    common = (SCRIPTS / "Common.ps1").read_text(encoding="utf-8")
    assert "$exitCode -ne 0" in common, "Common.ps1 doesn't check native exit codes."
    for path in scripts:
        if path.name == "Common.ps1":
            continue
        text = path.read_text(encoding="utf-8")
        assert "Common.ps1" in text, f"{path.name} doesn't dot-source the checked-execution helpers."


def test_invoke_checked_separates_stderr_and_fails_loudly():
    """A tool warning on stderr must not end up inside captured YAML or JSON,
    and a nonzero exit must raise instead of returning partial output."""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 isn't installed.")
    script = f"""
. '{SCRIPTS / "Common.ps1"}'
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) {{ 'SKIP'; exit 0 }}
$text = Invoke-Checked -FilePath $py -Arguments @('-c', 'import sys; sys.stderr.write("noise\\n"); print("apiVersion: v1")') -WarningAction SilentlyContinue
if ($text -ne 'apiVersion: v1') {{ "STDOUT_POLLUTED:$text"; exit 1 }}
try {{
    Invoke-Checked -FilePath $py -Arguments @('-c', 'import sys; sys.exit(3)') -WarningAction SilentlyContinue | Out-Null
    'NO_THROW_ON_FAILURE'; exit 1
}} catch {{ }}
'OK'
"""
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=180,
    )
    output = result.stdout.strip()
    if output == "SKIP":
        pytest.skip("python isn't available to the PowerShell session.")
    assert result.returncode == 0 and output.endswith("OK"), f"{output}{result.stderr}"


def test_scripts_carry_no_environment_specific_identifiers(scripts):
    """These are samples: no subscription, tenant, cluster, or resource group baked in."""
    banned = ("bbenz-ray", "4498459e", "westus3", "pocSession", "a18443c2")
    for path in scripts:
        text = path.read_text(encoding="utf-8")
        for needle in banned:
            assert needle not in text, f"{path.name} contains an environment-specific value: {needle}"
        assert not GUID.search(text), f"{path.name} contains a hardcoded GUID."


def test_scripts_resolve_samples_through_the_repository(scripts):
    """No absolute paths: the scripts have to work from any clone location."""
    for path in scripts:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"[A-Za-z]:\\\\?[A-Za-z]", text), f"{path.name} contains an absolute Windows path."
        if path.name in {"Deploy-RayQuickstart.ps1", "Protect-RayCluster.ps1", "Invoke-RayQuickstartJobs.ps1"}:
            assert "Get-SamplePath" in text, f"{path.name} doesn't resolve samples through the shared helper."


def test_cleanup_script_explains_what_still_costs_money():
    text = (SCRIPTS / "Remove-RayQuickstart.ps1").read_text(encoding="utf-8")
    assert "node pool" in text.lower()
    assert "bill" in text.lower(), "Cleanup should say plainly that GPU nodes keep billing."


def test_gpu_pool_script_waits_for_allocatable_capacity():
    """A Ready node is not a usable GPU: the device plugin registers separately."""
    common = (SCRIPTS / "Common.ps1").read_text(encoding="utf-8")
    assert "nvidia.com/gpu" in common
    for name in ("Add-RayGpuNodePool.ps1", "Deploy-RayQuickstart.ps1"):
        assert "Assert-AllocatableGpu" in (SCRIPTS / name).read_text(encoding="utf-8"), (
            f"{name} creates or uses GPU capacity without waiting for it to register."
        )
