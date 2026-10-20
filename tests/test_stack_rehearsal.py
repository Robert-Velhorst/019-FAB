import os
import subprocess
import sys

import pytest

from scripts.run_stack_rehearsal import fixture_api, isolated_environment, python_script_command, stop_owned_child


def test_fixture_child_imports_active_environment_dependencies(tmp_path):
    script = tmp_path / "dependency_probe.py"
    script.write_text("import flask, sys; print('dependency-loaded:' + sys.argv[1])")
    command = python_script_command(script, "fixture")
    assert command[0] == sys._base_executable
    result = subprocess.run(command, cwd=tmp_path, env=isolated_environment(),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "dependency-loaded:fixture"


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
