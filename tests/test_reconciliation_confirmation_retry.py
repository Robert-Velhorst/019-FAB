from concurrent.futures import ThreadPoolExecutor

import pytest

from src.operations.local_bank_transactions import LocalBankTransactionImportService
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_reconciliation import LocalReconciliationService
from src.operations.local_review import LocalReviewService


@pytest.fixture
def confirmed(tmp_path, request):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    bank = LocalBankTransactionImportService(ledger)
    bank.import_transactions([{"id": "synthetic-retry", "date": "2026-09-30",
                               "amount": -42.5, "description": "Synthetic Shop"}])
    transaction = bank.transactions_for_reconciliation()[0]
    service = LocalReconciliationService(ledger)
    missing_id = service.run([transaction])["results"][0]["reconciliationMatchId"]
    missing_review = ledger.list_review_items(status="pending")[0]["id"]
    document_id = ledger.register_document({
        "source": "manual", "sourceDocumentId": "synthetic-retry-doc", "processingStatus": "processed",
        "vendorName": "Synthetic Shop", "transactionDate": "2026-09-30", "totalAmount": 42.5,
    })
    match_id = service.run(bank.transactions_for_reconciliation())["results"][0]["reconciliationMatchId"]
    candidate_review = ledger.list_review_items(status="pending", document_id=document_id)[0]["id"]
    if getattr(request, "param", None) == "review":
        assert LocalReviewService(ledger).resolve_review_item(candidate_review, "approved", learn_rule=False)["success"]
    else:
        assert service.resolve_match(match_id, "approved", "Original confirmation")["success"]
    return ledger, service, document_id, transaction["ledgerBankTransactionId"], match_id, missing_id, missing_review, candidate_review


def state(ledger):
    with ledger._connection() as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return {row[0]: [tuple(item) for item in connection.execute(
            'SELECT * FROM "' + row[0].replace('"', '""') + '" ORDER BY rowid'
        )] for row in tables}


def test_same_confirmation_retry_is_read_only_and_preserves_original_decision(confirmed):
    ledger, service, _doc, _bank, match, *_rest = confirmed
    before = state(ledger)
    result = service.resolve_match(match, "approved", "Retry note must not replace approval")
    assert result["success"] and result["alreadyFinal"]
    assert state(ledger) == before


@pytest.mark.parametrize("confirmed", ["review"], indirect=True)
def test_normal_review_approval_retains_reconciliation_provenance_for_later_repair(confirmed):
    ledger, _service, _doc, _bank, match, _missing, _missing_review, candidate_review = confirmed
    original = ledger.get_review_item(candidate_review)
    assert original["corrected_data"]["reconciliationMatchId"] == match
    assert original["corrected_data"]["correctionId"]
    ledger.resolve_review_item(candidate_review, "pending", corrected_data=original["corrected_data"])
    result = LocalReviewService(ledger).resolve_review_item(candidate_review, "approved", learn_rule=False)
    assert result["success"] and result["alreadyFinal"]
    assert ledger.get_review_item(candidate_review)["corrected_data"]["reconciliationMatchId"] == match


@pytest.mark.parametrize("kind", ["candidate", "missing"])
def test_existing_dashboard_review_action_repairs_verified_completed_task(confirmed, kind):
    ledger, _service, _doc, _bank, match, missing, missing_review, candidate_review = confirmed
    review_id = candidate_review if kind == "candidate" else missing_review
    old = ledger.get_review_item(review_id)
    ledger.resolve_review_item(review_id, "pending", corrected_data=old["corrected_data"])
    final = ledger.get_reconciliation_match(match)
    missing_history = ledger.get_reconciliation_match(missing)
    result = LocalReviewService(ledger).resolve_review_item(review_id, "approved")
    assert result["success"] and result["alreadyFinal"]
    assert ledger.get_review_item(review_id)["status"] == "resolved"
    assert ledger.get_reconciliation_match(match) == final
    assert ledger.get_reconciliation_match(missing) == missing_history


def test_review_repair_refuses_corrections_to_confirmed_facts(confirmed):
    ledger, _service, _doc, _bank, _match, _missing, _missing_review, review_id = confirmed
    old = ledger.get_review_item(review_id)
    ledger.resolve_review_item(review_id, "pending", corrected_data=old["corrected_data"])
    before = state(ledger)
    result = LocalReviewService(ledger).resolve_review_item(review_id, "approved", corrections={"totalAmount": 99})
    assert not result["success"] and result["status"] == "stale_evidence"
    assert state(ledger) == before


@pytest.mark.parametrize("kind", ["candidate", "missing"])
@pytest.mark.parametrize("form", [False, True])
def test_real_review_api_and_form_repair_completed_tasks(confirmed, kind, form):
    from src.operations.local_api import create_app

    ledger, _service, _doc, _bank, match, _missing, missing_review, candidate_review = confirmed
    review_id = candidate_review if kind == "candidate" else missing_review
    old = ledger.get_review_item(review_id)
    ledger.resolve_review_item(review_id, "pending", corrected_data=old["corrected_data"])
    original = ledger.get_reconciliation_match(match)
    app = create_app({"fab_local_ledger_path": ledger.path})
    path = f"/review/{review_id}/resolve" if form else f"/api/review/{review_id}/resolve"
    response = app.test_client().post(path, **({"data": {"status": "approved"}} if form
                                               else {"json": {"status": "approved"}}))
    assert response.status_code == (302 if form else 200)
    assert ledger.get_review_item(review_id)["status"] == "resolved"
    assert ledger.get_reconciliation_match(match) == original


