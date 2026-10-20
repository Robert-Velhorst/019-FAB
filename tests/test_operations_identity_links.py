import io
import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.operations.local_ledger import LocalOperationsLedger
from src.operations.operations_client import OperationsClient


def config_for(tmp_path):
    return {"fab_operations_api_url": "http://localhost:3000", "fab_local_ledger_enabled": True,
            "fab_local_ledger_path": str(tmp_path / "ledger.sqlite3")}


def reply(value):
    response = requests.Response()
    response.status_code = 200
    response.raw = io.BytesIO(json.dumps(value).encode())
    return response


def test_colliding_ids_remain_local_and_translate_after_restart(tmp_path):
    config = config_for(tmp_path)
    client = OperationsClient(config)
    existing = client.local_ledger.register_document({"source": "gmail", "sourceDocumentId": "old", "originalFilename": "old.pdf"})
    client.local_ledger.bind_remote_identity(client.destination_key, "documents", existing, 90)
    client.session.request = MagicMock(return_value=reply({"id": 1}))
    created = client.register_document("gmail", {"id": "new", "filename": "new.pdf"})
    assert created != existing
    assert client.local_ledger.get_document(created)["source_document_id"] == "new"
    assert client.local_ledger.get_document(existing)["source_document_id"] == "old"
    assert client.local_ledger.get_remote_identity(client.destination_key, "documents", created) == 1
    client.session.close()

    restarted = OperationsClient(config)
    def remote_update(_method, _url, **_kwargs):
        assert restarted.local_ledger.get_document(created)["processing_status"] == "validated"
        return reply({"success": True})
    restarted.session.request = MagicMock(side_effect=remote_update)
    result = restarted.update_document(created, processing_status="validated", duplicate_of_document_id=existing)
    assert result["status"] == "success" and result["localLedger"]
    args, kwargs = restarted.session.request.call_args
    assert args[:2] == ("PATCH", "http://localhost:3000/api/fab/operations/documents/1")
    assert kwargs["json"]["duplicateOfDocumentId"] == 90
    assert restarted.local_ledger.get_document(created)["duplicate_of_document_id"] == existing
    restarted.session.close()


def test_workflow_and_related_foreign_keys_use_separate_remote_ids(tmp_path):
    client = OperationsClient(config_for(tmp_path))
    client.local_ledger.create_workflow_run({"triggerSource": "preexisting"})
    responses = iter([7, 42, 80, 81, 82, 83])
    client.session.request = MagicMock(side_effect=lambda *_args, **_kwargs: reply({"id": next(responses)}))
    run = client.create_workflow_run("manual")
    document = client.register_document("gmail", {"id": "mail-1", "filename": "bill.pdf"})
    review = client.create_review_item(document, "check_amount")
    route = client.create_routing_attempt(document, "waveapps_business", "pending", workflow_run_id=run)
    match = client.create_reconciliation_match("external-bank-id", "review", document_id=document)
    audit = client.record_audit_event("document.checked", "bookkeeping_document", str(document), {"reviewId": review})
    assert all(value is not None for value in (run, document, review, route, match, audit))
    calls = client.session.request.call_args_list
    assert calls[2].kwargs["json"]["documentId"] == 42
    assert calls[3].kwargs["json"]["workflowRunId"] == 7
    assert calls[3].kwargs["json"]["documentId"] == 42
    assert calls[4].kwargs["json"]["documentId"] == 42
    assert calls[5].kwargs["json"]["entityId"] == "42"
    assert calls[5].kwargs["json"]["details"]["localEvidence"] == {"reviewId": review}
    client.session.request = MagicMock(return_value=reply({"success": True}))
    client.update_workflow_run(run, status="completed")
    assert client.session.request.call_args.args[1].endswith("/workflow-runs/7")
    client.session.close()


@pytest.mark.parametrize("change", [
    {"fab_operations_api_url": "http://localhost:4000"},
    {"fab_operations_identity_scope": "new-database-generation"},
    {"fab_operations_endpoints": {"documents": "/api/v2/documents"}},
])
def test_missing_or_changed_destination_links_never_guess_numeric_ids(tmp_path, change):
    config = config_for(tmp_path)
    client = OperationsClient(config)
    document = client.local_ledger.register_document({"source": "gmail", "sourceDocumentId": "offline", "originalFilename": "bill.pdf"})
    client.session.request = MagicMock()
    result = client.update_document(document, processing_status="validated")
    assert result["status"] == "deferred" and result["localLedger"]
    assert client.create_review_item(document, "check") is not None
    client.session.request.assert_not_called()
    client.local_ledger.bind_remote_identity(client.destination_key, "documents", document, 22)
    changed = OperationsClient({**config, **change})
    changed.session.request = MagicMock()
    assert changed.update_document(document, processing_status="reviewed")["status"] == "deferred"
    changed.session.request.assert_not_called()
    client.session.close()
    changed.session.close()


