import pytest

from src.operations.local_bank_transactions import LocalBankTransactionImportService
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_reconciliation import LocalReconciliationService
from src.operations.local_review import LocalReviewService


@pytest.fixture
def candidate(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    document_id = ledger.register_document({
        "source": "manual", "sourceDocumentId": "synthetic-approval",
        "processingStatus": "processed", "vendorName": "Synthetic Shop",
        "transactionDate": "2026-09-30", "totalAmount": 42.5,
    })
    bank = LocalBankTransactionImportService(ledger)
    bank.import_transactions([{
        "id": "synthetic-approval-bank", "date": "2026-09-30", "amount": -42.5,
        "description": "Synthetic Shop",
    }])
    transaction = bank.transactions_for_reconciliation()[0]
    service = LocalReconciliationService(ledger)
    result = service.run([transaction])
    match_id = result["results"][0]["reconciliationMatchId"]
    return ledger, service, document_id, transaction["ledgerBankTransactionId"], match_id


def database_state(ledger):
    with ledger._connection() as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
        return {row[0]: [tuple(item) for item in connection.execute(
            'SELECT * FROM "' + row[0].replace('"', '""') + '" ORDER BY rowid'
        )] for row in tables}


@pytest.mark.parametrize("change", [
    {"totalAmount": 99}, {"vendorName": "Changed Shop"},
    {"contentSha256": "f" * 64}, {"extractedData": {"total_amount": 99}},
    {"processingStatus": "duplicate"},
])
def test_document_changed_after_matching_cannot_be_confirmed(candidate, change):
    ledger, service, document_id, _bank_id, match_id = candidate
    ledger.update_document(document_id, change)
    before = database_state(ledger)
    result = service.resolve_match(match_id, "approved")
    assert result["success"] is False
    assert result["status"] == "stale_evidence"
    assert database_state(ledger) == before


@pytest.mark.parametrize("change", [{"amount": -99}, {"currency": "USD"}, {"accountIdentifier": "changed"}])
def test_bank_changed_after_matching_cannot_be_confirmed(candidate, change):
    ledger, service, _document_id, bank_id, match_id = candidate
    ledger.update_bank_transaction(bank_id, change)
    before = database_state(ledger)
    result = service.resolve_match(match_id, "reconciled")
    assert result["success"] is False
    assert result["status"] == "stale_evidence"
    assert database_state(ledger) == before


def test_failed_nested_reconciliation_rolls_back_review_corrections_and_learning(candidate):
    ledger, _service, _document_id, bank_id, _match_id = candidate
    review_id = ledger.list_review_items(status="pending")[0]["id"]
    ledger.update_bank_transaction(bank_id, {"amount": -99})
    before = database_state(ledger)
    result = LocalReviewService(ledger).resolve_review_item(
        review_id, "approved", corrections={"category": "Office Supplies"}, learn_rule=True,
    )
    assert result["success"] is False
    assert result["status"] == "stale_evidence"
    assert database_state(ledger) == before


def test_unchanged_candidate_can_be_approved_through_review(candidate):
    ledger, _service, document_id, bank_id, _match_id = candidate
    review_id = ledger.list_review_items(status="pending")[0]["id"]
    result = LocalReviewService(ledger).resolve_review_item(review_id, "approved", learn_rule=False)
    assert result["success"] is True
    assert ledger.get_document_core(document_id)["reconciliation_status"] == "reconciled"
    assert ledger.get_bank_transaction(bank_id)["reconciliation_status"] == "reconciled"


@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize("form", [False, True])
def test_api_reports_stale_approval_as_conflict(candidate, review, form):
    from src.operations.local_api import create_app

    ledger, _service, document_id, _bank_id, match_id = candidate
    review_id = ledger.list_review_items(status="pending")[0]["id"]
    ledger.update_document(document_id, {"totalAmount": 99})
    app = create_app({"fab_local_ledger_path": ledger.path})
    before = database_state(ledger)
    path = f"/api/review/{review_id}/resolve" if review else f"/api/reconciliation/{match_id}/resolve"
    response = app.test_client().post(
        path.removeprefix("/api") if form else path,
        **({"data": {"status": "approved"}} if form else {"json": {"status": "approved", "learnRule": False}}),
    )
    assert response.status_code == 409
    assert response.json["status"] == "stale_evidence"
    assert database_state(ledger) == before


def test_review_cannot_hide_a_duplicate_processing_status(candidate):
    ledger, _service, document_id, _bank_id, _match_id = candidate
    review_id = ledger.list_review_items(status="pending")[0]["id"]
    ledger.update_document(document_id, {"processingStatus": "duplicate"})
    before = database_state(ledger)
    result = LocalReviewService(ledger).resolve_review_item(review_id, "approved", learn_rule=False)
    assert result["success"] is False
    assert result["status"] == "stale_evidence"
    assert database_state(ledger) == before


@pytest.mark.parametrize("link", [None, True, 1.5, "missing", 99999])
def test_invalid_review_match_reference_rolls_back_instead_of_closing(candidate, link):
    ledger, _service, _document_id, _bank_id, _match_id = candidate
    review_id = ledger.list_review_items(status="pending")[0]["id"]
    ledger.resolve_review_item(review_id, status="pending", corrected_data={"reconciliationMatchId": link})
    before = database_state(ledger)
    result = LocalReviewService(ledger).resolve_review_item(review_id, "approved", learn_rule=False)
    assert result["success"] is False
    assert database_state(ledger) == before


def test_legacy_candidate_requires_refresh_without_destroying_it(candidate):
    ledger, service, _document_id, _bank_id, match_id = candidate
    match = ledger.get_reconciliation_match(match_id)
    metadata = match["metadata"]
    metadata.pop("approvalDocumentHash", None)
    ledger.update_reconciliation_match(match_id, {"metadata": metadata})
    before = database_state(ledger)
    assert service.resolve_match(match_id, "approved")["status"] == "stale_evidence"
    assert database_state(ledger) == before
    bank = LocalBankTransactionImportService(ledger)
    refreshed = service.run(bank.transactions_for_reconciliation())
    assert refreshed["results"][0]["reconciliationMatchId"] == match_id
    assert service.resolve_match(match_id, "approved")["success"]


def test_financial_correction_and_confirmation_require_rematching(candidate):
    ledger, _service, _document_id, _bank_id, _match_id = candidate
    review_id = ledger.list_review_items(status="pending")[0]["id"]
    before = database_state(ledger)
    result = LocalReviewService(ledger).resolve_review_item(
        review_id, "approved", corrections={"totalAmount": 99, "category": "Office Supplies"}, learn_rule=True,
    )
    assert result["status"] == "stale_evidence"
    assert not result["success"]
    assert database_state(ledger) == before


def test_one_document_cannot_confirm_two_bank_candidates(candidate):
    ledger, service, _document_id, _bank_id, first_match = candidate
    bank = LocalBankTransactionImportService(ledger)
    bank.import_transactions([{
        "id": "synthetic-second-bank", "date": "2026-09-30", "amount": -42.5,
        "description": "Synthetic Shop",
    }])
    results = service.run(bank.transactions_for_reconciliation())["results"]
    second_match = next(row["reconciliationMatchId"] for row in results if row["reconciliationMatchId"] != first_match)
    assert service.resolve_match(first_match, "approved")["success"]
    before = database_state(ledger)
    assert service.resolve_match(second_match, "approved")["status"] == "stale_evidence"
    assert database_state(ledger) == before


@pytest.mark.parametrize("change", [
    {"ledgerBankTransactionId": True}, {"ledger_bank_transaction_id": 1.5},
    {"id": "different-transaction"}, {"amount": -99},
])
def test_changed_candidate_bank_snapshot_cannot_redirect_confirmation(candidate, change):
    ledger, service, _document_id, _bank_id, match_id = candidate
    metadata = ledger.get_reconciliation_match(match_id)["metadata"]
    metadata["bankTransaction"].update(change)
    ledger.update_reconciliation_match(match_id, {"metadata": metadata})
    before = database_state(ledger)
    assert service.resolve_match(match_id, "approved")["status"] == "stale_evidence"
    assert database_state(ledger) == before


def test_review_cannot_resolve_a_match_owned_by_another_document(candidate):
    ledger, _service, _document_id, _bank_id, match_id = candidate
    other_document = ledger.register_document({"source": "manual", "sourceDocumentId": "synthetic-other-review"})
    other_review = ledger.create_review_item({
        "documentId": other_document, "reason": "reconciliation_candidate",
        "correctedData": {"reconciliationMatchId": match_id},
    })
    before = database_state(ledger)
    result = LocalReviewService(ledger).resolve_review_item(other_review, "approved", learn_rule=False)
    assert result["status"] == "stale_evidence"
    assert database_state(ledger) == before
