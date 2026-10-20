from unittest.mock import patch

import pytest

from src.operations.local_bookkeeping_records import LocalBookkeepingRecordService
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_routing import LocalRoutingService


def fixture(tmp_path, target):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    document = ledger.register_document({
        "source": "synthetic", "sourceDocumentId": "route", "documentType": "receipt",
        "processingStatus": "reviewed", "vendorName": "Synthetic Vendor",
        "category": "Office Supplies" if target == "waveapps" else "Personal",
        "transactionDate": "2026-06-28", "totalAmount": 42.5,
        "metadata": {"targetSystem": target},
    })
    config = {"mijngeldzaken_category_mapping": {"Personal": "Huishouden"}}
    return ledger, document, LocalRoutingService(ledger, config)


@pytest.mark.parametrize("target", ["waveapps", "mijngeldzaken"])
@pytest.mark.parametrize("failure", ["normalization", "audit"])
def test_document_draft_preparation_rolls_back_every_related_write(tmp_path, target, failure):
    ledger, document, service = fixture(tmp_path, target)
    before = ledger.get_document_core(document)
    audit = ledger.record_audit_event
    normalize = LocalBookkeepingRecordService.record_export_state

    def fail_after_record(service, *args, **kwargs):
        normalize(service, *args, **kwargs)
        raise RuntimeError("Synthetic normalized-state failure")

    def fail_audit(payload):
        if payload["action"].endswith("_draft_prepared"):
            audit(payload)
            raise RuntimeError("Synthetic routing completion audit failure")
        return audit(payload)

    failure_patch = (patch.object(LocalBookkeepingRecordService, "record_export_state", new=fail_after_record)
                     if failure == "normalization" else patch.object(ledger, "record_audit_event", side_effect=fail_audit))
    with failure_patch, pytest.raises(RuntimeError, match="Synthetic"):
        service.prepare_document_route(document)
    assert ledger.list_routing_attempts() == []
    assert ledger.get_document_core(document)["processing_status"] == before["processing_status"]
    assert ledger.get_document_core(document)["metadata"] == before["metadata"]
    assert ledger.list_bookkeeping_records() == []
    assert not any(event["action"].endswith("_draft_prepared") for event in ledger.list_audit_events())


@pytest.mark.parametrize("target", ["waveapps", "mijngeldzaken"])
def test_bank_record_route_and_normalized_state_roll_back_after_audit_failure(tmp_path, target):
    ledger, _, service = fixture(tmp_path, target)
    ledger.upsert_vendor_category_rule({
        "vendorName": "Synthetic Bank Vendor", "category": "Office Supplies" if target == "waveapps" else "Personal",
        "targetSystem": target, "status": "approved", "confidenceScore": 0.97,
    })
    bank_id = ledger.upsert_bank_transaction({
        "accountIdentifier": "synthetic-account", "transactionId": "synthetic-bank-route",
        "transactionDate": "2026-06-28", "amount": -42.5, "currency": "EUR",
        "description": "Synthetic supplies", "counterparty": "Synthetic Bank Vendor",
    })
    record_id = LocalBookkeepingRecordService(ledger, service.config).upsert_from_bank_transaction(bank_id)["recordId"]
    LocalBookkeepingRecordService(ledger, service.config).resolve_record(
        record_id, status="approved", resolution="Synthetic test approval",
        corrections={"category": "Office Supplies" if target == "waveapps" else "Personal", "targetSystem": target},
    )
    before = ledger.get_bookkeeping_record(record_id)
    audit = ledger.record_audit_event

    def fail_after_audit(payload):
        audit(payload)
        if payload["action"].endswith("_draft_prepared"):
            raise RuntimeError("Synthetic bank route audit failure")

    with patch.object(ledger, "record_audit_event", side_effect=fail_after_audit), pytest.raises(RuntimeError, match="Synthetic"):
        service.prepare_bookkeeping_record_route(record_id)
    assert ledger.list_routing_attempts() == []
    assert ledger.get_bookkeeping_record(record_id) == before
    assert not any(event["action"].endswith("_draft_prepared") for event in ledger.list_audit_events())


@pytest.mark.parametrize("target", ["waveapps", "mijngeldzaken"])
def test_missing_field_review_rolls_back_if_blocked_attempt_cannot_be_saved(tmp_path, target):
    ledger, document, service = fixture(tmp_path, target)
    ledger.update_document(document, {"transactionDate": ""})
    before = ledger.get_document_core(document)
    create = ledger.create_routing_attempt

    def fail_after_attempt(payload):
        create(payload)
        raise RuntimeError("Synthetic blocked attempt failure")

    with patch.object(ledger, "create_routing_attempt", side_effect=fail_after_attempt), pytest.raises(RuntimeError, match="Synthetic"):
        service.prepare_document_route(document)
    assert ledger.list_review_items(document_id=document) == []
    assert ledger.list_routing_attempts() == []
    assert ledger.get_document_core(document) == before
    assert ledger.list_bookkeeping_records() == []


