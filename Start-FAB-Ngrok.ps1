[CmdletBinding()]
param(
    [int]$TimeoutSeconds = 60,
    [string]$Url = "",
    [string]$DashboardUrl = ""
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
        throw "FAB could not verify ownership of the private ngrok inspector."
    }

    if (
        $listeners.Count -ne 1 -or
        [int]$listeners[0].OwningProcess -ne $ProcessId
    ) {
        throw "The private FAB ngrok inspector is not owned by the managed ngrok process."
    }
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

function ConvertTo-YamlScalar {
    param([Parameter(Mandatory = $true)][string]$Value)
    return ConvertTo-Json -InputObject $Value -Compress
}

function Protect-ManagedNgrokDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)

    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $userPermission = "{0}:(OI)(CI)F" -f $currentUser
    $systemPermission = "*S-1-5-18:(OI)(CI)F"
    $administratorsPermission = "*S-1-5-32-544:(OI)(CI)F"
    & (Join-Path $env:SystemRoot "System32\icacls.exe") `
        $Path /inheritance:r /grant:r $userPermission $systemPermission $administratorsPermission /Q | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not restrict access to FAB's managed ngrok files."
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

$requestedDashboardUrl = $null
if ($DashboardUrl) {
    try {
        $requestedDashboardUrl = [System.Uri]$DashboardUrl
    }
    catch {
        throw "-DashboardUrl must be a reserved HTTPS origin without credentials, path, query, or fragment."
    }
    if (-not (Test-CleanHttpsOrigin -Uri $requestedDashboardUrl)) {
        throw "-DashboardUrl must be a reserved HTTPS origin without credentials, path, query, or fragment."
    }
    $DashboardUrl = $requestedDashboardUrl.GetLeftPart([System.UriPartial]::Authority)
    if ($Url -and [string]::Equals($Url, $DashboardUrl, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "The dashboard and API require separate ngrok HTTPS origins."
    }
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
        $dashboardMatches = if ($DashboardUrl) {
            [string]$cloudStatus.dashboardUrl -eq $DashboardUrl
        }
        else {
            -not [string]$cloudStatus.dashboardUrl
        }
        if ([string]$cloudStatus.status -eq "active" -and [bool]$cloudStatus.active -and $dashboardMatches) {
            Write-Host "FAB cloud access is already active." -ForegroundColor Green
            Write-Host "Endpoint: $($cloudStatus.publicUrl)"
            if ($cloudStatus.dashboardUrl) {
                Write-Host "Operator dashboard: $($cloudStatus.dashboardUrl)"
            }
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
Protect-ManagedNgrokDirectory -Path $managedRoot
$overlayPath = Join-Path $managedRoot "fab-ngrok-$id.yml"
$stdoutPath = Join-Path $managedRoot "fab-ngrok-$id.out.log"
$stderrPath = Join-Path $managedRoot "fab-ngrok-$id.err.log"
$apiPort = [int]$apiUri.Port
$apiUpstream = ConvertTo-YamlScalar -Value "http://127.0.0.1:$apiPort"
$apiEndpointUrlYaml = ""
if ($Url) {
    $apiEndpointUrlYaml = "    url: $(ConvertTo-YamlScalar -Value $Url)"
}
$dashboardPort = 0
$dashboardEndpointYaml = ""
$dashboardHeaders = @{}
$dashboardPassword = $null
$dashboardSecurePassword = $null
$credential = $null
$basicValue = $null
$overlay = $null
$dashboardAuthRequired = $false
if ($DashboardUrl) {
    $dashboardRuntimeUrl = $null
    try {
        $dashboardRuntimeUrl = [System.Uri]$runtime.dashboardUrl
    }
    catch {
        throw "FAB's recorded dashboard URL is invalid. Restart FAB before enabling remote dashboard access."
    }
    if (
        $dashboardRuntimeUrl.Scheme -ne "http" -or
        $dashboardRuntimeUrl.Host -notin @("127.0.0.1", "localhost", "::1") -or
        $dashboardRuntimeUrl.AbsolutePath -ne "/admin/operations" -or
        $dashboardRuntimeUrl.Query -or
        $dashboardRuntimeUrl.Fragment
    ) {
        throw "The recorded FAB dashboard is not the expected loopback operator dashboard."
    }
    $dashboardPort = [int]$dashboardRuntimeUrl.Port
    $dashboardUsername = Read-Host "Create a username for the remote FAB dashboard (letters, numbers, dot, underscore, or hyphen)"
    if ($dashboardUsername -notmatch '^[A-Za-z0-9._-]{3,64}$') {
        throw "The dashboard username must be 3-64 characters and contain only letters, numbers, dot, underscore, or hyphen."
    }
    $dashboardSecurePassword = Read-Host "Create a remote FAB dashboard password (12-128 characters)" -AsSecureString
    $passwordPointer = [IntPtr]::Zero
    try {
        $passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($dashboardSecurePassword)
        $dashboardPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
    }
    finally {
        if ($passwordPointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
        }
        if ($dashboardSecurePassword) {
            $dashboardSecurePassword.Dispose()
        }
    }
    if (
        $dashboardPassword.Length -lt 12 -or
        $dashboardPassword.Length -gt 128 -or
        $dashboardPassword -match '[\r\n\x00-\x1f\x7f]'
    ) {
        $dashboardPassword = $null
        throw "The dashboard password must be 12-128 characters and contain no control characters."
    }
    $credential = ConvertTo-YamlScalar -Value "${dashboardUsername}:$dashboardPassword"
    $dashboardUrlYaml = ConvertTo-YamlScalar -Value $DashboardUrl
    $dashboardUpstream = ConvertTo-YamlScalar -Value "http://127.0.0.1:$dashboardPort"
    $dashboardEndpointYaml = @"
  - name: fab-managed-dashboard
    url: $dashboardUrlYaml
    upstream:
      url: $dashboardUpstream
    traffic_policy:
      on_http_request:
        - actions:
            - type: basic-auth
              config:
                realm: FAB Operator Dashboard
                enforce: true
                credentials:
                  - $credential
            - type: remove-headers
              config:
                headers:
                  - authorization
"@
    $basicValue = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("${dashboardUsername}:$dashboardPassword"))
    $dashboardHeaders = @{ Authorization = "Basic $basicValue" }
    $dashboardPassword = $null
    $basicValue = $null
    $dashboardAuthRequired = $true
}
$overlay = @"
version: 3
agent:
  web_addr: 127.0.0.1:$inspectionPort
  log: stdout
  log_format: json
endpoints:
  - name: fab-managed
$apiEndpointUrlYaml
    upstream:
      url: $apiUpstream
$dashboardEndpointYaml
"@
[System.IO.File]::WriteAllText(
    $overlayPath,
    $overlay,
    (New-Object System.Text.UTF8Encoding($false))
)

$process = $null
$tunnel = $null
$publicUrl = ""
$started = $false
try {
    & $ngrok.Source config check --config $mainConfig --config $overlayPath | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "The isolated FAB ngrok configuration is invalid."
    }

    $arguments = @("start", "fab-managed")
    if ($DashboardUrl) {
        $arguments += "fab-managed-dashboard"
    }
    $arguments += @(
        "--config", ('"' + $mainConfig + '"'),
        "--config", ('"' + $overlayPath + '"')
    )

    $process = Start-Process `
        -FilePath $ngrok.Source `
        -ArgumentList $arguments `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -PassThru

    $deadline = (Get-Date).AddSeconds([Math]::Max(10, $TimeoutSeconds))
    $apiTunnel = $null
    $dashboardTunnel = $null
    do {
        Start-Sleep -Milliseconds 500
        $process.Refresh()
        try {
            $tunnels = Invoke-RestMethod -Uri "http://127.0.0.1:$inspectionPort/api/tunnels" -TimeoutSec 2
            $apiTunnel = @(
                $tunnels.tunnels |
                    Where-Object {
                        [string]$_.name -eq "fab-managed" -and
                        [string]$_.public_url -like "https://*" -and
                        ([string]$_.config.addr) -match ":$apiPort$"
                    }
            ) | Select-Object -First 1
            if ($DashboardUrl) {
                $dashboardTunnel = @(
                    $tunnels.tunnels |
                        Where-Object {
                            [string]$_.name -eq "fab-managed-dashboard" -and
                            [string]$_.public_url -eq $DashboardUrl -and
                            ([string]$_.config.addr) -match ":$dashboardPort$"
                        }
                ) | Select-Object -First 1
            }
        }
        catch {
            $apiTunnel = $null
            $dashboardTunnel = $null
        }
    } while ((-not $apiTunnel -or ($DashboardUrl -and -not $dashboardTunnel)) -and -not $process.HasExited -and (Get-Date) -lt $deadline)

    if (-not $apiTunnel -or ($DashboardUrl -and -not $dashboardTunnel)) {
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

    Assert-FabNgrokInspectorOwned -Port $inspectionPort -ProcessId $process.Id

    $publicUrl = ([string]$apiTunnel.public_url).TrimEnd("/")
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

    $dashboardPublicUrl = $null
    if ($DashboardUrl) {
        $dashboardPublicUrl = ([string]$dashboardTunnel.public_url).TrimEnd("/")
        $dashboardPublicUri = [System.Uri]$dashboardPublicUrl
        if (-not (Test-CleanHttpsOrigin -Uri $dashboardPublicUri) -or $dashboardPublicUrl -ne $DashboardUrl) {
            throw "The remote dashboard endpoint did not match its reserved HTTPS origin."
        }
        if ((Get-HttpStatusCode -RequestUrl "$dashboardPublicUrl/admin/operations") -ne 401) {
            throw "The remote dashboard did not reject an unauthenticated request."
        }
        if ((Get-HttpStatusCode -RequestUrl "$dashboardPublicUrl/admin/operations" -RequestHeaders $dashboardHeaders) -ne 200) {
            throw "The authenticated remote dashboard could not be reached."
        }
    }

    $dashboardRuntimePort = $null
    if ($dashboardPublicUrl) {
        $dashboardRuntimePort = $dashboardPort
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
        dashboardUrl = $dashboardPublicUrl
        dashboardPort = $dashboardRuntimePort
        dashboardAuthRequired = $dashboardAuthRequired
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
    if ($dashboardPublicUrl) {
        Write-Host "Operator dashboard: $dashboardPublicUrl (ngrok Basic Auth required)"
    }
    Write-Host "HAI manifest: $publicUrl/api/hai/manifest"
    Write-Host "Authentication: required"
    Write-Host "Use Stop-FAB-Ngrok.cmd to stop only this managed tunnel."
}
finally {
    $apiToken = ""
    $haiApiToken = ""
    $dashboardHeaders.Clear()
    $dashboardPassword = $null
    $dashboardSecurePassword = $null
    $credential = $null
    $basicValue = $null
    $overlay = $null
    if (-not $started) {
        $processStopped = $true
        if ($process) {
            $processStopped = Stop-FabStartedNgrokProcess -Process $process
        }
        if ($processStopped) {
            foreach ($path in @($overlayPath, $stdoutPath, $stderrPath)) {
                Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
            }
            Remove-Item -LiteralPath $cloudRuntimePath -Force -ErrorAction SilentlyContinue
        }
        else {
            $cleanupRuntime = [ordered]@{
                version = 1
                service = "fab-ngrok-tunnel"
                instanceRoot = $root
                instanceId = $expectedInstanceId
                processId = $process.Id
                inspectorPort = $inspectionPort
                publicUrl = $publicUrl
                dashboardUrl = $dashboardPublicUrl
                dashboardPort = $dashboardRuntimePort
                dashboardAuthRequired = $dashboardAuthRequired
                localApiBaseUrl = $apiBaseUrl
                status = "cleanup_required"
                authRequired = $true
                haiManifestUrl = if ($publicUrl) { "$publicUrl/api/hai/manifest" } else { "" }
                overlayPath = $overlayPath
                stdoutPath = $stdoutPath
                stderrPath = $stderrPath
            }
            try {
                Write-JsonAtomic -Value $cleanupRuntime -Path $cloudRuntimePath
                Write-Warning "FAB could not confirm that the failed ngrok startup stopped. Ownership metadata and agent logs were retained; run Stop-FAB-Ngrok.cmd to retry cleanup."
            }
            catch {
                Write-Warning "FAB could not confirm that ngrok stopped or persist its cleanup record. Agent files were retained under $managedRoot; inspect PID $($process.Id) before removing them."
            }
        }
    }
}
