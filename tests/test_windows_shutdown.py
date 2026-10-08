import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SHELLS = sorted({path for name in ("pwsh", "powershell") if (path := shutil.which(name))})


def run_script(tmp_path, shell, script):
    if not shell:
        pytest.skip("PowerShell unavailable")
    fixture = tmp_path / "fixture.ps1"
    fixture.write_text("$ErrorActionPreference='Stop'\nSet-StrictMode -Version Latest\n" + script, encoding="utf-8")
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File", str(fixture)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=50)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("failure", ["worker", "api", "web", "alive", "query", "unresponsive", "leases", "tunnel", "none"])
def test_incomplete_shutdown_retains_metadata_and_leases(tmp_path, failure):
    source = (ROOT / "Stop-FAB.ps1").read_text(encoding="utf-8")
    tail = source[source.index("foreach ($ownedWebPid in $webPids) {"):source.index("\n}\nfinally {\n    foreach ($process in $script:fabStopProcesses.Values)")]
    (tmp_path / "runtime.json").write_text('{"existing":"preserve"}', encoding="utf-8")
    (tmp_path / "worker.json").write_text('{"existing":"worker"}', encoding="utf-8")
    if failure == "tunnel":
        (tmp_path / "cloud.json").write_text('{"existing":"tunnel"}', encoding="utf-8")
        (tmp_path / "Stop-FAB-Ngrok.ps1").write_text(
            "param([switch]$Quiet)\nthrow 'synthetic-tunnel-stop-failed'\n", encoding="utf-8"
        )
        start = source.index("if (Test-Path -LiteralPath $cloudRuntimePath) {")
        end = source.index("$apiPid = $null", start)
        tail = source[start:end] + tail
    script = r"""
    $root=(Get-Location).Path; $webRoot=$root
    $runtimePath=Join-Path $root 'runtime.json'; $workerRuntimePath=Join-Path $root 'worker.json'
    $cloudRuntimePath=Join-Path $root 'cloud.json'
    $webPids=@(300); $workerPid=200; $apiPids=@(100); $apiToken='synthetic-only'
    $venvPython='Invoke-SyntheticLeaseCleanup'; $stopFailed=$false
    $failure='__FAILURE__'; $cleanupCalls=0; $events=[Collections.Generic.List[string]]::new()
    function Stop-FabDashboardProcessTree {
        $events.Add('web'); if ($failure -eq 'web') { throw 'web-stop-failed' }
    }
    function Stop-FabProcessTree {
        param($ProcessId,$CommandMarker,$Name)
        $kind=if ($ProcessId -eq 200) { 'worker' } else { 'api' }
        $events.Add($kind); if ($failure -eq $kind) { throw "$kind-stop-failed" }
    }
    function Find-RunningFabApiProcessIds {
        if ($failure -eq 'query') { throw 'synthetic-query-failure' }
        if ($failure -eq 'alive') { 100 }
        if ($failure -eq 'unresponsive') { $script:stopFailed=$true }
    }
    function Find-RunningFabDashboardProcessIds { }
    function Get-FabWorkerRuntimeProcessId { }
    function Invoke-SyntheticLeaseCleanup {
        $script:cleanupCalls++
        $global:LASTEXITCODE=if ($failure -eq 'leases') { 19 } else { 0 }
        if ($failure -ne 'leases') {
            Remove-Item -LiteralPath $runtimePath -Force
            Remove-Item -LiteralPath $workerRuntimePath -Force
        }
        'lease'
    }
    function Test-Path {
        param([Parameter(Position=0)]$Path,$LiteralPath)
        if ($LiteralPath -eq 'Invoke-SyntheticLeaseCleanup') { return $true }
        $testPath=if ($LiteralPath) { $LiteralPath } else { $Path }
        Microsoft.PowerShell.Management\Test-Path -LiteralPath $testPath
    }
    $failureMessage=$null
    try { __TAIL__ } catch { $failureMessage=$_.Exception.Message }
    @{error=$failureMessage; cleanupCalls=$cleanupCalls; events=@($events.ToArray()); runtimeExists=(Test-Path $runtimePath); workerExists=(Test-Path $workerRuntimePath)} | ConvertTo-Json -Compress
    """.replace("__FAILURE__", failure).replace("__TAIL__", tail)
    observed = run_script(tmp_path, shutil.which("pwsh") or shutil.which("powershell"), script)
    assert observed["events"] == ["web", "worker", "api"]
    if failure == "none":
        assert observed["error"] is None
        assert observed["cleanupCalls"] == 1
        assert not observed["runtimeExists"] and not observed["workerExists"]
    else:
        assert observed["error"] is not None
        assert observed["cleanupCalls"] == (1 if failure == "leases" else 0)
        assert observed["runtimeExists"] and observed["workerExists"]


