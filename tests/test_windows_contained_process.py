import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SHELLS = sorted({path for name in ("pwsh", "powershell") if (path := shutil.which(name))})


def run_fixture(tmp_path, shell, body):
    helper = ROOT / "scripts/Windows-Job.ps1"
    assert helper.exists(), "Missing native Windows containment launcher"
    script = (
        "$ErrorActionPreference='Stop'\nSet-StrictMode -Version Latest\n"
        + ". '" + str(helper).replace("'", "''") + "'\n"
        + "$python='" + sys._base_executable.replace("'", "''") + "'\n"
        + body
    )
    path = tmp_path / "fixture.ps1"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File", str(path)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=50)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
@pytest.mark.parametrize("shell", SHELLS or [None])
@pytest.mark.parametrize("mode", ["rollback", "dispose", "committed"])
def test_job_contains_descendant_after_intermediate_exits(tmp_path, shell, mode):
    if not shell:
        pytest.skip("PowerShell is unavailable")
    (tmp_path / "sleeper.py").write_text("import time\ntime.sleep(40)\n", encoding="utf-8")
    (tmp_path / "middle.py").write_text(
        "import pathlib,subprocess,sys\n"
        "p=subprocess.Popen([sys.executable,'sleeper.py'])\n"
        "pathlib.Path('child.tmp').write_text(str(p.pid))\n"
        "pathlib.Path('child.tmp').replace('child.pid')\n", encoding="utf-8",
    )
    (tmp_path / "parent.py").write_text(
        "import pathlib,subprocess,sys,time\n"
        "subprocess.run([sys.executable,'middle.py'],check=True,timeout=10)\n"
        "pathlib.Path('ready').touch()\ntime.sleep(40)\n", encoding="utf-8",
    )
    result = run_fixture(tmp_path, shell, r"""
    $parent=$null; $child=$null; $other=$null
    try {
        $parent=Start-FabContainedProcess -FilePath $python -ArgumentList @('parent.py') -WorkingDirectory (Get-Location).Path -RedirectStandardOutput 'parent.out' -RedirectStandardError 'parent.err'
        $other=Start-Process -FilePath $python -ArgumentList @('sleeper.py') -WorkingDirectory (Get-Location).Path -WindowStyle Hidden -PassThru
        $watch=[Diagnostics.Stopwatch]::StartNew()
        while (-not (Test-Path 'ready') -and $watch.Elapsed.TotalSeconds -lt 10) { Start-Sleep -Milliseconds 50 }
        $child=Get-Process -Id ([int](Get-Content 'child.pid' -Raw))
        [void]$child.Handle
        if ('__MODE__' -eq 'committed') {
            $parent.FabJob.Commit()
            $parent.FabJob.Dispose()
            $parent.Kill()
        } elseif ('__MODE__' -eq 'dispose') { $parent.FabJob.Dispose() }
        else { $parent.FabJob.Terminate() }
        @{parentExited=$parent.WaitForExit(5000); childExited=$child.WaitForExit(5000); otherAlive=(-not $other.HasExited)} | ConvertTo-Json -Compress
    } finally {
        if ($parent) { $parent.FabJob.Dispose() }
        foreach ($p in @($parent,$child,$other)) {
            if ($p) { try { if (-not $p.HasExited) { $p.Kill(); [void]$p.WaitForExit(5000) } } finally { $p.Dispose() } }
        }
    }
    """.replace("__MODE__", mode))
    assert result == {"parentExited": True, "childExited": True, "otherAlive": True}


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
@pytest.mark.parametrize("shell", SHELLS or [None])
def test_contained_spawn_preserves_arguments_environment_and_logs(tmp_path, shell):
    if not shell:
        pytest.skip("PowerShell is unavailable")
    (tmp_path / "arguments.py").write_text(
        "import json,os,sys\nprint(json.dumps({'args':sys.argv[1:],'value':os.environ['FAB_TEST_VALUE'],'cwd':os.getcwd()}))\n"
        "print('synthetic-stderr',file=sys.stderr)\n", encoding="utf-8",
    )
    result = run_fixture(tmp_path, shell, r"""
    $env:FAB_TEST_VALUE='synthetic-only'
    $p=$null
    try {
        $p=Start-FabContainedProcess -FilePath $python -ArgumentList @('arguments.py','','two words','a"b','trailing\','&|<>') -WorkingDirectory (Get-Location).Path -RedirectStandardOutput 'stdout.log' -RedirectStandardError 'stderr.log'
        if (-not $p.WaitForExit(10000)) { throw 'child-timeout' }
        @{exitCode=$p.ExitCode; output=(Get-Content 'stdout.log' -Raw | ConvertFrom-Json); error=(Get-Content 'stderr.log' -Raw).Trim()} | ConvertTo-Json -Compress -Depth 4
    } finally { if ($p) { $p.FabJob.Dispose(); $p.Dispose() } }
    """)
    assert result["exitCode"] == 0
    assert result["output"] == {"args": ["", "two words", 'a"b', "trailing\\", "&|<>"], "value": "synthetic-only", "cwd": str(tmp_path)}
    assert result["error"] == "synthetic-stderr"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
