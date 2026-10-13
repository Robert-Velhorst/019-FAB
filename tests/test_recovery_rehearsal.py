import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def rehearsal_module():
    assert importlib.util.find_spec("src.run_recovery_rehearsal"), "Synthetic rehearsal CLI is missing"
    return importlib.import_module("src.run_recovery_rehearsal")


def test_rehearsal_uses_real_backup_and_maintenance_restore_in_fresh_directory(monkeypatch):
    module = rehearsal_module()
    from src.config_loader import ConfigLoader
    from src.operations.local_backup import LocalBackupService

    monkeypatch.setattr(ConfigLoader, "__init__", lambda *_a, **_kw: pytest.fail("Must not load operator config"))
    seen = []
    original = LocalBackupService.restore_backup

    def observed_restore(service, *args, **kwargs):
        root = Path(service.project_root)
        assert root.name.startswith("fab-recovery-rehearsal-")
        assert Path(service.ledger_path).is_relative_to(root)
        assert Path(service.backup_dir).is_relative_to(root)
        assert service.config["fab_maintenance_mode"] is True
        assert kwargs["restore_mode"] == "ledger_and_source_evidence"
        seen.append(root)
        return original(service, *args, **kwargs)

    monkeypatch.setattr(LocalBackupService, "restore_backup", observed_restore)
    result = module.run_rehearsal()
    assert result["status"] == "passed"
    assert result["syntheticOnly"] is True
    assert result["externalSubmission"] == "not_executed"
    assert result["sourceEvidence"]["coverageStatus"] == "complete"
    assert result["sourceEvidence"]["includedDocuments"] == 1
    assert all(result["checks"].values())
    assert result["checks"]["sourceBytesRestored"] is True
    assert result["checks"]["sqliteIntegrity"] is True
    assert result["checks"]["ledgerRowsRestored"] is True
    assert len(result["sourceSha256"]) == 64
    assert seen and all(not root.exists() for root in seen)
    assert len(json.dumps(result)) < 4096
    assert str(seen[0]) not in json.dumps(result)


def test_cli_ignores_live_environment_and_rejects_destination_arguments(tmp_path):
    rehearsal_module()
    sentinel = tmp_path / "operator-ledger.sqlite3"
    sentinel.write_bytes(b"do not open or change this operator ledger")
    environment = dict(os.environ, FAB_LOCAL_LEDGER_PATH=str(sentinel),
                       FAB_LOCAL_BACKUP_DIR=str(tmp_path / "operator-backups"),
                       FAB_INSTANCE_ROOT=str(tmp_path), FAB_DEPLOYMENT_PROFILE="windows",
                       FAB_LOCAL_API_TOKEN_FILE=str(tmp_path / "missing-secret"))
    result = subprocess.run([sys.executable, "-m", "src.run_recovery_rehearsal"], cwd=ROOT,
                            env=environment, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "passed"
    rejected = subprocess.run([sys.executable, "-m", "src.run_recovery_rehearsal", "--ledger", str(sentinel)],
                              cwd=ROOT, env=environment, capture_output=True, text=True, timeout=30)
    assert rejected.returncode != 0
    assert sentinel.read_bytes() == b"do not open or change this operator ledger"
    assert not (tmp_path / "operator-backups").exists()
    assert not (tmp_path / "data").exists()


def test_rehearsal_fails_closed_and_bounds_errors(monkeypatch, capsys):
    module = rehearsal_module()
    from src.operations.local_backup import LocalBackupService

    def fail(*_args, **_kwargs):
        raise ValueError("private-path-or-secret-" * 1000)

    monkeypatch.setattr(LocalBackupService, "create_backup", fail)
    assert module.main([]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "failed"
    assert "private-path-or-secret" not in output
    assert len(output) < 4096