@pytest.mark.skipif(os.name != "nt", reason="Windows process identities")
@pytest.mark.parametrize("shell", SHELLS or [None])
@pytest.mark.parametrize("mode", ["normal", "foreign", "stale", "discovery-failure"])
def test_shutdown_uses_retained_process_instances(tmp_path, shell, mode):
    helper = ROOT / "scripts/Windows-Process.ps1"
    assert helper.exists(), "Missing shared process identity/cleanup helper"
    source = (ROOT / "Stop-FAB.ps1").read_text(encoding="utf-8")
    register = source[source.index("function Register-FabStopProcess {"):source.index("function Get-FabInstanceId {")]
    functions = source[source.index("function Stop-FabProcessTree {"):source.index("# Discover and stop validated instances")]
    (tmp_path / "sleeper.py").write_text("import time\ntime.sleep(40)\n", encoding="utf-8")
    (tmp_path / "middle.py").write_text(
        "import pathlib,subprocess,sys\np=subprocess.Popen([sys.executable,'sleeper.py'])\n"
        "pathlib.Path('child.tmp').write_text(str(p.pid))\npathlib.Path('child.tmp').replace('child.pid')\n",
        encoding="utf-8")
    (tmp_path / "parent.py").write_text(
        "import pathlib,subprocess,sys,time\nsubprocess.run([sys.executable,'middle.py'],check=True)\n"
        "pathlib.Path('ready').touch()\ntime.sleep(40)\n", encoding="utf-8")
    script = r"""
    . '__HELPER__'
    . '__JOB_HELPER__'
    $script:fabStopProcesses=@{}
    __REGISTER__
    __FUNCTIONS__
    $p=$null; $child=$null; $other=$null; $failureMessage=$null
    try {
        $p=Start-FabContainedProcess -FilePath '__PYTHON__' -ArgumentList @('parent.py') -WorkingDirectory (Get-Location).Path -RedirectStandardOutput 'parent.out' -RedirectStandardError 'parent.err'
        $p.FabJob.Commit(); $p.FabJob.Dispose()
        $other=Start-Process -FilePath '__PYTHON__' -ArgumentList @('sleeper.py') -WorkingDirectory (Get-Location).Path -WindowStyle Hidden -PassThru
        $watch=[Diagnostics.Stopwatch]::StartNew()
        while (-not (Test-Path 'ready') -and $watch.Elapsed.TotalSeconds -lt 10) { Start-Sleep -Milliseconds 50 }
        $child=Get-Process -Id ([int](Get-Content 'child.pid' -Raw)); [void]$child.Handle
        $actual=Get-CimInstance Win32_Process -Filter "ProcessId = $($p.Id)"
        $row=[pscustomobject]@{ProcessId=$actual.ProcessId; CreationDate=$actual.CreationDate; CommandLine=$actual.CommandLine}
        [void](Register-FabStopProcess -Row $row)
        if ('__MODE__' -ne 'discovery-failure') {
            $retained=$script:fabStopProcesses[[int]$p.Id]
            $retained | Add-Member -NotePropertyName FabJob -NotePropertyValue ([Fab.Windows.JobLease]::Open($p.FabJob.JobName,$retained))
        }
        if ('__MODE__' -eq 'stale') {
            $row.CreationDate=$row.CreationDate.AddMilliseconds(1)
            try { [void](Register-FabStopProcess -Row $row) } catch { $failureMessage=$_.Exception.Message }
        } else {
            if ('__MODE__' -eq 'foreign') {
                $row.CommandLine='unrelated-program'
                function Get-CimInstance { $row }
            } elseif ('__MODE__' -eq 'discovery-failure') {
                function Get-CimInstance {
                    param($Filter,$Property,$ErrorAction,$OperationTimeoutSec)
                    if ($Filter -like 'ParentProcessId*') { throw 'synthetic-discovery-failure' }
                    $row
                }
            }
            try { Stop-FabProcessTree -ProcessId $p.Id -CommandMarker 'parent.py' -Name 'synthetic service' } catch { $failureMessage=$_.Exception.Message }
        }
        @{error=$failureMessage; parentExited=$p.WaitForExit(500); childExited=$child.WaitForExit(500); otherAlive=(-not $other.HasExited)} | ConvertTo-Json -Compress
    } finally {
        foreach ($process in $script:fabStopProcesses.Values) {
            if ($process.PSObject.Properties['FabJob']) { $process.FabJob.Dispose() }
            $process.Dispose()
        }
        foreach ($process in @($p,$child,$other)) {
            if ($process) { try { if (-not $process.HasExited) { $process.Kill(); [void]$process.WaitForExit(5000) } } finally { $process.Dispose() } }
        }
    }
    """
    replacements = {
        "HELPER": str(helper), "JOB_HELPER": str(ROOT / "scripts/Windows-Job.ps1"),
        "PYTHON": sys._base_executable, "MODE": mode,
    }
    for key, value in replacements.items():
        script = script.replace(f"__{key}__", value.replace("'", "''"))
    script = script.replace("__REGISTER__", register).replace("__FUNCTIONS__", functions)
    observed = run_script(tmp_path, shell, script)
    assert observed["otherAlive"]
    if mode in {"foreign", "stale"}:
        assert observed["error"] is not None
        assert not observed["parentExited"] and not observed["childExited"]
    else:
        assert observed["parentExited"] and observed["childExited"]
        assert (observed["error"] is None) == (mode == "normal")