@pytest.mark.parametrize("shell", SHELLS or [None])
def test_committed_service_survives_launcher_exit_until_root_exits(tmp_path, shell):
    if not shell:
        pytest.skip("PowerShell is unavailable")
    (tmp_path / "sleeper.py").write_text("import time\ntime.sleep(40)\n", encoding="utf-8")
    (tmp_path / "parent.py").write_text(
        "import pathlib,subprocess,sys,time\n"
        "p=subprocess.Popen([sys.executable,'sleeper.py'])\n"
        "pathlib.Path('child.tmp').write_text(str(p.pid))\n"
        "pathlib.Path('child.tmp').replace('child.pid')\n"
        "deadline=time.monotonic()+40\n"
        "while not pathlib.Path('stop').exists() and time.monotonic()<deadline: time.sleep(.05)\n",
        encoding="utf-8",
    )
    helper = str(ROOT / "scripts/Windows-Job.ps1").replace("'", "''")
    python = sys._base_executable.replace("'", "''")
    (tmp_path / "launch.ps1").write_text(
        "$ErrorActionPreference='Stop'\n. '" + helper + "'\n"
        "$p=Start-FabContainedProcess -FilePath '" + python + "' -ArgumentList @('parent.py') "
        "-WorkingDirectory (Get-Location).Path -RedirectStandardOutput 'parent.out' -RedirectStandardError 'parent.err'\n"
        "try { $p.FabJob.Commit(); $p.Id } finally { $p.FabJob.Dispose(); $p.Dispose() }\n",
        encoding="utf-8",
    )
    result = run_fixture(tmp_path, shell, r"""
    $p=$null; $child=$null
    try {
        $shell=(Get-Process -Id $PID).Path
        $rootId=& $shell -NoProfile -NonInteractive -File 'launch.ps1'
        if ($LASTEXITCODE -ne 0) { throw 'nested-launch-failed' }
        $p=Get-Process -Id ([int]$rootId); [void]$p.Handle
        $watch=[Diagnostics.Stopwatch]::StartNew()
        while (-not (Test-Path 'child.pid') -and $watch.Elapsed.TotalSeconds -lt 10) { Start-Sleep -Milliseconds 50 }
        $child=Get-Process -Id ([int](Get-Content 'child.pid' -Raw)); [void]$child.Handle
        $survived=(-not $p.HasExited) -and (-not $child.HasExited)
        [IO.File]::WriteAllText((Join-Path (Get-Location) 'stop'), '')
        @{survived=$survived; rootExited=$p.WaitForExit(5000); childExited=$child.WaitForExit(5000)} | ConvertTo-Json -Compress
    } finally {
        foreach ($process in @($p,$child)) {
            if ($process) { try { if (-not $process.HasExited) { $process.Kill(); [void]$process.WaitForExit(5000) } } finally { $process.Dispose() } }
        }
    }
    """)
    assert result == {"survived": True, "rootExited": True, "childExited": True}


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
@pytest.mark.parametrize("shell", SHELLS or [None])
def test_failed_native_creation_releases_log_handles(tmp_path, shell):
    if not shell:
        pytest.skip("PowerShell is unavailable")
    (tmp_path / "invalid.exe").write_bytes(b"not an executable")
    result = run_fixture(tmp_path, shell, r"""
    $failure=$null
    try {
        Start-FabContainedProcess -FilePath (Join-Path (Get-Location) 'invalid.exe') -WorkingDirectory (Get-Location).Path -RedirectStandardOutput 'stdout.log' -RedirectStandardError 'stderr.log' | Out-Null
    } catch { $failure=$_.Exception.Message }
    foreach ($path in @('stdout.log','stderr.log')) {
        $file=[IO.File]::Open((Join-Path (Get-Location) $path), 'Open', 'ReadWrite', 'None')
        $file.Dispose()
    }
    @{failure=$failure; handlesReleased=$true} | ConvertTo-Json -Compress
    """)
    assert "create suspended service" in result["failure"]
    assert result["handlesReleased"] is True


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
@pytest.mark.parametrize("shell", SHELLS or [None])
def test_launcher_crash_immediately_after_creation_leaves_no_suspended_orphan(tmp_path, shell):
    if not shell:
        pytest.skip("PowerShell is unavailable")
    # Fault injection into a disposable helper copy freezes the exact boundary
    # after native creation, before any later assignment or resume can happen.
    fault = tmp_path / "fault"
    fault.mkdir()
    source = (ROOT / "scripts/Windows-Job.cs").read_text(encoding="utf-8")
    anchor = '"create suspended service");'
    assert source.count(anchor) == 1
    source = source.replace(anchor, anchor + '\n'
        'File.WriteAllText(Path.Combine(directory, "root.tmp"), pi.ProcessId.ToString());\n'
        'File.Move(Path.Combine(directory, "root.tmp"), Path.Combine(directory, "root.pid"));\n'
        'Thread.Sleep(30000);\n')
    (fault / "Windows-Job.cs").write_text(source, encoding="utf-8")
    shutil.copyfile(ROOT / "scripts/Windows-Job.ps1", fault / "Windows-Job.ps1")
    python = sys._base_executable.replace("'", "''")
    (tmp_path / "launch.ps1").write_text(
        "$ErrorActionPreference='Stop'\n. './fault/Windows-Job.ps1'\n"
        "$p=Start-FabContainedProcess -FilePath '" + python + "' -ArgumentList @('-c','pass') "
        "-WorkingDirectory (Get-Location).Path -RedirectStandardOutput 'parent.out' -RedirectStandardError 'parent.err'\n",
        encoding="utf-8",
    )
    result = run_fixture(tmp_path, shell, r"""
    $launcher=$null; $child=$null
    try {
        $shell=(Get-Process -Id $PID).Path
        $launcher=Start-Process -FilePath $shell -ArgumentList @('-NoProfile','-NonInteractive','-File','launch.ps1') -WorkingDirectory (Get-Location).Path -WindowStyle Hidden -RedirectStandardOutput 'launch.out' -RedirectStandardError 'launch.err' -PassThru
        $watch=[Diagnostics.Stopwatch]::StartNew()
        while (-not (Test-Path 'root.pid') -and $watch.Elapsed.TotalSeconds -lt 10) { Start-Sleep -Milliseconds 50 }
        $child=Get-Process -Id ([int](Get-Content 'root.pid' -Raw)); [void]$child.Handle
        $launcher.Kill()
        @{launcherExited=$launcher.WaitForExit(5000); childExited=$child.WaitForExit(3000)} | ConvertTo-Json -Compress
    } finally {
        foreach ($process in @($launcher,$child)) {
            if ($process) { try { if (-not $process.HasExited) { $process.Kill(); [void]$process.WaitForExit(5000) } } finally { $process.Dispose() } }
        }
    }
    """)
    assert result == {"launcherExited": True, "childExited": True}


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
@pytest.mark.parametrize("shell", SHELLS or [None])
def test_reopened_job_requires_original_process_membership(tmp_path, shell):
    result = run_fixture(tmp_path, shell, r"""
    $p=$null; $other=$null; $opened=$null; $failure=$null
    try {
        $p=Start-FabContainedProcess -FilePath $python -ArgumentList @('-c','import time;time.sleep(40)') -WorkingDirectory (Get-Location).Path -RedirectStandardOutput 'parent.out' -RedirectStandardError 'parent.err'
        $p.FabJob.Commit()
        $jobName=$p.FabJob.JobName
        $p.FabJob.Dispose()
        $other=Start-Process -FilePath $python -ArgumentList @('-c','"import time;time.sleep(40)"') -WindowStyle Hidden -PassThru
        try { $invalid=[Fab.Windows.JobLease]::Open($jobName,$other); $invalid.Dispose() } catch { $failure=$_.Exception.Message }
        $opened=[Fab.Windows.JobLease]::Open($jobName,$p)
        $opened.Terminate()
        @{rejected=($null -ne $failure); stopped=$p.WaitForExit(5000); otherAlive=(-not $other.HasExited)} | ConvertTo-Json -Compress
    } finally {
        if ($opened) { $opened.Dispose() }
        foreach ($process in @($p,$other)) {
            if ($process) { try { if (-not $process.HasExited) { $process.Kill(); [void]$process.WaitForExit(5000) } } finally { $process.Dispose() } }
        }
    }
    """)
    assert result == {"rejected": True, "stopped": True, "otherAlive": True}


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
@pytest.mark.parametrize("shell", SHELLS or [None])
def test_recorded_job_can_stop_descendants_after_root_exit(tmp_path, shell):
    (tmp_path / "root.py").write_text(
        "import pathlib,subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(40)'])\n"
        "pathlib.Path('child.tmp').write_text(str(child.pid))\n"
        "pathlib.Path('child.tmp').replace('child.pid')\ntime.sleep(40)\n",
        encoding="utf-8",
    )
    result = run_fixture(tmp_path, shell, r"""
    $p=$null; $child=$null; $opened=$null
    try {
        $p=Start-FabContainedProcess -FilePath $python -ArgumentList @('root.py') -WorkingDirectory (Get-Location).Path -RedirectStandardOutput 'parent.out' -RedirectStandardError 'parent.err'
        $name=$p.FabJob.JobName
        $watch=[Diagnostics.Stopwatch]::StartNew()
        while (-not (Test-Path 'child.pid') -and $watch.Elapsed.TotalSeconds -lt 10) { Start-Sleep -Milliseconds 50 }
        $child=Get-Process -Id ([int](Get-Content 'child.pid' -Raw)); [void]$child.Handle
        $p.Kill(); [void]$p.WaitForExit(5000)
        $aliveBefore=-not $child.HasExited
        $opened=[Fab.Windows.JobLease]::Open($name,$null)
        $opened.Terminate(); $opened.Dispose(); $opened=$null
        $p.FabJob.Dispose()
        $missing=[Fab.Windows.JobLease]::Open($name,$null)
        @{aliveBefore=$aliveBefore; stopped=$child.WaitForExit(5000); missing=($null -eq $missing)} | ConvertTo-Json -Compress
    } finally {
        if ($opened) { $opened.Dispose() }
        if ($p) { $p.FabJob.Dispose() }
        foreach ($process in @($p,$child)) {
            if ($process) { try { if (-not $process.HasExited) { $process.Kill(); [void]$process.WaitForExit(5000) } } finally { $process.Dispose() } }
        }
    }
    """)
    assert result == {"aliveBefore": True, "stopped": True, "missing": True}
