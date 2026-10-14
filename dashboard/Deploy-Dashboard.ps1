<#
.SYNOPSIS
    Deploys the read-only Ray on AKS demo dashboard into a cluster, or removes it.
.DESCRIPTION
    Applies deploy/dashboard.yaml (namespace ray-dashboard, read-only RBAC, the
    Deployment, and an ingress default-deny policy), ships the app as a
    ConfigMap, and binds log, service, and event reading only in the Ray
    namespaces that exist. Nothing is built or pushed: the pod runs the Azure
    Linux distroless Python image, pinned by digest.

    For the metric charts, and for job output whose pods are gone, the script
    also creates a user-assigned managed identity with Monitoring Data Reader on
    the Azure Monitor workspace and Log Analytics Reader on the Container
    insights workspace, federated with the dashboard's service account through
    the cluster's OIDC issuer. No secret is created. Use -SkipAzureMonitor to
    run with the Kubernetes API and the live GPU feed only.

    The dashboard has no Service, load balancer, or ingress, and listens on the
    pod's loopback interface: open it with kubectl port-forward.

    It never changes your default kubectl context, and refuses to run when the
    context doesn't point at -ClusterName.
.EXAMPLE
    .\Deploy-Dashboard.ps1 -ResourceGroup rg-ray -ClusterName aks-ray
.EXAMPLE
    .\Deploy-Dashboard.ps1 -ResourceGroup rg-ray -ClusterName aks-ray -ReplayStart 2026-09-29T22:20:00Z -ReplayEnd 2026-09-29T23:30:00Z -ReplayLabel 'the September 29 burst'
.EXAMPLE
    .\Deploy-Dashboard.ps1 -ResourceGroup rg-ray -ClusterName aks-ray -Remove
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$ResourceGroup,
    [Parameter(Mandatory)][string]$ClusterName,
    [string]$Subscription,
    [string]$Context,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_-]{2,127}$')][string]$IdentityName = 'id-ray-dashboard',
    [string]$MonitorWorkspaceId,
    [string]$LogAnalyticsWorkspaceId,
    [switch]$SkipAzureMonitor,
    [string]$ReplayStart,
    [string]$ReplayEnd,
    [string]$ReplayLabel = 'Recorded run',
    [ValidatePattern('^[a-z0-9]([-a-z0-9]*[a-z0-9])?$')][string[]]$RayNamespaces = @('ray-learn', 'ray-scale', 'ray-probe'),
    [ValidateRange(1024, 65535)][int]$LocalPort = 8090,
    [switch]$Remove
)

. (Join-Path $PSScriptRoot '..\scripts\Common.ps1')
Assert-Tool -Name az, kubectl

$Namespace = 'ray-dashboard'
$ServiceAccount = 'ray-dashboard'
$FederatedName = 'ray-dashboard'
$AppFiles = @('server.py', 'collect.py', 'clients.py', 'metrics.py', 'index.html', 'app.css', 'ui.js', 'stages.js', 'app.js', 'favicon.svg')
$AppDir = Join-Path $PSScriptRoot 'app'
$Manifest = Join-Path $PSScriptRoot 'deploy\dashboard.yaml'

$common = @('--subscription', $Subscription)
if (-not $Subscription) { $common = @() }

function Invoke-Az {
    param([Parameter(Mandatory)][string[]]$Arguments, [switch]$AsJson)
    return Invoke-Checked -FilePath 'az' -Arguments ($Arguments + $common) -AsJson:$AsJson
}

function Invoke-KubectlApply {
    # Server-side apply, so the app ConfigMap never hits the client-side annotation size limit.
    param([Parameter(Mandatory)][string]$Content)
    $path = Join-Path ([IO.Path]::GetTempPath()) "ray-dashboard-$([guid]::NewGuid()).yaml"
    try {
        [IO.File]::WriteAllText($path, $Content, [Text.UTF8Encoding]::new($false))
        Invoke-Kubectl -Context $ctx -Arguments @('apply', '--server-side', '--force-conflicts',
            '--field-manager', 'ray-dashboard-deploy', '-f', $path) | Out-Host
    }
    finally {
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    }
}

