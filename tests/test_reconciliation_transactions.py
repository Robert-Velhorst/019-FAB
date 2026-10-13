import pytest

from src.operations.local_bank_transactions import LocalBankTransactionImportService
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_reconciliation import LocalReconciliationService


@pytest.fixture
def reconciliation_fixture(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    document_id = ledger.register_document({
        "source": "manual", "sourceDocumentId": "synthetic-reconciliation",
        "originalFilename": "synthetic.pdf", "processingStatus": "processed",
        "vendorName": "Synthetic Shop", "category": "Office Supplies",
        "transactionDate": "2026-09-30", "totalAmount": 42.5, "confidenceScore": 1.0,
    })
    bank = LocalBankTransactionImportService(ledger, {})
    bank.import_transactions([{
        "id": "synthetic-bank", "date": "2026-09-30", "amount": -42.5,
        "description": "Synthetic Shop",
    }])
    return ledger, document_id, bank.transactions_for_reconciliation()


def database_state(ledger):
    with ledger._connection() as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
        return {row[0]: [tuple(item) for item in connection.execute(
            'SELECT * FROM "' + row[0].replace('"', '""') + '" ORDER BY rowid'
        )] for row in tables}


@pytest.mark.parametrize("failure_point", ["update_bank_transaction", "record_audit_event"])
def test_match_resolution_failure_rolls_back_all_local_state(reconciliation_fixture, monkeypatch, failure_point):
    ledger, _document_id, transactions = reconciliation_fixture
    service = LocalReconciliationService(ledger)
    match_id = service.run(transactions)["results"][0]["reconciliationMatchId"]
    before = database_state(ledger)

    def fail(*_args, **_kwargs):
        raise RuntimeError("Synthetic reconciliation failure")

    monkeypatch.setattr(ledger, failure_point, fail)
    with pytest.raises(RuntimeError, match="Synthetic reconciliation failure"):
        service.resolve_match(match_id, "approved")
    assert database_state(ledger) == before


def test_reconciliation_run_failure_rolls_back_candidates_and_reviews(reconciliation_fixture, monkeypatch):
    ledger, _document_id, transactions = reconciliation_fixture
    before = database_state(ledger)

    def fail(*_args, **_kwargs):
        raise RuntimeError("Synthetic review creation failure")

    monkeypatch.setattr(ledger, "create_review_item", fail)
    with pytest.raises(RuntimeError, match="Synthetic review creation failure"):
        LocalReconciliationService(ledger).run(transactions)
    assert database_state(ledger) == before


def test_needs_review_keeps_matching_review_open(reconciliation_fixture):
    ledger, document_id, transactions = reconciliation_fixture
    service = LocalReconciliationService(ledger)
    match_id = service.run(transactions)["results"][0]["reconciliationMatchId"]
    result = service.resolve_match(match_id, "needs_review")
    assert result["success"]
    assert ledger.list_review_items(document_id=document_id)[0]["status"] == "pending"


def test_resolution_does_not_close_another_match_review(reconciliation_fixture):
    ledger, document_id, transactions = reconciliation_fixture
    service = LocalReconciliationService(ledger)
    match_id = service.run(transactions)["results"][0]["reconciliationMatchId"]
    other_match = ledger.create_reconciliation_match({
        "documentId": document_id, "bankTransactionId": "another-synthetic-bank", "status": "candidate",
    })
    unrelated = ledger.create_review_item({
        "documentId": document_id, "reason": "reconciliation_candidate",
        "correctedData": {"reconciliationMatchId": other_match},
    })
    service.resolve_match(match_id, "approved")
    assert ledger.get_review_item(unrelated)["status"] == "pending"


@pytest.mark.parametrize("noise_count", [60, 205])
def test_resolution_finds_match_review_beyond_fifty_unrelated_reviews(reconciliation_fixture, noise_count):
    ledger, document_id, transactions = reconciliation_fixture
    service = LocalReconciliationService(ledger)
    match_id = service.run(transactions)["results"][0]["reconciliationMatchId"]
    review_id = ledger.list_review_items(document_id=document_id)[0]["id"]
    with ledger.write_transaction():
        for index in range(noise_count):
            ledger.create_review_item({
                "documentId": document_id, "reason": f"synthetic-unrelated-{index}",
            })
    service.resolve_match(match_id, "approved")
    assert ledger.get_review_item(review_id)["status"] == "resolved"
    assert len(ledger.list_review_items(status="pending", document_id=document_id, limit=500)) == noise_count


def test_approval_refreshes_normalized_record_after_closing_review(reconciliation_fixture):
    ledger, document_id, transactions = reconciliation_fixture
    service = LocalReconciliationService(ledger)
    match_id = service.run(transactions)["results"][0]["reconciliationMatchId"]
    service.resolve_match(match_id, "approved")
    record = ledger.get_bookkeeping_record_by_document(document_id)
    assert not record["review_required"]
    assert record["status"] != "needs_review"


@pytest.mark.parametrize("noise_count", [60, 205])
def test_repeat_run_does_not_count_an_existing_older_review_as_created(reconciliation_fixture, noise_count):
    ledger, document_id, transactions = reconciliation_fixture
    service = LocalReconciliationService(ledger)
    service.run(transactions)
    with ledger.write_transaction():
        for index in range(noise_count):
            ledger.create_review_item({"documentId": document_id, "reason": f"synthetic-noise-{index}"})
    summary = service.run(transactions)
    assert summary["reviewItemsCreated"] == 0
    assert len(ledger.list_review_items(document_id=document_id, limit=500)) == noise_count + 1


@pytest.mark.parametrize("bad_evidence", [[1], {"reconciliationMatchId": True}, {"reconciliationMatchId": 1.5}, {}])
def test_invalid_review_link_is_not_closed_or_trusted(reconciliation_fixture, bad_evidence):
    ledger, document_id, transactions = reconciliation_fixture
    service = LocalReconciliationService(ledger)
    match_id = service.run(transactions)["results"][0]["reconciliationMatchId"]
    bad_review = ledger.create_review_item({
        "documentId": document_id, "reason": "unmatched_document", "correctedData": bad_evidence,
    })
    service.resolve_match(match_id, "approved")
    assert ledger.get_review_item(bad_review)["status"] == "pending"


def test_reopened_match_has_a_pending_review_and_normalized_gate(reconciliation_fixture):
    ledger, document_id, transactions = reconciliation_fixture
    service = LocalReconciliationService(ledger)
    match_id = service.run(transactions)["results"][0]["reconciliationMatchId"]
    service.resolve_match(match_id, "approved")
    service.resolve_match(match_id, "needs_review")
    pending = ledger.list_review_items(status="pending", document_id=document_id)
    assert len(pending) == 1
    assert pending[0]["corrected_data"]["reconciliationMatchId"] == match_id
    assert ledger.get_bookkeeping_record_by_document(document_id)["review_required"]


def test_matching_computation_does_not_hold_the_writer(reconciliation_fixture):
    ledger, _document_id, transactions = reconciliation_fixture

    class ObservedReconciler:
        def reconcile(self, _transactions, _documents):
            assert ledger._write_transaction_connection.get() is None
            return []

    assert LocalReconciliationService(ledger, reconciler=ObservedReconciler()).run(transactions)["matchesRecorded"] == 0


def test_match_resolution_uses_one_database_connection(reconciliation_fixture, monkeypatch):
    ledger, _document_id, transactions = reconciliation_fixture
    service = LocalReconciliationService(ledger)
    match_id = service.run(transactions)["results"][0]["reconciliationMatchId"]
    original = ledger._connect
    opened = []

    def connect():
        connection = original()
        opened.append(connection)
        return connection

    monkeypatch.setattr(ledger, "_connect", connect)
    assert service.resolve_match(match_id, "approved")["success"]
    assert len(opened) == 1
