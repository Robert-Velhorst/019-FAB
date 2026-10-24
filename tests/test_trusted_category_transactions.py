from unittest.mock import patch

import pytest

from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_bookkeeping_records import LocalBookkeepingRecordService
from src.operations.local_processing import LocalDocumentProcessor


def seed(ledger, name):
    document_id = ledger.register_document({
        "source": "synthetic", "sourceDocumentId": name,
        "originalFilename": name + ".pdf", "documentType": "receipt",
        "processingStatus": "needs_review", "vendorName": "Praxis",
        "category": "Manual Review", "totalAmount": 42.5,
    })
    review_id = ledger.create_review_item({
        "documentId": document_id, "reason": "manual_review_category",
        "details": "Synthetic category gate",
    })
    return document_id, review_id


@pytest.fixture
def setup(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    processor = LocalDocumentProcessor(ledger)
    with (patch("src.operations.local_processing.LocalBackupService.create_backup",
                return_value={"backupPath": "synthetic-package"}),
          patch("src.operations.local_processing.LocalBackupService.inspect_backup",
                return_value={"success": True, "status": "valid", "manifest": {}})):
        yield ledger, processor


@pytest.mark.parametrize("separate_connection", [False, True])
def test_operator_category_correction_during_backup_is_preserved(setup, separate_connection):
    ledger, processor = setup
    document, review = seed(ledger, "changed")
    fresh, _ = seed(ledger, "fresh")
    timestamp = ledger.get_document_core(document)["updated_at"]
    operator = LocalOperationsLedger(ledger.path) if separate_connection else ledger

    def correct(**_kwargs):
        operator.update_document(document, {"category": "Operator decision"})
        operator.resolve_review_item(review, status="resolved", resolution="Operator decision")
        return {"backupPath": "synthetic-package"}

    with (patch.object(operator, "_now", return_value=timestamp),
          patch("src.operations.local_processing.LocalBackupService.create_backup", side_effect=correct)):
        result = processor.apply_trusted_category_suggestions()
    assert ledger.get_document_core(document)["category"] == "Operator decision"
    assert ledger.get_document_core(document)["updated_at"] == timestamp
    assert result["deferredDocuments"] == 1
    assert result["updatedDocuments"] == 1
    assert result["documentIds"] == [fresh]


@pytest.mark.parametrize("failure", ["normalization", "fields", "review", "audit"])
def test_category_batch_rolls_back_every_related_write(setup, failure):
    ledger, processor = setup
    rows = [seed(ledger, name) for name in ("first", "second")]
    original_audit = ledger.record_audit_event
    original_upsert = processor._replace_trusted_category_extracted_field
    original_normalize = LocalBookkeepingRecordService.upsert_from_document
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic second-document failure")
        return original_upsert(*args, **kwargs)

    def fail_audit(payload):
        if payload["action"] == "local_processing.trusted_category_batch_completed":
            raise RuntimeError("synthetic completion failure")
        return original_audit(payload)

    def fail_normalize(service, *args, **kwargs):
        original_normalize(service, *args, **kwargs)
        raise RuntimeError("synthetic normalization failure after persistence")

    failure_patch = {
        "normalization": patch.object(LocalBookkeepingRecordService, "upsert_from_document", new=fail_normalize),
        "fields": patch.object(processor, "_replace_trusted_category_extracted_field", side_effect=fail_second),
        "review": patch.object(ledger, "resolve_review_item", side_effect=RuntimeError("synthetic review failure")),
        "audit": patch.object(ledger, "record_audit_event", side_effect=fail_audit),
    }[failure]
    with failure_patch, pytest.raises(RuntimeError, match="synthetic"):
        processor.apply_trusted_category_suggestions()
    for document, review in rows:
        assert ledger.get_document_core(document)["category"] == "Manual Review"
        assert ledger.get_document_core(document)["processing_status"] == "needs_review"
        assert ledger.list_extracted_fields(document_id=document) == []
        assert ledger.list_review_items(document_id=document)[0]["status"] == "pending"
    assert ledger.list_bookkeeping_records() == []
    assert not any(row["action"] == "local_processing.trusted_category_applied"
                   for row in ledger.list_audit_events(limit=100))


def test_changed_evidence_and_new_candidates_wait_for_a_new_backup(setup):
    ledger, processor = setup
    document, _ = seed(ledger, "original")
    timestamp = ledger.get_document_core(document)["updated_at"]
    new_ids = []

    def change_evidence(**_kwargs):
        ledger.update_document(document, {"totalAmount": 99})
        new_ids.append(seed(ledger, "new")[0])
        return {"backupPath": "synthetic-package"}

    with (patch.object(ledger, "_now", return_value=timestamp),
          patch("src.operations.local_processing.LocalBackupService.create_backup", side_effect=change_evidence)):
        result = processor.apply_trusted_category_suggestions()
    assert result["candidates"] == 1 and result["deferredDocuments"] == 1
    assert result["updatedDocuments"] == 0
    assert ledger.get_document_core(document)["total_amount"] == 99
    for document_id in [document, *new_ids]:
        assert ledger.get_document_core(document_id)["category"] == "Manual Review"
        assert ledger.list_review_items(document_id=document_id)[0]["status"] == "pending"
    actions = {row["action"] for row in ledger.list_audit_events(limit=100)}
    assert "local_processing.trusted_category_deferred" in actions
    assert "local_processing.trusted_category_batch_completed" in actions


def test_unchanged_category_automation_uses_a_real_authenticated_backup(tmp_path, monkeypatch):
    from pathlib import Path
    from src.operations.local_backup import LocalBackupService

    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    document, _ = seed(ledger, "real-backup")
    config = {"fab_instance_root": str(tmp_path), "fab_local_backup_dir": str(tmp_path / "backups")}
    monkeypatch.setenv("FAB_BACKUP_INTEGRITY_KEY", "R7nP9qV3xM6bT1cE5dG8hJ0kL2sU4wZf")
    monkeypatch.delenv("FAB_BACKUP_INTEGRITY_KEY_FILE", raising=False)
    result = LocalDocumentProcessor(ledger, config).apply_trusted_category_suggestions()
    assert result["updatedDocuments"] == 1 and result["deferredDocuments"] == 0
    assert ledger.get_document_core(document)["category"] == "Construction Materials & Tools"
    package = Path(config["fab_local_backup_dir"]) / result["preMutationBackup"]["backupFilename"]
    inspected = LocalBackupService(ledger, config).inspect_backup(str(package))
    assert inspected["status"] == "valid" and inspected["integrityStatus"] == "authenticated"
    assert inspected["manifest"]["ledgerSha256"] == result["preMutationBackup"]["ledgerSha256"]