def test_conflicting_links_are_quarantined_not_reassigned(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    destination = "a" * 64
    ledger.bind_remote_identity(destination, "documents", 1, 20)
    ledger.bind_remote_identity(destination, "documents", 1, 20)
    with pytest.raises(ValueError, match="conflicts"):
        ledger.bind_remote_identity(destination, "documents", 2, 20)
    assert ledger.get_remote_identity(destination, "documents", 1) is None
    with pytest.raises(ValueError, match="conflicts"):
        ledger.bind_remote_identity(destination, "documents", 1, 20)


def test_conflicting_remote_registration_cannot_resubmit_or_update(tmp_path):
    client = OperationsClient(config_for(tmp_path))
    client.session.request = MagicMock(side_effect=[reply({"id": 10}), reply({"id": 11})])
    document = {"id": "mail", "filename": "bill.pdf", "content_sha256": "a" * 64}
    local_id = client.register_document("gmail", document)
    assert client.register_document("gmail", {"id": "mail", "filename": "bill.pdf"}) == local_id
    assert client.local_ledger.get_document(local_id)["content_sha256"] == "a" * 64
    assert client.local_ledger.get_remote_identity(client.destination_key, "documents", local_id) is None
    assert client.register_document("gmail", document) == local_id
    assert client.update_document(local_id, processing_status="validated")["status"] == "deferred"
    assert client.session.request.call_count == 2
    assert any(event["action"] == "operations.remote.failed" for event in client.local_ledger.list_audit_events())
    client.session.close()


def test_unavailable_required_ledger_never_falls_back_to_remote_ids(tmp_path):
    with patch.object(OperationsClient, "_build_local_ledger", return_value=None):
        client = OperationsClient(config_for(tmp_path))
    client.session.request = MagicMock()
    assert client.register_document("gmail", {"id": "mail", "filename": "bill.pdf"}) is None
    assert client.update_document(1, processing_status="validated")["status"] == "failed"
    client.session.request.assert_not_called()
    client.session.close()


def test_null_enabled_setting_keeps_path_configured_ledger_authoritative(tmp_path):
    with patch.object(OperationsClient, "_build_local_ledger", return_value=None):
        client = OperationsClient({**config_for(tmp_path), "fab_local_ledger_enabled": None})
    client.session.request = MagicMock()
    assert client.local_required
    assert client.update_document(1, processing_status="validated")["status"] == "failed"
    client.session.request.assert_not_called()
    client.session.close()


def test_disabled_mirror_does_not_read_identity_links(tmp_path):
    client = OperationsClient({**config_for(tmp_path), "fab_operations_enabled": False})
    client.local_ledger.get_remote_identity_link = MagicMock(side_effect=AssertionError("Unneeded remote lookup"))
    client.session.request = MagicMock()
    document = client.register_document("gmail", {"id": "local-only", "filename": "bill.pdf"})
    assert client.update_document(document, processing_status="validated")["status"] == "persisted_local"
    client.local_ledger.get_remote_identity_link.assert_not_called()
    client.session.request.assert_not_called()
    client.session.close()


def test_invalid_local_references_cannot_select_record_one(tmp_path):
    client = OperationsClient(config_for(tmp_path))
    document = client.local_ledger.register_document({"source": "gmail", "sourceDocumentId": "original", "originalFilename": "bill.pdf"})
    client.session.request = MagicMock()
    assert client.update_document(True, processing_status="validated")["status"] == "failed"
    assert client.create_review_item(True, "invalid") is None
    assert client.local_ledger.get_document(document)["processing_status"] == "imported"
    client.session.request.assert_not_called()
    client.session.close()


def test_schema_two_upgrade_keeps_verified_recovery_snapshot(tmp_path):
    path = str(tmp_path / "ledger.sqlite3")
    ledger = LocalOperationsLedger(path)
    document = ledger.register_document({"source": "gmail", "sourceDocumentId": "retained", "originalFilename": "bill.pdf"})
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE remote_identity_links")
        connection.execute("DELETE FROM schema_migrations WHERE version = 3")
        connection.execute("PRAGMA user_version = 2")
    connection.close()
    upgraded = LocalOperationsLedger(path)
    assert upgraded.schema_status()["currentVersion"] == 3
    assert upgraded.get_document(document)["source_document_id"] == "retained"
    manifests = list((tmp_path / "schema_backups").glob("*.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text())
    assert manifest["sourceSchemaVersion"] == 2 and manifest["targetSchemaVersion"] == 3
    with sqlite3.connect(str(manifests[0].parent / manifest["backupFilename"])) as snapshot:
        assert snapshot.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert snapshot.execute("PRAGMA user_version").fetchone()[0] == 2
    snapshot.close()