@pytest.mark.skipif(os.name != "nt", reason="Windows worker ownership")
@pytest.mark.parametrize("shell", SHELLS or [None])
@pytest.mark.parametrize("foreign", [False, True])
def test_worker_registration_requires_checkout_python(tmp_path, shell, foreign):
    source = (ROOT / "Stop-FAB.ps1").read_text(encoding="utf-8")
    register = source[source.index("function Register-FabStopProcess {"):source.index("function Get-FabInstanceId {")]
    get_id = source[source.index("function Get-FabProcessId {"):source.index("function Test-FabDashboardProcess {")]
    get_worker = source[source.index("function Get-FabWorkerRuntimeProcessId {"):source.index("function Stop-FabProcessTree {")]
    script = r"""
    . '__PROCESS_HELPER__'
    . '__JOB_HELPER__'
    $script:fabStopProcesses=@{}
    __REGISTER__
    __GET_ID__
    __GET_WORKER__
    $p=$null; $failureMessage=$null; $result=$null
    try {
        $p=Start-FabContainedProcess -FilePath '__PYTHON__' -ArgumentList @('-c','import time; time.sleep(40) # src.run_worker') -WorkingDirectory (Get-Location).Path -RedirectStandardOutput 'worker.out' -RedirectStandardError 'worker.err'
        $actual=Get-CimInstance Win32_Process -Filter "ProcessId = $($p.Id)"
        $descendant=Get-CimInstance Win32_Process -Filter "ParentProcessId = $($p.Id)" | Where-Object Name -eq 'python.exe' | Select-Object -First 1
        if ($descendant) { $actual=$descendant }
        $expectedId=[int]$actual.ProcessId
        $row=[pscustomobject]@{ProcessId=$actual.ProcessId; ParentProcessId=$actual.ParentProcessId; CreationDate=$actual.CreationDate; CommandLine=$actual.CommandLine; ExecutablePath=$actual.ExecutablePath; Name=$actual.Name}
        if (__FOREIGN__) { $row.CommandLine='"C:\other-checkout\.venv\Scripts\python.exe" -m src.run_worker'; $row.ExecutablePath='C:\other-checkout\.venv\Scripts\python.exe'; $row.ParentProcessId=0 }
        function Get-CimInstance {
            param($Filter,$OperationTimeoutSec,$ErrorAction)
            if ($Filter -eq "ProcessId = $expectedId") { $row }
            else { CimCmdlets\Get-CimInstance Win32_Process -Filter $Filter -OperationTimeoutSec 2 -ErrorAction Stop }
        }
        @{instanceRoot='__REPO__'; pid=$expectedId} | ConvertTo-Json | Set-Content 'worker.json'
        try { $result=Get-FabWorkerRuntimeProcessId -Path 'worker.json' -ExpectedRoot '__REPO__' } catch { $failureMessage=$_.Exception.Message }
        @{error=$failureMessage; correctId=($result -eq $expectedId); alive=(-not $p.HasExited); command=$row.CommandLine; result=$result; expectedId=$expectedId} | ConvertTo-Json -Compress
    } finally {
        foreach ($process in $script:fabStopProcesses.Values) { $process.Dispose() }
        if ($p) { $p.FabJob.Dispose(); $p.Dispose() }
    }
    """
    for key, value in {
        "PROCESS_HELPER": str(ROOT / "scripts/Windows-Process.ps1"),
        "JOB_HELPER": str(ROOT / "scripts/Windows-Job.ps1"),
        "PYTHON": str(ROOT / ".venv/Scripts/python.exe"), "REPO": str(ROOT),
    }.items():
        script = script.replace(f"__{key}__", value.replace("'", "''"))
    script = script.replace("__REGISTER__", register).replace("__GET_ID__", get_id).replace("__GET_WORKER__", get_worker)
    script = script.replace("__FOREIGN__", "$true" if foreign else "$false")
    observed = run_script(tmp_path, shell, script)
    assert observed["alive"]
    if foreign:
        assert observed["error"] is not None and not observed["correctId"]
    else:
        assert observed["error"] is None and observed["correctId"], json.dumps(observed)


