from unittest.mock import patch
from pathlib import Path

import pytest

from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_backup import LocalBackupService
from src.operations.local_processing import LocalDocumentProcessor


def fixture(tmp_path, ledger=None, prefix="", same_dates=False):
    if ledger is None:
        ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    identifiers = []
    for name, date in (("first", "2026-01-02"), ("second", "2026-02-02")):
        if same_dates:
            date = "2026-01-02"
        identifiers.append(ledger.register_document({
            "source": "synthetic", "sourceDocumentId": prefix + name, "originalFilename": prefix + name + ".pdf",
            "processingStatus": "needs_review", "vendorName": "Synthetic Subscription",
            "transactionDate": date, "totalAmount": 10, "category": "Manual Review",
            "extractedData": {"vendor_name": "Synthetic Subscription", "transaction_date": date, "total_amount": 10},
        }))
    canonical, subject = identifiers
    candidate = ledger.record_duplicate_candidate({"documentId": subject, "candidateDocumentId": canonical,
        "matchType": "exact_fingerprint_match", "status": "pending", "confidenceScore": 1})
    review = ledger.create_review_item({"documentId": subject, "reason": "duplicate_candidate", "details": "Retain until assessed"})
    ledger.update_document(subject, {"duplicateOfDocumentId": canonical})
    return ledger, canonical, subject, candidate, review


def test_reassessment_preserves_operator_confirmation_during_backup(tmp_path):
    ledger, canonical, subject, candidate, review = fixture(tmp_path)

    def confirm_during_backup(**_kwargs):
        ledger.resolve_duplicate_candidate(candidate, "confirmed", "Operator decision")
        ledger.resolve_review_item(review, status="resolved", resolution="Operator decision")
        ledger.update_document(subject, {"processingStatus": "duplicate"})
        return {"success": True, "manifest": {"ledgerSha256": "a" * 64, "ledgerBytes": 100}}

    with patch("src.operations.local_processing.LocalBackupService.create_backup", side_effect=confirm_during_backup):
        result = LocalDocumentProcessor(ledger).reassess_duplicate_candidates()
    assert ledger.list_duplicate_candidates()[0]["status"] == "confirmed"
    assert ledger.get_document_core(subject)["duplicate_of_document_id"] == canonical
    assert ledger.get_document_core(subject)["processing_status"] == "duplicate"
    assert result["deferredPairs"] == 1 and result["rejectedPairs"] == 0
    assert result["confirmedDuplicateLinksModified"] is False


@pytest.mark.parametrize("failure", ["normalization", "audit"])
def test_reassessment_rolls_back_candidate_links_reviews_and_records(tmp_path, failure):
    ledger, canonical, subject, candidate, review = fixture(tmp_path)
    _, canonical_two, subject_two, candidate_two, review_two = fixture(tmp_path, ledger=ledger, prefix="other-")
    original_audit = ledger.record_audit_event

    def fail_audit(payload):
        if payload["action"] == "local_processing.duplicate_candidate_reassessment_completed":
            raise RuntimeError("synthetic completion audit failure")
        return original_audit(payload)

    failure_patch = (
        patch("src.operations.local_processing.LocalBookkeepingRecordService.upsert_from_document",
              side_effect=RuntimeError("synthetic normalization failure"))
        if failure == "normalization" else patch.object(ledger, "record_audit_event", side_effect=fail_audit)
    )
    with failure_patch:
        with pytest.raises(RuntimeError, match="synthetic"):
            LocalDocumentProcessor(ledger).reassess_duplicate_candidates(create_backup=False)
    candidates = {row["id"]: row for row in ledger.list_duplicate_candidates()}
    reviews = {row["id"]: row for row in ledger.list_review_items()}
    for canonical_id, subject_id, candidate_id, review_id in (
        (canonical, subject, candidate, review), (canonical_two, subject_two, candidate_two, review_two),
    ):
        assert candidates[candidate_id]["status"] == "pending"
        assert ledger.get_document_core(subject_id)["duplicate_of_document_id"] == canonical_id
        assert ledger.get_document_core(subject_id)["processing_status"] == "needs_review"
        assert reviews[review_id]["status"] == "pending"
    assert ledger.list_bookkeeping_records() == []


def test_reassessment_detects_evidence_change_even_with_the_same_timestamp(tmp_path):
    ledger, canonical, subject, candidate, _ = fixture(tmp_path)
    timestamp = ledger.get_document_core(subject)["updated_at"]

    def correction_during_backup(**_kwargs):
        ledger.update_document(subject, {"vendorName": "Operator-corrected vendor"})
        return {"success": True, "manifest": {"ledgerSha256": "a" * 64, "ledgerBytes": 100}}

    with (patch.object(ledger, "_now", return_value=timestamp),
          patch("src.operations.local_processing.LocalBackupService.create_backup", side_effect=correction_during_backup)):
        result = LocalDocumentProcessor(ledger).reassess_duplicate_candidates()
    assert result["deferredPairs"] == 1 and result["rejectedPairs"] == 0
    assert ledger.get_document_core(subject)["updated_at"] == timestamp
    assert ledger.get_document_core(subject)["vendor_name"] == "Operator-corrected vendor"
    assert ledger.get_document_core(subject)["duplicate_of_document_id"] == canonical
    assert ledger.list_duplicate_candidates()[0]["id"] == candidate
    assert ledger.list_duplicate_candidates()[0]["status"] == "pending"