function ConvertTo-ConfigMapJson {
    <#
        Builds a ConfigMap here, rather than capturing 'kubectl create configmap -o yaml':
        PowerShell decodes a native command's output with the console code page, which in a
        standard Windows terminal garbles every non-ASCII character in the app.
    #>
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Namespace,
        [Parameter(Mandatory)][System.Collections.IDictionary]$Data
    )
    $values = [ordered]@{}
    foreach ($key in $Data.Keys) { $values[$key] = [string]$Data[$key] }
    $object = [ordered]@{
        apiVersion = 'v1'
        kind       = 'ConfigMap'
        metadata   = [ordered]@{
            name      = $Name
            namespace = $Namespace
            labels    = [ordered]@{ 'app.kubernetes.io/part-of' = 'ray-on-aks-dashboard' }
        }
        data       = $values
    }
    return ($object | ConvertTo-Json -Depth 5)
}

# --- Target ---
Write-Step "Checking cluster $ClusterName."
$cluster = Invoke-Az -AsJson -Arguments @('aks', 'show', '--resource-group', $ResourceGroup, '--name', $ClusterName, '--output', 'json')
$ctx = Get-KubeContext -Context $Context
$server = Invoke-Kubectl -Context $ctx -Arguments @('config', 'view', '--minify', '-o', 'jsonpath={.clusters[0].cluster.server}')
$apiHost = ([Uri]$server).Host
$fqdns = @('fqdn', 'privateFqdn', 'azurePortalFqdn') | ForEach-Object { Get-PropertyValue $cluster @($_) } | Where-Object { $_ }
if ($apiHost -notin $fqdns) {
    throw "Context '$ctx' points at $apiHost, not cluster $ClusterName ($($fqdns -join ', ')). Pass -Context."
}
Write-Step "Target context: $ctx"

# --- Remove ---
if ($Remove) {
    if ($PSCmdlet.ShouldProcess($ctx, "Delete namespace $Namespace, the dashboard's ClusterRoles, and its RoleBindings")) {
        foreach ($ns in $RayNamespaces) {
            Invoke-Kubectl -Context $ctx -Arguments @('delete', 'rolebinding', 'ray-dashboard', '-n', $ns, '--ignore-not-found') | Out-Host
        }
        Invoke-Kubectl -Context $ctx -Arguments @('delete', '-f', $Manifest, '--ignore-not-found', '--wait=true') | Out-Host
    }
    if (-not $SkipAzureMonitor) {
        $identity = @(Invoke-Az -AsJson -Arguments @('identity', 'list', '--resource-group', $ResourceGroup,
                '--query', "[?name=='$IdentityName']", '--output', 'json')) | Select-Object -First 1
        if ($identity -and $PSCmdlet.ShouldProcess($IdentityName, 'Delete its role assignments and the managed identity')) {
            $assignments = @(Invoke-Az -AsJson -Arguments @('role', 'assignment', 'list', '--all',
                    '--query', "[?principalId=='$($identity.principalId)'].id", '--output', 'json'))
            foreach ($id in $assignments) { Invoke-Az -Arguments @('role', 'assignment', 'delete', '--ids', $id) | Out-Null }
            Invoke-Az -Arguments @('identity', 'delete', '--resource-group', $ResourceGroup, '--name', $IdentityName) | Out-Null
            Write-Step "Deleted $IdentityName and $($assignments.Count) role assignment(s)."
        }
    }
    Write-Step 'The dashboard is removed.'
    return
}

