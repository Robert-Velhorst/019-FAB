import pytest

from src.operations.local_bank_transactions import LocalBankTransactionImportService
from src.operations.local_api import create_app
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_reconciliation import LocalReconciliationService, _evidence_fingerprint
from tests.test_reconciliation_account_identity import state


@pytest.fixture
def ledger(tmp_path):
    return LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))


def transaction(**changes):
    return {"date": "2026-09-30", "amount": -4.28, "account_identifier": "synthetic", **changes}


@pytest.mark.parametrize("key", ["id", "transaction_id"])
@pytest.mark.parametrize("value", [None, "", "   ", True, 1.5, [], {"id": "synthetic"}])
def test_malformed_explicit_reference_is_rejected_before_matching(ledger, key, value):
    before = state(ledger)
    with pytest.raises(ValueError):
        LocalReconciliationService(ledger).run([transaction(**{key: value})])
    assert state(ledger) == before


def test_conflicting_direct_reference_aliases_are_rejected_atomically(ledger):
    before = state(ledger)
    with pytest.raises(ValueError):
        LocalReconciliationService(ledger).run([transaction(id="synthetic-A", transaction_id="synthetic-B")])
    assert state(ledger) == before


@pytest.mark.parametrize("change", [{"id": "synthetic-other"}, {"accountIdentifier": "synthetic-other"}])
def test_imported_bank_alias_conflict_cannot_be_saved_under_another_identity(ledger, change):
    bank = LocalBankTransactionImportService(ledger)
    bank.import_transactions([transaction(id="synthetic-imported")], account_identifier="synthetic")
    captured = bank.transactions_for_reconciliation()[0]
    before = state(ledger)
    with pytest.raises(ValueError):
        LocalReconciliationService(ledger).run([{**captured, **change}])
    assert state(ledger) == before


def test_legacy_conflicting_reference_cannot_be_confirmed_even_with_retained_hash(ledger):
    ledger.register_document({"source": "manual", "sourceDocumentId": "synthetic-legacy",
        "processingStatus": "processed", "transactionDate": "2026-09-30", "totalAmount": 4.28})
    bank = LocalBankTransactionImportService(ledger)
    bank.import_transactions([transaction(id="synthetic-imported")], account_identifier="synthetic")
    service = LocalReconciliationService(ledger)
    match_id = service.run(bank.transactions_for_reconciliation())["results"][0]["reconciliationMatchId"]
    metadata = ledger.get_reconciliation_match(match_id)["metadata"]
    metadata["bankTransaction"]["id"] = "synthetic-wrong"
    metadata["approvalBankHash"] = _evidence_fingerprint(metadata["bankTransaction"])
    ledger.update_reconciliation_match(match_id, {"bankTransactionId": "synthetic-wrong", "metadata": metadata})
    before = state(ledger)
    result = service.resolve_match(match_id, "approved")
    assert result["success"] is False
    assert result["status"] == "stale_evidence"
    assert state(ledger) == before


@pytest.mark.parametrize("value", [0, 123, "synthetic-exact"])
def test_consistent_string_or_integer_aliases_share_one_reference(ledger, value):
    service = LocalReconciliationService(ledger)
    first = service.run([transaction(id=value, transaction_id=str(value))])["results"][0]["reconciliationMatchId"]
    record = ledger.get_reconciliation_match(first)
    assert record["bank_transaction_id"] == str(value)
    assert service.run([transaction(transaction_id=str(value))])["results"][0]["reconciliationMatchId"] == first


@pytest.mark.parametrize("changes", [{"id": True}, {"id": "synthetic-A", "transaction_id": "synthetic-B"}])
def test_json_api_reports_invalid_reference_without_financial_writes(ledger, changes):
    client = create_app({"fab_local_ledger_path": ledger.path}).test_client()
    before = state(ledger)
    response = client.post("/api/reconciliation/run", json={"bankTransactions": [transaction(**changes)]})
    assert response.status_code == 400
    assert state(ledger) == before