def test_reassessment_defers_only_changed_pairs_and_processes_unchanged_work(tmp_path):
    ledger, canonical, subject, candidate, review = fixture(tmp_path)
    _, _, next_subject, next_candidate, _ = fixture(tmp_path, ledger=ledger, prefix="next-")

    def confirm_one_pair(**_kwargs):
        ledger.resolve_duplicate_candidate(candidate, "confirmed", "Operator decision")
        ledger.resolve_review_item(review, status="resolved", resolution="Operator decision")
        ledger.update_document(subject, {"processingStatus": "duplicate"})
        return {"success": True, "manifest": {"ledgerSha256": "a" * 64, "ledgerBytes": 100}}

    with patch("src.operations.local_processing.LocalBackupService.create_backup", side_effect=confirm_one_pair):
        result = LocalDocumentProcessor(ledger).reassess_duplicate_candidates()
    candidates = {row["id"]: row for row in ledger.list_duplicate_candidates()}
    assert result["candidatePairs"] == 2 and result["deferredPairs"] == 1 and result["rejectedPairs"] == 1
    assert candidates[candidate]["status"] == "confirmed"
    assert candidates[next_candidate]["status"] == "rejected"
    assert ledger.get_document_core(subject)["duplicate_of_document_id"] == canonical
    assert ledger.get_document_core(next_subject)["duplicate_of_document_id"] is None


@pytest.mark.parametrize("failure", ["candidate", "review"])
def test_retained_duplicate_replacement_and_review_failure_preserve_old_candidates(tmp_path, failure):
    ledger, canonical, subject, _, review = fixture(tmp_path, same_dates=True)
    ledger.clear_document_duplicate(subject)
    ledger.resolve_review_item(review, status="resolved", resolution="Synthetic setup")
    ledger.record_duplicate_candidate({"documentId": canonical, "candidateDocumentId": subject,
        "matchType": "stale_match_type", "status": "pending"})
    original_ids = {row["id"] for row in ledger.list_duplicate_candidates()}
    method = "record_duplicate_candidate" if failure == "candidate" else "create_review_item"
    with patch.object(ledger, method, side_effect=RuntimeError("synthetic replacement persistence failure")):
        with pytest.raises(RuntimeError, match="replacement persistence failure"):
            LocalDocumentProcessor(ledger).reassess_duplicate_candidates(create_backup=False)
    rows = ledger.list_duplicate_candidates()
    assert {row["id"] for row in rows} == original_ids
    assert all(row["status"] == "pending" for row in rows)
    assert ledger.list_review_items()[0]["status"] == "resolved"
    assert ledger.list_bookkeeping_records() == []


def test_false_candidate_transition_cannot_be_counted_as_a_success(tmp_path):
    ledger, canonical, subject, _, _ = fixture(tmp_path)
    with patch.object(ledger, "resolve_duplicate_candidate", return_value=False):
        with pytest.raises(RuntimeError, match="transition could not be retained"):
            LocalDocumentProcessor(ledger).reassess_duplicate_candidates(create_backup=False)
    assert ledger.get_document_core(subject)["duplicate_of_document_id"] == canonical
    assert ledger.list_duplicate_candidates()[0]["status"] == "pending"


def test_unchanged_reassessment_uses_a_real_verified_pre_mutation_backup(tmp_path, monkeypatch):
    ledger, _, subject, _, _ = fixture(tmp_path)
    config = {"fab_instance_root": str(tmp_path), "fab_local_backup_dir": str(tmp_path / "backups")}
    monkeypatch.setenv("FAB_BACKUP_INTEGRITY_KEY", "R7nP9qV3xM6bT1cE5dG8hJ0kL2sU4wZf")
    monkeypatch.delenv("FAB_BACKUP_INTEGRITY_KEY_FILE", raising=False)
    result = LocalDocumentProcessor(ledger, config).reassess_duplicate_candidates()
    assert result["deferredPairs"] == 0 and result["rejectedPairs"] == 1
    assert result["externalSubmission"] == "not_executed"
    package = Path(config["fab_local_backup_dir"]) / result["backup"]["backupFilename"]
    inspected = LocalBackupService(ledger, config).inspect_backup(str(package))
    assert inspected["status"] == "valid" and inspected["integrityStatus"] == "authenticated"
    assert inspected["manifest"]["ledgerSha256"] == result["backup"]["ledgerSha256"]
    assert ledger.get_document_core(subject)["duplicate_of_document_id"] is None