@pytest.mark.parametrize("target", ["waveapps", "mijngeldzaken"])
def test_valid_local_draft_retains_required_approval_and_never_executes_operator(tmp_path, target):
    ledger, document, service = fixture(tmp_path, target)
    with (patch.object(service.operator, "execute", side_effect=AssertionError("External Wave execution")),
          patch.object(service.mijngeldzaken_operator, "execute", side_effect=AssertionError("External MijnGeldzaken execution"))):
        result = service.prepare_document_route(document)
    assert result["success"] and result["status"] in {"draft_prepared", "needs_confirmation"}
    attempt = ledger.get_routing_attempt(result["routingAttemptId"])
    assert attempt["metadata"]["externalSubmission"] == "not_executed"
    assert attempt["metadata"]["approvalRequiredForSubmit"] is True
    assert ledger.get_document_core(document)["processing_status"] == "export_draft_prepared"
    assert ledger.get_bookkeeping_record_by_document(document)["export_status"] == "draft_prepared"


def test_failed_nested_preparation_preserves_unrelated_outer_transaction(tmp_path):
    ledger, document, service = fixture(tmp_path, "waveapps")
    audit = ledger.record_audit_event

    def fail_completion(payload):
        audit(payload)
        if payload["action"].endswith("_draft_prepared"):
            raise RuntimeError("Synthetic nested route failure")

    with ledger.write_transaction():
        ledger.record_audit_event({"action": "synthetic.outer_preserved"})
        with patch.object(ledger, "record_audit_event", side_effect=fail_completion), pytest.raises(RuntimeError, match="Synthetic"):
            service.prepare_document_route(document)
    assert ledger.list_routing_attempts() == []
    assert ledger.get_document_core(document)["processing_status"] == "reviewed"
    assert ledger.list_bookkeeping_records() == []
    assert [event["action"] for event in ledger.list_audit_events()] == ["synthetic.outer_preserved"]


@pytest.mark.parametrize("target", ["waveapps", "mijngeldzaken"])
@pytest.mark.parametrize("approved", [False, True])
def test_repeated_preparation_returns_existing_draft_without_rewriting_state(tmp_path, target, approved):
    ledger, document, service = fixture(tmp_path, target)
    first = service.prepare_document_route(document)
    record_id = ledger.get_bookkeeping_record_by_document(document)["id"]
    if approved:
        ledger.update_bookkeeping_record(record_id, {"exportStatus": "approved_not_submitted"})
    before_document = ledger.get_document_core(document)
    before_record = ledger.get_bookkeeping_record(record_id)
    before_audit = ledger.list_audit_events()
    with patch.object(LocalBookkeepingRecordService, "record_export_state", side_effect=AssertionError("Unchanged draft must not rewrite normalized state")):
        second = service.prepare_document_route(document)
    assert second["success"] and second["status"] == "already_prepared"
    assert second["routingAttemptId"] == first["routingAttemptId"]
    assert len(ledger.list_routing_attempts()) == 1
    assert ledger.get_document_core(document) == before_document
    assert ledger.get_bookkeeping_record(record_id) == before_record
    assert ledger.list_audit_events() == before_audit


@pytest.mark.parametrize("target", ["waveapps", "mijngeldzaken"])
@pytest.mark.parametrize("change", [{"totalAmount": 99}, {"confidenceScore": 0.2}])
def test_prepared_source_drift_requires_review_instead_of_a_replacement_draft(tmp_path, target, change):
    ledger, document, service = fixture(tmp_path, target)
    first = service.prepare_document_route(document)
    old_attempt = ledger.get_routing_attempt(first["routingAttemptId"])
    ledger.update_document(document, change)
    result = service.prepare_document_route(document)
    assert not result["success"] and result["status"] == "needs_review"
    assert ledger.get_routing_attempt(first["routingAttemptId"]) == old_attempt
    assert sum(row["status"] in {"draft_prepared", "needs_confirmation"} for row in ledger.list_routing_attempts()) == 1
    assert ledger.get_document_core(document)["processing_status"] == "needs_review"
    assert ledger.list_review_items(document_id=document)[0]["status"] == "pending"


def test_existing_wave_business_draft_is_not_reused_for_personal_target(tmp_path):
    ledger, document, service = fixture(tmp_path, "waveapps_business")
    first = service.prepare_document_route(document)
    operation_id = first["operation"]["operation_id"]
    ledger.update_document(document, {"metadata": {"targetSystem": "waveapps_personal"}})
    result = service.prepare_document_route(document)
    assert result["status"] == "needs_review"
    assert ledger.get_routing_attempt(first["routingAttemptId"])["metadata"]["operation"]["operation_id"] == operation_id
    assert len([row for row in ledger.list_routing_attempts() if row["status"] == "draft_prepared"]) == 1


@pytest.mark.parametrize("target, alias", [("waveapps", "wave"), ("mijngeldzaken", "mijngeldzaken.nl")])
def test_equivalent_generic_target_aliases_reuse_the_same_draft(tmp_path, target, alias):
    ledger, document, service = fixture(tmp_path, target)
    first = service.prepare_document_route(document)
    second = service.prepare_document_route(document, target_system=alias)
    assert second["status"] == "already_prepared"
    assert second["routingAttemptId"] == first["routingAttemptId"]
    assert len(ledger.list_routing_attempts()) == 1


