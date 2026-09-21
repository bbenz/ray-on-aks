<#
.SYNOPSIS
    Installs the pinned KubeRay operator chart, or reports the existing one.
.DESCRIPTION
    Refuses to install a second operator over an existing KubeRay release.
    Sharing one operator is normal: confirm with its owner that the installed
    version and watched namespaces cover your namespace instead of installing
    another copy.
.EXAMPLE
    .\Install-KubeRay.ps1
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Version = '1.7.0',
    [string]$Namespace = 'kuberay-system',
    [string]$ReleaseName = 'kuberay-operator',
    [string]$Context
)

. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Tool -Name helm, kubectl

$contextName = Get-KubeContext -Context $Context
Write-Step "Target context: $contextName"

$helmCommon = @()
if ($Context) { $helmCommon = @('--kube-context', $Context) }

$releases = Invoke-Checked -FilePath 'helm' -AsJson -Arguments (@('list', '--all-namespaces', '-o', 'json') + $helmCommon)
$existing = @($releases | Where-Object { $_.chart -like 'kuberay-operator-*' })
if ($existing.Count -gt 0) {
    foreach ($release in $existing) {
        Write-Host "Existing KubeRay release: $($release.name) in $($release.namespace), chart $($release.chart), status $($release.status)."
    }
    Write-Step "Leaving the existing operator alone. Confirm with its owner that it watches your namespace."
    return
}

$crds = & kubectl @(@('api-resources', '--api-group=ray.io', '-o', 'name') + @(if ($Context) { @('--context', $Context) } else { @() })) 2>$null
if (-not [string]::IsNullOrWhiteSpace(($crds | Out-String))) {
    throw "ray.io resource types already exist without a Helm release. Inspect that installation before adding another."
}

if (-not $PSCmdlet.ShouldProcess($contextName, "Install kuberay-operator $Version into $Namespace")) { return }

Invoke-Checked -FilePath 'helm' -Arguments @('repo', 'add', 'kuberay', 'https://ray-project.github.io/kuberay-helm/') | Out-Null
Invoke-Checked -FilePath 'helm' -Arguments @('repo', 'update', 'kuberay') | Out-Null
Invoke-Checked -FilePath 'helm' -Arguments (@(
        'install', $ReleaseName, 'kuberay/kuberay-operator', '--version', $Version,
        '--namespace', $Namespace, '--create-namespace', '--wait', '--timeout', '10m'
    ) + $helmCommon) | Out-Null

Write-Step "Installed kuberay-operator $Version."
Invoke-Kubectl -Context $Context -Arguments @('-n', $Namespace, 'get', 'deployments,pods')
