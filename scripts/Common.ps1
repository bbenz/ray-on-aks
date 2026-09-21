<#
.SYNOPSIS
    Shared helpers for the Ray on AKS sample scripts.
.DESCRIPTION
    Dot-source this file. It provides checked native-command execution, tool
    checks, and the resolved path to the samples directory. It never changes
    your default kubectl context: pass -Context to target a specific cluster.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$script:RepoRoot = Split-Path -Parent $PSScriptRoot
$script:SamplesRoot = Join-Path $script:RepoRoot 'samples'

function Get-SamplePath {
    param([Parameter(Mandatory)][string]$RelativePath)
    $path = Join-Path $script:SamplesRoot $RelativePath
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Sample not found: $RelativePath"
    }
    return $path
}

function Assert-Tool {
    param([Parameter(Mandatory)][string[]]$Name)
    $missing = @($Name | Where-Object { -not (Get-Command $_ -ErrorAction SilentlyContinue) })
    if ($missing.Count -gt 0) {
        throw "Required tools are not on PATH: $($missing -join ', ')"
    }
}

function Invoke-Checked {
    <#
        Runs a native command, fails on a nonzero exit code, and returns its
        output. Use -AsJson to parse stdout as JSON.
    #>
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string[]]$Arguments,
        [switch]$AsJson
    )
    $output = & $FilePath @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        $text = ($output | Out-String).Trim()
        throw "$FilePath $($Arguments -join ' ') failed with exit code ${LASTEXITCODE}: $text"
    }
    $text = ($output | Out-String)
    if (-not $AsJson) { return $text.TrimEnd() }
    if ([string]::IsNullOrWhiteSpace($text)) {
        throw "$FilePath $($Arguments -join ' ') returned no JSON."
    }
    return $text | ConvertFrom-Json
}

function Invoke-Kubectl {
    param(
        [Parameter(Mandatory)][string[]]$Arguments,
        [string]$Context,
        [switch]$AsJson
    )
    $all = @($Arguments)
    if ($Context) { $all += @('--context', $Context) }
    return Invoke-Checked -FilePath 'kubectl' -Arguments $all -AsJson:$AsJson
}

function Get-KubeContext {
    param([string]$Context)
    if ($Context) { return $Context }
    return (Invoke-Checked -FilePath 'kubectl' -Arguments @('config', 'current-context'))
}

function Write-Step {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "==> $Message"
}

function Test-KubeObject {
    <#
        Returns $true when the named object exists. Absence is not an error.
    #>
    param(
        [Parameter(Mandatory)][string]$Kind,
        [Parameter(Mandatory)][string]$Name,
        [string]$Namespace,
        [string]$Context
    )
    $arguments = @('get', $Kind, $Name, '--ignore-not-found', '-o', 'name')
    if ($Namespace) { $arguments += @('-n', $Namespace) }
    if ($Context) { $arguments += @('--context', $Context) }
    $output = & kubectl @arguments 2>$null
    return -not [string]::IsNullOrWhiteSpace(($output | Out-String))
}

function Assert-AllocatableGpu {
    <#
        Waits until a node in the pool advertises an allocatable GPU. A Ready
        node is not enough: the device plugin registers the resource separately,
        and work submitted in that gap fails.
    #>
    param(
        [Parameter(Mandatory)][string]$Pool,
        [string]$Context,
        [int]$TimeoutSeconds = 900
    )
    Write-Step "Waiting for pool '$Pool' to advertise allocatable nvidia.com/gpu."
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $nodes = Invoke-Kubectl -Context $Context -AsJson -Arguments @(
            'get', 'nodes', '-l', "kubernetes.azure.com/agentpool=$Pool", '-o', 'json'
        )
        $ready = @($nodes.items | Where-Object {
            $_.status.allocatable.'nvidia.com/gpu' -and
            ($_.status.conditions | Where-Object { $_.type -eq 'Ready' -and $_.status -eq 'True' })
        })
        if ($ready.Count -gt 0) {
            $names = ($ready | ForEach-Object { "$($_.metadata.name)=$($_.status.allocatable.'nvidia.com/gpu')" }) -join ', '
            Write-Step "GPU capacity is registered: $names"
            return $ready
        }
        Start-Sleep -Seconds 15
    }
    throw "No node in pool '$Pool' advertised an allocatable GPU within $TimeoutSeconds seconds."
}
