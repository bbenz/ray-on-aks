<#
.SYNOPSIS
    Removes the sample's Kubernetes objects, and optionally the GPU node pool.
.DESCRIPTION
    Deleting Ray objects stops nothing on the Azure bill by itself. The GPU node
    keeps billing until the node pool is removed or scaled to zero, so this
    script reports what remains.

    It only deletes the namespace it was given and the optional egress-proxy
    namespace. It never deletes the KubeRay operator, the cluster, or the
    resource group.
.EXAMPLE
    .\Remove-RayQuickstart.ps1 -RemoveGpuPool -ResourceGroup rg-ray -ClusterName aks-ray
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Namespace = 'ray-learn',
    [switch]$RemoveProxy,
    [switch]$RemoveGpuPool,
    [string]$ResourceGroup,
    [string]$ClusterName,
    [string]$Subscription,
    [string]$Pool = 'raygpu',
    [string]$Context
)

. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Tool -Name kubectl

if ($RemoveGpuPool -and (-not $ResourceGroup -or -not $ClusterName)) {
    throw '-RemoveGpuPool needs -ResourceGroup and -ClusterName.'
}

if (Test-KubeObject -Kind namespace -Name $Namespace -Context $Context) {
    if ($PSCmdlet.ShouldProcess($Namespace, 'Delete the sample namespace and everything in it')) {
        Write-Step "Export any results you still need before this completes."
        Invoke-Kubectl -Context $Context -Arguments @(
            '-n', $Namespace, 'delete', 'raycluster', '--all', '--cascade=foreground', '--wait=true', '--timeout=5m') | Out-Null
        Invoke-Kubectl -Context $Context -Arguments @('delete', 'namespace', $Namespace, '--wait=true', '--timeout=5m') | Out-Null
        Write-Step "Deleted namespace '$Namespace'."
    }
}
else {
    Write-Step "Namespace '$Namespace' doesn't exist; nothing to delete."
}

if ($RemoveProxy -and (Test-KubeObject -Kind namespace -Name 'ray-egress' -Context $Context)) {
    if ($PSCmdlet.ShouldProcess('ray-egress', 'Delete the egress proxy namespace')) {
        Invoke-Kubectl -Context $Context -Arguments @('delete', 'namespace', 'ray-egress', '--wait=true', '--timeout=5m') | Out-Null
        Write-Step 'Deleted namespace ray-egress.'
    }
}

if ($RemoveGpuPool) {
    Assert-Tool -Name az
    $common = @('--subscription', $Subscription)
    if (-not $Subscription) { $common = @() }
    if ($PSCmdlet.ShouldProcess("$ClusterName/$Pool", 'Delete the GPU node pool')) {
        Write-Step "Deleting node pool '$Pool'. This is what stops the GPU VM charge."
        Invoke-Checked -FilePath 'az' -Arguments (@(
                'aks', 'nodepool', 'delete', '--resource-group', $ResourceGroup,
                '--cluster-name', $ClusterName, '--name', $Pool, '--output', 'none') + $common) | Out-Null
    }
    $pools = Invoke-Checked -FilePath 'az' -AsJson -Arguments (@(
            'aks', 'nodepool', 'list', '--resource-group', $ResourceGroup,
            '--cluster-name', $ClusterName, '--output', 'json') + $common)
    $pools | Select-Object name, count, vmSize, provisioningState | Format-Table
    if (@($pools | Where-Object name -eq $Pool).Count -gt 0) {
        throw "Node pool '$Pool' still exists. Confirm the deletion before assuming charges stopped."
    }
}
else {
    Write-Step 'The GPU node pool was left in place and continues to bill. Remove it or scale it to zero when finished.'
}
