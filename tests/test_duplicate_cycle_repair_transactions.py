from unittest.mock import patch

import pytest

from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_processing import LocalDocumentProcessor


def cycle_fixture(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    first = ledger.register_document({"source": "synthetic", "sourceDocumentId": "first", "originalFilename": "first.pdf"})
    second = ledger.register_document({"source": "synthetic", "sourceDocumentId": "second", "originalFilename": "second.pdf"})
    ledger.update_document(first, {"duplicateOfDocumentId": second})
    ledger.update_document(second, {"duplicateOfDocumentId": first})
    return ledger, first, second


def test_cycle_repair_does_not_clear_links_changed_while_backing_up(tmp_path):
    ledger, first, second = cycle_fixture(tmp_path)

    def intervening_correction(**_kwargs):
        ledger.clear_document_duplicate(first)
        ledger.update_document(first, {"processingStatus": "validated"})
        return {"success": True, "manifest": {"ledgerSha256": "a" * 64, "ledgerBytes": 100}}

    with patch("src.operations.local_processing.LocalBackupService.create_backup", side_effect=intervening_correction):
        summary = LocalDocumentProcessor(ledger).repair_duplicate_cycles()
    assert ledger.get_document_core(first)["processing_status"] == "validated"
    assert ledger.get_document_core(second)["duplicate_of_document_id"] == first
    assert summary["cyclesRepaired"] == 0 and summary["cyclesSkipped"] == 1
    assert ledger.list_review_items() == []


@pytest.mark.parametrize("fail_at", [1, 2])
def test_cycle_repair_rolls_back_all_members_if_a_review_cannot_persist(tmp_path, fail_at):
    ledger, first, second = cycle_fixture(tmp_path)
    create_review = ledger.create_review_item
    calls = []

    def failing_review(payload):
        calls.append(payload)
        if len(calls) == fail_at:
            raise RuntimeError("synthetic review persistence failure")
        return create_review(payload)

    with patch.object(ledger, "create_review_item", side_effect=failing_review):
        with pytest.raises(RuntimeError, match="review persistence failure"):
            LocalDocumentProcessor(ledger).repair_duplicate_cycles(create_backup=False)
    assert ledger.get_document_core(first)["duplicate_of_document_id"] == second
    assert ledger.get_document_core(second)["duplicate_of_document_id"] == first
    assert ledger.get_document_core(first)["processing_status"] == "imported"
    assert ledger.get_document_core(second)["processing_status"] == "imported"
    assert ledger.list_review_items() == []
    assert ledger.list_bookkeeping_records() == []
    assert not any(event["action"] == "local_processing.duplicate_cycle_repaired" for event in ledger.list_audit_events())


@pytest.mark.parametrize("failure", ["normalization", "audit"])
def test_cycle_repair_does_not_commit_without_normalized_records_and_audit(tmp_path, failure):
    ledger, first, second = cycle_fixture(tmp_path)
    audit = ledger.record_audit_event

    def failing_audit(payload):
        if payload["action"] == "local_processing.duplicate_cycle_repaired":
            raise RuntimeError("synthetic final audit failure")
        return audit(payload)

    failure_patch = (
        patch("src.operations.local_processing.LocalBookkeepingRecordService.upsert_from_document",
              side_effect=RuntimeError("synthetic normalization failure"))
        if failure == "normalization" else patch.object(ledger, "record_audit_event", side_effect=failing_audit)
    )
    with failure_patch:
        with pytest.raises(RuntimeError, match="synthetic"):
            LocalDocumentProcessor(ledger).repair_duplicate_cycles(create_backup=False)
    assert ledger.get_document_core(first)["duplicate_of_document_id"] == second
    assert ledger.get_document_core(second)["duplicate_of_document_id"] == first
    assert ledger.list_review_items() == []
    assert ledger.list_bookkeeping_records() == []
    assert not any(event["action"] == "local_processing.duplicate_cycle_repaired" for event in ledger.list_audit_events())
