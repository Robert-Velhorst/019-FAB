import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.operations.local_ledger import LocalOperationsLedger
from src.worker.runtime import WorkerAlreadyRunningError


ROOT = Path(__file__).resolve().parents[1]
BASE_PYTHON = getattr(sys, "_base_executable", sys.executable)


def seed_leases(ledger, count=506):
    names = [f"hai_command:request-{index:04d}" for index in range(count)]
    names += ["local_connector_intake", "local_autonomous_cycle"]
    names += ["haiXcommand:foreign", "other-worker"]
    timestamp = "2026-01-01T00:00:00+00:00"
    with sqlite3.connect(ledger.path) as connection:
        connection.executemany(
            "INSERT INTO runtime_leases (lease_name,owner_token,acquired_at,heartbeat_at,"
            "expires_at,metadata_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            [(name, "synthetic-owner-secret", timestamp, timestamp,
              "2050-01-01T00:00:00+00:00", "{}", timestamp, timestamp) for name in names],
        )
    return names


def reject_release_audit(ledger, name):
    with sqlite3.connect(ledger.path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_synthetic_audit BEFORE INSERT ON audit_events "
            "WHEN NEW.action='runtime_lease.force_released' AND NEW.entity_id='" + name + "' "
            "BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END"
        )


def test_force_release_rolls_back_when_audit_cannot_be_saved(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    seed_leases(ledger, count=1)
    reject_release_audit(ledger, "local_connector_intake")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic audit failure"):
        ledger.force_release_runtime_lease(
            "local_connector_intake", actor="Stop-FAB.ps1", reason="owned_services_stopped"
        )
    assert ledger.get_runtime_lease("local_connector_intake") is not None
    assert ledger.list_audit_events() == []


def test_shutdown_releases_every_owned_lease_in_one_connection(tmp_path, monkeypatch):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    names = seed_leases(ledger)
    connections = []
    connect = ledger._connect

    def counted_connect():
        connection = connect()
        connections.append(connection)
        return connection

    monkeypatch.setattr(ledger, "_connect", counted_connect)
    released = ledger.force_release_stopped_runtime_leases(actor="Stop-FAB.ps1")
    assert len(connections) == 1
    assert set(released) == set(names[:-2])
    with sqlite3.connect(ledger.path) as connection:
        remaining = {row[0] for row in connection.execute("SELECT lease_name FROM runtime_leases")}
        events = connection.execute(
            "SELECT entity_id,details_json FROM audit_events WHERE action='runtime_lease.force_released'"
        ).fetchall()
    assert remaining == set(names[-2:])
    assert len(events) == 508
    assert {row[0] for row in events} == set(released)
    assert all("synthetic-owner-secret" not in row[1] for row in events)
    for name, details_json in events:
        details = json.loads(details_json)
        assert details["actor"] == "Stop-FAB.ps1"
        assert details["reason"] == (
            "owned_hai_api_stopped" if name.startswith("hai_command:") else "owned_services_stopped"
        )
        assert details["externalSubmission"] == "not_executed"
    assert ledger.force_release_stopped_runtime_leases(actor="Stop-FAB.ps1") == []


def test_shutdown_batch_is_all_or_nothing_on_audit_failure(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    names = seed_leases(ledger, count=105)
    reject_release_audit(ledger, "hai_command:request-0103")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic audit failure"):
        ledger.force_release_stopped_runtime_leases(actor="Stop-FAB.ps1")
    with sqlite3.connect(ledger.path) as connection:
        remaining = {row[0] for row in connection.execute("SELECT lease_name FROM runtime_leases")}
        audits = connection.execute("SELECT count(*) FROM audit_events").fetchone()[0]
    assert remaining == set(names)
    assert audits == 0


def test_actual_shutdown_program_cleans_more_than_500_hai_leases(tmp_path, monkeypatch):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    names = seed_leases(ledger)
    monkeypatch.setattr(
        "src.config_loader.ConfigLoader",
        lambda **kwargs: SimpleNamespace(get_all_config=lambda: {"fab_local_ledger_path": ledger.path}),
    )
    monkeypatch.chdir(tmp_path)
    source = (Path(__file__).resolve().parents[1] / "Stop-FAB.ps1").read_text(encoding="utf-8")
    program = source.split('$cleanupScript = @"\n', 1)[1].split('\n"@', 1)[0]
    namespace = {}
    exec(compile(program, "Stop-FAB.ps1 lease cleanup", "exec"), namespace)
    assert set(namespace["released"]) == set(names[:-2])
    assert {row["leaseName"] for row in ledger.list_runtime_leases()} == set(names[-2:])


def test_invalid_lease_owner_blocks_the_entire_shutdown_batch(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    names = seed_leases(ledger, count=3)
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("UPDATE runtime_leases SET owner_token='' WHERE lease_name='local_connector_intake'")
    with pytest.raises(RuntimeError, match="could not be released"):
        ledger.force_release_stopped_runtime_leases(actor="Stop-FAB.ps1")
    with sqlite3.connect(ledger.path) as connection:
        remaining = {row[0] for row in connection.execute("SELECT lease_name FROM runtime_leases")}
        audits = connection.execute("SELECT count(*) FROM audit_events").fetchone()[0]
    assert remaining == set(names)
    assert audits == 0


def test_shutdown_cleanup_rejects_active_worker_with_missing_registration(tmp_path, monkeypatch):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    seed_leases(ledger, count=1)
    monkeypatch.setattr(
        "src.config_loader.ConfigLoader",
        lambda **kwargs: SimpleNamespace(get_all_config=lambda: {"fab_local_ledger_path": ledger.path}),
    )
    monkeypatch.chdir(tmp_path)
    worker_code = (
        "import sys,time\nfrom pathlib import Path\n"
        f"sys.path.insert(0,{str(ROOT)!r})\n"
        "from src.worker.runtime import managed_worker_runtime\n"
        "root=Path(sys.argv[1])\nwith managed_worker_runtime(root):\n"
        " (root/'data/fab-worker-runtime.json').unlink()\n"
        " (root/'ready').touch()\n time.sleep(40)\n"
    )
    worker = subprocess.Popen([BASE_PYTHON, "-c", worker_code, str(tmp_path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "ready").exists() and time.monotonic() < deadline:
            assert worker.poll() is None
            time.sleep(0.02)
        assert (tmp_path / "ready").exists()
        source = (ROOT / "Stop-FAB.ps1").read_text(encoding="utf-8")
        program = source.split('$cleanupScript = @"\n', 1)[1].split('\n"@', 1)[0]
        with pytest.raises(WorkerAlreadyRunningError):
            exec(compile(program, "Stop-FAB.ps1 lease cleanup", "exec"), {})
        assert worker.poll() is None
        assert ledger.get_runtime_lease("local_connector_intake") is not None
        assert ledger.list_audit_events() == []
    finally:
        worker.terminate()
        worker.communicate(timeout=10)


def test_worker_cannot_start_during_cleanup_or_registration_removal(tmp_path, monkeypatch):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    seed_leases(ledger, count=1)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "src.config_loader.ConfigLoader",
        lambda **kwargs: SimpleNamespace(get_all_config=lambda: {"fab_local_ledger_path": ledger.path}),
    )
    data = tmp_path / "data"
    data.mkdir()
    (data / "fab-runtime.json").write_text('{"synthetic":true}', encoding="utf-8")
    (data / "fab-worker-runtime.json").write_text('{"synthetic":true}', encoding="utf-8")
    worker_code = (
        "import sys,json\nfrom pathlib import Path\n"
        f"sys.path.insert(0,{str(ROOT)!r})\n"
        "from src.worker.runtime import managed_worker_runtime,WorkerAlreadyRunningError\n"
        "try:\n with managed_worker_runtime(Path(sys.argv[1])):\n  print(json.dumps({'blocked':False}))\n"
        "except WorkerAlreadyRunningError:\n print(json.dumps({'blocked':True}))\n"
    )
    probes = []

    def probe_worker():
        result = subprocess.run([BASE_PYTHON, "-c", worker_code, str(tmp_path)],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"blocked": True}
        probes.append("blocked")

    release = LocalOperationsLedger.force_release_stopped_runtime_leases

    def guarded_release(self, **kwargs):
        probe_worker()
        return release(self, **kwargs)

    unlink = Path.unlink

    def guarded_unlink(self, *args, **kwargs):
        if self == data / "fab-worker-runtime.json":
            probe_worker()
        return unlink(self, *args, **kwargs)

    monkeypatch.setattr(LocalOperationsLedger, "force_release_stopped_runtime_leases", guarded_release)
    monkeypatch.setattr(Path, "unlink", guarded_unlink)
    source = (ROOT / "Stop-FAB.ps1").read_text(encoding="utf-8")
    program = source.split('$cleanupScript = @"\n', 1)[1].split('\n"@', 1)[0]
    exec(compile(program, "Stop-FAB.ps1 lease cleanup", "exec"), {})
    assert probes == ["blocked", "blocked"]
    assert not (data / "fab-runtime.json").exists()
    assert not (data / "fab-worker-runtime.json").exists()


def test_actual_shutdown_preserves_records_when_batch_audit_fails(tmp_path, monkeypatch):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    names = seed_leases(ledger, count=105)
    reject_release_audit(ledger, "hai_command:request-0103")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "src.config_loader.ConfigLoader",
        lambda **kwargs: SimpleNamespace(get_all_config=lambda: {"fab_local_ledger_path": ledger.path}),
    )
    data = tmp_path / "data"
    data.mkdir()
    records = [data / "fab-runtime.json", data / "fab-worker-runtime.json"]
    for record in records:
        record.write_bytes(b'{"synthetic":"preserve exactly"}')
    source = (ROOT / "Stop-FAB.ps1").read_text(encoding="utf-8")
    program = source.split('$cleanupScript = @"\n', 1)[1].split('\n"@', 1)[0]
    with pytest.raises(sqlite3.IntegrityError, match="synthetic audit failure"):
        exec(compile(program, "Stop-FAB.ps1 lease cleanup", "exec"), {})
    assert all(record.read_bytes() == b'{"synthetic":"preserve exactly"}' for record in records)
    with sqlite3.connect(ledger.path) as connection:
        remaining = {row[0] for row in connection.execute("SELECT lease_name FROM runtime_leases")}
        audits = connection.execute("SELECT count(*) FROM audit_events").fetchone()[0]
    assert remaining == set(names)
    assert audits == 0
