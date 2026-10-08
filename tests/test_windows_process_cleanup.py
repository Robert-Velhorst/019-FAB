import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SHELLS = sorted({path for name in ("pwsh", "powershell") if (path := shutil.which(name))})


@pytest.mark.skipif(os.name != "nt", reason="Windows process ownership test")
@pytest.mark.parametrize("shell", SHELLS or [None])
@pytest.mark.parametrize("inspection_failure", [False, True])
@pytest.mark.parametrize("parent_exited", [False, True])
def test_real_owned_process_cleanup_preserves_unrelated_process(tmp_path, shell, inspection_failure, parent_exited, intermediate_exited=False):
    if not shell:
        pytest.skip("PowerShell is unavailable")
    source = (ROOT / "Start-FAB.ps1").read_text(encoding="utf-8")
    function = source[source.index("function Stop-FabSpawnedProcessTree {"):source.index("function Invoke-FabNativeCommand {")]
    # This program only records its child PID and waits. No FAB/provider code runs.
    child_script = tmp_path / "sleeper.py"
    child_script.write_text("import time\ntime.sleep(25)\n", encoding="utf-8")
    publish_child = (
        "pathlib.Path('child.pid.tmp').write_text(str(child.pid))\n"
        "pathlib.Path('child.pid.tmp').replace('child.pid')\n"
    )
    if intermediate_exited:
        (tmp_path / "intermediate.py").write_text(
            "import pathlib, subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, 'sleeper.py'])\n" + publish_child,
            encoding="utf-8",
        )
    parent_script = tmp_path / "parent.py"
    parent_script.write_text(
        "import pathlib, subprocess, sys, time\n"
        + ("child = subprocess.Popen([sys.executable, 'intermediate.py'])\nchild.wait(timeout=10)\n"
           if intermediate_exited else "child = subprocess.Popen([sys.executable, 'sleeper.py'])\n" + publish_child)
        + "pathlib.Path('parent.ready').touch()\n"
        + ("" if parent_exited else "time.sleep(25)\n"), encoding="utf-8",
    )
    command = r"""
    $ErrorActionPreference = 'Stop'
    Set-StrictMode -Version Latest
    __FUNCTION__
    . '__PROCESS_HELPER__'
    . '__JOB_HELPER__'
    $python = '__PYTHON__'
    $root = (Get-Location).Path
    $parent = $null
    $other = $null
    $child = $null
    try {
        if (__CONTAINED__) {
            $parent = Start-FabContainedProcess -FilePath $python -ArgumentList @('parent.py') -WorkingDirectory $root -RedirectStandardOutput 'parent.out' -RedirectStandardError 'parent.err'
        } else {
            $parent = Start-Process -FilePath $python -ArgumentList @('parent.py') -WorkingDirectory $root -WindowStyle Hidden -PassThru
        }
        $other = Start-Process -FilePath $python -ArgumentList @('sleeper.py') -WorkingDirectory $root -WindowStyle Hidden -PassThru
        $deadline = [Diagnostics.Stopwatch]::StartNew()
        while (-not (Test-Path -LiteralPath 'parent.ready') -and $deadline.Elapsed.TotalSeconds -lt 10) {
            Start-Sleep -Milliseconds 50
        }
        $childId = [int](Get-Content -LiteralPath 'child.pid' -Raw)
        $child = Get-Process -Id $childId -ErrorAction Stop
        [void]$child.Handle
        if (__PARENT_EXITED__ -and -not $parent.WaitForExit(10000)) { throw 'synthetic-parent-did-not-exit' }
        if (__INSPECTION_FAILURE__) {
            function Get-CimInstance { throw 'synthetic-inspection-failure' }
        }
        $failure = $null
        try { Stop-FabSpawnedProcessTree -Process $parent }
        catch { $failure = $_.Exception.Message }
        $parentExited = $parent.WaitForExit(1500)
        $childExited = $child.WaitForExit(1500)
        [ordered]@{parentExited=$parentExited; childExited=$childExited; otherAlive=(-not $other.HasExited); failure=$failure} |
            ConvertTo-Json -Compress
    }
    finally {
        if ($parent -and $parent.PSObject.Properties['FabJob']) { $parent.FabJob.Dispose() }
        foreach ($process in @($parent, $child, $other)) {
            if ($null -ne $process) {
                try { if (-not $process.HasExited) { $process.Kill(); [void]$process.WaitForExit(5000) } }
                finally { $process.Dispose() }
            }
        }
    }
    """.replace("__FUNCTION__", function).replace("__PYTHON__", sys._base_executable.replace("'", "''"))
    command = command.replace("__INSPECTION_FAILURE__", "$true" if inspection_failure else "$false")
    command = command.replace("__PARENT_EXITED__", "$true" if parent_exited else "$false")
    command = command.replace("__CONTAINED__", "$true" if intermediate_exited else "$false")
    command = command.replace("__JOB_HELPER__", str(ROOT / "scripts/Windows-Job.ps1").replace("'", "''"))
    command = command.replace("__PROCESS_HELPER__", str(ROOT / "scripts/Windows-Process.ps1").replace("'", "''"))
    fixture = tmp_path / "cleanup.ps1"
    fixture.write_text(command, encoding="utf-8")
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File", str(fixture)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=50)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed["parentExited"] is True, observed
    assert observed["otherAlive"] is True, observed
    if inspection_failure:
        assert observed["failure"] is not None, observed
    else:
        assert observed["failure"] is None, observed
        assert observed["childExited"] is True, observed


@pytest.mark.skipif(os.name != "nt", reason="Windows process ownership test")
def test_cleanup_covers_descendant_of_already_exited_intermediate(tmp_path):
    test_real_owned_process_cleanup_preserves_unrelated_process(
        tmp_path, SHELLS[0] if SHELLS else None, False, False, intermediate_exited=True,
    )