def test_conflicting_supersession_pointer_cannot_close_review_or_repair_other_tasks(confirmed):
    ledger, _service, _doc, _bank, match, missing, missing_review, candidate_review = confirmed
    for review in (missing_review, candidate_review):
        old = ledger.get_review_item(review)
        ledger.resolve_review_item(review, "pending", corrected_data=old["corrected_data"])
    metadata = ledger.get_reconciliation_match(missing)["metadata"]
    metadata["supersededBy"]["reconciliationMatchId"] = match
    metadata["supersededBy"]["documentId"] = True
    ledger.update_reconciliation_match(missing, {"metadata": metadata})
    before = state(ledger)
    result = LocalReviewService(ledger).resolve_review_item(missing_review, "resolved")
    assert not result["success"] and result["status"] == "stale_evidence"
    assert state(ledger) == before


@pytest.mark.parametrize("change", ["document", "bank", "owner", "document_owner", "bank_status", "hash", "status"])
def test_retry_rejects_changed_or_incomplete_completed_evidence(confirmed, change):
    ledger, service, document, bank, match, *_rest = confirmed
    if change == "document":
        ledger.update_document(document, {"totalAmount": 99})
    elif change == "bank":
        ledger.update_bank_transaction(bank, {"amount": -99})
    elif change in {"owner", "document_owner"}:
        metadata = ledger.get_bank_transaction(bank)["metadata"]
        metadata["latestReconciliation"]["reconciliationMatchId" if change == "owner" else "documentId"] = True
        ledger.update_bank_transaction(bank, {"metadata": metadata})
    elif change == "bank_status":
        ledger.update_bank_transaction(bank, {"reconciliationStatus": "needs_review"})
    elif change == "hash":
        metadata = ledger.get_reconciliation_match(match)["metadata"]
        metadata.pop("approvalDocumentHash")
        ledger.update_reconciliation_match(match, {"metadata": metadata})
    before = state(ledger)
    result = service.resolve_match(match, "reconciled" if change == "status" else "approved")
    assert not result["success"] and result["status"] == "stale_evidence"
    assert state(ledger) == before


def test_retry_repairs_only_exact_open_review_links_without_rewriting_final_match(confirmed):
    ledger, service, _doc, _bank, match, missing, missing_review, candidate_review = confirmed
    final = ledger.get_reconciliation_match(match)
    superseded = ledger.get_reconciliation_match(missing)
    for review in (missing_review, candidate_review):
        old = ledger.get_review_item(review)
        ledger.resolve_review_item(review, "pending", corrected_data=old["corrected_data"])
    unrelated = ledger.create_review_item({"reason": "missing_receipt", "correctedData": {
        "reconciliationMatchId": missing, "reconciliation_match_id": missing + 1000,
    }})
    result = service.resolve_match(match, "approved")
    assert result["success"] and result["alreadyFinal"]
    assert ledger.get_review_item(missing_review)["status"] == "resolved"
    assert ledger.get_review_item(candidate_review)["status"] == "resolved"
    assert ledger.get_review_item(unrelated)["status"] == "pending"
    assert ledger.get_reconciliation_match(match) == final
    assert ledger.get_reconciliation_match(missing) == superseded
    before = state(ledger)
    assert service.resolve_match(match, "approved")["success"]
    assert state(ledger) == before


def test_failed_retry_repair_rolls_back_all_review_changes(confirmed, monkeypatch):
    ledger, service, _doc, _bank, match, _missing, missing_review, candidate_review = confirmed
    for review in (missing_review, candidate_review):
        old = ledger.get_review_item(review)
        ledger.resolve_review_item(review, "pending", corrected_data=old["corrected_data"])
    before = state(ledger)
    original = ledger.record_audit_event

    def fail(payload):
        if payload["action"] == "local_reconciliation.confirmation_retry.repaired":
            raise RuntimeError("Synthetic retry repair failure")
        return original(payload)

    monkeypatch.setattr(ledger, "record_audit_event", fail)
    with pytest.raises(RuntimeError, match="Synthetic retry repair failure"):
        service.resolve_match(match, "approved")
    assert state(ledger) == before


def test_concurrent_retries_repair_once_and_preserve_confirmed_history(confirmed):
    ledger, _service, _doc, _bank, match, _missing, missing_review, _candidate_review = confirmed
    old = ledger.get_review_item(missing_review)
    ledger.resolve_review_item(missing_review, "pending", corrected_data=old["corrected_data"])
    original = ledger.get_reconciliation_match(match)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: LocalReconciliationService(ledger).resolve_match(match, "approved"), range(2)))
    assert all(result["success"] and result["alreadyFinal"] for result in results)
    assert sorted(result["repairedItems"] for result in results) == [0, 1]
    assert ledger.get_reconciliation_match(match) == original
    assert ledger.get_review_item(missing_review)["status"] == "resolved"
