[CmdletBinding()]
param(
    [switch]$Quiet
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimePath = Join-Path $root "data\fab-ngrok-runtime.json"
$managedRoot = Join-Path $env:LOCALAPPDATA "FAB\ngrok"

function Get-FabInstanceId {
    param([Parameter(Mandatory = $true)][string]$Path)

    $normalized = [System.IO.Path]::GetFullPath($Path).TrimEnd("\", "/").Replace("\", "/")
    if ($env:OS -eq "Windows_NT") {
        $normalized = $normalized.ToLowerInvariant()
    }
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($normalized)
        return ([System.BitConverter]::ToString($sha256.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
    }
}

function Test-OwnedManagedPath {
    param([AllowNull()][object]$Path)

    if (-not $Path) {
        return $false
    }
    try {
        $candidate = [System.IO.Path]::GetFullPath([string]$Path)
        $expectedRoot = [System.IO.Path]::GetFullPath($managedRoot).TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
        return $candidate.StartsWith($expectedRoot, [System.StringComparison]::OrdinalIgnoreCase)
    }
    catch {
        return $false
    }
}

function Test-FabManagedNgrokProcess {
    param(
        [AllowNull()][object]$Process,
        [Parameter(Mandatory = $true)][string]$OverlayPath
    )

    if (-not $Process) {
        return $false
    }
    $normalizedCommand = ([string]$Process.CommandLine).Replace("\", "/").ToLowerInvariant()
    $overlayMarker = ([System.IO.Path]::GetFullPath($OverlayPath)).Replace("\", "/").ToLowerInvariant()
    return (
        ([string]$Process.Name).ToLowerInvariant() -eq "ngrok.exe" -and
        $normalizedCommand.Contains("fab-managed") -and
        $normalizedCommand.Contains($overlayMarker)
    )
}

function Assert-FabNgrokProcessStopped {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [Parameter(Mandatory = $true)][string]$OverlayPath
    )

    $remainingProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
    if (Test-FabManagedNgrokProcess -Process $remainingProcess -OverlayPath $OverlayPath) {
        throw "The managed FAB ngrok tunnel is still running. Ownership metadata and agent files are being retained."
    }
}

if (-not (Test-Path -LiteralPath $runtimePath)) {
    if (-not $Quiet) {
        Write-Host "No managed FAB tunnel is recorded."
    }
    return
}

try {
    $runtime = Get-Content -LiteralPath $runtimePath -Raw | ConvertFrom-Json
}
catch {
    throw "The managed FAB tunnel metadata is unreadable. Refusing to stop any process."
}

$expectedRoot = [System.IO.Path]::GetFullPath($root).TrimEnd("\", "/")
try {
    $runtimeRoot = [System.IO.Path]::GetFullPath([string]$runtime.instanceRoot).TrimEnd("\", "/")
}
catch {
    throw "The managed FAB tunnel metadata has no valid project root. Refusing to stop any process."
}
if (
    $runtimeRoot -ne $expectedRoot -or
    [string]$runtime.instanceId -ne (Get-FabInstanceId -Path $root) -or
    [string]$runtime.service -ne "fab-ngrok-tunnel" -or
    [int]$runtime.version -ne 1 -or
    -not [bool]$runtime.authRequired -or
    -not (Test-OwnedManagedPath -Path $runtime.overlayPath)
) {
    throw "The managed FAB tunnel metadata does not belong to this checkout. Refusing to stop any process."
}

$processId = 0
try {
    $processId = [int]$runtime.processId
}
catch {
    throw "The managed FAB tunnel metadata has no valid process ID. Refusing to remove ownership state or agent files."
}
if ($processId -le 0) {
    throw "The managed FAB tunnel metadata has no valid process ID. Refusing to remove ownership state or agent files."
}
$process = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction Stop
if ($process) {
    if (-not (Test-FabManagedNgrokProcess -Process $process -OverlayPath ([string]$runtime.overlayPath))) {
        throw "PID $processId no longer matches this checkout's managed FAB tunnel. Refusing to stop it."
    }
    Stop-Process -Id $processId -Force -ErrorAction Stop
    Wait-Process -Id $processId -Timeout 10 -ErrorAction SilentlyContinue
    Assert-FabNgrokProcessStopped -ProcessId $processId -OverlayPath ([string]$runtime.overlayPath)
}

foreach ($path in @($runtime.overlayPath, $runtime.stdoutPath, $runtime.stderrPath)) {
    if (Test-OwnedManagedPath -Path $path) {
        Remove-Item -LiteralPath ([string]$path) -Force -ErrorAction SilentlyContinue
    }
}
Remove-Item -LiteralPath $runtimePath -Force

if (-not $Quiet) {
    Write-Host "The managed FAB ngrok tunnel is stopped."
}
