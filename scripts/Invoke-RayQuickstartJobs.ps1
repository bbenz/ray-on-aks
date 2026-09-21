<#
.SYNOPSIS
    Runs the quickstart RayJobs and verifies their actual output.
.DESCRIPTION
    Submits the GPU check and, unless -SkipInference is set, the batch inference
    job. Polls for a terminal state, fails fast on FAILED, then checks the real
    results: a T4-class device with tensor_sum 10.0, and four inference records
    carrying the pinned model revision. Copies the JSON Lines output locally,
    because it only lives on the head pod's ephemeral storage.

    Use -Secured when the cluster is protected by the network policies in
    samples/security: submitter pods then need the ray-submit label.
.EXAMPLE
    .\Invoke-RayQuickstartJobs.ps1 -OutputDirectory .\out
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Namespace = 'ray-learn',
    [string]$Context,
    [switch]$Secured,
    [switch]$SkipInference,
    [string]$NameSuffix = '',
    [string]$OutputDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) 'out'),
    [int]$TimeoutMinutes = 30
)

. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Tool -Name kubectl

if ($NameSuffix -and $NameSuffix -notmatch '^-[a-z0-9]([-a-z0-9]{0,30}[a-z0-9])?$') {
    throw "NameSuffix must start with '-' and stay DNS-compatible."
}

$quickstart = Get-SamplePath 'quickstart'
$labelPatch = Get-SamplePath 'security/rayjob-submitter-label.patch.json'

function Submit-Job {
    param([string]$File, [string]$JobName)

    $manifest = Join-Path $quickstart $File
    $document = Invoke-Checked -FilePath 'kubectl' -Arguments @('create', '--dry-run=client', '-f', $manifest, '-o', 'json') | ConvertFrom-Json
    if ($Secured) {
        $document = Invoke-Checked -FilePath 'kubectl' -Arguments @(
            'patch', '--local', '-f', $manifest, '--type', 'json', '--patch-file', $labelPatch, '-o', 'json'
        ) | ConvertFrom-Json
    }
    $document.metadata.name = $JobName
    $document.metadata | Add-Member -NotePropertyName namespace -NotePropertyValue $Namespace -Force
    $rendered = Join-Path ([IO.Path]::GetTempPath()) "$JobName-$([guid]::NewGuid().ToString('n')).json"
    Set-Content -LiteralPath $rendered -Value ($document | ConvertTo-Json -Depth 40) -Encoding utf8

    Write-Step "Submitting RayJob '$JobName'."
    Invoke-Kubectl -Context $Context -Arguments @('create', '-f', $rendered) | Out-Null
    Remove-Item -LiteralPath $rendered -Force

    $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    $previous = ''
    while ((Get-Date) -lt $deadline) {
        $job = Invoke-Kubectl -Context $Context -AsJson -Arguments @('-n', $Namespace, 'get', 'rayjob', $JobName, '-o', 'json')
        $status = "$($job.status.jobStatus)/$($job.status.jobDeploymentStatus)"
        if ($status -ne $previous) { Write-Host "  $JobName : $status"; $previous = $status }
        if ($job.status.jobStatus -in @('FAILED', 'STOPPED') -or $job.status.jobDeploymentStatus -eq 'Failed') {
            Invoke-Kubectl -Context $Context -Arguments @('-n', $Namespace, 'logs', "-l", "job-name=$JobName", '--all-containers=true', '--tail=-1')
            throw "RayJob '$JobName' failed. Inspect the logs above and the RayJob status; don't rerun over the evidence."
        }
        if ($job.status.jobStatus -eq 'SUCCEEDED') {
            return (Invoke-Kubectl -Context $Context -Arguments @(
                    '-n', $Namespace, 'logs', "-l", "job-name=$JobName", '--all-containers=true', '--tail=-1'))
        }
        Start-Sleep -Seconds 15
    }
    throw "RayJob '$JobName' didn't reach a terminal state within $TimeoutMinutes minutes."
}

if (-not (Test-KubeObject -Kind raycluster -Name 'ray-t4' -Namespace $Namespace -Context $Context)) {
    throw "RayCluster 'ray-t4' not found in namespace '$Namespace'. Deploy it first."
}
if (-not $PSCmdlet.ShouldProcess("$Namespace/ray-t4", 'Submit the quickstart RayJobs')) { return }

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

$logs = Submit-Job -File 'rayjob-gpu-check.yaml' -JobName "ray-gpu-check$NameSuffix"
$gpuLine = @($logs -split "`n" | Where-Object { $_ -match '"tensor_sum"' }) | Select-Object -Last 1
if (-not $gpuLine) { throw 'The GPU check succeeded but printed no result record.' }
$gpu = ($gpuLine -replace '^[^{]*', '') | ConvertFrom-Json
if ($gpu.tensor_sum -ne 10.0) { throw "The GPU check returned tensor_sum $($gpu.tensor_sum); expected 10.0." }
Write-Step "GPU check passed on $($gpu.gpu): CUDA $($gpu.cuda_runtime), PyTorch $($gpu.torch_version)."

if ($SkipInference) { return }

Submit-Job -File 'rayjob-inference.yaml' -JobName "ray-inference$NameSuffix" | Out-Null
$head = Invoke-Kubectl -Context $Context -Arguments @(
    '-n', $Namespace, 'get', 'pods', '-l', 'ray.io/cluster=ray-t4,ray.io/node-type=head',
    '-o', 'jsonpath={.items[0].metadata.name}')
if (-not $head) { throw 'No Ray head pod was found; the results file could not be copied.' }

$destination = Join-Path $OutputDirectory "ray-inference-results$NameSuffix.jsonl"
Write-Step "Copying results from $head to $destination."
Invoke-Kubectl -Context $Context -Arguments @(
    '-n', $Namespace, 'cp', '-c', 'ray-head', "${head}:/tmp/ray-inference-results.jsonl", $destination) | Out-Null

$records = @(Get-Content -LiteralPath $destination | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json })
if ($records.Count -ne 4) { throw "Expected four inference records; found $($records.Count)." }
$revisions = @($records.model_revision | Sort-Object -Unique)
if ($revisions.Count -ne 1) { throw "The records don't share one model revision: $($revisions -join ', ')" }
$records | Select-Object id, gpu, model_revision, summary | Format-Table -Wrap
Write-Step "Four records written to $destination. Review the summaries: a small model can still state something the input didn't say."
