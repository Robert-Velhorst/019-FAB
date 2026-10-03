import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def run_startup(tmp_path, stage, *, reuse=(), cleanup_failure=False, warning_stop=False, mismatched_listener=False, shell=None):
    shell = shell or shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("PowerShell is unavailable")
    source = (ROOT / "Start-FAB.ps1").read_text(encoding="utf-8")
    block = source[source.index("$apiPid = $null"):source.index('\nWrite-Host ""', source.index("$apiPid = $null"))]
    helper = str(ROOT / "scripts/Windows-Profile.ps1").replace("'", "''")
    command = r"""
    $ErrorActionPreference = 'Stop'
    Set-StrictMode -Version Latest
    $WarningPreference = '__WARNING_PREFERENCE__'
    . '__HELPER__'
    $root = (Get-Location).Path
    $webRoot = $root
    $logsRoot = $root
    $runtimePath = Join-Path $root 'fab-runtime.json'
    $workerRuntimePath = Join-Path $root 'worker-runtime.json'
    $sourceFingerprint = 'synthetic'
    $deploymentProfile = 'local'
    $deploymentStorageId = 'synthetic'
    $requestedMaintenanceMode = $false
    $Development = $false
    $savedRuntime = $null
    $defaultApiPort = 5001
    $defaultWebPort = 3000
    $apiToken = 'synthetic-operator-0123456789-ABCDEFGHIJK'
    $haiApiToken = 'synthetic-hai-9876543210-LMNOPQRSTUVWXYZ'
    $webJwtSecret = 'synthetic-jwt-0123456789-ABCDEFGHIJKLM'
    $python = @{Source='not-a-real-process'}
    $node = @{Source='not-a-real-process'}
    $pnpm = @{Source='Invoke-FakeBuild'}
    $stage = '__STAGE__'
    $reuseApi = __REUSE_API__
    $reuseWorker = __REUSE_WORKER__
    $reuseWeb = __REUSE_WEB__
    $cleanupFailure = __CLEANUP_FAILURE__
    $mismatchedListener = __MISMATCHED_LISTENER__
    $started = [System.Collections.Generic.List[int]]::new()
    $stopped = [System.Collections.Generic.List[int]]::new()
    $events = [System.Collections.Generic.List[string]]::new()
    function Find-RunningFabApi {
        param($ExpectedRoot, $ApiToken, $ExpectedMaintenanceMode)
        if ($reuseApi -and -not $ExpectedMaintenanceMode) {
            [pscustomobject]@{ProcessId=110; Url='http://127.0.0.1:5001/api/live'}
        }
    }
    function Get-FabWorkerRuntimeProcessId {
        if ($reuseWorker) { 210 }
    }
    function Find-RunningFabDashboard {
        if ($reuseWeb) {
            [pscustomobject]@{
                ProcessId=310; ListenerProcessId=310; Mode='production'
                DashboardUrl='http://127.0.0.1:3000/admin/operations'
                IdentityUrl='http://127.0.0.1:3000/api/fab/runtime'
            }
        }
    }
    function Find-AvailableFabPort {
        param($StartPort, $Attempts)
        if ($stage -eq 'web-port' -and $StartPort -eq 3000) { throw 'web-port-failed' }
        $StartPort
    }
    function Test-FabWebBuildCurrent { $stage -ne 'build' }
    function Invoke-FakeBuild { $global:LASTEXITCODE = 18 }
    function Start-Process { throw 'unexpected-uncontained-service' }
    function Start-FabContainedProcess {
        param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle,
              $RedirectStandardOutput, $RedirectStandardError, [switch]$PassThru)
        $kind = if ($ArgumentList -contains 'src.operations.local_api') { 'api' }
                elseif ($ArgumentList -contains 'src.run_worker') { 'worker' } else { 'web' }
        if ($stage -eq $kind) { throw "$kind-spawn-failed" }
        $processId = @{api=100; worker=200; web=300}[$kind]
        $events.Add("start-$kind")
        $started.Add($processId)
        $lease = [pscustomobject]@{Kind=$kind; Events=$events; Stage=$stage; JobName=('Local\FAB-test-' + $kind)}
        $lease | Add-Member ScriptMethod Commit {
            $this.Events.Add("commit-$($this.Kind)")
            if ($this.Stage -eq 'commit' -and $this.Kind -eq 'web') { throw 'commit-failed' }
            if ($this.Stage -eq 'cancel' -and $this.Kind -eq 'web') {
                [IO.File]::WriteAllText((Join-Path (Get-Location) 'cancel-ready'), '')
                Start-Sleep -Seconds 25
            }
        }
        $lease | Add-Member ScriptMethod Dispose { $this.Events.Add("dispose-$($this.Kind)") }
        [pscustomobject]@{Id=$processId; FabJob=$lease; StartTime=[datetime]'2026-01-01T00:00:00Z'}
    }
    function Wait-FabEndpoint {
        param($Url, $Name, $ExpectedService, $ApiToken, $ExpectedInstanceRoot,
              $ExpectedMaintenanceMode, $ExpectedLocalApiEndpoint, $TimeoutSeconds)
        $events.Add("ready-$ExpectedService")
        if ($stage -eq 'health') { throw 'health-failed' }
    }
    function Get-FabListenerProcessId { if ($mismatchedListener) { 999 } elseif ($reuseWeb) { 310 } else { 300 } }
    function Test-FabProcessAncestor { -not $mismatchedListener }
    function Get-FabDashboardProcessRoot { 999 }
    function Get-FabProcessId {
        param($ProcessId, $CommandMarker)
        $ProcessId
    }
    function Wait-FabWorkerRuntime {
        param($Path, $ExpectedRoot, $ExpectedProcessId, $TimeoutSeconds)
        $events.Add('worker-registered')
        if ($stage -eq 'worker-runtime') { throw 'worker-runtime-failed' }
        $ExpectedProcessId
    }
    function Stop-FabSpawnedProcessTree {
        param($Process)
        $stopped.Add([int]$Process.Id)
        if ($cleanupFailure -and $Process.Id -eq 300) { throw 'cleanup-failed' }
    }
    function Invoke-RestMethod {
        [pscustomobject]@{reauthorizationRequired=$false; tokenPresent=$true; ready=$true}
    }
    $locked = $null
    if ($stage -eq 'metadata') {
        $locked = [System.IO.File]::Open($runtimePath, 'Open', 'ReadWrite', 'None')
    }
    $failure = $null
    try {
        __BLOCK__
    } catch { $failure = $_.Exception.Message }
    finally {
        if ($locked) { $locked.Dispose() }
        if ($stage -eq 'cancel') {
            $observed = @{started=@($started.ToArray()); stopped=@($stopped.ToArray()); events=@($events.ToArray())} | ConvertTo-Json -Compress
            [IO.File]::WriteAllText((Join-Path (Get-Location) 'cancel-result.json'), $observed)
        }
    }
    [ordered]@{failure=$failure; started=@($started.ToArray()); stopped=@($stopped.ToArray()); events=@($events.ToArray())} |
        ConvertTo-Json -Compress
    """.replace("__HELPER__", helper).replace("__BLOCK__", block).replace("__STAGE__", stage)
    for key, value in {"API": "api" in reuse, "WORKER": "worker" in reuse, "WEB": "web" in reuse}.items():
        command = command.replace(f"__REUSE_{key}__", "$true" if value else "$false")
    command = command.replace("__CLEANUP_FAILURE__", "$true" if cleanup_failure else "$false")
    command = command.replace("__WARNING_PREFERENCE__", "Stop" if warning_stop else "Continue")
    command = command.replace("__MISMATCHED_LISTENER__", "$true" if mismatched_listener else "$false")
    runtime = tmp_path / "fab-runtime.json"
    runtime.write_text('{"previous":"preserve-me"}', encoding="utf-8")
    (tmp_path / "worker-runtime.json").write_text('{"previous":"worker"}', encoding="utf-8")
    fixture = tmp_path / "startup-fixture.ps1"
    fixture.write_text(command, encoding="utf-8")
    if stage == "cancel":
        runner = tmp_path / "cancel-fixture.ps1"
        runner.write_text(r"""
        $ErrorActionPreference='Stop'
        $runner=[PowerShell]::Create()
        try {
            [void]$runner.AddScript("& '" + (Join-Path (Get-Location) 'startup-fixture.ps1').Replace("'", "''") + "'")
            $async=$runner.BeginInvoke()
            $watch=[Diagnostics.Stopwatch]::StartNew()
            while (-not (Test-Path 'cancel-ready') -and $watch.Elapsed.TotalSeconds -lt 10) { Start-Sleep -Milliseconds 50 }
            if (-not (Test-Path 'cancel-ready')) { throw 'cancellation-barrier-not-reached' }
            $runner.Stop()
            Get-Content 'cancel-result.json' -Raw
        } finally { $runner.Dispose() }
        """, encoding="utf-8")
        fixture = runner
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(("FAB_", "JWT_SECRET"))}
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File", str(fixture)],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    # Warnings can precede the fixture's final structured result.
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("stage,expected", [
    ("api", []), ("worker", [300, 100]), ("web-port", [100]),
    ("build", [100]), ("web", [100]),
    ("health", [300, 100]), ("metadata", [300, 200, 100]),
])
def test_failed_start_unwinds_every_new_service(tmp_path, stage, expected):
    result = run_startup(tmp_path, stage)
    assert result["failure"] is not None
    assert result["stopped"] == expected
    assert (tmp_path / "fab-runtime.json").read_text() == '{"previous":"preserve-me"}'


