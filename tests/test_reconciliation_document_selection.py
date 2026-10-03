import pytest

from src.operations.local_api import create_app
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_reconciliation import LocalReconciliationService


@pytest.fixture
def evidence(tmp_path):
    path = str(tmp_path / "ledger.sqlite3")
    ledger = LocalOperationsLedger(path)
    document_id = ledger.register_document({"source": "manual", "sourceDocumentId": "synthetic-selection",
        "originalFilename": "synthetic.pdf", "processingStatus": "processed",
        "vendorName": "Synthetic", "transactionDate": "2026-09-30", "totalAmount": 4.28})
    return path, ledger, document_id


def state(ledger):
    with ledger._connection() as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return {row[0]: list(map(tuple, connection.execute(
            'SELECT * FROM "' + row[0].replace('"', '""') + '" ORDER BY rowid'
        ))) for row in tables}


def test_explicit_empty_selection_does_not_match_entire_ledger(evidence):
    _, ledger, _ = evidence
    before = state(ledger)
    summary = LocalReconciliationService(ledger).run([], document_ids=[])
    assert summary["candidateDocuments"] == 0
    assert summary["results"] == []
    # Successful runs retain their normal audit; no document or review is created/changed.
    after = state(ledger)
    financial_tables = [name for name in before if "audit" not in name and name != "sqlite_sequence"]
    assert {name: after[name] for name in financial_tables} == {name: before[name] for name in financial_tables}


@pytest.mark.parametrize("selection", [True, False, 1, "1", {"1": True}, [True], [1.9], [1.0],
    [None], [0], [-1], [2**63], ["1.0"], [[],], [999999] * 501])
def test_invalid_selection_rejected_before_any_document_lookup(evidence, monkeypatch, selection):
    _, ledger, _ = evidence
    before = state(ledger)
    calls = []
    original = ledger.get_document_core

    def tracked(document_id):
        calls.append(document_id)
        return original(document_id)

    monkeypatch.setattr(ledger, "get_document_core", tracked)
    with pytest.raises(ValueError, match="documentIds"):
        LocalReconciliationService(ledger).run([], document_ids=selection)
    assert calls == []
    assert state(ledger) == before


def test_selection_is_captured_and_duplicates_are_not_reprocessed(evidence, monkeypatch):
    _, ledger, document_id = evidence
    calls = []
    original = ledger.get_document_core

    def tracked(selected):
        calls.append(selected)
        return original(selected)

    monkeypatch.setattr(ledger, "get_document_core", tracked)
    summary = LocalReconciliationService(ledger).run([], document_ids=iter([document_id, str(document_id)]))
    assert summary["candidateDocuments"] == 1
    assert calls == [document_id, document_id]


def test_infinite_selection_stops_after_bounded_consumption(evidence):
    _, ledger, _ = evidence
    consumed = []

    def selection():
        while len(consumed) < 501:
            consumed.append(1)
            yield 999999
        raise AssertionError("Selection read past the bounded prefix")

    with pytest.raises(ValueError, match="documentIds"):
        LocalReconciliationService(ledger).run([], document_ids=selection())
    assert len(consumed) == 501


def test_automatic_selection_remains_available_when_omitted(evidence):
    _, ledger, _ = evidence
    assert LocalReconciliationService(ledger).run([])["candidateDocuments"] == 1


def test_exact_reference_limit_is_accepted_and_deduplicated(evidence):
    _, ledger, document_id = evidence
    assert LocalReconciliationService(ledger).run([], document_ids=[document_id] * 500)["candidateDocuments"] == 1


def test_caller_selection_mutation_does_not_expand_captured_scope(evidence):
    _, ledger, document_id = evidence
    selection = []

    class ChangingReconciler:
        def reconcile(self, bank, documents):
            assert documents == []
            selection.append(document_id)
            return []

    report = LocalReconciliationService(ledger, reconciler=ChangingReconciler()).run([], document_ids=selection)
    assert report["candidateDocuments"] == 0
    assert report["results"] == []


@pytest.mark.parametrize("through_api", [False, True])
@pytest.mark.parametrize("limit", [1, 100])
def test_missing_explicit_document_rejects_entire_selection(evidence, through_api, limit):
    path, ledger, document_id = evidence
    before = state(ledger)
    if through_api:
        response = create_app({"fab_local_ledger_path": path}).test_client().post(
            "/api/reconciliation/run", json={"bankTransactions": [], "documentIds": [document_id, 999999], "limit": limit})
        assert response.status_code == 400
        assert "documentIds" in response.get_json()["error"]
    else:
        with pytest.raises(ValueError, match="documentIds"):
            LocalReconciliationService(ledger).run([], document_ids=[document_id, 999999], limit=limit)
    assert state(ledger) == before


@pytest.mark.parametrize("selection", [[], True, [True], [1.9], [2**63], [999999] * 501])
def test_http_selection_uses_same_guard(evidence, selection):
    path, ledger, _ = evidence
    before = state(ledger)
    client = create_app({"fab_local_ledger_path": path}).test_client()
    response = client.post("/api/reconciliation/run", json={"bankTransactions": [], "documentIds": selection})
    assert response.status_code == (200 if selection == [] else 400)
    if selection == []:
        assert response.get_json()["candidateDocuments"] == 0
    else:
        assert state(ledger) == before
