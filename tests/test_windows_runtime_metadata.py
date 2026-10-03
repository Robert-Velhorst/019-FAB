import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SHELLS = sorted({shell for name in ("pwsh", "powershell") if (shell := shutil.which(name))})


@pytest.mark.parametrize("shell", SHELLS or [None])
@pytest.mark.parametrize("existing,locked", [(False, False), (True, False), (True, True)])
def test_atomic_metadata_publication_and_failure_preservation(tmp_path, shell, existing, locked):
    if not shell:
        pytest.skip("PowerShell is unavailable")
    path = tmp_path / "runtime.json"
    if existing:
        path.write_text('{"original":true}', encoding="utf-8")
    helper = str(ROOT / "scripts/Windows-Profile.ps1").replace("'", "''")
    command = r"""
    $ErrorActionPreference = 'Stop'
    . '__HELPER__'
    $path = Join-Path (Get-Location).Path 'runtime.json'
    $lock = $null
    if (__LOCKED__) { $lock = [System.IO.File]::Open($path, 'Open', 'ReadWrite', 'None') }
    $failed = $false
    try { Write-FabRuntimeMetadata -Path $path -Runtime ([ordered]@{service='synthetic'; pid=123}) }
    catch { $failed = $true; $reason = $_.Exception.Message }
    finally { if ($lock) { $lock.Dispose() } }
    if ($failed) { @{failed=$failed; reason=$reason} | ConvertTo-Json -Compress }
    else { @{failed=$false} | ConvertTo-Json -Compress }
    """.replace("__HELPER__", helper).replace("__LOCKED__", "$true" if locked else "$false")
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                            cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["failed"] is locked, observed
    assert json.loads(path.read_text(encoding="utf-8-sig")) == (
        {"original": True} if locked else {"service": "synthetic", "pid": 123}
    )
    assert not list(tmp_path.glob("*.tmp"))