@pytest.mark.parametrize("reuse,expected", [
    (("api",), [300]), (("worker",), [300, 100]),
    (("web",), [100]), (("api", "worker", "web"), []),
])
def test_failure_never_stops_adopted_services(tmp_path, reuse, expected):
    result = run_startup(tmp_path, "health", reuse=reuse)
    assert result["failure"] == "health-failed"
    assert result["stopped"] == expected
    assert (tmp_path / "fab-runtime.json").read_text() == '{"previous":"preserve-me"}'
    if "worker" in reuse:
        assert (tmp_path / "worker-runtime.json").read_text() == '{"previous":"worker"}'


def test_cleanup_failure_does_not_skip_other_services_or_mask_startup_error(tmp_path):
    result = run_startup(tmp_path, "health", cleanup_failure=True)
    assert result["stopped"] == [300, 100]
    assert result["failure"] == "health-failed"


def test_success_records_actual_runtime_without_cleanup(tmp_path):
    result = run_startup(tmp_path, "success")
    assert result["failure"] is None
    assert result["started"] == [100, 300, 200]
    assert result["stopped"] == []
    for service in ("api", "web", "worker"):
        assert result["events"].count(f"commit-{service}") == 1
        assert result["events"].count(f"dispose-{service}") == 1
    runtime = json.loads((tmp_path / "fab-runtime.json").read_text(encoding="utf-8-sig"))
    assert (runtime["apiPid"], runtime["workerPid"], runtime["webPid"]) == (100, 200, 300)
    assert runtime["apiBaseUrl"] == "http://127.0.0.1:5001"
    for service, process_id in (("api", 100), ("worker", 200), ("web", 300)):
        identity = runtime["processes"][service]
        assert identity["rootPid"] == process_id
        assert identity["jobName"] == f"Local\\FAB-test-{service}"
        assert identity["startedAtTicks"] == "639028224000000000"
    assert not list(tmp_path.glob("*.tmp"))


