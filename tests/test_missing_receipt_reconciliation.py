import pytest

from src.operations.local_bank_transactions import LocalBankTransactionImportService
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_reconciliation import LocalReconciliationService


@pytest.fixture
def missing_receipt(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    bank = LocalBankTransactionImportService(ledger)
    bank.import_transactions([{
        "id": "synthetic-missing-receipt", "date": "2026-09-30",
        "amount": -12.5, "description": "Synthetic Shop",
    }])
    service = LocalReconciliationService(ledger)
    result = service.run(bank.transactions_for_reconciliation())
    return ledger, service, result["results"][0]["reconciliationMatchId"]


def database_state(ledger):
    with ledger._connection() as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
        return {row[0]: [tuple(item) for item in connection.execute(
            'SELECT * FROM "' + row[0].replace('"', '""') + '" ORDER BY rowid'
        )] for row in tables}


@pytest.mark.parametrize("status", ["resolved", "ignored", "rejected"])
def test_documentless_disposition_closes_only_its_missing_receipt_review(missing_receipt, status):
    ledger, service, match_id = missing_receipt
    own = ledger.list_review_items()[0]["id"]
    other = ledger.create_review_item({
        "reason": "missing_receipt", "correctedData": {"reconciliationMatchId": match_id + 100},
    })
    assert service.resolve_match(match_id, status)["success"]
    assert ledger.get_review_item(own)["status"] == "resolved"
    assert ledger.get_review_item(other)["status"] == "pending"
    assert ledger.list_bank_transactions()[0]["reconciliation_status"] == status


def test_reopening_documentless_match_restores_exactly_one_review(missing_receipt):
    ledger, service, match_id = missing_receipt
    own = ledger.list_review_items()[0]["id"]
    service.resolve_match(match_id, "ignored")
    service.resolve_match(match_id, "needs_review")
    service.resolve_match(match_id, "needs_review")
    pending = ledger.list_review_items(status="pending")
    assert len(pending) == 1
    assert pending[0]["id"] != own
    assert pending[0]["corrected_data"]["reconciliationMatchId"] == match_id
    assert ledger.get_review_item(own)["status"] == "resolved"
    assert ledger.list_bank_transactions()[0]["reconciliation_status"] == "needs_review"


@pytest.mark.parametrize("status", ["approved", "reconciled"])
def test_documentless_match_cannot_be_confirmed_as_reconciled(missing_receipt, status):
    ledger, service, match_id = missing_receipt
    before = database_state(ledger)
    result = service.resolve_match(match_id, status)
    assert not result["success"]
    assert result["status"] == "invalid_status"
    assert database_state(ledger) == before


def test_api_rejects_confirmation_without_document_evidence(missing_receipt):
    from src.operations.local_api import create_app

    ledger, _service, match_id = missing_receipt
    app = create_app({"fab_local_ledger_path": ledger.path})
    before = database_state(ledger)
    response = app.test_client().post(f"/api/reconciliation/{match_id}/resolve", json={"status": "approved"})
    assert response.status_code == 400
    assert not response.json["success"]
    assert database_state(ledger) == before


def test_missing_receipt_lookup_pages_without_closing_other_reviews(missing_receipt):
    ledger, service, match_id = missing_receipt
    own = ledger.list_review_items()[0]["id"]
    with ledger.write_transaction():
        for index in range(205):
            ledger.create_review_item({
                "reason": "missing_receipt", "correctedData": {"reconciliationMatchId": match_id + index + 100},
            })
        document = ledger.register_document({"source": "manual", "sourceDocumentId": "unrelated-review-doc"})
        other_document_review = ledger.create_review_item({
            "documentId": document, "reason": "missing_receipt", "correctedData": {"reconciliationMatchId": match_id},
        })
    service.resolve_match(match_id, "ignored")
    assert ledger.get_review_item(own)["status"] == "resolved"
    assert len(ledger.list_missing_receipt_review_items(limit=500)) == 206
    assert ledger.get_review_item(other_document_review)["status"] == "pending"


def test_failed_missing_receipt_reopening_rolls_back_match_and_bank(missing_receipt, monkeypatch):
    ledger, service, match_id = missing_receipt
    service.resolve_match(match_id, "ignored")
    before = database_state(ledger)

    def fail(*_args, **_kwargs):
        raise RuntimeError("Synthetic missing receipt failure")

    monkeypatch.setattr(ledger, "create_review_item", fail)
    with pytest.raises(RuntimeError, match="Synthetic missing receipt failure"):
        service.resolve_match(match_id, "needs_review")
    assert database_state(ledger) == before
