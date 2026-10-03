[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [switch]$Development,
    [switch]$Maintenance,
    [ValidateSet("local", "windows")][string]$DeploymentProfile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $root "scripts\Windows-Profile.ps1")
. (Join-Path $root "scripts\Windows-Job.ps1")
. (Join-Path $root "scripts\Windows-Process.ps1")
$lifecycleLock = Enter-FabLifecycleLock -Root $root
try {
if ($DeploymentProfile) {
    $env:FAB_DEPLOYMENT_PROFILE = $DeploymentProfile
}
$webRoot = Join-Path $root "web"
$dataRoot = Join-Path $root "data"
$logsRoot = Join-Path $root "logs"
$runtimePath = Join-Path $dataRoot "fab-runtime.json"
$workerRuntimePath = Join-Path $dataRoot "fab-worker-runtime.json"
$requestedMaintenanceMode = [bool]$Maintenance
$defaultApiPort = 5001
$defaultWebPort = 3000

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

function Test-FabEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$ExpectedService,
        [string]$ApiToken = "",
        [string]$ExpectedLocalApiEndpoint = "",
        [string]$ExpectedInstanceRoot = "",
        [AllowNull()][Nullable[bool]]$ExpectedMaintenanceMode = $null
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
            TimeoutSec = 2
            MaximumRedirection = 0
        }
        if ($ApiToken) {
            $request.Headers = @{ Authorization = "Bearer $ApiToken" }
        }
        $response = Invoke-RestMethod @request
        if ([string]$response.service -ne $ExpectedService) {
            return $false
        }
        if ($ExpectedLocalApiEndpoint -and ([string]$response.localApiEndpoint).TrimEnd("/") -ne $ExpectedLocalApiEndpoint.TrimEnd("/")) {
            return $false
        }
        if ($ExpectedInstanceRoot) {
            $actualInstanceId = [string]$response.instanceId
            $expectedInstanceId = Get-FabInstanceId -Path $ExpectedInstanceRoot
            if (-not $actualInstanceId -or $actualInstanceId -ne $expectedInstanceId) {
                return $false
            }
        }
        if ($null -ne $ExpectedMaintenanceMode) {
            $maintenanceProperty = $response.PSObject.Properties["maintenanceMode"]
            if (-not $maintenanceProperty -or [bool]$maintenanceProperty.Value -ne [bool]$ExpectedMaintenanceMode) {
                return $false
            }
        }
        return $true
    }
    catch {
        return $false
    }
}

function Test-TcpPortAvailable {
    param([Parameter(Mandatory = $true)][int]$Port)

    try {
        $activeListeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
        if (@($activeListeners | Where-Object { $_.Port -eq $Port }).Count -gt 0) {
            return $false
        }
    }
    catch {
        # The exclusive bind below remains the final authority if listener enumeration is unavailable.
    }
    $listener = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Loopback,
        $Port
    )
    try {
        $listener.ExclusiveAddressUse = $true
        $listener.Start()
        return $true
    }
    catch {
        return $false
    }
    finally {
        $listener.Stop()
    }
}

function Find-AvailableFabPort {
    param(
        [Parameter(Mandatory = $true)][int]$StartPort,
        [int]$Attempts = 20
    )

    for ($port = $StartPort; $port -lt ($StartPort + $Attempts); $port++) {
        if (Test-TcpPortAvailable -Port $port) {
            return $port
        }
    }
    throw "No free loopback port was found from $StartPort through $($StartPort + $Attempts - 1)."
}

function Get-FabProcessId {
    param(
        [AllowNull()][object]$ProcessId,
        [Parameter(Mandatory = $true)][string]$CommandMarker,
        [uint32]$OperationTimeoutSeconds = 0
    )

    if (-not $ProcessId) {
        return $null
    }

    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -OperationTimeoutSec $OperationTimeoutSeconds -ErrorAction SilentlyContinue
    $normalizedCommand = if ($process -and $process.CommandLine) { ([string]$process.CommandLine).Replace("\", "/") } else { "" }
    $normalizedMarker = $CommandMarker.Replace("\", "/")
    if (-not $process -or -not $normalizedCommand -or $normalizedCommand -notlike "*$normalizedMarker*") {
        return $null
    }

    return [int]$process.ProcessId
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
    if ($command.Contains($webRootMarker)) {
        return (
            $command.Contains("server/dev.ts") -or
            $command.Contains("dist/index.js") -or
            $command.Contains("dist/fab-standalone.js") -or
            $command.Contains("tsx") -or
            $command.Contains("pnpm") -or
            $command.Contains("npm-cli.js")
        )
    }

    return (
        $command -match "npm(\.cmd|-cli\.js).*(run )?dev" -or
        $command -match "pnpm(\.cmd|\.mjs)?.*(--dir .*)?dev"
    )
}

function Get-FabDashboardProcessRoot {
    param(
        [Parameter(Mandatory = $true)][int]$ListenerProcessId,
        [Parameter(Mandatory = $true)][string]$ExpectedWebRoot
    )

    $currentId = $ListenerProcessId
    $highestOwnedId = $ListenerProcessId
    for ($depth = 0; $depth -lt 8; $depth++) {
        $current = Get-CimInstance Win32_Process -Filter "ProcessId = $currentId" -ErrorAction SilentlyContinue
        if (-not (Test-FabDashboardProcess -Process $current -ExpectedWebRoot $ExpectedWebRoot)) {
            break
        }
        $highestOwnedId = [int]$current.ProcessId
        if (-not $current.ParentProcessId) {
            break
        }
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($current.ParentProcessId)" -ErrorAction SilentlyContinue
        if (-not (Test-FabDashboardProcess -Process $parent -ExpectedWebRoot $ExpectedWebRoot)) {
            break
        }
        $currentId = [int]$parent.ProcessId
    }
    return $highestOwnedId
}

function Test-FabProcessAncestor {
    param(
        [Parameter(Mandatory = $true)][int]$AncestorProcessId,
        [Parameter(Mandatory = $true)][int]$DescendantProcessId,
        [System.Diagnostics.Stopwatch]$Watch,
        [int]$TimeoutSeconds = 0
    )

    $currentId = $DescendantProcessId
    for ($depth = 0; $depth -lt 12 -and $currentId; $depth++) {
        if ($Watch -and $Watch.Elapsed.TotalSeconds -ge $TimeoutSeconds) { return $false }
        if ($currentId -eq $AncestorProcessId) {
            return $true
        }
        $operationSeconds = if ($Watch) { [uint32][Math]::Min(2, [Math]::Max(1, [Math]::Ceiling($TimeoutSeconds - $Watch.Elapsed.TotalSeconds))) } else { 0 }
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $currentId" -OperationTimeoutSec $operationSeconds -ErrorAction SilentlyContinue
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

function Get-FabDashboardMode {
    param([AllowNull()][object]$Process)

    if ($Process -and $Process.CommandLine) {
        $command = ([string]$Process.CommandLine).Replace("\", "/").ToLowerInvariant()
        if ($command.Contains("dist/index.js") -or $command.Contains("dist/fab-standalone.js")) {
            return "production"
        }
    }
    return "development"
}

function Find-RunningFabDashboard {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedRoot,
        [Parameter(Mandatory = $true)][string]$ExpectedWebRoot,
        [Parameter(Mandatory = $true)][string]$ExpectedLocalApiEndpoint
    )

    $processes = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -eq "node.exe" -and
            (Test-FabDashboardProcess -Process $_ -ExpectedWebRoot $ExpectedWebRoot)
        } |
        Sort-Object ProcessId
    foreach ($process in $processes) {
        $listeners = Get-NetTCPConnection -State Listen -OwningProcess $process.ProcessId -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalAddress -in @("127.0.0.1", "::1") } |
            Sort-Object LocalPort -Unique
        foreach ($listener in $listeners) {
            $baseUrl = "http://127.0.0.1:$($listener.LocalPort)"
            $identityUrl = "$baseUrl/api/fab/runtime"
            if (Test-FabEndpoint -Url $identityUrl -ExpectedService "fab-operator-dashboard" -ExpectedLocalApiEndpoint $ExpectedLocalApiEndpoint -ExpectedInstanceRoot $ExpectedRoot) {
                return [PSCustomObject]@{
                    ProcessId = Get-FabDashboardProcessRoot -ListenerProcessId ([int]$process.ProcessId) -ExpectedWebRoot $ExpectedWebRoot
                    ListenerProcessId = [int]$process.ProcessId
                    DashboardUrl = "$baseUrl/admin/operations"
                    IdentityUrl = $identityUrl
                    Mode = Get-FabDashboardMode -Process $process
                }
            }
        }
    }
    return $null
}

function Test-FabWebBuildCurrent {
    param([Parameter(Mandatory = $true)][string]$ExpectedWebRoot)

    $serverOutput = Join-Path $ExpectedWebRoot "dist\fab-standalone.js"
    $clientOutput = Join-Path $ExpectedWebRoot "dist\public\index.html"
    if (-not (Test-Path -LiteralPath $serverOutput) -or -not (Test-Path -LiteralPath $clientOutput)) {
        return $false
    }

    $outputTime = @(
        (Get-Item -LiteralPath $serverOutput).LastWriteTimeUtc,
        (Get-Item -LiteralPath $clientOutput).LastWriteTimeUtc
    ) | Sort-Object | Select-Object -First 1
    $sourcePaths = @(
        (Join-Path $ExpectedWebRoot "client"),
        (Join-Path $ExpectedWebRoot "server"),
        (Join-Path $ExpectedWebRoot "shared")
    )
    $sourceFiles = @(
        Get-ChildItem -LiteralPath $sourcePaths -Recurse -File -ErrorAction SilentlyContinue
        Get-Item -LiteralPath (Join-Path $ExpectedWebRoot "package.json")
        Get-Item -LiteralPath (Join-Path $ExpectedWebRoot "pnpm-lock.yaml")
        Get-Item -LiteralPath (Join-Path $ExpectedWebRoot "vite.config.ts")
        Get-Item -LiteralPath (Join-Path $ExpectedWebRoot "tsconfig.json")
    )
    $latestSourceTime = $sourceFiles |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1 -ExpandProperty LastWriteTimeUtc
    return $outputTime -ge $latestSourceTime
}

function Find-RunningFabApi {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedRoot,
        [string]$ApiToken = "",
        [Parameter(Mandatory = $true)][bool]$ExpectedMaintenanceMode
    )

    $processes = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -eq "python.exe" -and
            $_.CommandLine -and
            $_.CommandLine -like "*src.operations.local_api*"
        } |
        Sort-Object ProcessId
    foreach ($process in $processes) {
        $listeners = Get-NetTCPConnection -State Listen -OwningProcess $process.ProcessId -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalAddress -in @("127.0.0.1", "::1") } |
            Sort-Object LocalPort -Unique
        foreach ($listener in $listeners) {
            $url = "http://127.0.0.1:$($listener.LocalPort)/api/live"
            if (Test-FabEndpoint -Url $url -ExpectedService "fab-ledger-api" -ApiToken $ApiToken -ExpectedInstanceRoot $ExpectedRoot -ExpectedMaintenanceMode $ExpectedMaintenanceMode) {
                return [PSCustomObject]@{
                    ProcessId = [int]$process.ProcessId
                    Url = $url
                }
            }
        }
    }
    return $null
}

