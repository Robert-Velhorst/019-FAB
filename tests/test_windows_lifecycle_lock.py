import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SHELLS = sorted({path for name in ("pwsh", "powershell") if (path := shutil.which(name))})


@pytest.mark.parametrize("shell", SHELLS or [None])
def test_lifecycle_exclusion_is_reentrant_but_blocks_other_processes(tmp_path, shell):
    if not shell:
        pytest.skip("PowerShell unavailable")
    helper = ROOT / "scripts/Windows-Process.ps1"
    peer = tmp_path / "peer.ps1"
    peer.write_text(
        "$ErrorActionPreference='Stop'\n. '" + str(helper).replace("'", "''") + "'\n"
        "try { $lock=Enter-FabLifecycleLock -Root (Get-Location).Path; "
        "try { 'acquired' } finally { Exit-FabLifecycleLock -Lock $lock } } catch { 'blocked' }\n",
        encoding="utf-8",
    )
    script = r"""
    $ErrorActionPreference='Stop'; Set-StrictMode -Version Latest
    . '__HELPER__'
    function Invoke-Peer {
        $p=Start-Process -FilePath '__SHELL__' -ArgumentList @('-NoProfile','-NonInteractive','-File','peer.ps1') -WorkingDirectory (Get-Location).Path -WindowStyle Hidden -RedirectStandardOutput 'peer.out' -RedirectStandardError 'peer.err' -PassThru
        try { if (-not $p.WaitForExit(10000)) { $p.Kill(); throw 'peer timed out' }; return (Get-Content 'peer.out' -Raw).Trim() } finally { $p.Dispose() }
    }
    $outer=Enter-FabLifecycleLock -Root (Get-Location).Path
    try {
        $inner=Enter-FabLifecycleLock -Root (Get-Location).Path
        try { $first=Invoke-Peer } finally { Exit-FabLifecycleLock -Lock $inner }
        $second=Invoke-Peer
    } finally { Exit-FabLifecycleLock -Lock $outer }
    $third=Invoke-Peer
    @{first=$first; second=$second; third=$third} | ConvertTo-Json -Compress
    """.replace("__HELPER__", str(helper).replace("'", "''")).replace("__SHELL__", shell.replace("'", "''"))
    fixture = tmp_path / "fixture.ps1"
    fixture.write_text(script, encoding="utf-8")
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File", str(fixture)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "first": "blocked", "second": "blocked", "third": "acquired"
    }


@pytest.mark.parametrize("shell", SHELLS or [None])
@pytest.mark.parametrize("filename", ["Start-FAB.ps1", "Stop-FAB.ps1"])
def test_complete_launcher_refuses_concurrent_lifecycle_work(tmp_path, shell, filename):
    if not shell:
        pytest.skip("PowerShell unavailable")
    installation = tmp_path / "installation"
    scripts = installation / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(ROOT / filename, installation / filename)
    for helper in ("Windows-Process.ps1", "Windows-Profile.ps1", "Windows-Job.ps1"):
        shutil.copyfile(ROOT / "scripts" / helper, scripts / helper)
    script = r"""
    $ErrorActionPreference='Stop'; Set-StrictMode -Version Latest
    . '__HELPER__'
    $lock=Enter-FabLifecycleLock -Root '__INSTALLATION__'
    $p=$null
    try {
        $arguments=@('-NoProfile','-NonInteractive','-File','"__LAUNCHER__"')
        if ('__FILENAME__' -eq 'Start-FAB.ps1') { $arguments+='-NoBrowser' }
        $p=Start-Process -FilePath '__SHELL__' -ArgumentList $arguments -WorkingDirectory '__INSTALLATION__' -WindowStyle Hidden -RedirectStandardOutput 'launcher.out' -RedirectStandardError 'launcher.err' -PassThru
        if (-not $p.WaitForExit(10000)) { $p.Kill(); throw 'launcher timed out' }
        @{blocked=($p.ExitCode -ne 0); error=(Get-Content 'launcher.err' -Raw).Trim()} | ConvertTo-Json -Compress
    } finally {
        if ($p) { $p.Dispose() }
        Exit-FabLifecycleLock -Lock $lock
    }
    """
    for key, value in {
        "HELPER": str(scripts / "Windows-Process.ps1"), "INSTALLATION": str(installation),
        "LAUNCHER": str(installation / filename), "FILENAME": filename, "SHELL": shell,
    }.items():
        script = script.replace(f"__{key}__", value.replace("'", "''"))
    fixture = tmp_path / "fixture.ps1"
    fixture.write_text(script, encoding="utf-8")
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File", str(fixture)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed["blocked"]
    assert "Another FAB start or stop operation is active" in observed["error"]
    assert not (installation / "data").exists()
    assert not (installation / ".venv").exists()
