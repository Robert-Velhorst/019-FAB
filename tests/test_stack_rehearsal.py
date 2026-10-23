import os
import site
import subprocess
import sys

import pytest

from scripts.run_stack_rehearsal import ROOT, fixture_api, fixture_python_environment, isolated_environment, stop_owned_child


def test_fixture_python_uses_only_owned_bootstrap_paths(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "unrelated-project")
    monkeypatch.setenv("FAB_LOCAL_API_TOKEN", "private-provider-token")
    env = fixture_python_environment()
    assert env['PYTHONPATH'].split(os.pathsep) == [str(ROOT / 'runtime_bootstrap'), str(ROOT), *site.getsitepackages()]
    assert 'FAB_LOCAL_API_TOKEN' not in env


def test_qualified_interpreter_bootstraps_from_an_isolated_working_directory(tmp_path):
    result = subprocess.run([sys._base_executable, '-c',
        'import json; from fab_sqlite_runtime import runtime_evidence; print(json.dumps(runtime_evidence()))'],
        cwd=tmp_path, env=fixture_python_environment(), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert '"version": "3.54.1"' in result.stdout


def test_rehearsal_does_not_inherit_provider_or_application_credentials(monkeypatch):
    monkeypatch.setenv("FAB_LOCAL_API_TOKEN", "private-provider-token")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "private-credentials-path")
    monkeypatch.setenv("JWT_SECRET", "private-signing-secret")
    monkeypatch.setenv("HTTPS_PROXY", "private-proxy")
    monkeypatch.setenv("PYTHONPATH", "unrelated-project")
    env = isolated_environment()
    assert not {"FAB_LOCAL_API_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS", "JWT_SECRET",
                "HTTPS_PROXY", "PYTHONPATH"} & env.keys()
    assert env.get("PATH") == os.environ.get("PATH")


def test_fixture_api_refuses_an_existing_ledger_without_changing_files(tmp_path):
    root = tmp_path / "fab-stack-rehearsal-guard"
    root.mkdir()
    (root / ".rehearsal").write_text("synthetic-only-v1")
    ledger = root / "ledger.sqlite3"
    source = root / "synthetic-receipt.txt"
    ledger.write_bytes(b"preserved existing ledger fixture")
    source.write_bytes(b"preserved existing source fixture")
    with pytest.raises(RuntimeError, match="fresh owned rehearsal directory"):
        fixture_api(str(root), 12_345)
    assert ledger.read_bytes() == b"preserved existing ledger fixture"
    assert source.read_bytes() == b"preserved existing source fixture"


def test_owned_rehearsal_child_is_stopped_and_reaped(tmp_path):
    child = subprocess.Popen([sys._base_executable, "-c", "import time; time.sleep(30)"],
                             cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        stop_owned_child(child)
        assert child.poll() is not None
        stop_owned_child(child)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
