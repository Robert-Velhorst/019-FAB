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