def test_dashboard_discovery_continues_after_candidate_failure(tmp_path):
    source = (ROOT / "Stop-FAB.ps1").read_text(encoding="utf-8")
    discovery = source[source.index("function Find-RunningFabDashboardProcessIds {"):source.index("function Test-FabEndpoint {")]
    script = r"""
    $script:stopFailed=$false; $script:fabStopProcesses=@{}
    function Get-CimInstance {
        [pscustomobject]@{Name='node.exe'; ProcessId=101}
        [pscustomobject]@{Name='node.exe'; ProcessId=102}
    }
    function Test-FabDashboardProcess { $true }
    function Register-FabStopProcess {
        param($Row)
        if ($Row.ProcessId -eq 101) { throw 'synthetic vanished candidate' }
        $script:fabStopProcesses[102]=[pscustomobject]@{HasExited=$false}
    }
    function Get-NetTCPConnection { [pscustomobject]@{LocalAddress='127.0.0.1'; LocalPort=3000} }
    function Test-FabEndpoint { $true }
    function Get-FabDashboardProcessRoot { 102 }
    __DISCOVERY__
    $found=@(Find-RunningFabDashboardProcessIds -ExpectedRoot (Get-Location).Path -ExpectedWebRoot (Get-Location).Path)
    @{failed=$script:stopFailed; found=$found} | ConvertTo-Json -Compress
    """.replace("__DISCOVERY__", discovery)
    observed = run_script(tmp_path, shutil.which("pwsh") or shutil.which("powershell"), script)
    assert observed == {"failed": True, "found": [102]}


