[CmdletBinding()]
param(
    [int]$TimeoutSeconds = 60,
    [string]$Url = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$fabRuntimePath = Join-Path $root "data\fab-runtime.json"
$cloudRuntimePath = Join-Path $root "data\fab-ngrok-runtime.json"
$mainConfig = Join-Path $env:LOCALAPPDATA "ngrok\ngrok.yml"
$managedRoot = Join-Path $env:LOCALAPPDATA "FAB\ngrok"
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$ngrok = Get-Command ngrok -ErrorAction Stop

Set-Location -LiteralPath $root

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

function Find-AvailableLoopbackPort {
    param([int]$StartPort)

    foreach ($port in $StartPort..($StartPort + 30)) {
        $listener = [System.Net.Sockets.TcpListener]::new(
            [System.Net.IPAddress]::Loopback,
            $port
        )
        try {
            $listener.Start()
            return $port
        }
        catch {
            continue
        }
        finally {
            $listener.Stop()
        }
    }
    throw "No free private ngrok inspection port is available."
}

function Test-CleanHttpsOrigin {
    param([Parameter(Mandatory = $true)][System.Uri]$Uri)

    return (
        $Uri.IsAbsoluteUri -and
        $Uri.Scheme -eq "https" -and
        $Uri.Host -and
        -not $Uri.UserInfo -and
        $Uri.AbsolutePath -eq "/" -and
        -not $Uri.Query -and
        -not $Uri.Fragment
    )
}

function Get-HttpStatusCode {
    param(
        [Parameter(Mandatory = $true)][string]$RequestUrl,
        [hashtable]$RequestHeaders = @{}
    )

    try {
        Invoke-WebRequest -Uri $RequestUrl -Headers $RequestHeaders -UseBasicParsing -TimeoutSec 15 | Out-Null
        return 200
    }
    catch {
        if ($_.Exception.Response) {
            return [int]$_.Exception.Response.StatusCode
        }
        return 0
    }
}

function Write-JsonAtomic {
    param(
        [Parameter(Mandatory = $true)][object]$Value,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $temporaryPath = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        $json = $Value | ConvertTo-Json -Depth 6
        [System.IO.File]::WriteAllText(
            $temporaryPath,
            $json,
            (New-Object System.Text.UTF8Encoding($false))
        )
        Move-Item -LiteralPath $temporaryPath -Destination $Path -Force
    }
    finally {
        Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path -LiteralPath $fabRuntimePath)) {
    throw "FAB is not running. Start it with Start-FAB.cmd first."
}
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "FAB's isolated Python runtime is missing. Run Start-FAB.cmd first."
}
if (-not (Test-Path -LiteralPath $mainConfig)) {
    throw "ngrok is not configured for this Windows user."
}

