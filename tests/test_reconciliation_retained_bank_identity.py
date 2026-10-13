import pytest

from src.operations.local_api import create_app
from src.operations.local_bank_transactions import LocalBankTransactionImportService
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_reconciliation import BANK_LEDGER_ID_KEYS, LocalReconciliationService
from tests.test_reconciliation_account_identity import state


@pytest.fixture
def ledger(tmp_path):
    return LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))


def transaction():
    return {"id": "synthetic-retained", "amount": -4.28, "date": "2026-09-30", "account_identifier": "synthetic"}


@pytest.mark.parametrize("key", BANK_LEDGER_ID_KEYS)
def test_null_retained_bank_reference_is_not_overwritten_as_unlinked(ledger, key):
    service = LocalReconciliationService(ledger)
    match_id = service.run([transaction()])["results"][0]["reconciliationMatchId"]
    metadata = ledger.get_reconciliation_match(match_id)["metadata"]
    metadata["bankTransaction"][key] = None
    ledger.update_reconciliation_match(match_id, {"metadata": metadata})
    before = state(ledger)
    with pytest.raises(ValueError):
        service.run([transaction()])
    assert state(ledger) == before


@pytest.mark.parametrize("kind", ["null", "boolean", "float", "decimal_string", "conflicting", "extra_null"])
def test_selected_native_history_with_bad_alias_cannot_create_replacement_record(ledger, kind):
    importer = LocalBankTransactionImportService(ledger)
    importer.import_transactions([transaction()], account_identifier="synthetic")
    captured = importer.transactions_for_reconciliation()
    bank_id = captured[0]["ledgerBankTransactionId"]
    service = LocalReconciliationService(ledger)
    match_id = service.run(captured)["results"][0]["reconciliationMatchId"]
    metadata = ledger.get_reconciliation_match(match_id)["metadata"]
    changes = {"null": {"ledgerBankTransactionId": None},
        "boolean": {"ledgerBankTransactionId": True}, "float": {"ledgerBankTransactionId": float(bank_id)},
        "decimal_string": {"ledgerBankTransactionId": f"{bank_id}.0"},
        "conflicting": {"ledger_bank_transaction_id": bank_id + 999}, "extra_null": {"bankTransactionRecordId": None}}
    metadata["bankTransaction"].update(changes[kind])
    ledger.update_reconciliation_match(match_id, {"metadata": metadata})
    before = state(ledger)
    with pytest.raises(ValueError):
        service.run(captured)
    assert state(ledger) == before


def test_api_reports_ambiguous_stored_reference_without_overwriting_it(ledger):
    client = create_app({"fab_local_ledger_path": ledger.path}).test_client()
    service = LocalReconciliationService(ledger)
    match_id = service.run([transaction()])["results"][0]["reconciliationMatchId"]
    metadata = ledger.get_reconciliation_match(match_id)["metadata"]
    metadata["bankTransaction"]["ledgerBankTransactionId"] = None
    ledger.update_reconciliation_match(match_id, {"metadata": metadata})
    before = state(ledger)
    response = client.post("/api/reconciliation/run", json={"bankTransactions": [transaction()]})
    assert response.status_code == 400
    assert state(ledger) == before


@pytest.mark.parametrize("reference", ["01", "+1", " 1 "])
def test_consistent_positive_string_aliases_reuse_native_history(ledger, reference):
    importer = LocalBankTransactionImportService(ledger)
    importer.import_transactions([transaction()], account_identifier="synthetic")
    captured = importer.transactions_for_reconciliation()
    assert captured[0]["ledgerBankTransactionId"] == 1
    service = LocalReconciliationService(ledger)
    match_id = service.run(captured)["results"][0]["reconciliationMatchId"]
    metadata = ledger.get_reconciliation_match(match_id)["metadata"]
    metadata["bankTransaction"]["ledgerBankTransactionId"] = reference
    ledger.update_reconciliation_match(match_id, {"metadata": metadata})
    assert service.run(captured)["results"][0]["reconciliationMatchId"] == match_id


@pytest.mark.parametrize("reference", ["1_0", "1" + chr(0x0660)])
def test_sql_numeric_prefilter_disagreement_does_not_create_replacement_history(ledger, reference):
    importer = LocalBankTransactionImportService(ledger)
    importer.import_transactions([transaction()], account_identifier="synthetic")
    captured = importer.transactions_for_reconciliation()
    service = LocalReconciliationService(ledger)
    match_id = service.run(captured)["results"][0]["reconciliationMatchId"]
    metadata = ledger.get_reconciliation_match(match_id)["metadata"]
    for key in BANK_LEDGER_ID_KEYS:
        metadata["bankTransaction"][key] = reference
    ledger.update_reconciliation_match(match_id, {"metadata": metadata})
    before = state(ledger)
    with pytest.raises(ValueError):
        service.run(captured)
    assert state(ledger) == before