def test_new_worker_is_not_started_before_dashboard_readiness(tmp_path):
    result = run_startup(tmp_path, "success")
    assert result["failure"] is None
    assert result["events"].index("ready-fab-ledger-api") < result["events"].index("start-worker")
    assert result["events"].index("ready-fab-operator-dashboard") < result["events"].index("start-worker")


def test_rollback_never_targets_a_different_dashboard_listener(tmp_path):
    result = run_startup(tmp_path, "worker", mismatched_listener=True)
    assert result["stopped"] == [300, 100]
    assert "listener" in result["failure"]
    assert result["started"] == [100, 300]


def test_warning_stop_cannot_abort_cleanup_or_mask_original_error(tmp_path):
    result = run_startup(tmp_path, "health", cleanup_failure=True, warning_stop=True)
    assert result["stopped"] == [300, 100]
    assert result["failure"] == "health-failed"


def test_worker_registration_failure_rolls_back_before_publishing_runtime(tmp_path):
    result = run_startup(tmp_path, "worker-runtime")
    assert result["failure"] == "worker-runtime-failed"
    assert result["stopped"] == [300, 200, 100]
    assert (tmp_path / "fab-runtime.json").read_text() == '{"previous":"preserve-me"}'


def test_commit_failure_rolls_back_and_disposes_all_new_jobs(tmp_path):
    result = run_startup(tmp_path, "commit")
    assert "commit-failed" in result["failure"]
    assert result["stopped"] == [300, 200, 100]
    for service in ("api", "web", "worker"):
        assert result["events"].count(f"dispose-{service}") == 1
    assert (tmp_path / "fab-runtime.json").read_text() == '{"previous":"preserve-me"}'


@pytest.mark.parametrize("shell", sorted({path for name in ("pwsh", "powershell") if (path := shutil.which(name))}) or [None])
def test_cancelled_startup_rolls_back_already_committed_jobs(tmp_path, shell):
    result = run_startup(tmp_path, "cancel", shell=shell)
    assert result["stopped"] == [300, 200, 100]
    for service in ("api", "web", "worker"):
        assert result["events"].count(f"dispose-{service}") == 1
    assert (tmp_path / "fab-runtime.json").read_text() == '{"previous":"preserve-me"}'
