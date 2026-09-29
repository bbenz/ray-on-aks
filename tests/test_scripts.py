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


def test_cleanup_whatif_does_not_fail_on_the_pool_it_didnt_delete():
    """Under -WhatIf the pool is still listed afterwards; only a real deletion
    that left the pool behind is an error."""
    text = (SCRIPTS / "Remove-RayQuickstart.ps1").read_text(encoding="utf-8")
    assert "$deleted = $true" in text
    assert re.search(r"if \(\$deleted -and @\(\$pools \| Where-Object name -eq \$Pool\)\.Count -gt 0\)", text)


def test_gpu_pool_script_waits_for_allocatable_capacity():
    """A Ready node is not a usable GPU: the device plugin registers separately."""
    common = (SCRIPTS / "Common.ps1").read_text(encoding="utf-8")
    assert "nvidia.com/gpu" in common
    for name in ("Add-RayGpuNodePool.ps1", "Deploy-RayQuickstart.ps1"):
        assert "Assert-AllocatableGpu" in (SCRIPTS / name).read_text(encoding="utf-8"), (
            f"{name} creates or uses GPU capacity without waiting for it to register."
        )


def run_pwsh(script):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 isn't installed.")
    return subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=180,
    )


def test_status_fields_are_read_through_the_strict_mode_helper(scripts):
    """Strict mode throws on a property that doesn't exist yet. A new RayJob has
    no status.jobStatus, and a new GPU node has no allocatable nvidia.com/gpu, so
    direct reads crashed the scripts in exactly the states they wait through."""
    for path in scripts:
        text = path.read_text(encoding="utf-8")
        assert ".status.jobStatus" not in text, f"{path.name} reads RayJob status directly."
        assert ".status.jobDeploymentStatus" not in text, f"{path.name} reads RayJob status directly."
        assert ".allocatable.'nvidia.com/gpu'" not in text, f"{path.name} reads node GPU capacity directly."


def test_property_helper_returns_null_for_fields_not_written_yet():
    script = f"""
. '{SCRIPTS / "Common.ps1"}'
$new = '{{"metadata":{{"name":"ray-gpu-check"}}}}' | ConvertFrom-Json
$done = '{{"status":{{"jobStatus":"SUCCEEDED","allocatable":{{"nvidia.com/gpu":"1"}}}}}}' | ConvertFrom-Json
if ($null -ne (Get-PropertyValue $new @('status', 'jobStatus'))) {{ 'EXPECTED_NULL'; exit 1 }}
if ((Get-PropertyValue $done @('status', 'jobStatus')) -ne 'SUCCEEDED') {{ 'WRONG_VALUE'; exit 1 }}
if ((Get-PropertyValue $done @('status', 'allocatable', 'nvidia.com/gpu')) -ne '1') {{ 'WRONG_GPU'; exit 1 }}
'OK'
"""
    result = run_pwsh(script)
    assert result.returncode == 0 and result.stdout.strip().endswith("OK"), f"{result.stdout}{result.stderr}"


def test_checked_helper_ignores_whatif_for_its_own_temp_files():
    """Under -WhatIf the stderr redirect and temp-file cleanup were skipped:
    warnings vanished, temp files leaked, and every call printed noise."""
    script = f"""
$WhatIfPreference = $true
. '{SCRIPTS / "Common.ps1"}'
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) {{ 'SKIP'; exit 0 }}
$text = Invoke-Checked -FilePath $py -Arguments @('-c', 'print(42)')
"RESULT:$text"
"""
    result = run_pwsh(script)
    if result.stdout.strip() == "SKIP":
        pytest.skip("python isn't available to the PowerShell session.")
    assert "RESULT:42" in result.stdout, f"{result.stdout}{result.stderr}"
    assert "What if" not in result.stdout, f"Invoke-Checked still honors -WhatIf internally: {result.stdout}"


def test_protect_script_renders_the_manifest_even_under_whatif():
    """The patches chain through the rendered file, so skipping the write under
    -WhatIf made the second patch fail on a file that was never created."""
    text = (SCRIPTS / "Protect-RayCluster.ps1").read_text(encoding="utf-8")
    assert re.search(r"Set-Content -LiteralPath \$rendered .*-WhatIf:\$false", text)


def test_results_copy_uses_a_relative_destination():
    """kubectl cp reads C:\\... as pod:path, so an absolute Windows path fails."""
    text = (SCRIPTS / "Invoke-RayQuickstartJobs.ps1").read_text(encoding="utf-8")
    assert "Push-Location -LiteralPath $OutputDirectory" in text
    copy = re.search(r"'cp', '-c', 'ray-head', \"\$\{head\}:/tmp/ray-inference-results.jsonl\", (\$\w+)\)", text)
    assert copy and copy[1] == "$fileName", "Copy results to a relative file name inside the output directory."


def test_install_script_distinguishes_leftover_crds_from_a_running_operator():
    """helm uninstall keeps CRDs, and the docs say not to delete them, so CRDs
    alone mustn't block a reinstall. A running operator without a release must."""
    text = (SCRIPTS / "Install-KubeRay.ps1").read_text(encoding="utf-8")
    assert "kuberay[/-]operator" in text, "Look for a running operator, not just the CRDs."
    assert "already running without a Helm release" in text


def test_name_suffix_does_not_need_a_leading_dash():
    """Through 'pwsh -File', a value like -sec binds as the -Secured switch."""
    text = (SCRIPTS / "Invoke-RayQuickstartJobs.ps1").read_text(encoding="utf-8")
    assert "$NameSuffix = \"-$NameSuffix\"" in text


def test_gpu_pool_script_checks_the_aks_preview_version():
    """--gpu-driver-mode only exists in aks-preview 22.0.0b6 and later."""
    text = (SCRIPTS / "Add-RayGpuNodePool.ps1").read_text(encoding="utf-8")
    assert "--gpu-driver-mode" in text
    assert "22.0.0b6" in text, "State and check the aks-preview version the flag needs."


def test_extension_versions_compare_betas_below_releases():
    script = f"""
. '{SCRIPTS / "Common.ps1"}'
$cases = @(
    @('21.0.0b7', '22.0.0b6', -1), @('22.0.0b6', '22.0.0b6', 0), @('22.0.0b9', '22.0.0b6', 1),
    @('22.0.0', '22.0.0b9', 1), @('22.1.0b1', '22.0.0', 1), @('19.0.0b29', '22.0.0b6', -1)
)
foreach ($case in $cases) {{
    $actual = Compare-ExtensionVersion $case[0] $case[1]
    if ($actual -ne $case[2]) {{ "MISMATCH $($case[0]) vs $($case[1]): $actual"; exit 1 }}
}}
'OK'
"""
    result = run_pwsh(script)
    assert result.returncode == 0 and result.stdout.strip().endswith("OK"), f"{result.stdout}{result.stderr}"