@pytest.mark.parametrize("parent", ["older", "recycled", "foreign", "prefix"])
def test_dashboard_ancestry_does_not_promote_recycled_or_foreign_parent(tmp_path, parent):
    source = (ROOT / "Stop-FAB.ps1").read_text(encoding="utf-8")
    matcher = source[source.index("function Test-FabDashboardProcess {"):source.index("function Get-FabDashboardProcessId {")]
    ancestry = source[source.index("function Get-FabDashboardProcessRoot {"):source.index("function Test-FabProcessAncestor {")]
    script = r"""
    $webRoot=Join-Path (Get-Location).Path 'web'
    $created=[datetime]'2026-01-01T00:00:00Z'
    $child=[pscustomobject]@{Name='node.exe'; ProcessId=102; ParentProcessId=101; CreationDate=$created; CommandLine="$webRoot/dist/fab-standalone.js"}
    $parent=[pscustomobject]@{Name='node.exe'; ProcessId=101; ParentProcessId=0; CreationDate=$created.AddSeconds(-1); CommandLine="$webRoot/dist/fab-standalone.js"}
    if ('__PARENT__' -eq 'recycled') { $parent.CreationDate=$created.AddSeconds(1) }
    if ('__PARENT__' -eq 'foreign') { $parent.CommandLine='npm.cmd run dev' }
    if ('__PARENT__' -eq 'prefix') { $parent.CommandLine="$webRoot-copy/server/dev.ts" }
    function Get-CimInstance { param($Filter) if ($Filter -eq 'ProcessId = 102') { $child } else { $parent } }
    function Register-FabStopProcess { param($Row) $Row.ProcessId }
    __MATCHER__
    __ANCESTRY__
    @{root=(Get-FabDashboardProcessRoot -ListenerProcessId 102 -ExpectedWebRoot $webRoot)} | ConvertTo-Json -Compress
    """.replace("__PARENT__", parent).replace("__MATCHER__", matcher).replace("__ANCESTRY__", ancestry)
    observed = run_script(tmp_path, shutil.which("pwsh") or shutil.which("powershell"), script)
    assert observed["root"] == (101 if parent == "older" else 102)


@pytest.mark.parametrize("mode", ["complete", "missing", "empty", "partial", "stale", "null", "no-services"])
def test_runtime_requires_containment_proof_for_each_service(tmp_path, mode):
    source = (ROOT / "Stop-FAB.ps1").read_text(encoding="utf-8")
    coverage = source[source.index("function Test-FabRuntimeContainmentCoverage {"):source.index("function Get-FabInstanceId {")]
    script = r"""
    __COVERAGE__
    $runtime=[pscustomobject]@{apiPid=100; workerPid=200; webPid=300; processes=[pscustomobject]@{
        api=[pscustomobject]@{rootPid=100; startedAtTicks='123'; jobName='job-api'}
        worker=[pscustomobject]@{rootPid=200; startedAtTicks='456'; jobName='job-worker'}
        web=[pscustomobject]@{rootPid=300; startedAtTicks='789'; jobName='job-web'}
    }}
    switch ('__MODE__') {
        missing { $runtime.PSObject.Properties.Remove('processes') }
        empty { $runtime.processes=[pscustomobject]@{} }
        partial { $runtime.processes.PSObject.Properties.Remove('worker') }
        stale { $runtime.processes.worker.rootPid=999 }
        null { $runtime.processes=$null }
        no-services { $runtime=[pscustomobject]@{apiPid=$null; workerPid=$null; webPid=$null} }
    }
    @{covered=(Test-FabRuntimeContainmentCoverage -Runtime $runtime)} | ConvertTo-Json -Compress
    """.replace("__COVERAGE__", coverage).replace("__MODE__", mode)
    observed = run_script(tmp_path, shutil.which("pwsh") or shutil.which("powershell"), script)
    assert observed["covered"] == (mode in {"complete", "no-services"})