function Get-FabWorkerRuntimeProcessId {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedRoot,
        [uint32]$OperationTimeoutSeconds = 0
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    try {
        $runtime = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        $actualRoot = [System.IO.Path]::GetFullPath([string]$runtime.instanceRoot).TrimEnd("\", "/")
        $expected = [System.IO.Path]::GetFullPath($ExpectedRoot).TrimEnd("\", "/")
        if ($actualRoot -ne $expected) {
            return $null
        }
        return Get-FabProcessId -ProcessId $runtime.pid -CommandMarker "src.run_worker" -OperationTimeoutSeconds $OperationTimeoutSeconds
    }
    catch {
        return $null
    }
}

function Wait-FabWorkerRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedRoot,
        [Parameter(Mandatory = $true)][int]$ExpectedProcessId,
        [ValidateRange(1, 120)][int]$TimeoutSeconds = 30
    )

    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    do {
        $remaining = $TimeoutSeconds - $watch.Elapsed.TotalSeconds
        if ($remaining -le 0) { break }
        $operationSeconds = [uint32][Math]::Min(2, [Math]::Ceiling($remaining))
        $registeredPid = Get-FabWorkerRuntimeProcessId -Path $Path -ExpectedRoot $ExpectedRoot -OperationTimeoutSeconds $operationSeconds
        if ($watch.Elapsed.TotalSeconds -ge $TimeoutSeconds) { break }
        if ($registeredPid -and (Test-FabProcessAncestor -AncestorProcessId $ExpectedProcessId -DescendantProcessId $registeredPid -Watch $watch -TimeoutSeconds $TimeoutSeconds)) {
            if ($watch.Elapsed.TotalSeconds -lt $TimeoutSeconds) { return $registeredPid }
            break
        }
        $remaining = $TimeoutSeconds - $watch.Elapsed.TotalSeconds
        if ($remaining -le 0) { break }
        $operationSeconds = [uint32][Math]::Min(2, [Math]::Ceiling($remaining))
        if (-not (Get-FabProcessId -ProcessId $ExpectedProcessId -CommandMarker "src.run_worker" -OperationTimeoutSeconds $operationSeconds)) {
            throw "FAB autonomous worker exited before confirming runtime ownership. Check logs\worker.err.log."
        }
        $remaining = $TimeoutSeconds - $watch.Elapsed.TotalSeconds
        if ($remaining -le 0) { break }
        Start-Sleep -Milliseconds ([int][Math]::Min(250, [Math]::Ceiling($remaining * 1000)))
    } while ($watch.Elapsed.TotalSeconds -lt $TimeoutSeconds)
    throw "FAB autonomous worker did not confirm runtime ownership within $TimeoutSeconds seconds. Check logs\worker.err.log."
}

function Wait-FabEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$ExpectedService,
        [string]$ApiToken = "",
        [string]$ExpectedLocalApiEndpoint = "",
        [string]$ExpectedInstanceRoot = "",
        [AllowNull()][Nullable[bool]]$ExpectedMaintenanceMode = $null,
        [int]$TimeoutSeconds = 45
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (Test-FabEndpoint -Url $Url -ExpectedService $ExpectedService -ApiToken $ApiToken -ExpectedLocalApiEndpoint $ExpectedLocalApiEndpoint -ExpectedInstanceRoot $ExpectedInstanceRoot -ExpectedMaintenanceMode $ExpectedMaintenanceMode) {
            return
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)

    throw "$Name did not become ready at $Url within $TimeoutSeconds seconds. Check $logsRoot."
}

function Stop-FabSpawnedProcessTree {
    param(
        [AllowNull()][System.Diagnostics.Process]$Process,
        [ValidateRange(0, 32)][int]$Depth = 0
    )

    Stop-FabOwnedProcessTree -Process $Process -Depth $Depth
}

