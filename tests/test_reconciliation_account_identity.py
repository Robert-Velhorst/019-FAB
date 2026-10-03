import pytest

from src.operations.local_api import create_app
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_reconciliation import LocalReconciliationService


@pytest.fixture
def ledger(tmp_path):
    return LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))


def transaction(account="synthetic-A", amount=-4.28, **extra):
    return {"id": "synthetic-shared-reference", "date": "2026-09-30", "amount": amount,
            "account_identifier": account, **extra}


def state(ledger):
    with ledger._connection() as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return {row[0]: [tuple(item) for item in connection.execute(
            'SELECT * FROM "' + row[0].replace('"', '""') + '" ORDER BY rowid'
        )] for row in tables}


@pytest.mark.parametrize("same_batch", [False, True])
def test_same_external_reference_in_different_direct_accounts_keeps_separate_evidence(ledger, same_batch):
    service = LocalReconciliationService(ledger)
    if same_batch:
        results = service.run([transaction(), transaction("synthetic-B", -99)])["results"]
        first, second = [item["reconciliationMatchId"] for item in results]
    else:
        first = service.run([transaction()])["results"][0]["reconciliationMatchId"]
        original = ledger.get_reconciliation_match(first)
        second = service.run([transaction("synthetic-B", -99)])["results"][0]["reconciliationMatchId"]
        assert ledger.get_reconciliation_match(first) == original
    assert first != second
    assert ledger.get_reconciliation_match(first)["metadata"]["bankTransaction"]["amount"] == -4.28
    assert ledger.get_reconciliation_match(second)["metadata"]["bankTransaction"]["amount"] == -99
    assert len(ledger.list_review_items(status="pending")) == 2
    assert service.run([transaction()])["results"][0]["reconciliationMatchId"] == first


@pytest.mark.parametrize("second", [transaction(), transaction(amount=-99)])
def test_duplicate_identity_inside_one_direct_batch_is_rejected_atomically(ledger, second):
    before = state(ledger)
    with pytest.raises(ValueError, match="duplicated"):
        LocalReconciliationService(ledger).run([transaction(), second])
    assert state(ledger) == before


@pytest.mark.parametrize("account", [True, 42, [], {"id": "synthetic"}])
def test_non_string_account_reference_is_rejected_without_recording(ledger, account):
    before = state(ledger)
    with pytest.raises(ValueError):
        LocalReconciliationService(ledger).run([transaction(account)])
    assert state(ledger) == before


def test_conflicting_account_aliases_cannot_select_another_accounts_history(ledger):
    before = state(ledger)
    with pytest.raises(ValueError):
        LocalReconciliationService(ledger).run([transaction(accountIdentifier="synthetic-B")])
    assert state(ledger) == before


def test_camel_case_account_alias_and_missing_scope_keep_independent_history(ledger):
    service = LocalReconciliationService(ledger)
    unspecified = {"id": "synthetic-shared-reference", "amount": -4.28, "date": "2026-09-30"}
    unscoped = service.run([unspecified])["results"][0]["reconciliationMatchId"]
    camel = {**unspecified, "accountIdentifier": "synthetic-A"}
    scoped = service.run([camel])["results"][0]["reconciliationMatchId"]
    assert scoped != unscoped
    assert service.run([transaction()])["results"][0]["reconciliationMatchId"] == scoped
    assert service.run([unspecified])["results"][0]["reconciliationMatchId"] == unscoped


def test_many_newer_other_accounts_do_not_hide_older_own_record(ledger):
    service = LocalReconciliationService(ledger)
    original = service.run([transaction()])["results"][0]["reconciliationMatchId"]
    metadata = ledger.get_reconciliation_match(original)["metadata"]
    with ledger.write_transaction():
        for index in range(120):
            ledger.create_reconciliation_match({"bankTransactionId": "synthetic-shared-reference", "status": "missing_receipt",
                "metadata": {**metadata, "bankTransaction": transaction(f"other-{index}")}})
    assert service.run([transaction()])["results"][0]["reconciliationMatchId"] == original


def test_json_api_keeps_accounts_separate_and_rejects_duplicate_batch(ledger):
    client = create_app({"fab_local_ledger_path": ledger.path}).test_client()
    first = client.post("/api/reconciliation/run", json={"bankTransactions": [transaction()]})
    assert first.status_code == 200
    first_id = first.get_json()["results"][0]["reconciliationMatchId"]
    original = ledger.get_reconciliation_match(first_id)
    second = client.post("/api/reconciliation/run", json={"bankTransactions": [transaction("synthetic-B", -99)]})
    assert second.status_code == 200
    assert second.get_json()["results"][0]["reconciliationMatchId"] != first_id
    assert ledger.get_reconciliation_match(first_id) == original
    before = state(ledger)
    rejected = client.post("/api/reconciliation/run", json={"bankTransactions": [transaction(), transaction(amount=-99)]})
    assert rejected.status_code == 400
    assert state(ledger) == before


def test_ambiguous_legacy_account_aliases_do_not_get_overwritten(ledger):
    service = LocalReconciliationService(ledger)
    match_id = service.run([transaction()])["results"][0]["reconciliationMatchId"]
    match = ledger.get_reconciliation_match(match_id)
    metadata = match["metadata"]
    metadata["bankTransaction"]["accountIdentifier"] = "synthetic-B"
    ledger.update_reconciliation_match(match_id, {"metadata": metadata})
    before = state(ledger)
    with pytest.raises(ValueError, match="conflict"):
        service.run([transaction()])
    assert state(ledger) == before