@pytest.mark.skipif(os.name != "nt", reason="Windows process groups")
@pytest.mark.parametrize("shell", SHELLS or [None])
@pytest.mark.parametrize("missing", [False, True])
def test_recorded_group_stops_service_without_endpoint_discovery(tmp_path, shell, missing):
    source = (ROOT / "Stop-FAB.ps1").read_text(encoding="utf-8")
    register = source[source.index("function Register-FabStopProcess {"):source.index("function Get-FabInstanceId {")]
    script = r"""
    . '__PROCESS_HELPER__'
    . '__JOB_HELPER__'
    $script:fabStopProcesses=@{}
    __REGISTER__
    $p=$null
    try {
        $p=Start-FabContainedProcess -FilePath '__PYTHON__' -ArgumentList @('-c','import time;time.sleep(40)') -WorkingDirectory (Get-Location).Path -RedirectStandardOutput 'root.out' -RedirectStandardError 'root.err'
        $identity=[pscustomobject]@{rootPid=$p.Id; startedAtTicks=[string]$p.StartTime.ToUniversalTime().Ticks; jobName=$p.FabJob.JobName}
        $p.FabJob.Commit(); $p.FabJob.Dispose()
        if (__MISSING__) { $identity.jobName='Local\FAB-'+[guid]::NewGuid().ToString('N') }
        function Invoke-RestMethod { throw 'Endpoint discovery must not be used' }
        $failure=$null
        try { Stop-FabRecordedProcess -Identity $identity } catch { $failure=$_.Exception.Message }
        @{stopped=$p.WaitForExit(500); error=$failure} | ConvertTo-Json -Compress
    } finally {
        foreach ($process in $script:fabStopProcesses.Values) { $process.Dispose() }
        if ($p) { try { if (-not $p.HasExited) { $p.Kill(); [void]$p.WaitForExit(5000) } } finally { $p.Dispose() } }
    }
    """
    for key, value in {
        "PROCESS_HELPER": str(ROOT / "scripts/Windows-Process.ps1"),
        "JOB_HELPER": str(ROOT / "scripts/Windows-Job.ps1"), "PYTHON": sys._base_executable,
    }.items():
        script = script.replace(f"__{key}__", value.replace("'", "''"))
    script = script.replace("__REGISTER__", register).replace("__MISSING__", "$true" if missing else "$false")
    observed = run_script(tmp_path, shell, script)
    assert observed["stopped"] is not missing
    assert (observed["error"] is not None) is missing


@pytest.mark.parametrize("mode", ["owned", "foreign", "mismatched"])
def test_startup_carries_over_only_owned_matching_process_identities(tmp_path, mode):
    source = (ROOT / "Start-FAB.ps1").read_text(encoding="utf-8")
    carryover = source[source.index("$processIdentities = @{}"):source.index("# Retain rollback handles until metadata publication succeeds.")]
    script = r"""
    $root=(Get-Location).Path
    $runtimeMetadata=[ordered]@{apiPid=100; workerPid=200; webPid=300}
    $apiProcess=$null; $webProcess=$null; $workerProcess=$null
    $savedRuntime=[pscustomobject]@{root=$root; processes=[pscustomobject]@{
        api=[pscustomobject]@{rootPid=100; startedAtTicks='123'; jobName='synthetic-api'}
    }}
    if ('__MODE__' -eq 'foreign') { $savedRuntime.root=Join-Path $root 'other-checkout' }
    if ('__MODE__' -eq 'mismatched') { $savedRuntime.processes.api.rootPid=999 }
    __CARRYOVER__
    @{copied=$runtimeMetadata.processes.ContainsKey('api')} | ConvertTo-Json -Compress
    """.replace("__CARRYOVER__", carryover).replace("__MODE__", mode)
    observed = run_script(tmp_path, shutil.which("pwsh") or shutil.which("powershell"), script)
    assert observed["copied"] == (mode == "owned")


@pytest.mark.parametrize("mode", ["owned", "unknown", "mismatched"])
def test_shutdown_never_terminates_uncovered_recorded_group(tmp_path, mode):
    source = (ROOT / "Stop-FAB.ps1").read_text(encoding="utf-8")
    start = source.index("    if ($runtimeOwned -and $runtime.PSObject.Properties['processes'] -and")
    end = source.index("    try {\n    $apiPid =", start)
    stop_recorded = source[start:end]
    script = r"""
    $runtimeOwned=$true; $stopFailed=$false; $calls=0
    $runtime=[pscustomobject]@{apiPid=100; workerPid=$null; webPid=$null; processes=[pscustomobject]@{
        api=[pscustomobject]@{rootPid=100; startedAtTicks='123'; jobName='synthetic-api'}
    }}
    if ('__MODE__' -eq 'mismatched') { $runtime.processes.api.rootPid=999 }
    if ('__MODE__' -eq 'unknown') { $runtime.processes=[pscustomobject]@{foreign=$runtime.processes.api} }
    function Stop-FabRecordedProcess { $script:calls++ }
    __STOP_RECORDED__
    @{calls=$calls; failed=$stopFailed} | ConvertTo-Json -Compress
    """.replace("__STOP_RECORDED__", stop_recorded).replace("__MODE__", mode)
    observed = run_script(tmp_path, shutil.which("pwsh") or shutil.which("powershell"), script)
    assert observed == {"calls": 1 if mode == "owned" else 0, "failed": mode != "owned"}
