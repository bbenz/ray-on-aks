<#
.SYNOPSIS
    Adds a single fixed-size AKS-managed GPU node pool for the Ray samples.
.DESCRIPTION
    Checks the SKU's quota family and regional vCPU headroom, creates the pool,
    then waits for a Ready node that advertises an allocatable GPU.

    Quota is not capacity: a successful quota check doesn't guarantee that the
    region can allocate the VM, and Spot nodes can be evicted at any time.

    AKS-managed GPU pools install the driver, device plugin, and DCGM exporter.
    Don't add another GPU operator or device plugin to this pool. The preview
    doesn't support the cluster autoscaler on manually created managed GPU
    pools; use the autoscaling sample's separate pool for that scenario.
.EXAMPLE
    .\Add-RayGpuNodePool.ps1 -ResourceGroup rg-ray -ClusterName aks-ray
.EXAMPLE
    .\Add-RayGpuNodePool.ps1 -ResourceGroup rg-ray -ClusterName aks-ray -Spot -SpotMaxPrice 0.526 -Zone 2
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$ResourceGroup,
    [Parameter(Mandatory)][string]$ClusterName,
    [string]$Subscription,
    [ValidatePattern('^[a-z][a-z0-9]{0,11}$')][string]$Pool = 'raygpu',
    [string]$VmSize = 'Standard_NC4as_T4_v3',
    [ValidateRange(1, 4)][int]$NodeCount = 1,
    [string]$Zone,
    [switch]$Spot,
    [double]$SpotMaxPrice = -1,
    [string]$Context
)

. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Tool -Name az, kubectl

$common = @('--subscription', $Subscription)
if (-not $Subscription) { $common = @() }

# --gpu-driver-mode arrived in aks-preview 22.0.0b6; older versions reject the
# whole command with "unrecognized arguments".
$minimumPreview = '22.0.0b6'
$installedPreview = (& az extension show --name aks-preview --query version --output tsv 2>$null | Out-String).Trim()
if (-not $installedPreview -or (Compare-ExtensionVersion $installedPreview $minimumPreview) -lt 0) {
    $found = if ($installedPreview) { "aks-preview $installedPreview is installed" } else { 'aks-preview is not installed' }
    throw "$found; --gpu-driver-mode needs $minimumPreview or later. Run: az extension add --upgrade --name aks-preview"
}
$feature = Invoke-Checked -FilePath 'az' -Arguments (@(
        'feature', 'show', '--namespace', 'Microsoft.ContainerService', '--name', 'ManagedGPUExperiencePreview',
        '--query', 'properties.state', '--output', 'tsv'
    ) + $common)
if ($feature -ne 'Registered') {
    throw ("The ManagedGPUExperiencePreview feature is '$feature'. Run: az feature register --namespace Microsoft.ContainerService " +
        "--name ManagedGPUExperiencePreview, wait for Registered, then az provider register --namespace Microsoft.ContainerService")
}

Write-Step "Checking the target cluster."
$cluster = Invoke-Checked -FilePath 'az' -AsJson -Arguments (@(
        'aks', 'show', '--resource-group', $ResourceGroup, '--name', $ClusterName, '--output', 'json'
    ) + $common)
if ($cluster.provisioningState -ne 'Succeeded' -or $cluster.powerState.code -ne 'Running') {
    throw "Cluster $ClusterName is $($cluster.provisioningState)/$($cluster.powerState.code); it must be Succeeded/Running."
}
$location = $cluster.location

$existing = @($cluster.agentPoolProfiles | Where-Object name -eq $Pool)
if ($existing.Count -gt 0) {
    throw "Node pool '$Pool' already exists. Inspect it, or choose another pool name and update the sample selectors."
}

Write-Step "Checking the $VmSize SKU and quota in $location."
$sku = Invoke-Checked -FilePath 'az' -AsJson -Arguments (@(
        'vm', 'list-skus', '--location', $location, '--resource-type', 'virtualMachines',
        '--size', $VmSize, '--all', '--output', 'json'
    ) + $common) | Where-Object name -eq $VmSize | Select-Object -First 1
if (-not $sku) { throw "$VmSize isn't offered in $location." }
$restrictions = @($sku.restrictions)
if ($restrictions.Count -gt 0) {
    Write-Warning "$VmSize reports $($restrictions.Count) restriction(s) in ${location}: $(($restrictions | ForEach-Object { $_.reasonCode }) -join ', ')"
}
$vCpus = [int](@($sku.capabilities | Where-Object name -eq 'vCPUs').value | Select-Object -First 1)
$required = $vCpus * $NodeCount

$usage = Invoke-Checked -FilePath 'az' -AsJson -Arguments (@(
        'vm', 'list-usage', '--location', $location, '--output', 'json'
    ) + $common)
$quotaName = if ($Spot) { 'lowPriorityCores' } else { $sku.family }
$rows = @($usage | Where-Object { $_.name.value -in @($quotaName, 'cores') })
foreach ($row in $rows) {
    $available = [int]$row.limit - [int]$row.currentValue
    Write-Step "Quota $($row.name.localizedValue): limit $($row.limit), used $($row.currentValue), available $available."
    if ([int]$row.currentValue -lt 0) {
        Write-Warning "Reported usage for $($row.name.value) is negative; treat it as unknown, not as headroom."
    }
    elseif ($available -lt $required) {
        throw "$($row.name.value) has $available vCPUs available; $NodeCount x $VmSize needs $required."
    }
}
if ($rows.Count -eq 0) {
    Write-Warning ("No quota row matched '$quotaName' or 'cores'. An empty 'az vm list-usage' result usually means Microsoft.Compute " +
        "isn't registered in this subscription: az provider register --namespace Microsoft.Compute --wait. The creation call still enforces quota.")
}

$arguments = @(
    'aks', 'nodepool', 'add', '--resource-group', $ResourceGroup, '--cluster-name', $ClusterName,
    '--name', $Pool, '--mode', 'User', '--node-vm-size', $VmSize, '--node-count', "$NodeCount",
    '--os-sku', 'Ubuntu', '--node-osdisk-type', 'Managed', '--node-osdisk-size', '128',
    '--node-taints', 'sku=gpu:NoSchedule', '--enable-managed-gpu=true', '--gpu-driver-mode', 'DevicePlugin'
) + $common
if ($Zone) { $arguments += @('--zones', $Zone) }
if ($Spot) {
    $arguments += @('--priority', 'Spot', '--eviction-policy', 'Delete',
        '--spot-max-price', $SpotMaxPrice.ToString([Globalization.CultureInfo]::InvariantCulture))
}

if (-not $PSCmdlet.ShouldProcess("$ClusterName/$Pool", "Create $NodeCount x $VmSize GPU node pool")) { return }

Write-Step "Creating node pool '$Pool'. GPU nodes bill from creation, including while drivers and images download."
$created = Invoke-Checked -FilePath 'az' -Arguments ($arguments + @('--output', 'json')) -AsJson
if ($created.provisioningState -ne 'Succeeded') {
    throw "Node pool creation returned $($created.provisioningState)."
}

Assert-AllocatableGpu -Pool $Pool -Context $Context | Out-Null
Write-Step "Pool '$Pool' is ready. A registered device is not proof of working CUDA: run the GPU check next."