# --- Azure Monitor access through workload identity ---
$settings = [ordered]@{ CLUSTER_NAME = $ClusterName }
if (-not $SkipAzureMonitor) {
    $issuer = Get-PropertyValue $cluster @('oidcIssuerProfile', 'issuerUrl')
    if (-not $issuer -or -not (Get-PropertyValue $cluster @('oidcIssuerProfile', 'enabled'))) {
        throw ("The cluster's OIDC issuer is off, so the dashboard can't get Azure Monitor tokens. Enable it with " +
            "'az aks update --enable-oidc-issuer' (the workload identity webhook isn't needed), or pass -SkipAzureMonitor.")
    }
    if (-not $MonitorWorkspaceId) {
        $found = @(Invoke-Az -AsJson -Arguments @('resource', 'list', '--resource-group', $ResourceGroup,
                '--resource-type', 'Microsoft.Monitor/accounts', '--query', '[].id', '--output', 'json'))
        if ($found.Count -ne 1) {
            throw "Found $($found.Count) Azure Monitor workspaces in $ResourceGroup. Pass -MonitorWorkspaceId, or -SkipAzureMonitor."
        }
        $MonitorWorkspaceId = $found[0]
    }
    $promEndpoint = Invoke-Az -Arguments @('resource', 'show', '--ids', $MonitorWorkspaceId,
        '--query', 'properties.metrics.prometheusQueryEndpoint', '--output', 'tsv')
    if (-not $LogAnalyticsWorkspaceId) {
        $LogAnalyticsWorkspaceId = Get-PropertyValue $cluster @('addonProfiles', 'omsagent', 'config', 'logAnalyticsWorkspaceResourceID')
    }
    $scopes = [ordered]@{ $MonitorWorkspaceId = 'Monitoring Data Reader' }
    if ($LogAnalyticsWorkspaceId) {
        $settings.LOG_ANALYTICS_WORKSPACE_ID = Invoke-Az -Arguments @('monitor', 'log-analytics', 'workspace', 'show',
            '--ids', $LogAnalyticsWorkspaceId, '--query', 'customerId', '--output', 'tsv')
        $scopes[$LogAnalyticsWorkspaceId] = 'Log Analytics Reader'
    }
    else {
        Write-Warning 'Container insights is off, so finished jobs whose pods are gone show no output.'
    }
    $settings.PROMETHEUS_QUERY_ENDPOINT = $promEndpoint

    $identity = @(Invoke-Az -AsJson -Arguments @('identity', 'list', '--resource-group', $ResourceGroup,
            '--query', "[?name=='$IdentityName']", '--output', 'json')) | Select-Object -First 1
    if (-not $identity) {
        if ($PSCmdlet.ShouldProcess($IdentityName, "Create a user-assigned managed identity in $ResourceGroup")) {
            Write-Step "Creating managed identity $IdentityName."
            $identity = Invoke-Az -AsJson -Arguments @('identity', 'create', '--resource-group', $ResourceGroup,
                '--name', $IdentityName, '--location', $cluster.location, '--output', 'json')
        }
    }
    if ($identity) {
        $settings.AZURE_CLIENT_ID = $identity.clientId
        $settings.AZURE_TENANT_ID = $identity.tenantId
        foreach ($scope in $scopes.Keys) {
            $role = $scopes[$scope]
            $existing = @(Invoke-Az -AsJson -Arguments @('role', 'assignment', 'list', '--scope', $scope, '--role', $role,
                    '--query', "[?principalId=='$($identity.principalId)'].id", '--output', 'json'))
            if ($existing.Count -gt 0 -or -not $PSCmdlet.ShouldProcess($scope, "Grant $IdentityName '$role'")) { continue }
            Write-Step "Granting '$role' on $(($scope -split '/')[-1])."
            # A new identity can take a minute to replicate before Azure RBAC accepts it.
            for ($attempt = 1; ; $attempt++) {
                try {
                    Invoke-Az -Arguments @('role', 'assignment', 'create', '--assignee-object-id', $identity.principalId,
                        '--assignee-principal-type', 'ServicePrincipal', '--role', $role, '--scope', $scope, '--output', 'none') | Out-Null
                    break
                }
                catch {
                    if ($attempt -ge 6) { throw }
                    Start-Sleep -Seconds 15
                }
            }
        }
        $subject = "system:serviceaccount:${Namespace}:${ServiceAccount}"
        $credentials = @(Invoke-Az -AsJson -Arguments @('identity', 'federated-credential', 'list', '--identity-name', $IdentityName,
                '--resource-group', $ResourceGroup, '--output', 'json'))
        $match = $credentials | Where-Object { $_.issuer -eq $issuer -and $_.subject -eq $subject } | Select-Object -First 1
        if (-not $match -and $PSCmdlet.ShouldProcess($IdentityName, "Trust $subject from this cluster's OIDC issuer")) {
            Write-Step "Federating $IdentityName with $subject."
            Invoke-Az -Arguments @('identity', 'federated-credential', 'create', '--name', $FederatedName,
                '--identity-name', $IdentityName, '--resource-group', $ResourceGroup, '--issuer', $issuer,
                '--subject', $subject, '--audiences', 'api://AzureADTokenExchange', '--output', 'none') | Out-Null
        }
    }
}
if ($ReplayStart -and $ReplayEnd) {
    $settings.REPLAY_START = $ReplayStart
    $settings.REPLAY_END = $ReplayEnd
    $settings.REPLAY_LABEL = $ReplayLabel
}

