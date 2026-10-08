[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$webRoot = Join-Path $root "web"
$runtimePath = Join-Path $root "data\fab-runtime.json"
$workerRuntimePath = Join-Path $root "data\fab-worker-runtime.json"
$cloudRuntimePath = Join-Path $root "data\fab-ngrok-runtime.json"
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

Set-Location -LiteralPath $root
. (Join-Path $root 'scripts\Windows-Process.ps1')
. (Join-Path $root 'scripts\Windows-Job.ps1')
$lifecycleLock = Enter-FabLifecycleLock -Root $root
try {
$script:fabStopProcesses = @{}

function Register-FabStopProcess {
    param([Parameter(Mandatory = $true)][object]$Row)

    $processId = [int]$Row.ProcessId
    if ($script:fabStopProcesses.ContainsKey($processId)) {
        $retained = $script:fabStopProcesses[$processId]
        $createdAt = ([DateTime]$Row.CreationDate).ToUniversalTime()
        if ([Math]::Abs($retained.StartTime.ToUniversalTime().Ticks - $createdAt.Ticks) -ge 10) {
            throw 'FAB process identity changed after discovery. Runtime state was retained.'
        }
    }
    else {
        $script:fabStopProcesses[$processId] = Get-FabProcessReference -Row $Row
    }
    return $processId
}

function Stop-FabRecordedProcess {
    param([Parameter(Mandatory = $true)][object]$Identity)

    $row = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$Identity.rootPid)" -OperationTimeoutSec 2 -ErrorAction Stop
    $process = $null
    if ($row) {
        $processId = Register-FabStopProcess -Row $row
        $process = $script:fabStopProcesses[$processId]
        if ([string]$process.StartTime.ToUniversalTime().Ticks -ne [string]$Identity.startedAtTicks) {
            throw 'Recorded FAB process identity no longer matches. Shutdown state was retained.'
        }
    }
    Initialize-FabWindowsJob
    $lease = [Fab.Windows.JobLease]::Open([string]$Identity.jobName, $process)
    try {
        if ($null -ne $lease) { $lease.Terminate() }
        elseif ($null -ne $process -and -not $process.HasExited) {
            throw 'Recorded FAB job is missing while its original root is alive. Shutdown state was retained.'
        }
    }
    finally { if ($null -ne $lease) { $lease.Dispose() } }
}

function Test-FabRuntimeContainmentCoverage {
    param([Parameter(Mandatory = $true)][object]$Runtime)

    foreach ($service in @('api', 'worker', 'web')) {
        $pidProperty = $Runtime.PSObject.Properties[$service + 'Pid']
        if (-not $pidProperty -or -not $pidProperty.Value) { continue }
        $groups = $Runtime.PSObject.Properties['processes']
        if (-not $groups -or $null -eq $groups.Value) { return $false }
        $identity = $groups.Value.PSObject.Properties[$service]
        if (-not $identity -or $null -eq $identity.Value) { return $false }
        foreach ($field in @('rootPid', 'startedAtTicks', 'jobName')) {
            if (-not $identity.Value.PSObject.Properties[$field]) { return $false }
        }
        if ([int]$identity.Value.rootPid -ne [int]$pidProperty.Value) { return $false }
    }
    return $true
}

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

function Get-FabProcessId {
    param(
        [AllowNull()][object]$ProcessId,
        [Parameter(Mandatory = $true)][string]$CommandMarker,
        [string]$ExpectedRoot = ''
    )

    if (-not $ProcessId) {
        return $null
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -OperationTimeoutSec 2 -ErrorAction Stop
    if (-not $process -or -not $process.CommandLine -or $process.CommandLine -notlike "*$CommandMarker*") {
        return $null
    }
    if ($ExpectedRoot) {
        $expectedPython = (Join-Path ([IO.Path]::GetFullPath($ExpectedRoot)) '.venv\Scripts\python.exe').Replace('\', '/')
        $command = ([string]$process.CommandLine).Replace('\', '/')
        $image = ([string]$process.ExecutablePath).Replace('\', '/')
        $comparison = [StringComparison]::OrdinalIgnoreCase
        $matchesPython = (
            $image.Equals($expectedPython, $comparison) -or
            $command.StartsWith('"' + $expectedPython + '" ', $comparison) -or
            $command.StartsWith($expectedPython + ' ', $comparison)
        )
        $ancestor = $process
        for ($depth = 0; -not $matchesPython -and $depth -lt 12; $depth++) {
            if (-not $ancestor.PSObject.Properties['ParentProcessId'] -or -not $ancestor.ParentProcessId) { break }
            $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($ancestor.ParentProcessId)" -OperationTimeoutSec 2 -ErrorAction Stop
            if (-not $parent -or [string]$parent.Name -ne 'python.exe' -or
                ([DateTime]$parent.CreationDate).ToUniversalTime() -gt ([DateTime]$ancestor.CreationDate).ToUniversalTime()) { break }
            [void](Register-FabStopProcess -Row $parent)
            $parentCommand = ([string]$parent.CommandLine).Replace('\', '/')
            $matchesPython = (
                ([string]$parent.ExecutablePath).Replace('\', '/').Equals($expectedPython, $comparison) -or
                $parentCommand.StartsWith('"' + $expectedPython + '" ', $comparison) -or
                $parentCommand.StartsWith($expectedPython + ' ', $comparison)
            )
            $ancestor = $parent
        }
        if ([string]$process.Name -ne 'python.exe' -or -not $matchesPython) {
            throw 'Worker registration does not identify this checkout Python runtime. Shutdown state was retained.'
        }
    }
    return Register-FabStopProcess -Row $process
}

function Test-FabDashboardProcess {
    param(
        [AllowNull()][object]$Process,
        [Parameter(Mandatory = $true)][string]$ExpectedWebRoot
    )

    if (-not $Process -or -not $Process.CommandLine) {
        return $false
    }

    $name = ([string]$Process.Name).ToLowerInvariant()
    if ($name -notin @("node.exe", "cmd.exe")) {
        return $false
    }

    $command = ([string]$Process.CommandLine).Replace("\", "/").ToLowerInvariant()
    $webRootMarker = [System.IO.Path]::GetFullPath($ExpectedWebRoot).Replace("\", "/").TrimEnd("/").ToLowerInvariant()
    if ($command -match ('(?:^|[\s"''])(?:' + [regex]::Escape($webRootMarker) + '/)')) {
        return (
            $command.Contains("server/dev.ts") -or
            $command.Contains("dist/fab-standalone.js") -or
            $command.Contains("dist/index.js") -or
            $command.Contains("tsx") -or
            $command.Contains("pnpm") -or
            $command.Contains("npm-cli.js")
        )
    }

    return $false
}

function Get-FabDashboardProcessId {
    param(
        [AllowNull()][object]$ProcessId,
        [Parameter(Mandatory = $true)][string]$ExpectedWebRoot
    )

    if (-not $ProcessId) {
        return $null
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -OperationTimeoutSec 2 -ErrorAction Stop
    $command = if ($process -and $process.CommandLine) { ([string]$process.CommandLine).Replace("\", "/").ToLowerInvariant() } else { "" }
    $webRootMarker = [System.IO.Path]::GetFullPath($ExpectedWebRoot).Replace("\", "/").TrimEnd("/").ToLowerInvariant()
    if (-not $command.Contains($webRootMarker)) {
        return $null
    }
    if (-not (Test-FabDashboardProcess -Process $process -ExpectedWebRoot $ExpectedWebRoot)) {
        return $null
    }
    return Register-FabStopProcess -Row $process
}

function Get-FabDashboardProcessRoot {
    param(
        [Parameter(Mandatory = $true)][int]$ListenerProcessId,
        [Parameter(Mandatory = $true)][string]$ExpectedWebRoot
    )

    $currentId = $ListenerProcessId
    $highestOwnedId = $ListenerProcessId
    for ($depth = 0; $depth -lt 8; $depth++) {
        $current = Get-CimInstance Win32_Process -Filter "ProcessId = $currentId" -OperationTimeoutSec 2 -ErrorAction Stop
        if (-not (Test-FabDashboardProcess -Process $current -ExpectedWebRoot $ExpectedWebRoot)) {
            break
        }
        $highestOwnedId = Register-FabStopProcess -Row $current
        if (-not $current.ParentProcessId) {
            break
        }
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($current.ParentProcessId)" -OperationTimeoutSec 2 -ErrorAction Stop
        if (-not (Test-FabDashboardProcess -Process $parent -ExpectedWebRoot $ExpectedWebRoot)) {
            break
        }
        if (([DateTime]$parent.CreationDate).ToUniversalTime() -gt ([DateTime]$current.CreationDate).ToUniversalTime()) {
            break
        }
        $currentId = [int]$parent.ProcessId
    }
    return $highestOwnedId
}

function Test-FabProcessAncestor {
    param(
        [Parameter(Mandatory = $true)][int]$AncestorProcessId,
        [Parameter(Mandatory = $true)][int]$DescendantProcessId
    )

    $currentId = $DescendantProcessId
    for ($depth = 0; $depth -lt 12 -and $currentId; $depth++) {
        if ($currentId -eq $AncestorProcessId) {
            return $true
        }
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $currentId" -OperationTimeoutSec 2 -ErrorAction Stop
        if (-not $process -or -not $process.ParentProcessId) {
            break
        }
        $currentId = [int]$process.ParentProcessId
    }
    return $false
}

function Get-FabListenerProcessId {
    param([Parameter(Mandatory = $true)][string]$Url)

    try {
        $uri = [System.Uri]$Url
        if ($uri.Host -notin @("127.0.0.1", "localhost", "::1")) {
            return $null
        }
        $listener = Get-NetTCPConnection -State Listen -LocalPort $uri.Port -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalAddress -in @("127.0.0.1", "::1") } |
            Select-Object -First 1
        if ($listener) {
            return [int]$listener.OwningProcess
        }
    }
    catch {
        return $null
    }
    return $null
}

function Find-RunningFabDashboardProcessIds {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedRoot,
        [Parameter(Mandatory = $true)][string]$ExpectedWebRoot
    )

    $matches = [System.Collections.Generic.List[int]]::new()
    $processes = Get-CimInstance Win32_Process -OperationTimeoutSec 2 -ErrorAction Stop |
        Where-Object {
            $_.Name -eq "node.exe" -and
            (Test-FabDashboardProcess -Process $_ -ExpectedWebRoot $ExpectedWebRoot)
        }
    foreach ($process in $processes) {
        try {
            [void](Register-FabStopProcess -Row $process)
            $listeners = Get-NetTCPConnection -State Listen -OwningProcess $process.ProcessId -ErrorAction Stop |
                Where-Object { $_.LocalAddress -in @("127.0.0.1", "::1") } |
                Sort-Object LocalPort -Unique
        }
        catch { $script:stopFailed = $true; Write-Warning $_.Exception.Message -WarningAction Continue; continue }
        $verified = $false
        try { foreach ($listener in $listeners) {
            $identityUrl = "http://127.0.0.1:$($listener.LocalPort)/api/fab/runtime"
            if (Test-FabEndpoint -Url $identityUrl -ExpectedService "fab-operator-dashboard" -ExpectedInstanceRoot $ExpectedRoot) {
                $verified = $true
                $rootProcessId = Get-FabDashboardProcessRoot -ListenerProcessId ([int]$process.ProcessId) -ExpectedWebRoot $ExpectedWebRoot
                if (-not $matches.Contains($rootProcessId)) {
                    $matches.Add($rootProcessId)
                }
                break
            }
        } }
        catch { $script:stopFailed = $true; Write-Warning $_.Exception.Message -WarningAction Continue; continue }
        if (-not $verified -and -not $script:fabStopProcesses[[int]$process.ProcessId].HasExited) {
            $script:stopFailed = $true
        }
    }
    return @($matches)
}

function Test-FabEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$ExpectedService,
        [string]$ApiToken = "",
        [string]$ExpectedInstanceRoot = "",
        [switch]$AllowLegacyInstance
    )

    try {
        $uri = [System.Uri]$Url
        $probeHost = $uri.DnsSafeHost
        if ($probeHost -ne 'localhost') { $probeHost = ([Net.IPAddress]$probeHost).ToString() }
        if (-not $uri.IsAbsoluteUri -or $uri.Scheme -notin @('http', 'https') -or
            $probeHost -notin @('127.0.0.1', 'localhost', '::1') -or $uri.UserInfo) { return $false }
        $request = @{
            Uri = $Url
            UseBasicParsing = $true
            TimeoutSec = 5
            MaximumRedirection = 0
        }
        if ($ApiToken) {
            $request.Headers = @{ Authorization = "Bearer $ApiToken" }
        }
        $response = Invoke-RestMethod @request
        if ([string]$response.service -ne $ExpectedService) {
            return $false
        }
        if ($ExpectedInstanceRoot) {
            $instanceIdProperty = $response.PSObject.Properties["instanceId"]
            $instanceId = if ($instanceIdProperty) { [string]$instanceIdProperty.Value } else { "" }
            if (-not $instanceId -and $AllowLegacyInstance) {
                $legacyRootProperty = $response.PSObject.Properties["instanceRoot"]
                $legacyRoot = if ($legacyRootProperty) { [string]$legacyRootProperty.Value } else { "" }
                if (-not $legacyRoot) {
                    return $false
                }
                $actualLegacyRoot = [System.IO.Path]::GetFullPath($legacyRoot).TrimEnd("\", "/")
                $expectedLegacyRoot = [System.IO.Path]::GetFullPath($ExpectedInstanceRoot).TrimEnd("\", "/")
                return $actualLegacyRoot -eq $expectedLegacyRoot
            }
            $expectedInstanceId = Get-FabInstanceId -Path $ExpectedInstanceRoot
            if (-not $instanceId -or $instanceId -ne $expectedInstanceId) {
                return $false
            }
        }
        return $true
    }
    catch {
        return $false
    }
}

function Find-RunningFabApiProcessIds {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedRoot,
        [string]$ApiToken = ""
    )

    $matches = [System.Collections.Generic.List[int]]::new()
    $processes = Get-CimInstance Win32_Process -OperationTimeoutSec 2 -ErrorAction Stop |
        Where-Object {
            $_.Name -eq "python.exe" -and
            $_.CommandLine -and
            $_.CommandLine -like "*src.operations.local_api*"
        }
    foreach ($process in $processes) {
        $command = ([string]$process.CommandLine).Replace('\', '/')
        $expectedPython = (Join-Path ([IO.Path]::GetFullPath($ExpectedRoot)) '.venv\Scripts\python.exe').Replace('\', '/')
        if (-not $command.StartsWith('"' + $expectedPython + '" ', [StringComparison]::OrdinalIgnoreCase) -and
            -not $command.StartsWith($expectedPython + ' ', [StringComparison]::OrdinalIgnoreCase)) { continue }
        try {
            [void](Register-FabStopProcess -Row $process)
            $listeners = Get-NetTCPConnection -State Listen -OwningProcess $process.ProcessId -ErrorAction Stop |
                Where-Object { $_.LocalAddress -in @("127.0.0.1", "::1") } |
                Sort-Object LocalPort -Unique
        }
        catch { $script:stopFailed = $true; Write-Warning $_.Exception.Message -WarningAction Continue; continue }
        foreach ($listener in $listeners) {
            $baseUrl = "http://127.0.0.1:$($listener.LocalPort)"
            foreach ($path in @("/api/live", "/api/health")) {
                if (Test-FabEndpoint -Url "$baseUrl$path" -ExpectedService "fab-ledger-api" -ApiToken $ApiToken -ExpectedInstanceRoot $ExpectedRoot -AllowLegacyInstance) {
                    $matches.Add([int]$process.ProcessId)
                    break
                }
            }
            if ($matches.Contains([int]$process.ProcessId)) {
                break
            }
        }
        if (-not $matches.Contains([int]$process.ProcessId) -and -not $script:fabStopProcesses[[int]$process.ProcessId].HasExited) {
            $script:stopFailed = $true
        }
    }
    return @($matches | Select-Object -Unique)
}

function Get-FabWorkerRuntimeProcessId {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedRoot
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    $runtime = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    $actualRoot = [System.IO.Path]::GetFullPath([string]$runtime.instanceRoot).TrimEnd("\", "/")
    $expected = [System.IO.Path]::GetFullPath($ExpectedRoot).TrimEnd("\", "/")
    if ($actualRoot -ne $expected) {
        throw 'Worker runtime metadata belongs to another checkout. Shutdown state was retained.'
    }
    return Get-FabProcessId -ProcessId $runtime.pid -CommandMarker "src.run_worker" -ExpectedRoot $ExpectedRoot
}

function Stop-FabProcessTree {
    param(
        [AllowNull()][object]$ProcessId,
        [Parameter(Mandatory = $true)][string]$CommandMarker,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if (-not $ProcessId) {
        return
    }

    if (-not $script:fabStopProcesses.ContainsKey([int]$ProcessId)) {
        throw "Refusing to stop $Name without a retained process identity."
    }
    $retained = $script:fabStopProcesses[[int]$ProcessId]
    if ($retained.HasExited) {
        if (-not $retained.PSObject.Properties['FabJob']) {
            throw 'Exited legacy FAB root has no containment proof. Shutdown state was retained.'
        }
        return
    }
    $rootProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -OperationTimeoutSec 2 -ErrorAction Stop
    if (-not $rootProcess) {
        throw "Could not verify $Name before stopping it."
    }
    if (-not $rootProcess.CommandLine -or $rootProcess.CommandLine -notlike "*$CommandMarker*") {
        throw "Refusing to stop PID $ProcessId because it no longer matches $Name."
    }
    [void](Register-FabStopProcess -Row $rootProcess)
    Stop-FabOwnedProcessTree -Process $retained
    if (-not $retained.PSObject.Properties['FabJob']) {
        throw 'Legacy FAB service has no recorded containment proof. Verify its descendants before clearing shutdown state.'
    }
    Write-Host "Stopped $Name."
}

function Stop-FabDashboardProcessTree {
    param(
        [AllowNull()][object]$ProcessId,
        [Parameter(Mandatory = $true)][string]$ExpectedWebRoot
    )

    if (-not $ProcessId) {
        return
    }
    if (-not $script:fabStopProcesses.ContainsKey([int]$ProcessId)) {
        throw 'Refusing to stop the dashboard without a retained process identity.'
    }
    $retained = $script:fabStopProcesses[[int]$ProcessId]
    if ($retained.HasExited) {
        if (-not $retained.PSObject.Properties['FabJob']) {
            throw 'Exited legacy dashboard root has no containment proof. Shutdown state was retained.'
        }
        return
    }
    $rootProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -OperationTimeoutSec 2 -ErrorAction Stop
    if (-not $rootProcess) {
        throw 'Could not verify the dashboard before stopping it.'
    }
    if (-not (Test-FabDashboardProcess -Process $rootProcess -ExpectedWebRoot $ExpectedWebRoot)) {
        throw "Refusing to stop PID $ProcessId because it no longer matches the FAB dashboard."
    }

    [void](Register-FabStopProcess -Row $rootProcess)
    Stop-FabOwnedProcessTree -Process $retained
    if (-not $retained.PSObject.Properties['FabJob']) {
        throw 'Legacy dashboard has no recorded containment proof. Verify its descendants before clearing shutdown state.'
    }
    Write-Host "Stopped FAB dashboard."
}

# Discover and stop validated instances while retaining their original handles.
try {
$runtime = $null
$stopFailed = $false
if (Test-Path -LiteralPath $runtimePath) {
    try {
        $runtime = Get-Content -LiteralPath $runtimePath -Raw | ConvertFrom-Json
        if ($null -eq $runtime) { throw 'Runtime metadata is empty.' }
    }
    catch {
        $stopFailed = $true
        Write-Warning "Runtime metadata is unreadable and will be retained at $runtimePath." -WarningAction Continue
    }
}

$apiToken = [string]$env:FAB_LOCAL_API_TOKEN
try {
    if ($apiToken.Length -lt 32) {
        if (-not (Test-Path -LiteralPath $venvPython)) {
            throw "FAB's isolated Python runtime is missing."
        }
        $apiToken = & $venvPython -c "from src.config_loader import ConfigLoader; from src.security.local_secret_store import LocalSecretStore; c=ConfigLoader('config/config.ini').get_all_config(); token=c.get('fab_local_api_token', c.get('fab_operations_api_token', c.get('operations_api_token', ''))); token=token if len(str(token or '')) >= 32 else ''; p={} if token else LocalSecretStore(c).load(); print(str(token or (p.get('runtime') or {}).get('operator_api_token') or ''))"
        if ($LASTEXITCODE -ne 0) {
            throw "FAB could not resolve its configured API credential."
        }
    }
}
catch {
    Write-Warning "FAB could not read its API token while recovering runtime ownership."
}
$apiToken = [string]$apiToken

if (Test-Path -LiteralPath $cloudRuntimePath) {
    try {
        & (Join-Path $root "Stop-FAB-Ngrok.ps1") -Quiet
    }
    catch {
        $stopFailed = $true
        Write-Warning "FAB could not stop the managed ngrok process safely: $($_.Exception.Message)" -WarningAction Continue
    }
}

$apiPid = $null
$workerPid = $null
$webPid = $null
$webPids = [System.Collections.Generic.List[int]]::new()
if ($runtime) {
    $runtimeOwned = $false
    try {
        $runtimeOwned = (
            [System.IO.Path]::GetFullPath([string]$runtime.root).TrimEnd("\", "/") -eq
            [System.IO.Path]::GetFullPath($root).TrimEnd("\", "/")
        )
    }
    catch {
        $runtimeOwned = $false
    }
    if (-not $runtimeOwned) {
        $stopFailed = $true
        Write-Warning 'Runtime metadata belongs to another checkout or has no verifiable owner. It will be retained.' -WarningAction Continue
    }
    if ($runtimeOwned -and -not (Test-FabRuntimeContainmentCoverage -Runtime $runtime)) {
        $stopFailed = $true
        Write-Warning 'Runtime containment records are incomplete. Verify detached descendants before clearing runtime state.' -WarningAction Continue
    }
    if ($runtimeOwned -and $runtime.PSObject.Properties['processes'] -and $null -ne $runtime.processes) {
        foreach ($entry in $runtime.processes.PSObject.Properties) {
            try {
                if ($entry.Name -notin @('api', 'web', 'worker') -or $null -eq $entry.Value) {
                    throw 'Unknown or empty service containment identity. It will not be terminated.'
                }
                $expectedPid = $runtime.PSObject.Properties[$entry.Name + 'Pid']
                if (-not $expectedPid -or -not $expectedPid.Value -or
                    -not $entry.Value.PSObject.Properties['rootPid'] -or
                    [int]$entry.Value.rootPid -ne [int]$expectedPid.Value) {
                    throw 'Recorded process group does not match its service root. It will not be terminated.'
                }
                Stop-FabRecordedProcess -Identity $entry.Value
            }
            catch { $stopFailed = $true; Write-Warning $_.Exception.Message -WarningAction Continue }
        }
    }
    elseif ($runtimeOwned -and ($runtime.apiPid -or $runtime.workerPid -or $runtime.webPid)) {
        $stopFailed = $true
        Write-Warning 'Legacy runtime has no recorded process groups. Verify any detached descendants before clearing runtime state.' -WarningAction Continue
    }
    try {
    $apiPid = Get-FabProcessId -ProcessId $runtime.apiPid -CommandMarker "src.operations.local_api" -ExpectedRoot $root
    if ($runtimeOwned) {
        $workerPid = Get-FabProcessId -ProcessId $runtime.workerPid -CommandMarker "src.run_worker" -ExpectedRoot $root
    }
    if (
        -not $runtime.apiUrl -or
        -not (Test-FabEndpoint -Url $runtime.apiUrl -ExpectedService "fab-ledger-api" -ApiToken $apiToken -ExpectedInstanceRoot $root -AllowLegacyInstance:$runtimeOwned)
    ) {
        $apiPid = $null
    }
    if (
        $runtime.webIdentityUrl -and
        (Test-FabEndpoint -Url $runtime.webIdentityUrl -ExpectedService "fab-operator-dashboard" -ExpectedInstanceRoot $root -AllowLegacyInstance:$runtimeOwned)
    ) {
        $webListenerPid = Get-FabListenerProcessId -Url $runtime.webIdentityUrl
        if ($webListenerPid) {
            $savedWebPid = Get-FabDashboardProcessId -ProcessId $runtime.webPid -ExpectedWebRoot $webRoot
            if ($savedWebPid -and (Test-FabProcessAncestor -AncestorProcessId $savedWebPid -DescendantProcessId $webListenerPid)) {
                $webPid = $savedWebPid
            }
            else {
                $webPid = Get-FabDashboardProcessRoot -ListenerProcessId $webListenerPid -ExpectedWebRoot $webRoot
            }
        }
    }
    else {
        $webPid = $null
    }
    }
    catch { $stopFailed = $true; Write-Warning $_.Exception.Message -WarningAction Continue }
}
$apiPids = [System.Collections.Generic.List[int]]::new()
if ($apiPid) {
    $apiPids.Add([int]$apiPid)
}
try { foreach ($discoveredApiPid in @(Find-RunningFabApiProcessIds -ExpectedRoot $root -ApiToken $apiToken)) {
    if (-not $apiPids.Contains([int]$discoveredApiPid)) {
        $apiPids.Add([int]$discoveredApiPid)
    }
}
} catch { $stopFailed = $true; Write-Warning $_.Exception.Message -WarningAction Continue }
if ($webPid) {
    $webPids.Add([int]$webPid)
}
try { foreach ($discoveredWebPid in @(Find-RunningFabDashboardProcessIds -ExpectedRoot $root -ExpectedWebRoot $webRoot)) {
    if (-not $webPids.Contains([int]$discoveredWebPid)) {
        $webPids.Add([int]$discoveredWebPid)
    }
}
} catch { $stopFailed = $true; Write-Warning $_.Exception.Message -WarningAction Continue }
$managedWorkerPid = $null
try { $managedWorkerPid = Get-FabWorkerRuntimeProcessId -Path $workerRuntimePath -ExpectedRoot $root }
catch { $stopFailed = $true; Write-Warning $_.Exception.Message -WarningAction Continue }
if ($managedWorkerPid) {
    $workerPid = $managedWorkerPid
}
elseif (Test-Path -LiteralPath $workerRuntimePath) {
    $workerPid = $null
}

if (-not $runtime -and $apiPids.Count -eq 0 -and $webPids.Count -eq 0 -and -not $workerPid) {
    Write-Host "No owned FAB services were found. The managed services are already stopped."
}

foreach ($ownedWebPid in $webPids) {
    try { Stop-FabDashboardProcessTree -ProcessId $ownedWebPid -ExpectedWebRoot $webRoot }
    catch { $stopFailed = $true; Write-Warning $_.Exception.Message -WarningAction Continue }
}
try { Stop-FabProcessTree -ProcessId $workerPid -CommandMarker "src.run_worker" -Name "FAB autonomous worker" }
catch { $stopFailed = $true; Write-Warning $_.Exception.Message -WarningAction Continue }
foreach ($ownedApiPid in $apiPids) {
    try { Stop-FabProcessTree -ProcessId $ownedApiPid -CommandMarker "src.operations.local_api" -Name "FAB ledger API" }
    catch { $stopFailed = $true; Write-Warning $_.Exception.Message -WarningAction Continue }
}
$remainingApi = @(Find-RunningFabApiProcessIds -ExpectedRoot $root -ApiToken $apiToken)
$remainingWeb = @(Find-RunningFabDashboardProcessIds -ExpectedRoot $root -ExpectedWebRoot $webRoot)
$remainingWorker = Get-FabWorkerRuntimeProcessId -Path $workerRuntimePath -ExpectedRoot $root
if ($stopFailed -or $remainingApi.Count -gt 0 -or $remainingWeb.Count -gt 0 -or $remainingWorker) {
    throw 'FAB shutdown is incomplete. Runtime metadata and leases were retained; resolve the reported service before restarting.'
}

try {
    if (-not (Test-Path -LiteralPath $venvPython)) {
        throw "FAB's isolated Python runtime is missing."
    }
    $cleanupScript = @"
from src.config_loader import ConfigLoader
from src.operations.local_ledger import LocalOperationsLedger, default_ledger_path
from src.worker.runtime import managed_worker_maintenance
from pathlib import Path

config = ConfigLoader(config_file='config/config.ini').get_all_config()
ledger_path = str(
    config.get('fab_local_ledger_path')
    or config.get('operations_ledger_path')
    or default_ledger_path()
)
with managed_worker_maintenance(Path.cwd()):
    ledger = LocalOperationsLedger(ledger_path)
    released = ledger.force_release_stopped_runtime_leases(actor='Stop-FAB.ps1')
    (Path.cwd() / 'data' / 'fab-worker-runtime.json').unlink(missing_ok=True)
    (Path.cwd() / 'data' / 'fab-runtime.json').unlink(missing_ok=True)
print(chr(44).join(released))
"@
    $releasedLeases = & $venvPython -c $cleanupScript
    if ($LASTEXITCODE -ne 0) {
        throw "Lease cleanup exited with code $LASTEXITCODE."
    }
    if ($releasedLeases) {
        Write-Host "Released stopped FAB runtime leases: $releasedLeases"
    }
}
catch {
    throw "FAB services stopped, but runtime lease cleanup failed. Runtime metadata was retained: $($_.Exception.Message)"
}

Write-Host "FAB local services are stopped."
}
finally {
    foreach ($process in $script:fabStopProcesses.Values) {
        if ($process.PSObject.Properties['FabJob']) { $process.FabJob.Dispose() }
        $process.Dispose()
    }
    $script:fabStopProcesses.Clear()
}
}
finally { Exit-FabLifecycleLock -Lock $lifecycleLock }
