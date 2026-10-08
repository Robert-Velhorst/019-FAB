import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SHELLS = sorted({path for name in ("pwsh", "powershell") if (path := shutil.which(name))})


@pytest.mark.parametrize("shell", SHELLS or [None])
def test_worker_registration_wait_checks_owner_exit_and_deadline(tmp_path, shell):
    if not shell:
        pytest.skip("PowerShell is unavailable")
    source = (ROOT / "Start-FAB.ps1").read_text(encoding="utf-8")
    assert "function Wait-FabWorkerRuntime {" in source
    function = source[source.index("function Wait-FabWorkerRuntime {"):source.index("function Wait-FabEndpoint {")]
    command = r"""
    $ErrorActionPreference = 'Stop'
    Set-StrictMode -Version Latest
    __FUNCTION__
    function Get-FabWorkerRuntimeProcessId {
        param($Path, $ExpectedRoot, $OperationTimeoutSeconds)
        if ($OperationTimeoutSeconds -ne 1) { throw 'registration-timeout-not-forwarded' }
        $script:reads += 1
        if ($mode -in @('dead', 'timeout')) { return $null }
        if ($mode -eq 'delayed' -and $script:reads -eq 1) { return $null }
        if ($mode -eq 'foreign') { return 999 }
        if ($mode -eq 'late') { Start-Sleep -Milliseconds 1200 }
        200
    }
    function Get-FabProcessId {
        param($ProcessId, $CommandMarker, $OperationTimeoutSeconds)
        if ($OperationTimeoutSeconds -ne 1) { throw 'liveness-timeout-not-forwarded' }
        if ($mode -eq 'dead') { return $null }
        100
    }
    function Test-FabProcessAncestor {
        param($AncestorProcessId, $DescendantProcessId, $Watch, $TimeoutSeconds)
        if (-not $Watch -or $TimeoutSeconds -ne 1) { throw 'ancestry-budget-not-forwarded' }
        if ($mode -eq 'late-ancestry') { Start-Sleep -Milliseconds 1200 }
        $AncestorProcessId -eq 100 -and $DescendantProcessId -eq 200
    }
    $results = @()
    foreach ($mode in @('ready', 'delayed', 'foreign', 'dead', 'timeout', 'late', 'late-ancestry')) {
        $script:reads = 0
        $watch = [Diagnostics.Stopwatch]::StartNew()
        $owner = $null
        $failure = $null
        try { $owner = Wait-FabWorkerRuntime -Path 'synthetic.json' -ExpectedRoot '.' -ExpectedProcessId 100 -TimeoutSeconds 1 }
        catch { $failure = $_.Exception.Message }
        $results += @{mode=$mode; owner=$owner; failure=$failure; reads=$script:reads; seconds=$watch.Elapsed.TotalSeconds}
    }
    ConvertTo-Json -InputObject $results -Compress
    """.replace("__FUNCTION__", function)
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                            cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    rows = {row["mode"]: row for row in json.loads(result.stdout)}
    for mode in ("ready", "delayed"):
        assert rows[mode]["owner"] == 200, rows[mode]
        assert rows[mode]["failure"] is None
    assert rows["delayed"]["reads"] >= 2
    for mode in ("foreign", "dead", "timeout", "late", "late-ancestry"):
        assert rows[mode]["owner"] is None, rows[mode]
        assert rows[mode]["failure"]
    assert rows["dead"]["reads"] == 1
    for mode in ("foreign", "timeout", "late", "late-ancestry"):
        assert 1 <= rows[mode]["seconds"] < 15


@pytest.mark.parametrize("shell", SHELLS or [None])
def test_registration_and_ancestry_forward_budget_to_cim(tmp_path, shell):
    if not shell:
        pytest.skip("PowerShell is unavailable")
    source = (ROOT / "Start-FAB.ps1").read_text(encoding="utf-8")
    functions = []
    for name in ("Get-FabProcessId", "Get-FabWorkerRuntimeProcessId", "Test-FabProcessAncestor"):
        start = source.index(f"function {name} {{")
        functions.append(source[start:source.index("\nfunction ", start + 1)])
    (tmp_path / "worker.json").write_text(json.dumps({"pid": 100, "instanceRoot": str(tmp_path)}), encoding="utf-8")
    command = r"""
    $ErrorActionPreference = 'Stop'
    Set-StrictMode -Version Latest
    __FUNCTIONS__
    $timeouts = [System.Collections.Generic.List[int]]::new()
    function Get-CimInstance {
        param($ClassName, $Filter, $OperationTimeoutSec)
        $timeouts.Add([int]$OperationTimeoutSec)
        if ($Filter -eq 'ProcessId = 100') {
            return [pscustomobject]@{ProcessId=100; ParentProcessId=0; CommandLine='python -m src.run_worker'}
        }
        if ($Filter -eq 'ProcessId = 200') {
            return [pscustomobject]@{ProcessId=200; ParentProcessId=100}
        }
        throw 'unexpected-process-query'
    }
    $registered = Get-FabWorkerRuntimeProcessId -Path 'worker.json' -ExpectedRoot (Get-Location).Path -OperationTimeoutSeconds 1
    $related = Test-FabProcessAncestor -AncestorProcessId 100 -DescendantProcessId 200 -Watch ([Diagnostics.Stopwatch]::StartNew()) -TimeoutSeconds 1
    @{registered=$registered; related=$related; timeouts=@($timeouts.ToArray())} | ConvertTo-Json -Compress
    """.replace("__FUNCTIONS__", "\n".join(functions))
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                            cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"registered": 100, "related": True, "timeouts": [1, 1]}
