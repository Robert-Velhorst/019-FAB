from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock, patch

import pytest

from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_processing import LocalDocumentProcessor


def seed(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    document = ledger.register_document({
        "source": "synthetic", "sourceDocumentId": "retry", "processingStatus": "failed",
        "metadata": {"processingError": "Synthetic failure", "processing": {"retryCount": 2}},
    })
    ledger.create_review_item({"documentId": document, "reason": "processing_failed"})
    return ledger, document


@pytest.mark.parametrize("after_audit_insert", [False, True])
def test_retry_claim_and_audit_roll_back_together(tmp_path, after_audit_insert):
    ledger, document = seed(tmp_path)
    processor = LocalDocumentProcessor(ledger)
    before = ledger.get_document_core(document)
    audit = ledger.record_audit_event

    def fail_retry_event(payload):
        if payload["action"] == "local_processing.retry_started":
            if after_audit_insert:
                audit(payload)
            raise RuntimeError("Synthetic retry audit failure")
        return audit(payload)

    with (patch.object(ledger, "record_audit_event", side_effect=fail_retry_event),
          patch.object(processor, "process_document") as process):
        with pytest.raises(RuntimeError, match="Synthetic retry audit failure"):
            processor.retry_document(document)
    after = ledger.get_document_core(document)
    assert after["processing_status"] == before["processing_status"]
    assert after["metadata"] == before["metadata"]
    assert ledger.list_review_items(document_id=document)[0]["status"] == "pending"
    process.assert_not_called()
    assert not any(event["action"] == "local_processing.retry_started" for event in ledger.list_audit_events())


def test_retry_preserves_a_newer_operator_decision(tmp_path):
    ledger, document = seed(tmp_path)
    processor = LocalDocumentProcessor(ledger)
    operator = LocalOperationsLedger(ledger.path)
    claim = processor._mark_retry_started

    def correct_before_claim(snapshot, actor):
        operator.update_document(document, {"processingStatus": "reviewed", "category": "Operator category"})
        return claim(snapshot, actor)

    with (patch.object(processor, "_mark_retry_started", side_effect=correct_before_claim),
          patch.object(processor, "process_document", return_value={"status": "processed"}) as process):
        result = processor.retry_document(document)
    assert ledger.get_document_core(document)["processing_status"] == "reviewed"
    assert ledger.get_document_core(document)["category"] == "Operator category"
    assert result["skipped"] and result["status"] == "reviewed"
    process.assert_not_called()


@pytest.mark.parametrize("fast_failure", [False, True])
def test_competing_callers_only_claim_and_process_a_failed_document_once(tmp_path, fast_failure):
    ledger, document = seed(tmp_path)
    barrier = Barrier(2)
    def finish(_document_id):
        if fast_failure:
            ledger.update_document(document, {"processingStatus": "failed"})
        return {"documentId": document, "status": "failed"}

    process = Mock(side_effect=finish)

    def retry():
        instance = LocalOperationsLedger(ledger.path)
        processor = LocalDocumentProcessor(instance)
        read = instance.get_document_core
        reads = 0

        def initial_snapshot(identifier):
            nonlocal reads
            snapshot = read(identifier)
            reads += 1
            if reads == 1:
                barrier.wait(timeout=10)
            return snapshot

        with (patch.object(instance, "get_document_core", side_effect=initial_snapshot),
              patch.object(processor, "process_document", side_effect=process)):
            return processor.retry_document(document)

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(retry) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert process.call_count == 1
    assert sum(bool(result.get("retry")) for result in results) == 1
    assert sum(bool(result.get("skipped")) for result in results) == 1
    assert ledger.get_document_core(document)["metadata"]["processing"]["retryCount"] == 3
    assert sum(event["action"] == "local_processing.retry_started" for event in ledger.list_audit_events()) == 1


def test_retry_defers_changed_evidence_even_when_status_and_timestamp_match(tmp_path):
    ledger, document = seed(tmp_path)
    processor = LocalDocumentProcessor(ledger)
    operator = LocalOperationsLedger(ledger.path)
    claim = processor._mark_retry_started
    timestamp = ledger.get_document_core(document)["updated_at"]

    def change_before_claim(snapshot, actor):
        with patch.object(operator, "_now", return_value=timestamp):
            operator.update_document(document, {"totalAmount": 99})
        return claim(snapshot, actor)

    with (patch.object(processor, "_mark_retry_started", side_effect=change_before_claim),
          patch.object(processor, "process_document") as process):
        result = processor.retry_document(document)
    current = ledger.get_document_core(document)
    assert current["updated_at"] == timestamp and current["processing_status"] == "failed"
    assert current["total_amount"] == 99
    assert current["metadata"]["processing"]["retryCount"] == 2
    assert result["skipped"] and result["reason"] == "document_changed_before_retry"
    process.assert_not_called()


def test_retry_skip_is_not_counted_as_a_retry(tmp_path):
    ledger, document = seed(tmp_path)
    processor = LocalDocumentProcessor(ledger)
    with patch.object(processor, "retry_document", return_value={"documentId": document, "status": "reviewed", "skipped": True}):
        result = processor.retry_failed()
    assert result["requested"] == 1 and result["retried"] == 0 and result["skipped"] == 1


def test_retry_claim_does_not_hydrate_document_history(tmp_path):
    ledger, document = seed(tmp_path)
    processor = LocalDocumentProcessor(ledger)
    with (patch.object(ledger, "get_document", side_effect=AssertionError("History hydration during retry admission")),
          patch.object(processor, "process_document", return_value={"status": "failed"})):
        result = processor.retry_document(document)
    assert result["retry"] and result["retryCount"] == 3


@pytest.mark.parametrize("batch", [False, True])
def test_authenticated_retry_api_preserves_a_newer_decision_and_reports_skips(tmp_path, batch):
    from src.operations.local_api import create_app

    ledger, document = seed(tmp_path)
    token = "R7nP9qV3xM6bT1cE5dG8hJ0kL2sU4wZf"
    app = create_app({
        "fab_instance_root": str(tmp_path), "fab_local_ledger_path": ledger.path,
        "fab_local_api_token": token, "fab_local_intake_paths": [],
        "gmail_enabled": False, "google_drive_enabled": False, "freshdesk_enabled": False,
    })
    client = app.test_client()
    path = "/api/documents/retry-failed" if batch else f"/api/documents/{document}/retry-processing"
    assert client.post(path, json={}).status_code == 401
    claim = LocalDocumentProcessor._mark_retry_started

    def correct_before_claim(processor, snapshot, actor):
        ledger.update_document(document, {"processingStatus": "reviewed", "category": "Operator category"})
        return claim(processor, snapshot, actor)

    with (patch.object(LocalDocumentProcessor, "_mark_retry_started", new=correct_before_claim),
          patch.object(LocalDocumentProcessor, "process_document") as process):
        response = client.post(path, json={}, headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200
    payload = response.get_json()
    if batch:
        assert payload["requested"] == 1 and payload["retried"] == 0 and payload["skipped"] == 1
        payload = payload["documents"][0]
    assert payload["status"] == "reviewed" and payload["skipped"]
    assert not payload.get("retry")
    assert ledger.get_document_core(document)["category"] == "Operator category"
    assert ledger.list_review_items(document_id=document)[0]["status"] == "pending"
    process.assert_not_called()