function Invoke-FabNativeCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [switch]$DiscardOutput
    )

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        if ($DiscardOutput) {
            & $FilePath @ArgumentList 2>$null | Out-Null
        }
        else {
            $commandOutput = @(& $FilePath @ArgumentList 2>&1)
            if ($LASTEXITCODE -ne 0) {
                $commandOutput | ForEach-Object { Write-Warning ([string]$_) }
            }
        }
        return $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}

function Remove-FabVirtualEnvironment {
    param(
        [Parameter(Mandatory = $true)][string]$InstanceRoot,
        [Parameter(Mandatory = $true)][string]$VenvPath
    )

    if (-not (Test-Path -LiteralPath $VenvPath)) {
        return
    }
    $expectedVenvPath = [System.IO.Path]::GetFullPath((Join-Path $InstanceRoot ".venv")).TrimEnd('\')
    $actualVenvPath = [System.IO.Path]::GetFullPath($VenvPath).TrimEnd('\')
    if ($actualVenvPath -ne $expectedVenvPath) {
        throw "Refusing to reconcile an unexpected Python environment path: $actualVenvPath"
    }
    $venvItem = Get-Item -LiteralPath $VenvPath -Force
    if (($venvItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing to reconcile FAB's Python environment because .venv is a reparse point."
    }
    Remove-Item -LiteralPath $VenvPath -Recurse -Force
}

Set-Location -LiteralPath $root

$requirementsPath = Join-Path $root "requirements-local.txt"
$requirementsHash = (Get-FileHash -LiteralPath $requirementsPath -Algorithm SHA256).Hash.ToLowerInvariant()
$venvRoot = Join-Path $root ".venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"
$venvRequirementsMarker = Join-Path $venvRoot ".fab-requirements.sha256"
if (Test-Path -LiteralPath $venvRoot) {
    $installedRequirementsHash = ""
    if (Test-Path -LiteralPath $venvRequirementsMarker) {
        $installedRequirementsHash = ([string](Get-Content -LiteralPath $venvRequirementsMarker -Raw)).Trim().ToLowerInvariant()
    }
    if ($installedRequirementsHash -ne $requirementsHash) {
        Write-Host "FAB's local dependency contract changed; rebuilding the isolated Python environment..."
        & (Join-Path $root "Stop-FAB.ps1")
        Remove-FabVirtualEnvironment -InstanceRoot $root -VenvPath $venvRoot
    }
}
if (-not (Test-Path -LiteralPath $venvPython)) {
    $pythonVersionProbe = "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)"
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        $probeExitCode = Invoke-FabNativeCommand -FilePath $pyLauncher.Source -ArgumentList @("-3.13", "-c", $pythonVersionProbe) -DiscardOutput
        if ($probeExitCode -eq 0) {
            Write-Host "Creating FAB's isolated Python 3.13 runtime..."
            $createExitCode = Invoke-FabNativeCommand -FilePath $pyLauncher.Source -ArgumentList @("-3.13", "-m", "venv", $venvRoot)
            if ($createExitCode -ne 0) {
                Remove-FabVirtualEnvironment -InstanceRoot $root -VenvPath $venvRoot
            }
        }
    }

    if (-not (Test-Path -LiteralPath $venvPython)) {
        $uv = Get-Command uv -ErrorAction SilentlyContinue
        if ($uv) {
            Write-Host "Creating FAB's isolated Python 3.13 runtime with uv..."
            $createExitCode = Invoke-FabNativeCommand -FilePath $uv.Source -ArgumentList @("venv", "--seed", "--python", "3.13", $venvRoot)
            if ($createExitCode -ne 0) {
                Remove-FabVirtualEnvironment -InstanceRoot $root -VenvPath $venvRoot
            }
        }
    }

    if (-not (Test-Path -LiteralPath $venvPython)) {
        $systemPython = Get-Command python -ErrorAction SilentlyContinue
        if ($systemPython) {
            $probeExitCode = Invoke-FabNativeCommand -FilePath $systemPython.Source -ArgumentList @("-c", $pythonVersionProbe) -DiscardOutput
            if ($probeExitCode -eq 0) {
                Write-Host "Creating FAB's isolated Python 3.13 runtime..."
                $createExitCode = Invoke-FabNativeCommand -FilePath $systemPython.Source -ArgumentList @("-m", "venv", $venvRoot)
                if ($createExitCode -ne 0) {
                    Remove-FabVirtualEnvironment -InstanceRoot $root -VenvPath $venvRoot
                }
            }
        }
    }

    if (-not (Test-Path -LiteralPath $venvPython)) {
        throw "FAB requires Python 3.13. Install Python 3.13, then run Start-FAB.cmd again."
    }
}

$python = Get-Command $venvPython -ErrorAction Stop
& $python.Source -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "The FAB .venv is not Python 3.13. Remove only the .venv directory, install Python 3.13, and run Start-FAB.cmd again."
}
$node = Get-Command node -ErrorAction Stop
$pnpm = Get-Command pnpm.cmd -ErrorAction Stop

& $python.Source -c "import importlib.util,sys; modules=('flask','waitress','PIL','pytesseract','pdf2image','langdetect','googleapiclient','sklearn','joblib'); sys.exit(0 if all(importlib.util.find_spec(module) for module in modules) else 1)" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing FAB local runtime dependencies..."
    & $python.Source -m pip install --disable-pip-version-check --no-input -r $requirementsPath
    if ($LASTEXITCODE -ne 0) {
        throw "FAB local runtime dependency installation failed with exit code $LASTEXITCODE."
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $root "config\config.ini"))) {
    Copy-Item -LiteralPath (Join-Path $root "config\config_template.ini") -Destination (Join-Path $root "config\config.ini")
}
if (-not (Test-Path -LiteralPath (Join-Path $webRoot ".env"))) {
    Copy-Item -LiteralPath (Join-Path $webRoot ".env.example") -Destination (Join-Path $webRoot ".env")
}

$startupSettingsJson = & $python.Source -c "import hashlib,json; from src.config_loader import ConfigLoader; c=ConfigLoader('config/config.ini').get_all_config(); storage=[c.get('fab_local_ledger_path', c.get('operations_ledger_path', '')), c.get('fab_local_backup_dir', c.get('operations_backup_dir', c.get('backup_dir', '')))]; print(json.dumps({'profile': c.get('fab_deployment_profile', c.get('operations_deployment_profile', 'local')), 'apiToken': c.get('fab_local_api_token', c.get('fab_operations_api_token', c.get('operations_api_token', ''))), 'haiToken': c.get('fab_hai_api_token', c.get('operations_hai_api_token', '')), 'apiPort': c.get('fab_local_api_port', c.get('operations_api_port', 5001)), 'storageId': hashlib.sha256(json.dumps(storage).encode()).hexdigest()}))"
if ($LASTEXITCODE -ne 0) {
    throw "FAB could not load deployment settings or secret files."
}
$startupSettings = $startupSettingsJson | ConvertFrom-Json
$startupSettingsJson = $null
$deploymentProfile = [string]$startupSettings.profile
$deploymentStorageId = [string]$startupSettings.storageId
if ($deploymentProfile -notin @("local", "windows")) {
    throw "Start-FAB.ps1 supports local and windows profiles only. Use the VM deployment entrypoints for vm."
}
if ($deploymentProfile -ne "local" -and $Development) {
    throw "The Windows production profile requires the production dashboard; omit -Development."
}
$env:FAB_DEPLOYMENT_PROFILE = $deploymentProfile
$apiToken = [string]$startupSettings.apiToken
if (-not $apiToken -or ($deploymentProfile -eq "local" -and $apiToken.Length -lt 32)) {
    $apiToken = & $python.Source -c "from src.config_loader import ConfigLoader; from src.security.local_secret_store import LocalSecretStore; c=ConfigLoader('config/config.ini').get_all_config(); print(LocalSecretStore(c).get_or_create_runtime_secret('operator_api_token'))"
    if ($LASTEXITCODE -ne 0 -or ([string]$apiToken).Length -lt 32) {
        throw "FAB could not provision its encrypted operator API credential."
    }
    $apiToken = [string]$apiToken
}

