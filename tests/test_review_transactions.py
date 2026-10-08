import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_bookkeeping_records import LocalBookkeepingRecordService
from src.operations.local_review import LocalReviewService


@pytest.fixture
def review_fixture(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    document_id = ledger.register_document({
        "source": "manual", "sourceDocumentId": "atomic-review",
        "originalFilename": "synthetic-invoice.pdf", "documentType": "vendor_invoice",
        "vendorName": "Synthetic Shop", "category": "Manual Review",
        "transactionDate": "2026-09-30", "totalAmount": 42.5,
        "processingStatus": "needs_review",
    })
    review_id = ledger.create_review_item({
        "documentId": document_id, "reason": "manual_review_category",
        "details": "Synthetic atomic correction",
    })
    return ledger, document_id, review_id


def database_state(ledger):
    with ledger._connection() as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
        return {row[0]: [tuple(item) for item in connection.execute(
            'SELECT * FROM "' + row[0].replace('"', '""') + '" ORDER BY rowid'
        )] for row in tables}


@pytest.mark.parametrize("failure_point", [
    "record_review_correction", "replace_bookkeeping_record_line_items", "record_audit_event",
])
def test_failed_correction_rolls_back_every_ledger_table(review_fixture, monkeypatch, failure_point):
    ledger, _document_id, review_id = review_fixture
    before = database_state(ledger)

    def fail(*_args, **_kwargs):
        raise RuntimeError("Synthetic correction failure")

    monkeypatch.setattr(ledger, failure_point, fail)
    with pytest.raises(RuntimeError, match="Synthetic correction failure"):
        LocalReviewService(ledger).resolve_review_item(
            review_id, status="approved", corrections={"category": "Office Supplies"}, learn_rule=True,
        )
    assert database_state(ledger) == before


def test_successful_correction_uses_one_connection(review_fixture, monkeypatch):
    ledger, document_id, review_id = review_fixture
    original = ledger._connect
    opened = []

    def connect():
        connection = original()
        opened.append(connection)
        return connection

    monkeypatch.setattr(ledger, "_connect", connect)
    result = LocalReviewService(ledger).resolve_review_item(
        review_id, status="approved", corrections={"category": "Office Supplies"}, learn_rule=True,
    )
    assert result["success"]
    assert len(opened) == 1
    assert ledger.get_document(document_id)["category"] == "Office Supplies"
    assert ledger.get_review_item(review_id)["status"] == "approved"


def test_nested_failure_rolls_back_only_its_savepoint(review_fixture):
    ledger, document_id, _review_id = review_fixture
    with ledger.write_transaction():
        ledger.update_document(document_id, {"vendorName": "Outer correction"})
        with pytest.raises(RuntimeError, match="Nested failure"):
            with ledger.write_transaction():
                ledger.update_document(document_id, {"category": "Must roll back"})
                raise RuntimeError("Nested failure")
        assert ledger.get_document(document_id)["category"] == "Manual Review"
    assert ledger.get_document(document_id)["vendor_name"] == "Outer correction"
    assert ledger.get_document(document_id)["category"] == "Manual Review"


def test_outer_failure_also_rolls_back_successful_nested_change(review_fixture):
    ledger, document_id, _review_id = review_fixture
    before = database_state(ledger)
    with pytest.raises(RuntimeError, match="Outer failure"):
        with ledger.write_transaction():
            with ledger.write_transaction():
                ledger.update_document(document_id, {"category": "Must roll back"})
            raise RuntimeError("Outer failure")
    assert database_state(ledger) == before


def test_read_only_snapshot_cannot_escalate_to_write_transaction(review_fixture):
    ledger, document_id, _review_id = review_fixture
    before = database_state(ledger)
    with ledger.read_snapshot():
        with pytest.raises(RuntimeError, match="read-only snapshot"):
            with ledger.write_transaction():
                ledger.update_document(document_id, {"category": "Must not write"})
    assert database_state(ledger) == before


def test_read_snapshot_inside_write_keeps_one_connection_and_sees_changes(review_fixture, monkeypatch):
    ledger, document_id, _review_id = review_fixture
    original = ledger._connect
    opened = []

    def connect():
        connection = original()
        opened.append(connection)
        return connection

    monkeypatch.setattr(ledger, "_connect", connect)
    with ledger.write_transaction():
        ledger.update_document(document_id, {"vendorName": "First correction"})
        with ledger.read_snapshot():
            assert ledger.get_document(document_id)["vendor_name"] == "First correction"
        ledger.update_document(document_id, {"category": "Office Supplies"})
    assert len(opened) == 1
    assert ledger.get_document(document_id)["category"] == "Office Supplies"


@pytest.mark.parametrize("failure_point", ["begin", "commit"])
def test_transaction_failure_closes_connection_and_restores_context(review_fixture, monkeypatch, failure_point):
    ledger, document_id, _review_id = review_fixture
    before = database_state(ledger)
    original = ledger._connect
    handle = original()

    class FailedConnection:
        def __getattr__(self, name):
            return getattr(handle, name)

        def execute(self, sql, *args):
            if failure_point == "begin" and sql == "BEGIN IMMEDIATE":
                raise OSError("Synthetic begin failure")
            return handle.execute(sql, *args)

        def commit(self):
            raise OSError("Synthetic commit failure")

    monkeypatch.setattr(ledger, "_connect", lambda: FailedConnection())
    with pytest.raises(OSError, match="Synthetic"):
        with ledger.write_transaction():
            ledger.update_document(document_id, {"category": "Must roll back"})
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        handle.execute("SELECT 1")
    monkeypatch.setattr(ledger, "_connect", original)
    assert database_state(ledger) == before
    with ledger.write_transaction():
        ledger.update_document(document_id, {"category": "Office Supplies"})
    assert ledger.get_document(document_id)["category"] == "Office Supplies"


def test_concurrent_decisions_cannot_both_resolve_the_same_review(review_fixture, monkeypatch):
    ledger, document_id, review_id = review_fixture
    paused = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    original = ledger.record_review_correction

    def pause_first(payload, *args, **kwargs):
        if payload["correctedData"].get("category") == "Office Supplies":
            paused.set()
            assert release.wait(10), "Synthetic first correction was not released"
        return original(payload, *args, **kwargs)

    monkeypatch.setattr(ledger, "record_review_correction", pause_first)

    def resolve(category, started=None):
        if started:
            started.set()
        return LocalReviewService(ledger).resolve_review_item(
            review_id, status="approved", corrections={"category": category}, learn_rule=False,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(resolve, "Office Supplies")
        try:
            assert paused.wait(5)
            second = pool.submit(resolve, "Different Decision", second_started)
            assert second_started.wait(5)
        finally:
            release.set()
        first_result = first.result(timeout=10)
        second_result = second.result(timeout=10)
    assert first_result["success"]
    assert second_result["status"] == "already_resolved"
    assert not second_result["success"]
    assert ledger.get_document(document_id)["category"] == "Office Supplies"
    assert len(ledger.list_review_corrections(document_id=document_id)) == 1


@pytest.mark.parametrize("fail_batch", [False, True])
def test_vendor_batch_shares_connection_and_rolls_back_as_one_change(review_fixture, monkeypatch, fail_batch):
    ledger, document_id, review_id = review_fixture
    other_document = ledger.register_document({
        "source": "manual", "sourceDocumentId": "atomic-review-second",
        "originalFilename": "synthetic-second.pdf", "documentType": "vendor_invoice",
        "vendorName": "Synthetic Shop", "category": "Manual Review",
        "transactionDate": "2026-09-29", "totalAmount": 30,
        "processingStatus": "needs_review",
    })
    other_review = ledger.create_review_item({
        "documentId": other_document, "reason": "manual_review_category", "details": "Synthetic batch",
    })
    before = database_state(ledger)
    original_connect = ledger._connect
    original_audit = ledger.record_audit_event
    opened = []

    def connect():
        connection = original_connect()
        opened.append(connection)
        return connection

    def audit(payload, *args, **kwargs):
        if fail_batch and payload.get("entityId") == str(other_review) and payload.get("action") == "local_review.review_item.resolve":
            raise RuntimeError("Synthetic batch audit failure")
        return original_audit(payload, *args, **kwargs)

    monkeypatch.setattr(ledger, "_connect", connect)
    monkeypatch.setattr(ledger, "record_audit_event", audit)

    def resolve():
        return LocalReviewService(ledger).resolve_review_item(
            review_id, status="approved", corrections={"category": "Office Supplies"},
            learn_rule=True, apply_to_matching_vendor=True,
        )

    if fail_batch:
        with pytest.raises(RuntimeError, match="Synthetic batch audit failure"):
            resolve()
        assert len(opened) == 1
        assert database_state(ledger) == before
    else:
        result = resolve()
        assert len(opened) == 1
        assert result["batchPropagation"]["appliedDocuments"] == 1
        for item in (document_id, other_document):
            assert ledger.get_document(item)["category"] == "Office Supplies"
        assert ledger.get_review_item(other_review)["status"] == "approved"


def test_api_correction_failure_keeps_financial_state_and_records_redacted_error(review_fixture, monkeypatch):
    from src.operations.local_api import create_app

    ledger, _document_id, review_id = review_fixture
    app = create_app({"fab_local_ledger_path": ledger.path})
    before = database_state(ledger)
    original = LocalOperationsLedger.record_audit_event

    def failed_audit(self, payload, *args, **kwargs):
        if payload.get("action") == "local_review.review_item.resolve":
            raise RuntimeError("Synthetic API audit failure")
        return original(self, payload, *args, **kwargs)

    monkeypatch.setattr(LocalOperationsLedger, "record_audit_event", failed_audit)
    response = app.test_client().post(f"/api/review/{review_id}/resolve", json={
        "status": "approved", "corrections": {"category": "Office Supplies"}, "learnRule": False,
    })
    assert response.status_code == 500
    after = database_state(ledger)
    error_audits = after.pop("audit_events")
    assert before.pop("audit_events") == []
    assert len(error_audits) == 1
    assert error_audits[0][2] == "local_api.unhandled_exception"
    assert "Synthetic API audit failure" not in error_audits[0][5]
    assert b"Synthetic API audit failure" not in response.data
    for state in (before, after):
        state["sqlite_sequence"] = [row for row in state["sqlite_sequence"] if row[0] != "audit_events"]
    assert after == before


@pytest.mark.parametrize("source_kind", ["document", "bank"])
def test_direct_normalized_record_creation_rolls_back_line_item_failure(review_fixture, monkeypatch, source_kind):
    ledger, document_id, _review_id = review_fixture
    bank_id = ledger.upsert_bank_transaction({
        "accountIdentifier": "synthetic-account", "transactionId": "synthetic-bank-record",
        "amount": -42.5, "transactionDate": "2026-09-30", "description": "Synthetic Shop",
    })
    before = database_state(ledger)

    def fail(*_args, **_kwargs):
        raise RuntimeError("Synthetic line item failure")

    monkeypatch.setattr(ledger, "replace_bookkeeping_record_line_items", fail)
    service = LocalBookkeepingRecordService(ledger)
    with pytest.raises(RuntimeError, match="Synthetic line item failure"):
        if source_kind == "document":
            service.upsert_from_document(document_id)
        else:
            service.upsert_from_bank_transaction(bank_id)
    assert database_state(ledger) == before


def test_normalized_record_resolution_rolls_back_audit_failure(review_fixture, monkeypatch):
    ledger, document_id, _review_id = review_fixture
    service = LocalBookkeepingRecordService(ledger)
    record_id = service.upsert_from_document(document_id)["recordId"]
    before = database_state(ledger)

    def fail(*_args, **_kwargs):
        raise RuntimeError("Synthetic record audit failure")

    monkeypatch.setattr(ledger, "record_audit_event", fail)
    with pytest.raises(RuntimeError, match="Synthetic record audit failure"):
        service.resolve_record(record_id, status="approved", corrections={"category": "Office Supplies", "amount": 50})
    assert database_state(ledger) == before


def test_independent_reader_sees_no_partial_correction(review_fixture):
    ledger, document_id, review_id = review_fixture
    reader = sqlite3.connect(ledger.path)
    try:
        with ledger.write_transaction():
            ledger.update_document(document_id, {"category": "Office Supplies"})
            ledger.resolve_review_item(review_id, status="approved")
            assert reader.execute("SELECT category FROM bookkeeping_documents WHERE id = ?", (document_id,)).fetchone()[0] == "Manual Review"
            assert reader.execute("SELECT status FROM review_items WHERE id = ?", (review_id,)).fetchone()[0] == "pending"
        assert reader.execute("SELECT category FROM bookkeeping_documents WHERE id = ?", (document_id,)).fetchone()[0] == "Office Supplies"
        assert reader.execute("SELECT status FROM review_items WHERE id = ?", (review_id,)).fetchone()[0] == "approved"
    finally:
        reader.close()


def test_interrupted_transaction_rolls_back_and_clears_context(review_fixture):
    ledger, document_id, _review_id = review_fixture
    before = database_state(ledger)
    with pytest.raises(KeyboardInterrupt):
        with ledger.write_transaction():
            ledger.update_document(document_id, {"category": "Must roll back"})
            raise KeyboardInterrupt()
    assert database_state(ledger) == before
    with ledger.write_transaction():
        ledger.update_document(document_id, {"category": "Office Supplies"})
    assert ledger.get_document(document_id)["category"] == "Office Supplies"
