import pytest

from src.operations.local_bank_transactions import LocalBankTransactionImportService
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_reconciliation import LocalReconciliationService
from src.reconciliation.automated_reconciliation import AutomatedReconciliation


@pytest.fixture
def evidence(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    document_id = ledger.register_document({
        "source": "manual", "sourceDocumentId": "synthetic-freshness",
        "originalFilename": "synthetic.pdf", "processingStatus": "processed",
        "vendorName": "Synthetic Shop", "transactionDate": "2026-09-30", "totalAmount": 42.5,
    })
    bank = LocalBankTransactionImportService(ledger)
    bank.import_transactions([{
        "id": "synthetic-bank-freshness", "date": "2026-09-30", "amount": -42.5,
        "description": "Synthetic Shop",
    }])
    return ledger, document_id, bank.transactions_for_reconciliation()


def database_state(ledger):
    with ledger._connection() as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
        return {row[0]: [tuple(item) for item in connection.execute(
            'SELECT * FROM "' + row[0].replace('"', '""') + '" ORDER BY rowid'
        )] for row in tables}


@pytest.mark.parametrize("change", [
    {"totalAmount": 99}, {"vendorName": "Changed Shop"}, {"processingStatus": "duplicate"},
])
def test_changed_document_during_computation_prevents_stale_results(evidence, change):
    ledger, document_id, transactions = evidence
    after_change = []

    class ChangingReconciler:
        def reconcile(self, bank, documents):
            result = AutomatedReconciliation({}).reconcile(bank, documents)
            ledger.update_document(document_id, change)
            after_change.append(database_state(ledger))
            return result

    with pytest.raises(ValueError, match="evidence changed"):
        LocalReconciliationService(ledger, reconciler=ChangingReconciler()).run(transactions)
    assert database_state(ledger) == after_change[0]


def test_changed_bank_during_computation_prevents_stale_results(evidence):
    ledger, _document_id, transactions = evidence
    bank_id = transactions[0]["ledgerBankTransactionId"]
    after_change = []

    class ChangingReconciler:
        def reconcile(self, bank, documents):
            result = AutomatedReconciliation({}).reconcile(bank, documents)
            ledger.update_bank_transaction(bank_id, {"amount": -99})
            after_change.append(database_state(ledger))
            return result

    with pytest.raises(ValueError, match="evidence changed"):
        LocalReconciliationService(ledger, reconciler=ChangingReconciler()).run(transactions)
    assert database_state(ledger) == after_change[0]


def test_already_stale_bank_input_is_rejected_before_matching(evidence):
    ledger, _document_id, transactions = evidence
    ledger.update_bank_transaction(transactions[0]["ledgerBankTransactionId"], {"amount": -99})
    before = database_state(ledger)
    with pytest.raises(ValueError, match="evidence changed"):
        LocalReconciliationService(ledger).run(transactions)
    assert database_state(ledger) == before


@pytest.mark.parametrize("bad_id", [True, 1.5, None, -1])
def test_malformed_stored_bank_reference_is_not_treated_as_ad_hoc(evidence, bad_id):
    ledger, _document_id, transactions = evidence
    transactions[0]["ledgerBankTransactionId"] = bad_id
    transactions[0]["ledger_bank_transaction_id"] = bad_id
    before = database_state(ledger)
    with pytest.raises(ValueError, match="evidence changed"):
        LocalReconciliationService(ledger).run(transactions)
    assert database_state(ledger) == before


def test_duplicated_stored_bank_input_cannot_create_multiple_candidates(evidence):
    ledger, _document_id, transactions = evidence
    before = database_state(ledger)
    with pytest.raises(ValueError, match="duplicated"):
        LocalReconciliationService(ledger).run(transactions * 2)
    assert database_state(ledger) == before


def test_conflicting_bank_reference_aliases_are_rejected(evidence):
    ledger, _document_id, transactions = evidence
    transactions[0]["ledger_bank_transaction_id"] += 100
    before = database_state(ledger)
    with pytest.raises(ValueError, match="evidence changed"):
        LocalReconciliationService(ledger).run(transactions)
    assert database_state(ledger) == before


def test_later_run_preserves_a_confirmed_document(evidence):
    ledger, document_id, transactions = evidence
    service = LocalReconciliationService(ledger)
    match = service.run(transactions)["results"][0]["reconciliationMatchId"]
    service.resolve_match(match, "approved")
    before = ledger.get_document(document_id)
    result = service.run([])
    assert result["unmatchedDocuments"] == 0
    assert ledger.get_document(document_id) == before


def test_closed_documents_do_not_hide_older_open_candidates(evidence):
    ledger, document_id, transactions = evidence
    with ledger.write_transaction():
        for index in range(120):
            ledger.register_document({
                "source": "manual", "sourceDocumentId": f"synthetic-closed-{index}",
                "processingStatus": "processed", "reconciliationStatus": "reconciled",
            })
    result = LocalReconciliationService(ledger).run(transactions, limit=1)
    assert result["candidateDocuments"] == 1
    assert result["matchedCandidates"] == 1
    assert result["results"][0]["documentId"] == document_id


def test_new_candidate_arriving_during_computation_invalidates_default_pool(evidence):
    ledger, _document_id, transactions = evidence
    after_change = []

    class ChangingPool:
        def reconcile(self, bank, documents):
            result = AutomatedReconciliation({}).reconcile(bank, documents)
            ledger.register_document({
                "source": "manual", "sourceDocumentId": "synthetic-new-candidate",
                "processingStatus": "processed", "totalAmount": 42.5,
            })
            after_change.append(database_state(ledger))
            return result

    with pytest.raises(ValueError, match="evidence changed"):
        LocalReconciliationService(ledger, reconciler=ChangingPool()).run(transactions)
    assert database_state(ledger) == after_change[0]


def test_explicit_selection_uses_core_rows_and_stops_at_the_limit(evidence, monkeypatch):
    ledger, document_id, _transactions = evidence
    original = ledger.get_document_core
    visited = []

    def core(row_id):
        visited.append(row_id)
        return original(row_id)

    def history_not_allowed(*_args, **_kwargs):
        raise AssertionError("Candidate selection must not load document histories")

    monkeypatch.setattr(ledger, "get_document_core", core)
    monkeypatch.setattr(ledger, "get_document", history_not_allowed)
    selected = LocalReconciliationService(ledger)._candidate_documents([document_id] * 100, limit=1)
    assert len(selected) == 1
    assert visited == [document_id]
