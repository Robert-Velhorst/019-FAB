import json

import pytest

from src.operations.local_api import create_app
from src.operations.local_hai_connector import LocalHaiConnector
from src.operations.local_ledger import LocalOperationsLedger


def state(ledger):
    with ledger._connection() as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return {row[0]: list(map(tuple, connection.execute(
            'SELECT * FROM "' + row[0].replace('"', '""') + '" ORDER BY rowid'
        ))) for row in tables}


@pytest.fixture
def connector(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    calls = []
    commands = ["run_reconciliation", "record_wave_attachment_verification", "refresh_notifications"]
    service = LocalHaiConnector(ledger, {"fab_hai_connector_enabled": True,
        "fab_hai_allowed_commands": ",".join(commands)}, executors={
            command: lambda payload, actor: calls.append(payload) or {} for command in commands})
    return service, ledger, calls


@pytest.mark.parametrize("command,field", [("run_reconciliation", "limit"),
    ("record_wave_attachment_verification", "documentId")])
@pytest.mark.parametrize("value", [True, 1.9, 1.0, "1", float("inf"), float("nan"), 2**63])
def test_integer_contract_rejects_coercion_before_execution(connector, command, field, value):
    service, ledger, calls = connector
    before = state(ledger)
    payload = {field: value}
    if field == "documentId":
        payload["evidence"] = {}
    assert service.plan(command, payload)["status"] == "invalid"
    assert service.execute("synthetic-invalid", command, payload)["status"] == "invalid"
    assert calls == []
    assert state(ledger) == before


@pytest.mark.parametrize("request_id", [True, 123, 1.0, None, [], {}])
def test_request_identity_must_be_a_string(connector, request_id):
    service, ledger, calls = connector
    before = state(ledger)
    assert service.execute(request_id, "refresh_notifications")["status"] == "invalid_request"
    assert calls == []
    assert state(ledger) == before


@pytest.mark.parametrize("endpoint", ["plan", "execute"])
@pytest.mark.parametrize("value", [True, 1.9, "1", float("inf")])
def test_authenticated_http_rejects_invalid_limit_without_changes(tmp_path, endpoint, value):
    path = str(tmp_path / "ledger.sqlite3")
    app = create_app({"fab_local_ledger_path": path, "fab_local_api_token": "synthetic-operator",
        "fab_hai_api_token": "synthetic-hai", "fab_hai_connector_enabled": True,
        "fab_hai_allowed_commands": "run_reconciliation"})
    ledger = LocalOperationsLedger(path)
    before = state(ledger)
    response = app.test_client().post("/api/hai/commands/" + endpoint,
        headers={"Authorization": "Bearer synthetic-hai"}, content_type="application/json",
        data=json.dumps({"commandId": "run_reconciliation", "requestId": "synthetic-limit",
                         "payload": {"limit": value}}))
    assert response.status_code == 400
    assert response.get_json()["status"] == "invalid"
    assert state(ledger) == before


def test_http_does_not_convert_boolean_request_identity(tmp_path):
    path = str(tmp_path / "ledger.sqlite3")
    app = create_app({"fab_local_ledger_path": path, "fab_local_api_token": "synthetic-operator",
        "fab_hai_api_token": "synthetic-hai", "fab_hai_connector_enabled": True,
        "fab_hai_allowed_commands": "refresh_notifications"})
    ledger = LocalOperationsLedger(path)
    before = state(ledger)
    response = app.test_client().post("/api/hai/commands/execute",
        headers={"Authorization": "Bearer synthetic-hai"},
        json={"commandId": "refresh_notifications", "requestId": True})
    assert response.status_code == 400
    assert response.get_json()["status"] == "invalid_request"
    assert state(ledger) == before


@pytest.mark.parametrize("payload", [{}, {"documentId": 1}, {"evidence": {}}])
def test_attestation_requires_both_manifest_fields_before_execution(connector, payload):
    service, ledger, calls = connector
    before = state(ledger)
    assert service.plan("record_wave_attachment_verification", payload)["status"] == "invalid"
    assert service.execute("synthetic-missing", "record_wave_attachment_verification", payload)["status"] == "invalid"
    assert calls == []
    assert state(ledger) == before


@pytest.mark.parametrize("limit", [1, 500])
def test_valid_integer_limits_and_string_replay_keys_remain_supported(connector, limit):
    service, _, calls = connector
    payload = {"limit": limit}
    assert service.plan("run_reconciliation", payload)["status"] == "ready"
    assert service.execute("synthetic-valid", "run_reconciliation", payload)["status"] == "completed"
    assert service.execute("synthetic-valid", "run_reconciliation", payload)["status"] == "already_executed"
    assert calls == [payload]


@pytest.mark.parametrize("document_id", [1, 2**63 - 1])
def test_manifest_and_normalizer_share_document_id_bounds(connector, document_id):
    service, _, _ = connector
    payload = {"documentId": document_id, "evidence": {}}
    plan = service.plan("record_wave_attachment_verification", payload)
    assert plan["status"] == "ready"
    assert plan["command"]["inputSchema"]["properties"]["documentId"]["maximum"] == 2**63 - 1
    assert plan["payload"] == payload
