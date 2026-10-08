import pytest

from src.operations.local_bank_transactions import LocalBankTransactionImportService
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_reconciliation import LocalReconciliationService
from src.operations.local_review import LocalReviewService


@pytest.fixture
def missing(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    bank = LocalBankTransactionImportService(ledger)
    bank.import_transactions([{"id": "synthetic-disposition", "date": "2026-09-30", "amount": -42.5,
                               "description": "Synthetic Shop"}])
    service = LocalReconciliationService(ledger)
    result = service.run(bank.transactions_for_reconciliation())
    return ledger, bank, service, ledger.list_bank_transactions()[0]["id"], result["results"][0]["reconciliationMatchId"]


def state(ledger):
    with ledger._connection() as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return {row[0]: [tuple(item) for item in connection.execute(
            'SELECT * FROM "' + row[0].replace('"', '""') + '" ORDER BY rowid'
        )] for row in tables}


@pytest.mark.parametrize("status", ["ignored", "resolved", "rejected"])
def test_changed_missing_receipt_cannot_be_disposed(missing, status):
    ledger, _bank, service, bank_id, match_id = missing
    ledger.update_bank_transaction(bank_id, {"amount": -99})
    before = state(ledger)
    assert service.resolve_match(match_id, status)["status"] == "stale_evidence"
    assert state(ledger) == before


def test_review_cannot_close_a_changed_bank_exception(missing):
    ledger, _bank, _service, bank_id, _match_id = missing
    review_id = ledger.list_review_items(status="pending")[0]["id"]
    ledger.update_bank_transaction(bank_id, {"currency": "USD"})
    before = state(ledger)
    assert LocalReviewService(ledger).resolve_review_item(review_id, "ignored")["status"] == "stale_evidence"
    assert state(ledger) == before


def test_matching_refreshes_changed_missing_receipt_evidence(missing):
    ledger, bank, service, bank_id, match_id = missing
    ledger.update_bank_transaction(bank_id, {"amount": -99})
    assert service.resolve_match(match_id, "ignored")["status"] == "stale_evidence"
    result = service.run(bank.transactions_for_reconciliation())
    assert result["results"][0]["reconciliationMatchId"] == match_id
    review = ledger.list_review_items(status="pending")[0]
    assert review["corrected_data"]["bankTransaction"]["amount"] == -99
    assert service.resolve_match(match_id, "ignored")["success"]
    assert len(ledger.list_missing_receipt_review_items()) == 1


@pytest.mark.parametrize("status", ["ignored", "resolved", "rejected", "needs_review"])
def test_old_candidate_cannot_downgrade_another_confirmed_match(missing, status):
    ledger, bank, service, _bank_id, _missing_id = missing
    document_id = ledger.register_document({
        "source": "manual", "sourceDocumentId": "synthetic-disposition-doc", "processingStatus": "processed",
        "vendorName": "Synthetic Shop", "transactionDate": "2026-09-30", "totalAmount": 42.5,
    })
    first = service.run(bank.transactions_for_reconciliation())["results"][0]["reconciliationMatchId"]
    bank.import_transactions([{"id": "synthetic-other", "date": "2026-09-30", "amount": -42.5,
                               "description": "Synthetic Shop"}])
    second_transaction = next(row for row in bank.transactions_for_reconciliation() if row["id"] == "synthetic-other")
    second = service.run([second_transaction])["results"][0]["reconciliationMatchId"]
    assert service.resolve_match(first, "approved")["success"]
    before = state(ledger)
    assert service.resolve_match(second, status)["status"] == "stale_evidence"
    assert state(ledger) == before


def test_own_ignored_missing_receipt_can_be_reopened(missing):
    ledger, _bank, service, _bank_id, match_id = missing
    assert service.resolve_match(match_id, "ignored")["success"]
    assert service.resolve_match(match_id, "needs_review")["success"]
    assert len(ledger.list_review_items(status="pending")) == 1


@pytest.mark.parametrize("status", ["ignored", "resolved", "needs_review"])
def test_old_missing_receipt_cannot_take_over_a_new_document_candidate(missing, status):
    ledger, bank, service, _bank_id, missing_id = missing
    ledger.register_document({
        "source": "manual", "sourceDocumentId": "synthetic-arriving-receipt", "processingStatus": "processed",
        "vendorName": "Synthetic Shop", "transactionDate": "2026-09-30", "totalAmount": 42.5,
    })
    matched = service.run(bank.transactions_for_reconciliation())["results"][0]
    assert matched["status"] == "candidate"
    before = state(ledger)
    assert service.resolve_match(missing_id, status)["status"] == "stale_evidence"
    assert state(ledger) == before


def test_closed_bank_rows_do_not_hide_older_open_work(missing):
    ledger, bank, _service, bank_id, _match_id = missing
    with ledger.write_transaction():
        for index in range(120):
            closed_id = ledger.upsert_bank_transaction({
                "accountIdentifier": "synthetic-closed", "transactionId": f"closed-{index}",
                "amount": -1, "transactionDate": "2026-09-30",
            })
            ledger.update_bank_transaction(closed_id, {"reconciliationStatus": "reconciled"})
    rows = bank.transactions_for_reconciliation(limit=1)
    assert len(rows) == 1
    assert rows[0]["ledgerBankTransactionId"] == bank_id


@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize("form", [False, True])
def test_api_rejects_stale_exception_closure(missing, review, form):
    from src.operations.local_api import create_app

    ledger, _bank, _service, bank_id, match_id = missing
    review_id = ledger.list_review_items(status="pending")[0]["id"]
    ledger.update_bank_transaction(bank_id, {"amount": -99})
    app = create_app({"fab_local_ledger_path": ledger.path})
    before = state(ledger)
    path = f"/api/review/{review_id}/resolve" if review else f"/api/reconciliation/{match_id}/resolve"
    response = app.test_client().post(
        path.removeprefix("/api") if form else path,
        **({"data": {"status": "ignored"}} if form else {"json": {"status": "ignored"}}),
    )
    assert response.status_code == 409
    assert response.json["status"] == "stale_evidence"
    assert state(ledger) == before


def test_same_bank_id_in_two_accounts_keeps_separate_missing_receipt_evidence(missing):
    ledger, bank, service, first_bank, first_match = missing
    bank.import_transactions([{"id": "synthetic-disposition", "date": "2026-09-30", "amount": -99}],
                             account_identifier="synthetic-second-account")
    second = next(row for row in bank.transactions_for_reconciliation() if row["account_identifier"] == "synthetic-second-account")
    second_match = service.run([second])["results"][0]["reconciliationMatchId"]
    assert second_match != first_match
    assert service.resolve_match(second_match, "ignored")["success"]
    assert ledger.get_bank_transaction(first_bank)["reconciliation_status"] == "missing_receipt"
    assert ledger.get_reconciliation_match(first_match)["metadata"]["bankTransaction"]["amount"] == -42.5