$haiApiToken = [string]$startupSettings.haiToken
if (-not $haiApiToken -or ($deploymentProfile -eq "local" -and $haiApiToken.Length -lt 32)) {
    $haiApiToken = & $python.Source -c "from src.config_loader import ConfigLoader; from src.security.local_secret_store import LocalSecretStore; c=ConfigLoader('config/config.ini').get_all_config(); print(LocalSecretStore(c).get_or_create_runtime_secret('hai_api_token'))"
    if ($LASTEXITCODE -ne 0 -or ([string]$haiApiToken).Length -lt 32) {
        throw "FAB could not provision its encrypted HAI API credential."
    }
    $haiApiToken = [string]$haiApiToken
}
if ([System.StringComparer]::Ordinal.Equals($apiToken, $haiApiToken)) {
    throw "FAB operator and HAI credentials must be different."
}

$previousPreflightInstanceRoot = $env:FAB_INSTANCE_ROOT
try {
    $env:FAB_INSTANCE_ROOT = $root
    Invoke-FabWithServiceCredentials -ApiToken $apiToken -HaiApiToken $haiApiToken -Action {
        & $python.Source -m src.run_deployment_preflight
        if ($LASTEXITCODE -ne 0) {
            throw "FAB deployment preflight blocked startup. No services were started."
        }
    }
}
finally {
    [Environment]::SetEnvironmentVariable("FAB_INSTANCE_ROOT", $previousPreflightInstanceRoot, "Process")
}
if ($deploymentProfile -ne "local") {
    $defaultApiPort = [int]$startupSettings.apiPort
    if ($env:PORT) {
        $defaultWebPort = [int]$env:PORT
    }
    if ($defaultWebPort -lt 1 -or $defaultWebPort -gt 65535) {
        throw "The Windows dashboard port must be between 1 and 65535."
    }
    if ($defaultWebPort -eq $defaultApiPort) {
        throw "The Windows API and dashboard must use different ports."
    }
}
$startupSettings = $null

$webJwtSecret = & $python.Source -c "from src.security.deployment_secrets import read_secret; print(read_secret('JWT_SECRET'))"
if ($LASTEXITCODE -ne 0) {
    throw "FAB could not resolve the dashboard signing secret. Configure JWT_SECRET or JWT_SECRET_FILE, not both."
}
$webJwtSecret = [string]$webJwtSecret
if (-not $webJwtSecret -or ($deploymentProfile -eq "local" -and $webJwtSecret.Length -lt 32)) {
    $webJwtSecret = & $python.Source -c "from src.config_loader import ConfigLoader; from src.security.local_secret_store import LocalSecretStore; c=ConfigLoader('config/config.ini').get_all_config(); print(LocalSecretStore(c).get_or_create_runtime_secret('web_jwt_secret'))"
    if ($LASTEXITCODE -ne 0 -or ([string]$webJwtSecret).Length -lt 32) {
        throw "FAB could not provision its encrypted dashboard signing secret."
    }
    $webJwtSecret = [string]$webJwtSecret
}
if ($deploymentProfile -ne "local") {
    Invoke-FabWithServiceCredentials -ApiToken $apiToken -HaiApiToken $haiApiToken -JwtSecret $webJwtSecret -Action {
        & $python.Source -c "import os; from src.security.deployment_secrets import strong_secret; raise SystemExit(0 if strong_secret(os.environ.get('JWT_SECRET')) else 1)"
        if ($LASTEXITCODE -ne 0) {
            throw "FAB requires a strong dashboard signing secret for the Windows profile."
        }
    }
}

$mijngeldzakenExportDir = & $python.Source -c "from src.config_loader import ConfigLoader; c=ConfigLoader('config/config.ini').get_all_config(); print(str(c.get('mijngeldzaken_export_dir') or c.get('operations_mijngeldzaken_export_dir') or 'data/exports/mijngeldzaken'))"
if ($LASTEXITCODE -ne 0) {
    throw "FAB could not read the configured MijnGeldzaken export directory."
}
$mijngeldzakenExportDir = [string]$mijngeldzakenExportDir
if (-not [System.IO.Path]::IsPathRooted($mijngeldzakenExportDir)) {
    $mijngeldzakenExportDir = Join-Path $root $mijngeldzakenExportDir
}

@(
    $dataRoot,
    (Join-Path $dataRoot "backups"),
    (Join-Path $dataRoot "reports"),
    (Join-Path $dataRoot "source_downloads"),
    (Join-Path $dataRoot "exports"),
    $mijngeldzakenExportDir,
    (Join-Path $root "downloads\sort-out"),
    $logsRoot
) | ForEach-Object {
    New-Item -ItemType Directory -Path $_ -Force | Out-Null
}

$tesseractCandidates = @(
    "C:\Program Files\Tesseract-OCR\tesseract.exe",
    "C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    (Join-Path $env:LOCALAPPDATA "Programs\Tesseract-OCR\tesseract.exe")
)
$tesseractPath = $tesseractCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $tesseractPath -and (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Host "Installing the local Tesseract OCR engine..."
    & winget install --id tesseract-ocr.tesseract --exact --source winget --silent --accept-source-agreements --accept-package-agreements --disable-interactivity | Out-Host
    $tesseractPath = $tesseractCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $tesseractPath) {
    Write-Warning "Tesseract OCR is not installed. Receipt OCR will remain unavailable until it is installed."
}
else {
    $tessdataRoot = Join-Path $dataRoot "tessdata"
    New-Item -ItemType Directory -Path $tessdataRoot -Force | Out-Null
    $installedTessdata = Join-Path (Split-Path -Parent $tesseractPath) "tessdata"
    foreach ($languageFile in @("eng.traineddata", "osd.traineddata")) {
        $sourceLanguage = Join-Path $installedTessdata $languageFile
        $targetLanguage = Join-Path $tessdataRoot $languageFile
        if ((Test-Path -LiteralPath $sourceLanguage) -and -not (Test-Path -LiteralPath $targetLanguage)) {
            Copy-Item -LiteralPath $sourceLanguage -Destination $targetLanguage
        }
    }

    $dutchLanguage = Join-Path $tessdataRoot "nld.traineddata"
    if (-not (Test-Path -LiteralPath $dutchLanguage)) {
        Write-Host "Installing Dutch OCR language data..."
        $dutchLanguageUrl = "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/87416418657359cb625c412a48b6e1d6d41c29bd/nld.traineddata"
        Invoke-WebRequest -Uri $dutchLanguageUrl -OutFile $dutchLanguage -UseBasicParsing
    }
    $dutchLanguageHash = (Get-FileHash -LiteralPath $dutchLanguage -Algorithm SHA256).Hash
    if ($dutchLanguageHash -ne "CED0E5E046A84C908A6AA7ACCBEF9A232C4A5D9A8276691B81C6EE64D02963F6") {
        Remove-Item -LiteralPath $dutchLanguage -Force
        throw "Dutch OCR language data failed checksum verification."
    }
}

