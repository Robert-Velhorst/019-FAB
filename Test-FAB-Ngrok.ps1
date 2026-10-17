[CmdletBinding()]
param(
    [int]$TimeoutSeconds = 45,
    [string]$Url = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimePath = Join-Path $root "data\fab-runtime.json"
$mainConfig = Join-Path $env:LOCALAPPDATA "ngrok\ngrok.yml"
$ngrok = Get-Command ngrok -ErrorAction Stop
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

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

if (-not (Test-Path -LiteralPath $runtimePath)) {
    throw "FAB is not running. Start it with Start-FAB.cmd first."
}
if (-not (Test-Path -LiteralPath $mainConfig)) {
    throw "ngrok is not configured for this Windows user."
}
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "FAB's isolated Python runtime is missing. Run Start-FAB.cmd first."
}

$runtime = Get-Content -LiteralPath $runtimePath -Raw | ConvertFrom-Json
$maintenanceProperty = $runtime.PSObject.Properties["maintenanceMode"]
if ($maintenanceProperty -and [bool]$maintenanceProperty.Value) {
    throw "FAB cloud verification is disabled during maintenance. Restart FAB in standard mode first."
}
$apiBaseUrl = [string]$runtime.apiBaseUrl
$apiUri = [System.Uri]$apiBaseUrl
if (
    -not $apiUri.IsAbsoluteUri -or
    $apiUri.Scheme -ne "http" -or
    $apiUri.Host -notin @("127.0.0.1", "localhost", "::1") -or
    $apiUri.AbsolutePath -ne "/" -or
    $apiUri.UserInfo -or
    $apiUri.Query -or
    $apiUri.Fragment
) {
    throw "FAB ngrok verification only accepts a loopback API endpoint."
}
if ($Url) {
    $requestedUrl = [System.Uri]$Url
    if (
        -not $requestedUrl.IsAbsoluteUri -or
        $requestedUrl.Scheme -ne "https" -or
        -not $requestedUrl.Host -or
        $requestedUrl.UserInfo -or
        $requestedUrl.AbsolutePath -ne "/" -or
        $requestedUrl.Query -or
        $requestedUrl.Fragment
    ) {
        throw "-Url must be a complete HTTPS origin without credentials, path, query, or fragment."
    }
    $Url = $requestedUrl.GetLeftPart([System.UriPartial]::Authority)
}