$runtime = Get-Content -LiteralPath $fabRuntimePath -Raw | ConvertFrom-Json
$runtimeRoot = [System.IO.Path]::GetFullPath([string]$runtime.root).TrimEnd("\", "/")
$expectedRoot = [System.IO.Path]::GetFullPath($root).TrimEnd("\", "/")
if ($runtimeRoot -ne $expectedRoot) {
    throw "The running FAB instance belongs to another checkout."
}
$maintenanceProperty = $runtime.PSObject.Properties["maintenanceMode"]
if ($maintenanceProperty -and [bool]$maintenanceProperty.Value) {
    throw "FAB cloud access is disabled during maintenance. Restart FAB in standard mode first."
}
$apiBaseUrl = ([string]$runtime.apiBaseUrl).TrimEnd("/")
$apiUri = [System.Uri]$apiBaseUrl
if (
    $apiUri.Scheme -ne "http" -or
    $apiUri.Host -notin @("127.0.0.1", "localhost", "::1") -or
    $apiUri.AbsolutePath -ne "/"
) {
    throw "Managed FAB ngrok access only accepts a loopback API endpoint."
}

$requestedUrl = $null
if ($Url) {
    try {
        $requestedUrl = [System.Uri]$Url
    }
    catch {
        throw "-Url must be a complete HTTPS origin without credentials, path, query, or fragment."
    }
    if (-not (Test-CleanHttpsOrigin -Uri $requestedUrl)) {
        throw "-Url must be a complete HTTPS origin without credentials, path, query, or fragment."
    }
    $Url = $requestedUrl.GetLeftPart([System.UriPartial]::Authority)
}

$credentialsJson = & $venvPython -m src.security.windows_runtime_credentials
if ($LASTEXITCODE -ne 0) {
    throw "FAB could not load its existing API/HAI credentials. Start FAB with the same configuration first."
}
try {
    $credentials = $credentialsJson | ConvertFrom-Json
    $apiToken = [string]$credentials.apiToken
    $haiApiToken = [string]$credentials.haiToken
}
finally {
    $credentialsJson = $null
    $credentials = $null
}
if ($apiToken.Length -lt 32 -or $haiApiToken.Length -lt 32) {
    throw "FAB requires existing API/HAI credentials of at least 32 characters."
}
if ([System.StringComparer]::Ordinal.Equals($apiToken, $haiApiToken)) {
    throw "FAB operator and HAI credentials must be different."
}
$headers = @{ Authorization = "Bearer $apiToken" }
$haiHeaders = @{ Authorization = "Bearer $haiApiToken" }
$expectedInstanceId = Get-FabInstanceId -Path $root
$localLive = Invoke-RestMethod -Uri "$apiBaseUrl/api/live" -Headers $headers -TimeoutSec 5
if (
    [string]$localLive.service -ne "fab-ledger-api" -or
    [string]$localLive.instanceId -ne $expectedInstanceId -or
    -not [bool]$localLive.authRequired -or
    [bool]$localLive.maintenanceMode
) {
    throw "The local FAB API identity or authentication boundary could not be verified."
}

if (Test-Path -LiteralPath $cloudRuntimePath) {
    try {
        $cloudStatus = Invoke-RestMethod -Uri "$apiBaseUrl/api/cloud/status" -Headers $headers -TimeoutSec 5
        if ([string]$cloudStatus.status -eq "active" -and [bool]$cloudStatus.active) {
            Write-Host "FAB cloud access is already active." -ForegroundColor Green
            Write-Host "Endpoint: $($cloudStatus.publicUrl)"
            Write-Host "HAI manifest: $($cloudStatus.haiManifestUrl)"
            $apiToken = ""
            $haiApiToken = ""
            return
        }
    }
    catch {
        # Ownership is checked again by the stop script before it can stop anything.
    }
    & (Join-Path $root "Stop-FAB-Ngrok.ps1") -Quiet
}

if (-not $Url) {
    try {
        $sharedInspector = Invoke-RestMethod -Uri "http://127.0.0.1:4040/api/tunnels" -TimeoutSec 2
        if (@($sharedInspector.tunnels).Count -gt 0) {
            throw "Another ngrok endpoint is already active. Reserve a separate FAB HTTPS endpoint and rerun with -Url. FAB will not stop or pool the existing endpoint."
        }
    }
    catch {
        if ($_.Exception.Message -like "Another ngrok endpoint is already active*") {
            throw
        }
    }
}

$inspectionPort = Find-AvailableLoopbackPort -StartPort 4041
$id = [Guid]::NewGuid().ToString("N")
New-Item -ItemType Directory -Path $managedRoot -Force | Out-Null
$overlayPath = Join-Path $managedRoot "fab-ngrok-$id.yml"
$stdoutPath = Join-Path $managedRoot "fab-ngrok-$id.out.log"
$stderrPath = Join-Path $managedRoot "fab-ngrok-$id.err.log"
$overlay = "version: 3`nagent:`n  web_addr: 127.0.0.1:$inspectionPort`n"
[System.IO.File]::WriteAllText(
    $overlayPath,
    $overlay,
    (New-Object System.Text.UTF8Encoding($false))
)

$process = $null
$started = $false
try {
    & $ngrok.Source config check --config $mainConfig --config $overlayPath | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "The isolated FAB ngrok configuration is invalid."
    }

    $arguments = @(
        "http", $apiUri.Port,
        "--config", ('"' + $mainConfig + '"'),
        "--config", ('"' + $overlayPath + '"'),
        "--log", "stdout",
        "--log-format", "json",
        "--name", "fab-managed"
    )
    if ($Url) {
        $arguments += @("--url", $Url)
    }

    $process = Start-Process `
        -FilePath $ngrok.Source `
        -ArgumentList $arguments `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -PassThru

    $deadline = (Get-Date).AddSeconds([Math]::Max(10, $TimeoutSeconds))
    $tunnel = $null
    do {
        Start-Sleep -Milliseconds 500
        $process.Refresh()
        try {
            $tunnels = Invoke-RestMethod -Uri "http://127.0.0.1:$inspectionPort/api/tunnels" -TimeoutSec 2
            $tunnel = @(
                $tunnels.tunnels |
                    Where-Object {
                        [string]$_.name -eq "fab-managed" -and
                        [string]$_.public_url -like "https://*" -and
                        ([string]$_.config.addr) -match ":$($apiUri.Port)$"
                    }
            ) | Select-Object -First 1
        }
        catch {
            $tunnel = $null
        }
    } while (-not $tunnel -and -not $process.HasExited -and (Get-Date) -lt $deadline)

    if (-not $tunnel) {
        $exitCode = "unknown"
        if ($process.HasExited) {
            $process.WaitForExit()
            $process.Refresh()
            if ($null -ne $process.ExitCode) {
                $exitCode = [string]$process.ExitCode
            }
        }
        $diagnostic = @(
            Get-Content -LiteralPath $stderrPath -ErrorAction SilentlyContinue
            Get-Content -LiteralPath $stdoutPath -ErrorAction SilentlyContinue
        ) | Select-String -Pattern "ERR_NGROK_[0-9]+|authentication failed|failed to start|address already in use|bind:" |
            Select-Object -Last 1 -ExpandProperty Line
        $classified = if ($diagnostic) {
            ([string]$diagnostic -replace 'https?://\S+', '<endpoint>').Trim()
        }
        else {
            "no classified agent diagnostic was emitted"
        }
        throw "FAB ngrok startup failed with code $exitCode`: $classified"
    }

    $publicUrl = ([string]$tunnel.public_url).TrimEnd("/")
    $publicUri = [System.Uri]$publicUrl
    if (-not (Test-CleanHttpsOrigin -Uri $publicUri)) {
        throw "The managed ngrok endpoint is not a clean HTTPS origin."
    }
    $publicUrl = $publicUri.GetLeftPart([System.UriPartial]::Authority)
    if ((Get-HttpStatusCode -RequestUrl "$publicUrl/api/live") -ne 401) {
        throw "FAB ngrok authentication did not fail closed."
    }
    $remoteLive = Invoke-RestMethod -Uri "$publicUrl/api/live" -Headers $headers -TimeoutSec 20
    if (
        [string]$remoteLive.service -ne "fab-ledger-api" -or
        [string]$remoteLive.instanceId -ne $expectedInstanceId -or
        [string]$remoteLive.status -ne "ok" -or
        -not [bool]$remoteLive.authRequired
    ) {
        throw "The remote endpoint did not return this authenticated FAB instance."
    }
    $manifest = Invoke-RestMethod -Uri "$publicUrl/api/hai/manifest" -Headers $haiHeaders -TimeoutSec 20
    if ([string]$manifest.version -ne "fab-hai-connector-v1") {
        throw "The remote FAB HAI manifest could not be verified."
    }
    if ((Get-HttpStatusCode -RequestUrl "$publicUrl/api/health" -RequestHeaders $haiHeaders) -ne 403) {
        throw "HAI credential escaped its route scope."
    }

    $now = (Get-Date).ToUniversalTime().ToString("o")
    $cloudRuntime = [ordered]@{
        version = 1
        service = "fab-ngrok-tunnel"
        instanceRoot = $root
        instanceId = $expectedInstanceId
        processId = $process.Id
        inspectorPort = $inspectionPort
        publicUrl = $publicUrl
        localApiBaseUrl = $apiBaseUrl
        status = "active"
        startedAt = $now
        verifiedAt = $now
        authRequired = $true
        haiManifestUrl = "$publicUrl/api/hai/manifest"
        overlayPath = $overlayPath
        stdoutPath = $stdoutPath
        stderrPath = $stderrPath
    }
    Write-JsonAtomic -Value $cloudRuntime -Path $cloudRuntimePath
    $started = $true

    Write-Host "FAB cloud access is active." -ForegroundColor Green
    Write-Host "Endpoint: $publicUrl"
    Write-Host "HAI manifest: $publicUrl/api/hai/manifest"
    Write-Host "Authentication: required"
    Write-Host "Use Stop-FAB-Ngrok.cmd to stop only this managed tunnel."
}
finally {
    $apiToken = ""
    $haiApiToken = ""
    if (-not $started) {
        if ($process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            Wait-Process -Id $process.Id -Timeout 10 -ErrorAction SilentlyContinue
        }
        foreach ($path in @($overlayPath, $stdoutPath, $stderrPath)) {
            Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        }
        Remove-Item -LiteralPath $cloudRuntimePath -Force -ErrorAction SilentlyContinue
    }
}
