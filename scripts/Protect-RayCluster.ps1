<#
.SYNOPSIS
    Recreates the quickstart RayCluster with fixed ports, network policies, and
    optionally Ray token authentication and an egress proxy.
.DESCRIPTION
    Ray picks several internal ports at random, so ports must be pinned before
    the pods exist and policies must be applied before Ray starts. This script
    renders the patched manifest locally, applies the policy set, deletes any
    existing ray-t4 in the foreground, and recreates it.

    Foreground deletion matters: with background deletion, terminating pods from
    the previous cluster still match the new cluster's selectors and can make a
    readiness check pass against the wrong pods.

    These controls reduce exposure. They don't make a shared Ray cluster safe
    for untrusted code or untrusted users.
.EXAMPLE
    .\Protect-RayCluster.ps1 -WithProxy -WithTokenAuth
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Namespace = 'ray-learn',
    [switch]$WithTokenAuth,
    [switch]$WithProxy,
    [switch]$Spot,
    [string]$Context,
    [string]$RenderTo,
    [int]$ReadyTimeoutMinutes = 20
)

. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Tool -Name kubectl

if ($WithTokenAuth -and -not $WithProxy) {
    throw 'The token-auth patch also sets proxy environment variables. Use -WithProxy, or copy the patch and remove the proxy operations.'
}

$quickstart = Get-SamplePath 'quickstart'
$security = Get-SamplePath 'security'
$source = Join-Path $quickstart 'raycluster-t4.yaml'
$patches = @(Join-Path $security 'ray-t4-fixed-ports.patch.json')
if ($Spot) { $patches += (Join-Path $quickstart 'spot-worker.patch.json') }
if ($WithTokenAuth) { $patches += (Join-Path $security 'ray-t4-token-auth-proxy.patch.json') }

$rendered = if ($RenderTo) { $RenderTo } else {
    Join-Path ([IO.Path]::GetTempPath()) "raycluster-isolated-$([guid]::NewGuid().ToString('n')).yaml"
}

Write-Step "Rendering the patched RayCluster from $([IO.Path]::GetFileName($source))."
$current = $source
foreach ($patch in $patches) {
    $yaml = Invoke-Checked -FilePath 'kubectl' -Arguments @(
        'patch', '--local', '-f', $current, '--type', 'json', '--patch-file', $patch, '-o', 'yaml')
    Set-Content -LiteralPath $rendered -Value $yaml -Encoding utf8
    $current = $rendered
}
Write-Step "Rendered: $rendered"

if (-not $PSCmdlet.ShouldProcess("$Namespace/ray-t4", 'Apply isolation and recreate the Ray cluster')) { return }

Write-Step 'Checking the policy peers this set assumes exist.'
foreach ($peer in @(
        @{ Label = 'app.kubernetes.io/name=kuberay-operator'; What = 'KubeRay operator' },
        @{ Label = 'k8s-app=kube-dns'; What = 'CoreDNS' },
        @{ Label = 'rsName=ama-metrics'; What = 'Azure Monitor metrics collector' })) {
    $pods = Invoke-Kubectl -Context $Context -AsJson -Arguments @(
        'get', 'pods', '--all-namespaces', '-l', $peer.Label, '-o', 'json')
    $found = @($pods.items)
    if ($found.Count -eq 0) {
        Write-Warning "No pods match '$($peer.Label)' ($($peer.What)). Adapt that policy peer before you rely on it."
    }
    else {
        Write-Host "  $($peer.What): $($found.Count) pod(s) in $((@($found.metadata.namespace) | Sort-Object -Unique) -join ', ')"
    }
}

Write-Step 'Applying network policies before creating Ray pods.'
Invoke-Kubectl -Context $Context -Arguments @('-n', $Namespace, 'apply', '-f', (Join-Path $security 'ray-t4-network-policies.yaml')) | Out-Null
if ($WithProxy) {
    Invoke-Kubectl -Context $Context -Arguments @('apply', '-f', (Join-Path $security 'egress-proxy.yaml')) | Out-Null
    Invoke-Kubectl -Context $Context -Arguments @('-n', $Namespace, 'apply', '-f', (Join-Path $security 'ray-t4-egress-proxy.yaml')) | Out-Null
    Invoke-Kubectl -Context $Context -Arguments @(
        '-n', 'ray-egress', 'rollout', 'status', 'deployment/ray-egress-proxy', '--timeout=5m') | Out-Null
}

if (Test-KubeObject -Kind raycluster -Name 'ray-t4' -Namespace $Namespace -Context $Context) {
    Write-Step 'Deleting the existing ray-t4 in the foreground so its pods are gone before recreation.'
    Invoke-Kubectl -Context $Context -Arguments @(
        '-n', $Namespace, 'delete', 'raycluster', 'ray-t4', '--cascade=foreground', '--wait=true', '--timeout=5m') | Out-Null
}

Write-Step 'Creating the isolated RayCluster.'
Invoke-Kubectl -Context $Context -Arguments @('-n', $Namespace, 'apply', '-f', $rendered) | Out-Null
Invoke-Kubectl -Context $Context -Arguments @(
    '-n', $Namespace, 'wait', 'raycluster/ray-t4',
    '--for=jsonpath={.status.state}=ready', "--timeout=${ReadyTimeoutMinutes}m") | Out-Null

Invoke-Kubectl -Context $Context -Arguments @('-n', $Namespace, 'get', 'networkpolicies')
Invoke-Kubectl -Context $Context -Arguments @('-n', $Namespace, 'get', 'pods', '-l', 'ray.io/cluster=ray-t4', '-o', 'wide')

Write-Step 'Applied. A successful apply is not an enforcement test.'
Write-Host 'Next: run the probes in samples/security against an allowed path, a denied path, and the head pod IP.'
if ($WithTokenAuth) {
    Write-Host 'Token auth is on: submitters need the cluster token, and RayJobs need the ray-submit label (-Secured in Invoke-RayQuickstartJobs.ps1).'
}