$credentialsJson = & $venvPython -m src.security.windows_runtime_credentials --require-strong
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
$localLive = Invoke-RestMethod -Uri "$apiBaseUrl/api/live" -Headers @{ Authorization = "Bearer $apiToken" } -TimeoutSec 5
if (
    [string]$localLive.service -ne "fab-ledger-api" -or
    [string]$localLive.instanceId -ne (Get-FabInstanceId -Path $root) -or
    -not [bool]$localLive.authRequired -or
    [bool]$localLive.maintenanceMode
) {
    throw "FAB cloud verification requires the standard local runtime."
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

function Find-AvailableLoopbackPort {
    param([int]$StartPort)
    foreach ($port in $StartPort..($StartPort + 20)) {
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
    throw "No free ngrok inspection port is available."
}

function Stop-FabStartedNgrokProcess {
    param([Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process)

    try {
        $Process.Refresh()
        if (-not $Process.HasExited) {
            $Process.Kill()
            if (-not $Process.WaitForExit(10000)) {
                return $false
            }
        }
        $Process.Refresh()
        return [bool]$Process.HasExited
    }
    catch {
        return $false
    }
}

function Assert-FabNgrokInspectorOwned {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][int]$ProcessId
    )

    try {
        $listeners = @(
            Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop |
                Where-Object { [string]$_.LocalAddress -eq "127.0.0.1" }
        )
    }
    catch {
        throw "FAB could not verify ownership of the temporary ngrok inspector."
    }

    if ($listeners.Count -ne 1 -or [int]$listeners[0].OwningProcess -ne $ProcessId) {
        throw "The temporary FAB ngrok inspector is not owned by the launched ngrok process."
    }
}

function Test-FabCleanHttpsOrigin {
    param([Parameter(Mandatory = $true)][System.Uri]$Uri)
    return $Uri.IsAbsoluteUri -and $Uri.Scheme -eq "https" -and $Uri.Host `
        -and -not $Uri.UserInfo -and $Uri.AbsolutePath -eq "/" `
        -and -not $Uri.Query -and -not $Uri.Fragment
}

function Complete-FabNgrokVerificationCleanup {
    param(
        [AllowNull()][System.Diagnostics.Process]$Process,
        [Parameter(Mandatory = $true)][string[]]$Paths
    )

    $processStopped = $true
    if ($Process) {
        $processStopped = Stop-FabStartedNgrokProcess -Process $Process
    }
    if ($processStopped) {
        foreach ($path in $Paths) {
            Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        }
        return $true
    }

    $processId = 0
    try {
        $processId = $Process.Id
    }
    catch {
    }
    Write-Warning "FAB could not confirm that the verification ngrok process stopped. Its configuration and logs were retained; inspect PID $processId before removing them: $($Paths -join ', ')"
    return $false
}

$inspectionPort = Find-AvailableLoopbackPort -StartPort 4041
$id = [Guid]::NewGuid().ToString("N")
$overlayPath = Join-Path $env:TEMP "fab-ngrok-$id.yml"
$stdoutPath = Join-Path $env:TEMP "fab-ngrok-$id.out.log"
$stderrPath = Join-Path $env:TEMP "fab-ngrok-$id.err.log"
$overlay = "version: 3`nagent:`n  web_addr: 127.0.0.1:$inspectionPort`n"
[System.IO.File]::WriteAllText(
    $overlayPath,
    $overlay,
    (New-Object System.Text.UTF8Encoding($false))
)

$process = $null
try {
    & $ngrok.Source config check --config $mainConfig --config $overlayPath | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "The isolated ngrok verification configuration is invalid."
    }

    $arguments = @(
        "http", $apiUri.Port,
        "--config", ('"' + $mainConfig + '"'),
        "--config", ('"' + $overlayPath + '"'),
        "--log", "stdout",
        "--log-format", "json",
        "--name", "fab-verification"
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

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $tunnel = $null
    do {
        Start-Sleep -Milliseconds 500
        try {
            $tunnels = Invoke-RestMethod `
                -Uri "http://127.0.0.1:$inspectionPort/api/tunnels" `
                -TimeoutSec 2
            $tunnel = @(
                $tunnels.tunnels |
                    Where-Object {
                        $_.public_url -like "https://*" -and
                        ([string]$_.config.addr) -match ":$($apiUri.Port)$"
                    }
            ) | Select-Object -First 1
        }
        catch {
            $tunnel = $null
        }
    } while (-not $tunnel -and -not $process.HasExited -and (Get-Date) -lt $deadline)

    if (-not $tunnel) {
        $reason = if ($process.HasExited) {
            $process.WaitForExit()
            $process.Refresh()
            $exitCode = if ($null -eq $process.ExitCode) { "unknown" } else { [string]$process.ExitCode }
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
            "ngrok exited with code $exitCode`: $classified"
        }
        else {
            "the temporary endpoint did not become ready"
        }
        throw "FAB ngrok verification failed: $reason."
    }

    Assert-FabNgrokInspectorOwned -Port $inspectionPort -ProcessId $process.Id
    $publicUri = [System.Uri]([string]$tunnel.public_url)
    if (-not (Test-FabCleanHttpsOrigin -Uri $publicUri)) {
        throw "The temporary ngrok endpoint is not a clean HTTPS origin."
    }
    $publicUrl = $publicUri.GetLeftPart([System.UriPartial]::Authority)

    $liveUrl = "$publicUrl/api/live"
    $unauthorizedStatus = 0
    try {
        Invoke-WebRequest -Uri $liveUrl -UseBasicParsing -TimeoutSec 15 | Out-Null
        $unauthorizedStatus = 200
    }
    catch {
        if ($_.Exception.Response) {
            $unauthorizedStatus = [int]$_.Exception.Response.StatusCode
        }
    }

    $authorized = Invoke-RestMethod `
        -Uri $liveUrl `
        -Headers @{ Authorization = "Bearer $apiToken" } `
        -TimeoutSec 20
    $manifest = Invoke-RestMethod `
        -Uri "$publicUrl/api/hai/manifest" `
        -Headers @{ Authorization = "Bearer $haiApiToken" } `
        -TimeoutSec 20
    $haiOperatorStatus = 0
    try {
        Invoke-WebRequest `
            -Uri "$publicUrl/api/health" `
            -Headers @{ Authorization = "Bearer $haiApiToken" } `
            -UseBasicParsing `
            -TimeoutSec 20 | Out-Null
        $haiOperatorStatus = 200
    }
    catch {
        if ($_.Exception.Response) {
            $haiOperatorStatus = [int]$_.Exception.Response.StatusCode
        }
    }
    if (
        $unauthorizedStatus -ne 401 -or
        [string]$authorized.status -ne "ok" -or
        [string]$authorized.instanceId -ne (Get-FabInstanceId -Path $root) -or
        -not [bool]$authorized.authRequired -or
        [string]$manifest.version -ne "fab-hai-connector-v1" -or
        $haiOperatorStatus -ne 403
    ) {
        throw "FAB ngrok authentication did not fail closed."
    }

    Write-Host "FAB ngrok HTTPS verification passed." -ForegroundColor Green
    Write-Host "Unauthenticated request: 401"
    Write-Host "Authenticated liveness: ok"
    Write-Host "Authenticated HAI manifest: ok"
    Write-Host "The temporary FAB tunnel will now be stopped."
}
finally {
    $apiToken = ""
    $haiApiToken = ""
    Complete-FabNgrokVerificationCleanup `
        -Process $process `
        -Paths @($overlayPath, $stdoutPath, $stderrPath) | Out-Null
}
