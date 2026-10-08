import io
import json

import pytest
from werkzeug.test import EnvironBuilder

from src.operations.local_api import create_app
from src.operations.local_hai_connector import LocalHaiConnector
from src.operations.local_ledger import LocalOperationsLedger


def evidence():
    return {"documentId": 1, "evidence": {"observedFields": {"amount": 4.28}}}


def connector(tmp_path, executor):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    service = LocalHaiConnector(ledger, {"fab_hai_connector_enabled": True,
        "fab_hai_allowed_commands": "record_wave_attachment_verification"},
        executors={"record_wave_attachment_verification": executor})
    return service, ledger


@pytest.mark.parametrize("fail", [False, True])
def test_executor_mutation_cannot_rewrite_audited_contract_or_replay_identity(tmp_path, fail):
    calls = []
    payload = evidence()

    def execute(captured, actor):
        calls.append(1)
        captured["evidence"]["observedFields"]["amount"] = 99
        if fail:
            raise RuntimeError("Synthetic failure")
        return {}

    service, ledger = connector(tmp_path, execute)
    result = service.execute("synthetic-snapshot", "record_wave_attachment_verification", payload)
    assert result["status"] == ("failed" if fail else "completed")
    assert payload == evidence()
    event = ledger.find_audit_event(action="hai.command.failed" if fail else "hai.command.completed",
        entity_type="hai_command_request", entity_id="synthetic-snapshot")
    assert event["details"]["payload"] == evidence()
    replay = service.execute("synthetic-snapshot", "record_wave_attachment_verification", evidence())
    assert replay["status"] == ("previously_failed" if fail else "already_executed")
    assert calls == [1]


def test_caller_nested_mutation_does_not_change_executor_input(tmp_path):
    payload = evidence()
    observed = []

    def execute(captured, actor):
        payload["evidence"]["observedFields"]["amount"] = 99
        observed.append(captured["evidence"]["observedFields"]["amount"])
        return {}

    service, _ = connector(tmp_path, execute)
    assert service.execute("synthetic-caller", "record_wave_attachment_verification", payload)["success"]
    assert observed == [4.28]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), b"binary", (1, 2),
    "x" * (1024 * 1024 + 1), [0] * 10001], ids=["nan", "infinity", "bytes", "tuple", "oversize", "wide"])
def test_invalid_or_unbounded_nested_metadata_is_rejected_before_execution(tmp_path, bad):
    calls = []
    service, ledger = connector(tmp_path, lambda payload, actor: calls.append(1) or {})
    payload = evidence()
    payload["evidence"]["observedFields"]["extra"] = bad
    assert service.plan("record_wave_attachment_verification", payload)["status"] == "invalid"
    assert service.execute("synthetic-invalid", "record_wave_attachment_verification", payload)["status"] == "invalid"
    assert calls == []
    assert ledger.find_audit_event(action="hai.command.requested", entity_type="hai_command_request",
        entity_id="synthetic-invalid") is None


@pytest.mark.parametrize("cycle", [False, True])
def test_deep_or_cyclic_metadata_is_rejected(tmp_path, cycle):
    service, _ = connector(tmp_path, lambda payload, actor: {})
    nested = {}
    if cycle:
        nested["next"] = nested
    else:
        for _ in range(40):
            nested = {"next": nested}
    payload = evidence()
    payload["evidence"]["observedFields"]["extra"] = nested
    assert service.plan("record_wave_attachment_verification", payload)["status"] == "invalid"


def test_exact_canonical_size_and_unicode_metadata_remain_supported(tmp_path):
    service, _ = connector(tmp_path, lambda payload, actor: {})
    payload = evidence()
    payload["evidence"]["observedFields"]["notes"] = ""
    overhead = len(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    remaining = 1024 * 1024 - overhead
    payload["evidence"]["observedFields"]["notes"] = "\u00e9" * (remaining // 2) + "x" * (remaining % 2)
    plan = service.plan("record_wave_attachment_verification", payload)
    assert plan["status"] == "ready"
    assert plan["payload"] == payload


@pytest.mark.parametrize("endpoint", ["plan", "execute"])
def test_hai_request_envelope_is_rejected_before_body_read(tmp_path, endpoint):
    path = str(tmp_path / "ledger.sqlite3")
    app = create_app({"fab_local_ledger_path": path, "fab_local_api_token": "synthetic-operator",
        "fab_hai_api_token": "synthetic-hai"})

    class NoRead(io.BytesIO):
        def read(self, *args):
            raise AssertionError("Oversized HAI body must not be read")

        def readinto(self, *args):
            raise AssertionError("Oversized HAI body must not be read")

    stream = NoRead(b'{"padding":"' + b'x' * (2 * 1024 * 1024) + b'"}')
    environ = EnvironBuilder(path="/api/hai/commands/" + endpoint, method="POST", input_stream=stream,
        content_type="application/json", headers={"Authorization": "Bearer synthetic-hai"}).get_environ()
    assert app.test_client().open(environ).status_code == 413


@pytest.mark.parametrize("endpoint", ["plan", "execute"])
def test_nonfinite_evidence_http_rejected_without_command_audit(tmp_path, endpoint):
    path = str(tmp_path / "ledger.sqlite3")
    app = create_app({"fab_local_ledger_path": path, "fab_local_api_token": "synthetic-operator",
        "fab_hai_api_token": "synthetic-hai", "fab_hai_connector_enabled": True,
        "fab_hai_allowed_commands": "record_wave_attachment_verification"})
    payload = evidence()
    payload["evidence"]["observedFields"]["amount"] = float("nan")
    response = app.test_client().post("/api/hai/commands/" + endpoint,
        headers={"Authorization": "Bearer synthetic-hai"}, content_type="application/json",
        data=json.dumps({"requestId": "synthetic-nonfinite", "commandId": "record_wave_attachment_verification", "payload": payload}))
    assert response.status_code == 400
    assert response.get_json()["status"] == "invalid"
    ledger = LocalOperationsLedger(path)
    assert ledger.find_audit_event(action="hai.command.requested", entity_type="hai_command_request",
        entity_id="synthetic-nonfinite") is None