# --- Kubernetes objects ---
$utf8 = [Text.UTF8Encoding]::new($false)
$appData = [ordered]@{}
foreach ($file in $AppFiles) {
    $path = Join-Path $AppDir $file
    if (-not (Test-Path -LiteralPath $path)) { throw "Missing app file: $file" }
    $appData[$file] = [IO.File]::ReadAllText($path, $utf8)
}
$appJson = ConvertTo-ConfigMapJson -Name 'ray-dashboard-app' -Namespace $Namespace -Data $appData
$settingsJson = ConvertTo-ConfigMapJson -Name 'ray-dashboard-settings' -Namespace $Namespace -Data $settings
# The pod template carries a hash of exactly what's applied, so any change to either ConfigMap rolls the pod.
$sha = [Security.Cryptography.SHA256]::Create()
$configHash = -join ($sha.ComputeHash($utf8.GetBytes($appJson + "`n" + $settingsJson)) | Select-Object -First 8 | ForEach-Object { $_.ToString('x2') })

$documents = (Get-Content -LiteralPath $Manifest -Raw) -split '(?m)^---\s*$'
$namespaceYaml = $documents | Where-Object { $_ -match '(?m)^kind:\s*Namespace\s*$' } | Select-Object -First 1
$fullYaml = ((Get-Content -LiteralPath $Manifest -Raw) -replace 'set-by-deploy-script', $configHash)

if (-not $PSCmdlet.ShouldProcess($ctx, "Deploy the dashboard to namespace $Namespace (config $configHash)")) { return }

Write-Step "Applying namespace $Namespace and the app ConfigMaps."
Invoke-KubectlApply -Content $namespaceYaml
Invoke-KubectlApply -Content $appJson
Invoke-KubectlApply -Content $settingsJson

Write-Step 'Applying RBAC, the Deployment, and the network policy.'
Invoke-KubectlApply -Content $fullYaml

$existingNamespaces = @((Invoke-Kubectl -Context $ctx -Arguments @('get', 'namespaces', '-o',
            'jsonpath={.items[*].metadata.name}')) -split '\s+')
foreach ($ns in $RayNamespaces) {
    if ($ns -notin $existingNamespaces) {
        Write-Step "Namespace $ns doesn't exist yet; rerun this script after creating it."
        continue
    }
    $binding = Invoke-Kubectl -Context $ctx -Arguments @('create', 'rolebinding', 'ray-dashboard', '-n', $ns,
        '--clusterrole', 'ray-dashboard-namespace-reader', '--serviceaccount', "${Namespace}:${ServiceAccount}",
        '--dry-run=client', '-o', 'yaml')
    Invoke-KubectlApply -Content $binding
}

Write-Step 'Waiting for the dashboard pod.'
Invoke-Kubectl -Context $ctx -Arguments @('-n', $Namespace, 'rollout', 'status', 'deployment/ray-dashboard', '--timeout=180s') | Out-Host

Write-Step 'The dashboard is running. Open it with:'
Write-Host "    kubectl --context $ctx -n $Namespace port-forward deployment/ray-dashboard ${LocalPort}:8080"
Write-Host "    http://localhost:$LocalPort/"
if (-not $SkipAzureMonitor) {
    Write-Host '    New role assignments can take a few minutes to apply; until then the Prometheus chip shows an error and the charts stay empty.'
}