@pytest.mark.parametrize("target", ["waveapps", "mijngeldzaken"])
def test_open_review_still_blocks_reuse_of_a_prepared_draft(tmp_path, target):
    ledger, document, service = fixture(tmp_path, target)
    first = service.prepare_document_route(document)
    ledger.create_review_item({"documentId": document, "reason": "validation_failed"})
    result = service.prepare_document_route(document)
    assert result["status"] == "blocked_review"
    assert result["routingAttemptId"] != first["routingAttemptId"]
    assert ledger.list_review_items(document_id=document)[0]["status"] == "pending"


@pytest.mark.parametrize("target", ["waveapps", "mijngeldzaken"])
@pytest.mark.parametrize("missing_binding", ["documentSnapshot", "targetSystem"])
def test_legacy_unbound_draft_requires_review_not_silent_replacement(tmp_path, target, missing_binding):
    import json

    ledger, document, service = fixture(tmp_path, target)
    first = service.prepare_document_route(document)
    metadata = dict(ledger.get_routing_attempt(first["routingAttemptId"])["metadata"])
    metadata.pop(missing_binding)
    with ledger._connection() as connection:
        connection.execute("UPDATE routing_attempts SET metadata_json = ? WHERE id = ?",
                           (json.dumps(metadata), first["routingAttemptId"]))
    result = service.prepare_document_route(document)
    assert result["status"] == "needs_review"
    assert sum(row["status"] == "draft_prepared" for row in ledger.list_routing_attempts()) == 1


@pytest.mark.parametrize("target", ["waveapps", "mijngeldzaken"])
def test_prepared_draft_without_normalized_record_requires_review(tmp_path, target):
    ledger, document, service = fixture(tmp_path, target)
    first = service.prepare_document_route(document)
    old_attempt = ledger.get_routing_attempt(first["routingAttemptId"])
    record = ledger.get_bookkeeping_record_by_document(document)
    with ledger._connection() as connection:
        connection.execute("DELETE FROM bookkeeping_record_line_items WHERE bookkeeping_record_id = ?", (record["id"],))
        connection.execute("DELETE FROM bookkeeping_records WHERE id = ?", (record["id"],))
    assert ledger.get_bookkeeping_record_by_document(document) is None
    result = service.prepare_document_route(document)
    assert not result["success"] and result["status"] == "needs_review"
    assert ledger.get_routing_attempt(first["routingAttemptId"]) == old_attempt
    assert sum(row["status"] == "draft_prepared" for row in ledger.list_routing_attempts()) == 1
    assert ledger.list_review_items(document_id=document)[0]["status"] == "pending"


@pytest.mark.parametrize("target", ["waveapps", "mijngeldzaken"])
def test_competing_preparations_create_one_draft_without_blocking_its_record(tmp_path, target):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    ledger, document, service = fixture(tmp_path, target)
    participants = [LocalRoutingService(LocalOperationsLedger(ledger.path), service.config) for _ in range(2)]
    barrier = Barrier(2)

    def prepare(participant):
        barrier.wait(timeout=10)
        return participant.prepare_document_route(document)

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(prepare, participant) for participant in participants]
        results = [future.result(timeout=20) for future in futures]
    assert sorted(result["status"] for result in results) == ["already_prepared", "draft_prepared"]
    assert len({result["routingAttemptId"] for result in results}) == 1
    assert len(ledger.list_routing_attempts()) == 1
    assert ledger.get_bookkeeping_record_by_document(document)["export_status"] == "draft_prepared"
    assert ledger.list_review_items(document_id=document) == []


@pytest.mark.parametrize("target", ["waveapps", "mijngeldzaken"])
def test_authenticated_route_retry_acknowledges_the_same_local_draft(tmp_path, target):
    from src.operations.local_api import create_app

    ledger, document, service = fixture(tmp_path, target)
    token = "R7nP9qV3xM6bT1cE5dG8hJ0kL2sU4wZf"
    app = create_app({
        **service.config, "fab_instance_root": str(tmp_path), "fab_local_ledger_path": ledger.path,
        "fab_local_api_token": token, "fab_local_intake_paths": [],
        "gmail_enabled": False, "google_drive_enabled": False, "freshdesk_enabled": False,
    })
    client = app.test_client()
    path = f"/api/documents/{document}/route"
    assert client.post(path, json={}).status_code == 401
    headers = {"Authorization": "Bearer " + token}
    first = client.post(path, json={}, headers=headers)
    assert first.status_code == 200 and first.get_json()["success"]
    record = ledger.get_bookkeeping_record_by_document(document)
    second = client.post(path, json={}, headers=headers)
    assert second.status_code == 200 and second.get_json()["status"] == "already_prepared"
    assert first.get_json()["routingAttemptId"] == second.get_json()["routingAttemptId"]
    assert ledger.get_bookkeeping_record_by_document(document) == record
    assert len(ledger.list_routing_attempts()) == 1
