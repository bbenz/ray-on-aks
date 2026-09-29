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
        standard output. Standard error is captured separately so a warning
        can't corrupt captured YAML or JSON. Use -AsJson to parse stdout.
    #>
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string[]]$Arguments,
        [switch]$AsJson
    )
    # Callers gate state changes with ShouldProcess. Under -WhatIf, the stderr
    # redirection and temp-file cleanup below would otherwise be skipped too,
    # which drops warnings, leaks temp files, and prints misleading noise.
    $WhatIfPreference = $false
    $errorPath = [IO.Path]::GetTempFileName()
    try {
        $output = & $FilePath @Arguments 2>$errorPath
        $exitCode = $LASTEXITCODE
        $stderr = (Get-Content -LiteralPath $errorPath -Raw -ErrorAction SilentlyContinue)
    }
    finally {
        Remove-Item -LiteralPath $errorPath -Force -ErrorAction SilentlyContinue
    }
    if ($exitCode -ne 0) {
        $detail = ((($output | Out-String) + "`n" + $stderr)).Trim()
        throw "$FilePath $($Arguments -join ' ') failed with exit code ${exitCode}: $detail"
    }
    if ($stderr -and -not [string]::IsNullOrWhiteSpace($stderr)) {
        foreach ($line in ($stderr.Trim() -split "`r?`n")) { Write-Warning ($line -replace '^WARNING:[ ]*', '') }
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

function Compare-ExtensionVersion {
    <#
        Compares Azure CLI extension versions such as 22.0.0b6. Returns -1, 0,
        or 1. A release (22.0.0) sorts after its betas (22.0.0b9).
    #>
    param(
        [Parameter(Mandatory)][string]$Left,
        [Parameter(Mandatory)][string]$Right
    )
    $parse = {
        param([string]$Text)
        if ($Text -notmatch '^(\d+)\.(\d+)\.(\d+)(?:b(\d+))?$') { throw "Unrecognized extension version: $Text" }
        $beta = if ($Matches[4]) { [long]$Matches[4] } else { [long]::MaxValue }
        return @([long]$Matches[1], [long]$Matches[2], [long]$Matches[3], $beta)
    }
    $a = & $parse $Left
    $b = & $parse $Right
    for ($i = 0; $i -lt 4; $i++) {
        if ($a[$i] -lt $b[$i]) { return -1 }
        if ($a[$i] -gt $b[$i]) { return 1 }
    }
    return 0
}

function Get-PropertyValue {
    <#
        Reads a property path that might not exist yet, such as a status field a
        controller hasn't written. Strict mode throws on the first missing
        property, so read those fields through this helper and treat $null as
        "not reported yet".
    #>
    param(
        $InputObject,
        [Parameter(Mandatory)][string[]]$Path
    )
    $current = $InputObject
    foreach ($name in $Path) {
        if ($null -eq $current) { return $null }
        $property = $current.PSObject.Properties[$name]
        if (-not $property) { return $null }
        $current = $property.Value
    }
    return $current
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
            $gpus = Get-PropertyValue $_ @('status', 'allocatable', 'nvidia.com/gpu')
            $isReady = @(Get-PropertyValue $_ @('status', 'conditions')) |
                Where-Object { $_ -and $_.type -eq 'Ready' -and $_.status -eq 'True' }
            ($gpus -and [int]$gpus -gt 0) -and [bool]$isReady
        })
        if ($ready.Count -gt 0) {
            $names = ($ready | ForEach-Object { "$($_.metadata.name)=$(Get-PropertyValue $_ @('status', 'allocatable', 'nvidia.com/gpu'))" }) -join ', '
            Write-Step "GPU capacity is registered: $names"
            return $ready
        }
        Start-Sleep -Seconds 15
    }
    throw "No node in pool '$Pool' advertised an allocatable GPU within $TimeoutSeconds seconds."
}