$popplerPath = & $python.Source -c "from src.utils.tesseract_runtime import resolve_poppler_path; print(resolve_poppler_path({}) or '')"
if (-not $popplerPath -and (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Host "Installing Poppler PDF rendering tools..."
    & winget install --id oschwartz10612.Poppler --exact --source winget --silent --accept-source-agreements --accept-package-agreements --disable-interactivity | Out-Host
    $popplerPath = & $python.Source -c "from src.utils.tesseract_runtime import resolve_poppler_path; print(resolve_poppler_path({}) or '')"
}
if (-not $popplerPath) {
    Write-Warning "Poppler is not installed. Image OCR will work, but PDF OCR will remain unavailable."
}

if (-not (Test-Path -LiteralPath (Join-Path $webRoot "node_modules"))) {
    Write-Host "Installing FAB dashboard dependencies..."
    & $pnpm.Source --dir $webRoot install --frozen-lockfile
    if ($LASTEXITCODE -ne 0) {
        throw "Dashboard dependency installation failed with exit code $LASTEXITCODE."
    }
}

$savedRuntime = $null
if (Test-Path -LiteralPath $runtimePath) {
    try {
        $savedRuntime = Get-Content -LiteralPath $runtimePath -Raw | ConvertFrom-Json
    }
    catch {
        Write-Warning "Ignoring unreadable runtime metadata at $runtimePath."
    }
}
if ($savedRuntime) {
    $savedProfileProperty = $savedRuntime.PSObject.Properties["deploymentProfile"]
    $savedStorageProperty = $savedRuntime.PSObject.Properties["deploymentStorageId"]
    $savedProfile = if ($savedProfileProperty) { [string]$savedProfileProperty.Value } else { "local" }
    $savedStorageId = if ($savedStorageProperty) { [string]$savedStorageProperty.Value } else { "" }
    if (($deploymentProfile -ne "local" -or $savedProfile -ne "local") -and
        ($savedProfile -ne $deploymentProfile -or $savedStorageId -ne $deploymentStorageId)) {
        throw "FAB production storage configuration changed. Run Stop-FAB.cmd with the previous configuration before starting this profile. No ledger is moved automatically."
    }
    $savedMaintenanceMode = $false
    $savedMaintenanceProperty = $savedRuntime.PSObject.Properties["maintenanceMode"]
    if ($savedMaintenanceProperty) {
        $savedMaintenanceMode = [bool]$savedMaintenanceProperty.Value
    }
    if ($savedMaintenanceMode -ne $requestedMaintenanceMode) {
        $requestedLabel = if ($requestedMaintenanceMode) { "maintenance" } else { "standard" }
        Write-Host "Switching FAB to $requestedLabel mode; restarting the local services..."
        & (Join-Path $root "Stop-FAB.ps1")
        if ($LASTEXITCODE -ne 0) {
            throw "FAB could not stop the previous runtime mode."
        }
        $savedRuntime = $null
    }
}
& $python.Source -m pip check
if ($LASTEXITCODE -ne 0) {
    throw "FAB's isolated Python dependency integrity check failed with exit code $LASTEXITCODE."
}
Set-Content -LiteralPath $venvRequirementsMarker -Value $requirementsHash -Encoding ascii -NoNewline

$sourceFingerprint = & $python.Source -m src.runtime_fingerprint
if ($LASTEXITCODE -ne 0 -or -not $sourceFingerprint) {
    throw "FAB could not fingerprint the current runtime sources."
}
$sourceFingerprint = ([string]$sourceFingerprint).Trim()
if ($savedRuntime) {
    $savedFingerprintProperty = $savedRuntime.PSObject.Properties["sourceFingerprint"]
    $savedFingerprint = if ($savedFingerprintProperty) { [string]$savedFingerprintProperty.Value } else { "" }
    if ($savedFingerprint -ne $sourceFingerprint) {
        Write-Host "FAB sources changed; restarting the local services..."
        & (Join-Path $root "Stop-FAB.ps1")
        if ($LASTEXITCODE -ne 0) {
            throw "FAB could not stop the stale local services."
        }
        $savedRuntime = $null
    }
}

$apiPid = $null
$apiProcess = $null
$workerProcess = $null
$webProcess = $null
$workerPid = $null
$webPid = $null
$webListenerPid = $null
$webMode = $null
$webProcessMarker = $null
$apiUrl = $null
$dashboardUrl = $null
$apiStartedThisRun = $false
$workerStartedThisRun = $false
$webStartedThisRun = $false
if ($savedRuntime) {
    $apiPid = Get-FabProcessId -ProcessId $savedRuntime.apiPid -CommandMarker "src.operations.local_api"
    $workerPid = Get-FabProcessId -ProcessId $savedRuntime.workerPid -CommandMarker "src.run_worker"
    if ($apiPid -and $savedRuntime.apiUrl -and (Test-FabEndpoint -Url $savedRuntime.apiUrl -ExpectedService "fab-ledger-api" -ApiToken $apiToken -ExpectedInstanceRoot $root -ExpectedMaintenanceMode $requestedMaintenanceMode)) {
        $apiUrl = [string]$savedRuntime.apiUrl
    }
    else {
        $apiPid = $null
    }
}

if (-not $apiPid) {
    $runningApi = Find-RunningFabApi -ExpectedRoot $root -ApiToken $apiToken -ExpectedMaintenanceMode $requestedMaintenanceMode
    if ($runningApi) {
        if ($deploymentProfile -ne "local" -and -not $savedRuntime) {
            throw "The Windows profile cannot adopt an unverified running API. Run Stop-FAB.cmd first."
        }
        $apiPid = [int]$runningApi.ProcessId
        $apiUrl = [string]$runningApi.Url
    }
}
if (-not $apiPid) {
    $oppositeModeApi = Find-RunningFabApi -ExpectedRoot $root -ApiToken $apiToken -ExpectedMaintenanceMode (-not $requestedMaintenanceMode)
    if ($oppositeModeApi) {
        throw "A FAB API for this checkout is already running in the other runtime mode. Run Stop-FAB.cmd before switching modes."
    }
}

$managedWorkerPid = Get-FabWorkerRuntimeProcessId -Path $workerRuntimePath -ExpectedRoot $root
if ($deploymentProfile -ne "local" -and $managedWorkerPid -and -not $savedRuntime) {
    throw "The Windows profile cannot adopt an unverified running worker. Run Stop-FAB.cmd first."
}
if ($requestedMaintenanceMode -and $managedWorkerPid) {
    throw "The FAB autonomous worker is still active. Run Stop-FAB.cmd, then start maintenance again."
}
if (-not $requestedMaintenanceMode -and $managedWorkerPid) {
    $workerPid = $managedWorkerPid
}
elseif (Test-Path -LiteralPath $workerRuntimePath) {
    $workerPid = $null
}
if ($requestedMaintenanceMode) {
    $workerPid = $null
}

if ($deploymentProfile -ne "local") {
    if ($apiPid -and ([System.Uri]$apiUrl).Port -ne $defaultApiPort) {
        throw "The running FAB API uses a different production port. Run Stop-FAB.cmd first."
    }
    if ($savedRuntime -and $savedRuntime.dashboardUrl -and ([System.Uri]$savedRuntime.dashboardUrl).Port -ne $defaultWebPort) {
        throw "The saved FAB dashboard uses a different production port. Run Stop-FAB.cmd first."
    }
    if (-not $apiPid -and -not (Test-TcpPortAvailable -Port $defaultApiPort)) {
        throw "The configured FAB API port is occupied. Stop the conflicting service or explicitly configure another port."
    }
    $expectedApiBase = "http://127.0.0.1:$defaultApiPort"
    $expectedWebIdentity = "http://127.0.0.1:$defaultWebPort/api/fab/runtime"
    $ownedDashboardReady = $savedRuntime -and (Test-FabEndpoint -Url $expectedWebIdentity -ExpectedService "fab-operator-dashboard" -ExpectedLocalApiEndpoint $expectedApiBase -ExpectedInstanceRoot $root)
    if (-not $ownedDashboardReady -and -not (Test-TcpPortAvailable -Port $defaultWebPort)) {
        throw "The configured FAB dashboard port is occupied. Stop the conflicting service or explicitly configure another port."
    }
}

# Roll back only services created here if any startup or registration step fails.
$startupCompleted = $false
try {
if (-not $apiPid) {
    $apiPort = Find-AvailableFabPort -StartPort $defaultApiPort -Attempts $(if ($deploymentProfile -eq "local") { 20 } else { 1 })
    $apiUrl = "http://127.0.0.1:$apiPort/api/live"
    $previousApiPort = $env:FAB_LOCAL_API_PORT
    $previousApiHost = $env:FAB_LOCAL_API_HOST
    $previousMaintenanceMode = $env:FAB_MAINTENANCE_MODE
    $previousApiInstanceRoot = $env:FAB_INSTANCE_ROOT
    $previousLocalApiToken = $env:FAB_LOCAL_API_TOKEN
    $previousHaiApiToken = $env:FAB_HAI_API_TOKEN
    try {
        $env:FAB_LOCAL_API_PORT = [string]$apiPort
        if ($deploymentProfile -ne "local") {
            $env:FAB_LOCAL_API_HOST = "127.0.0.1"
        }
        $env:FAB_MAINTENANCE_MODE = if ($requestedMaintenanceMode) { "true" } else { "false" }
        $env:FAB_INSTANCE_ROOT = $root
        $env:FAB_LOCAL_API_TOKEN = $apiToken
        $env:FAB_HAI_API_TOKEN = $haiApiToken
        $apiProcess = Invoke-FabWithServiceCredentials -ApiToken $apiToken -HaiApiToken $haiApiToken -Action {
            Start-FabContainedProcess -FilePath $python.Source -ArgumentList @("-m", "src.operations.local_api") -WorkingDirectory $root -RedirectStandardOutput (Join-Path $logsRoot "local-api.out.log") -RedirectStandardError (Join-Path $logsRoot "local-api.err.log")
        }
        $apiPid = $apiProcess.Id
        $apiStartedThisRun = $true
    }
    finally {
        [Environment]::SetEnvironmentVariable("FAB_LOCAL_API_HOST", $previousApiHost, "Process")
        if ($null -eq $previousApiPort) {
            Remove-Item Env:FAB_LOCAL_API_PORT -ErrorAction SilentlyContinue
        }
        else {
            $env:FAB_LOCAL_API_PORT = $previousApiPort
        }
        if ($null -eq $previousMaintenanceMode) {
            Remove-Item Env:FAB_MAINTENANCE_MODE -ErrorAction SilentlyContinue
        }
        else {
            $env:FAB_MAINTENANCE_MODE = $previousMaintenanceMode
        }
        if ($null -eq $previousApiInstanceRoot) {
            Remove-Item Env:FAB_INSTANCE_ROOT -ErrorAction SilentlyContinue
        }
        else {
            $env:FAB_INSTANCE_ROOT = $previousApiInstanceRoot
        }
        if ($null -eq $previousLocalApiToken) {
            Remove-Item Env:FAB_LOCAL_API_TOKEN -ErrorAction SilentlyContinue
        }
        else {
            $env:FAB_LOCAL_API_TOKEN = $previousLocalApiToken
        }
        if ($null -eq $previousHaiApiToken) {
            Remove-Item Env:FAB_HAI_API_TOKEN -ErrorAction SilentlyContinue
        }
        else {
            $env:FAB_HAI_API_TOKEN = $previousHaiApiToken
        }
    }
}
$apiBaseUrl = ([System.Uri]$apiUrl).GetLeftPart([System.UriPartial]::Authority)

if ($savedRuntime -and $savedRuntime.dashboardUrl) {
    $savedDashboardUri = [System.Uri]$savedRuntime.dashboardUrl
    $savedWebIdentityUrl = "$($savedDashboardUri.GetLeftPart([System.UriPartial]::Authority))/api/fab/runtime"
    if (Test-FabEndpoint -Url $savedWebIdentityUrl -ExpectedService "fab-operator-dashboard" -ExpectedLocalApiEndpoint $apiBaseUrl -ExpectedInstanceRoot $root) {
        $dashboardUrl = [string]$savedRuntime.dashboardUrl
        $webListenerPid = Get-FabListenerProcessId -Url $savedWebIdentityUrl
        if ($webListenerPid) {
            $savedWebProcessMarker = "pnpm"
            if ($savedRuntime.PSObject.Properties["webProcessMarker"]) {
                $savedWebProcessMarker = [string]$savedRuntime.webProcessMarker
            }
            $savedWebPid = Get-FabProcessId -ProcessId $savedRuntime.webPid -CommandMarker $savedWebProcessMarker
            if ($savedWebPid -and (Test-FabProcessAncestor -AncestorProcessId $savedWebPid -DescendantProcessId $webListenerPid)) {
                $webPid = $savedWebPid
            }
            else {
                $webPid = Get-FabDashboardProcessRoot -ListenerProcessId $webListenerPid -ExpectedWebRoot $webRoot
            }
            $listenerProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $webListenerPid" -ErrorAction SilentlyContinue
            $webMode = Get-FabDashboardMode -Process $listenerProcess
            $webProcessMarker = if ($webMode -eq "production") { "dist/fab-standalone.js" } else { "dev" }
        }
    }
    else {
        $webPid = $null
    }
}

if (-not $webPid) {
    $runningDashboard = Find-RunningFabDashboard -ExpectedRoot $root -ExpectedWebRoot $webRoot -ExpectedLocalApiEndpoint $apiBaseUrl
    if ($runningDashboard) {
        $webPid = [int]$runningDashboard.ProcessId
        $webListenerPid = [int]$runningDashboard.ListenerProcessId
        $dashboardUrl = [string]$runningDashboard.DashboardUrl
        $webIdentityUrl = [string]$runningDashboard.IdentityUrl
        $webMode = [string]$runningDashboard.Mode
        $webProcessMarker = if ($webMode -eq "production") { "dist/fab-standalone.js" } else { "dev" }
    }
}

if (-not $webPid) {
    $webPort = Find-AvailableFabPort -StartPort $defaultWebPort -Attempts $(if ($deploymentProfile -eq "local") { 20 } else { 1 })
    $dashboardUrl = "http://127.0.0.1:$webPort/admin/operations"
    $webIdentityUrl = "http://127.0.0.1:$webPort/api/fab/runtime"
    $webMode = if ($Development) { "development" } else { "production" }
    if ($webMode -eq "production" -and -not (Test-FabWebBuildCurrent -ExpectedWebRoot $webRoot)) {
        Write-Host "Building the FAB operator dashboard..."
        & $pnpm.Source --dir $webRoot build
        if ($LASTEXITCODE -ne 0) {
            throw "FAB dashboard production build failed with exit code $LASTEXITCODE."
        }
    }
    $previousWebPort = $env:PORT
    $previousLocalApiUrl = $env:FAB_LOCAL_API_URL
    $previousLocalApiPublicUrl = $env:FAB_LOCAL_API_PUBLIC_URL
    $previousLocalApiToken = $env:FAB_LOCAL_API_TOKEN
    $previousOperationsServiceToken = $env:FAB_OPERATIONS_SERVICE_TOKEN
    $previousWebInstanceRoot = $env:FAB_INSTANCE_ROOT
    $previousNodeEnvironment = $env:NODE_ENV
    $previousJwtSecret = $env:JWT_SECRET
    $previousWebHost = $env:FAB_WEB_HOST
    $previousOperatorLocalMode = $env:FAB_OPERATOR_LOCAL_MODE
    try {
        $env:PORT = [string]$webPort
        $env:FAB_LOCAL_API_URL = $apiBaseUrl
        if (-not $env:FAB_LOCAL_API_PUBLIC_URL) {
            $env:FAB_LOCAL_API_PUBLIC_URL = $apiBaseUrl
        }
        $env:FAB_INSTANCE_ROOT = $root
        $env:JWT_SECRET = $webJwtSecret
        $env:FAB_WEB_HOST = "127.0.0.1"
        $env:FAB_OPERATOR_LOCAL_MODE = "true"
        if ($apiToken) {
            $env:FAB_LOCAL_API_TOKEN = $apiToken
            $env:FAB_OPERATIONS_SERVICE_TOKEN = $apiToken
        }
        else {
            Remove-Item Env:FAB_LOCAL_API_TOKEN -ErrorAction SilentlyContinue
            Remove-Item Env:FAB_OPERATIONS_SERVICE_TOKEN -ErrorAction SilentlyContinue
        }
        if ($webMode -eq "development") {
            $tsxCli = Join-Path $webRoot "node_modules\tsx\dist\cli.mjs"
            if (-not (Test-Path -LiteralPath $tsxCli -PathType Leaf)) {
                throw "FAB development runner is missing. Install the locked web dependencies first."
            }
            $env:NODE_ENV = "development"
            $webProcess = Invoke-FabWithServiceCredentials -ApiToken $apiToken -HaiApiToken $haiApiToken -JwtSecret $webJwtSecret -Action {
                Start-FabContainedProcess -FilePath $node.Source -ArgumentList @($tsxCli, "watch", (Join-Path $webRoot "server\dev.ts")) -WorkingDirectory $webRoot -RedirectStandardOutput (Join-Path $logsRoot "web.out.log") -RedirectStandardError (Join-Path $logsRoot "web.err.log")
            }
            $webProcessMarker = "dev"
        }
        else {
            $env:NODE_ENV = "production"
            $webProcess = Invoke-FabWithServiceCredentials -ApiToken $apiToken -HaiApiToken $haiApiToken -JwtSecret $webJwtSecret -Action {
                Start-FabContainedProcess -FilePath $node.Source -ArgumentList @((Join-Path $webRoot "dist\fab-standalone.js")) -WorkingDirectory $webRoot -RedirectStandardOutput (Join-Path $logsRoot "web.out.log") -RedirectStandardError (Join-Path $logsRoot "web.err.log")
            }
            $webProcessMarker = "dist/fab-standalone.js"
        }
        $webPid = $webProcess.Id
        $webStartedThisRun = $true
    }
    finally {
        if ($null -eq $previousWebPort) {
            Remove-Item Env:PORT -ErrorAction SilentlyContinue
        }
        else {
            $env:PORT = $previousWebPort
        }
        if ($null -eq $previousLocalApiUrl) {
            Remove-Item Env:FAB_LOCAL_API_URL -ErrorAction SilentlyContinue
        }
        else {
            $env:FAB_LOCAL_API_URL = $previousLocalApiUrl
        }
        if ($null -eq $previousLocalApiPublicUrl) {
            Remove-Item Env:FAB_LOCAL_API_PUBLIC_URL -ErrorAction SilentlyContinue
        }
        else {
            $env:FAB_LOCAL_API_PUBLIC_URL = $previousLocalApiPublicUrl
        }
        if ($null -eq $previousLocalApiToken) {
            Remove-Item Env:FAB_LOCAL_API_TOKEN -ErrorAction SilentlyContinue
        }
        else {
            $env:FAB_LOCAL_API_TOKEN = $previousLocalApiToken
        }
        if ($null -eq $previousOperationsServiceToken) {
            Remove-Item Env:FAB_OPERATIONS_SERVICE_TOKEN -ErrorAction SilentlyContinue
        }
        else {
            $env:FAB_OPERATIONS_SERVICE_TOKEN = $previousOperationsServiceToken
        }
        if ($null -eq $previousWebInstanceRoot) {
            Remove-Item Env:FAB_INSTANCE_ROOT -ErrorAction SilentlyContinue
        }
        else {
            $env:FAB_INSTANCE_ROOT = $previousWebInstanceRoot
        }
        if ($null -eq $previousNodeEnvironment) {
            Remove-Item Env:NODE_ENV -ErrorAction SilentlyContinue
        }
        else {
            $env:NODE_ENV = $previousNodeEnvironment
        }
        if ($null -eq $previousJwtSecret) {
            Remove-Item Env:JWT_SECRET -ErrorAction SilentlyContinue
        }
        else {
            $env:JWT_SECRET = $previousJwtSecret
        }
        if ($null -eq $previousWebHost) {
            Remove-Item Env:FAB_WEB_HOST -ErrorAction SilentlyContinue
        }
        else {
            $env:FAB_WEB_HOST = $previousWebHost
        }
        if ($null -eq $previousOperatorLocalMode) {
            Remove-Item Env:FAB_OPERATOR_LOCAL_MODE -ErrorAction SilentlyContinue
        }
        else {
            $env:FAB_OPERATOR_LOCAL_MODE = $previousOperatorLocalMode
        }
    }
}
else {
    $dashboardUri = [System.Uri]$dashboardUrl
    $webIdentityUrl = "$($dashboardUri.GetLeftPart([System.UriPartial]::Authority))/api/fab/runtime"
}

Wait-FabEndpoint -Url $apiUrl -Name "FAB ledger API" -ExpectedService "fab-ledger-api" -ApiToken $apiToken -ExpectedInstanceRoot $root -ExpectedMaintenanceMode $requestedMaintenanceMode -TimeoutSeconds 120
Wait-FabEndpoint -Url $webIdentityUrl -Name "FAB operator dashboard" -ExpectedService "fab-operator-dashboard" -ExpectedLocalApiEndpoint $apiBaseUrl -ExpectedInstanceRoot $root -TimeoutSeconds 120
$webListenerPid = Get-FabListenerProcessId -Url $webIdentityUrl
if (-not $webListenerPid) {
    throw "FAB dashboard is responding but its loopback listener process could not be identified."
}
if (-not (Test-FabProcessAncestor -AncestorProcessId $webPid -DescendantProcessId $webListenerPid)) {
    if ($webStartedThisRun) {
        throw "The newly started FAB dashboard does not own the responding listener. Startup was stopped."
    }
    $webPid = Get-FabDashboardProcessRoot -ListenerProcessId $webListenerPid -ExpectedWebRoot $webRoot
}

if (-not $requestedMaintenanceMode -and -not $workerPid) {
    $previousWorkerInstanceRoot = $env:FAB_INSTANCE_ROOT
    try {
        $env:FAB_INSTANCE_ROOT = $root
        $workerProcess = Invoke-FabWithServiceCredentials -ApiToken $apiToken -HaiApiToken $haiApiToken -Action {
            Start-FabContainedProcess -FilePath $python.Source -ArgumentList @("-m", "src.run_worker") -WorkingDirectory $root -RedirectStandardOutput (Join-Path $logsRoot "worker.out.log") -RedirectStandardError (Join-Path $logsRoot "worker.err.log")
        }
        $workerPid = $workerProcess.Id
        $workerStartedThisRun = $true
    }
    finally {
        if ($null -eq $previousWorkerInstanceRoot) {
            Remove-Item Env:FAB_INSTANCE_ROOT -ErrorAction SilentlyContinue
        }
        else {
            $env:FAB_INSTANCE_ROOT = $previousWorkerInstanceRoot
        }
    }
}
if (-not $requestedMaintenanceMode) {
    $workerPid = Wait-FabWorkerRuntime -Path $workerRuntimePath -ExpectedRoot $root -ExpectedProcessId $workerPid -TimeoutSeconds 30
}

if (-not $requestedMaintenanceMode) {
try {
    $activationStatusRequest = @{
        UseBasicParsing = $true
        TimeoutSec = 5
    }
    if ($apiToken) {
        $activationStatusRequest.Headers = @{ Authorization = "Bearer $apiToken" }
    }

    $gmailStatus = Invoke-RestMethod @activationStatusRequest -Uri "$apiBaseUrl/api/connectors/gmail/authorization"
    $driveStatus = Invoke-RestMethod @activationStatusRequest -Uri "$apiBaseUrl/api/connectors/google-drive/authorization"
    $waveStatus = Invoke-RestMethod @activationStatusRequest -Uri "$apiBaseUrl/api/wave/setup"
    $waveReceiptExecutorStatus = Invoke-RestMethod @activationStatusRequest -Uri "$apiBaseUrl/api/wave/receipt-executor/status"
    if ([bool]$gmailStatus.reauthorizationRequired) {
        Write-Warning "Gmail needs fresh read-only consent. Open Finish activation in the FAB dashboard and select Reauthorize Gmail."
    }
    elseif (-not [bool]$gmailStatus.tokenPresent) {
        Write-Warning "Gmail is not authorized. Open Finish activation in the FAB dashboard and select Authorize Gmail."
    }
    if ([bool]$driveStatus.reauthorizationRequired) {
        Write-Warning "Google Drive needs fresh consent. Open Finish activation in the FAB dashboard and select Reauthorize Drive."
    }
    elseif (-not [bool]$driveStatus.tokenPresent) {
        Write-Warning "Google Drive is not authorized. Open Finish activation in the FAB dashboard and select Authorize Drive."
    }
    if (-not [bool]$waveStatus.ready) {
        Write-Warning "Wave is not ready. Open Finish activation in the FAB dashboard and select Connect Wave."
    }
    elseif (-not [bool]$waveReceiptExecutorStatus.ready) {
        Write-Warning "Wave receipt delivery is not ready. Open Finish activation in the FAB dashboard and review the Wave receipt session."
    }
}
catch {
    Write-Warning "FAB could not read provider activation status during startup."
}
}

$runtimeMetadata = [ordered]@{
    startedAt = (Get-Date).ToUniversalTime().ToString("o")
    root = $root
    apiPid = $apiPid
    workerPid = $workerPid
    webPid = $webPid
    webListenerPid = $webListenerPid
    webMode = $webMode
    webProcessMarker = $webProcessMarker
    apiUrl = $apiUrl
    apiBaseUrl = $apiBaseUrl
    dashboardUrl = $dashboardUrl
    webIdentityUrl = $webIdentityUrl
    sourceFingerprint = $sourceFingerprint
    maintenanceMode = $requestedMaintenanceMode
    deploymentProfile = $deploymentProfile
    deploymentStorageId = $deploymentStorageId
}
$processIdentities = @{}
$savedIdentitiesOwned = $false
if ($savedRuntime -and $savedRuntime.PSObject.Properties['root']) {
    try {
        $savedIdentitiesOwned = ([IO.Path]::GetFullPath([string]$savedRuntime.root).TrimEnd('\', '/') -eq [IO.Path]::GetFullPath($root).TrimEnd('\', '/'))
    }
    catch { $savedIdentitiesOwned = $false }
}
if ($savedIdentitiesOwned -and $savedRuntime.PSObject.Properties['processes'] -and $null -ne $savedRuntime.processes) {
    foreach ($entry in $savedRuntime.processes.PSObject.Properties) {
        if ($entry.Name -in @('api', 'web', 'worker') -and $null -ne $entry.Value -and
            $entry.Value.PSObject.Properties['rootPid'] -and
            [int]$entry.Value.rootPid -eq [int]$runtimeMetadata[$entry.Name + 'Pid']) {
            $processIdentities[$entry.Name] = $entry.Value
        }
    }
}
foreach ($name in @('api', 'web', 'worker')) {
    $process = @{api=$apiProcess; web=$webProcess; worker=$workerProcess}[$name]
    if ($null -ne $process) {
        $processIdentities[$name] = @{
            rootPid = $process.Id
            startedAtTicks = [string]$process.StartTime.ToUniversalTime().Ticks
            jobName = $process.FabJob.JobName
        }
    }
}
$runtimeMetadata.processes = $processIdentities
# Retain rollback handles until metadata publication succeeds. Only newly
# launched roots receive a lifetime handle; adopted services are left alone.
foreach ($process in @($apiProcess, $webProcess, $workerProcess)) {
    if ($null -ne $process) { $process.FabJob.Commit() }
}
Write-FabRuntimeMetadata -Path $runtimePath -Runtime $runtimeMetadata
$startupCompleted = $true
}
finally {
    # Pipeline cancellation bypasses catch, but still enters finally. Keep the
    # launcher-owned job handles until unsuccessful starts have been unwound.
    if (-not $startupCompleted) {
        if ($webStartedThisRun) {
            try { Stop-FabSpawnedProcessTree -Process $webProcess }
            catch { Write-Warning "Could not stop the newly started dashboard. Inspect this checkout with Stop-FAB.cmd." -WarningAction Continue }
        }
        if ($workerStartedThisRun) {
            try { Stop-FabSpawnedProcessTree -Process $workerProcess }
            catch { Write-Warning "Could not stop the newly started worker. Inspect this checkout with Stop-FAB.cmd." -WarningAction Continue }
        }
        if ($apiStartedThisRun) {
            try { Stop-FabSpawnedProcessTree -Process $apiProcess }
            catch { Write-Warning "Could not stop the newly started API. Inspect this checkout with Stop-FAB.cmd." -WarningAction Continue }
        }
    }
    foreach ($process in @($apiProcess, $webProcess, $workerProcess)) {
        if ($null -ne $process) {
            $process.FabJob.Dispose()
            if ($process -is [System.Diagnostics.Process]) { $process.Dispose() }
        }
    }
}

Write-Host ""
if ($requestedMaintenanceMode) {
    Write-Host "FAB maintenance is ready." -ForegroundColor Yellow
    Write-Host "The autonomous worker, normal mutations, HAI commands, and cloud access are locked."
}
else {
    Write-Host "FAB is ready." -ForegroundColor Green
}
Write-Host "Dashboard: $dashboardUrl"
Write-Host "Detailed ledger: $apiBaseUrl/"
if ($requestedMaintenanceMode) {
    Write-Host "Use Stop-FAB.cmd when recovery is complete, then Start-FAB.cmd to resume standard operation."
}
else {
    Write-Host "Use Stop-FAB.cmd to stop the local services."
}

if (-not $NoBrowser) {
    Start-Process $dashboardUrl
}
}
finally { Exit-FabLifecycleLock -Lock $lifecycleLock }
