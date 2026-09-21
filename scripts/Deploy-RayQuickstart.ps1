<#
.SYNOPSIS
    Deploys the quickstart RayCluster: namespace, code ConfigMap, and cluster.
.DESCRIPTION
    Refuses to reuse an existing namespace so the sample can't overwrite another
    workload. Waits for allocatable GPU capacity before creating Ray pods, then
    waits for the RayCluster to report ready.
.EXAMPLE
    .\Deploy-RayQuickstart.ps1
.EXAMPLE
    .\Deploy-RayQuickstart.ps1 -Spot -Context my-aks
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Namespace = 'ray-learn',
    [string]$GpuPool = 'raygpu',
    [switch]$Spot,
    [string]$Context,
    [int]$ReadyTimeoutMinutes = 20
)

. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Tool -Name kubectl

$contextName = Get-KubeContext -Context $Context
Write-Step "Target context: $contextName, namespace: $Namespace"

if (Test-KubeObject -Kind namespace -Name $Namespace -Context $Context) {
    throw "Namespace '$Namespace' already exists. Choose an unused namespace and update the manifests together."
}

$quickstart = Get-SamplePath 'quickstart'
$manifest = Join-Path $quickstart 'raycluster-t4.yaml'

if ($Spot) {
    $patch = Join-Path $quickstart 'spot-worker.patch.json'
    $rendered = Join-Path ([IO.Path]::GetTempPath()) "raycluster-t4-spot-$([guid]::NewGuid().ToString('n')).yaml"
    Write-Step "Adding the Azure Spot toleration to the worker group."
    $yaml = Invoke-Checked -FilePath 'kubectl' -Arguments @(
        'patch', '--local', '-f', $manifest, '--type', 'json', '--patch-file', $patch, '-o', 'yaml'
    )
    Set-Content -LiteralPath $rendered -Value $yaml -Encoding utf8
    $manifest = $rendered
}

if (-not $PSCmdlet.ShouldProcess("$contextName/$Namespace", 'Create the quickstart Ray cluster')) { return }

Assert-AllocatableGpu -Pool $GpuPool -Context $Context | Out-Null

Write-Step "Creating namespace '$Namespace' and the code ConfigMap."
Invoke-Kubectl -Context $Context -Arguments @('create', 'namespace', $Namespace) | Out-Null
Invoke-Kubectl -Context $Context -Arguments @(
    '-n', $Namespace, 'create', 'configmap', 'ray-learning-code',
    "--from-file=$(Join-Path $quickstart 'gpu_check.py')",
    "--from-file=$(Join-Path $quickstart 'batch_inference.py')",
    "--from-file=$(Join-Path $quickstart 'requirements-inference.txt')"
) | Out-Null

Write-Step "Applying the RayCluster."
Invoke-Kubectl -Context $Context -Arguments @('-n', $Namespace, 'apply', '-f', $manifest) | Out-Null
Invoke-Kubectl -Context $Context -Arguments @(
    '-n', $Namespace, 'wait', 'raycluster/ray-t4',
    '--for=jsonpath={.status.state}=ready', "--timeout=${ReadyTimeoutMinutes}m"
) | Out-Null

Invoke-Kubectl -Context $Context -Arguments @('-n', $Namespace, 'get', 'pods', '-l', 'ray.io/cluster=ray-t4', '-o', 'wide')
Write-Step "Cluster ready. Run Invoke-RayQuickstartJobs.ps1 to prove the GPU path end to end."
