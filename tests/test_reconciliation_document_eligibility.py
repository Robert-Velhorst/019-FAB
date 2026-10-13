import pytest

from src.operations.local_api import create_app
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_reconciliation import LocalReconciliationService, RECONCILIABLE_DOCUMENT_STATUSES


@pytest.fixture
def evidence(tmp_path):
    path = str(tmp_path / "ledger.sqlite3")
    ledger = LocalOperationsLedger(path)
    document_id = ledger.register_document({"source": "manual", "sourceDocumentId": "synthetic-eligibility",
        "originalFilename": "synthetic.pdf", "processingStatus": "processed",
        "vendorName": "Synthetic", "transactionDate": "2026-09-30", "totalAmount": 4.28})
    return path, ledger, document_id


def state(ledger):
    with ledger._connection() as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return {row[0]: list(map(tuple, connection.execute(
            'SELECT * FROM "' + row[0].replace('"', '""') + '" ORDER BY rowid'
        ))) for row in tables}


@pytest.mark.parametrize("status", ["imported", "needs_review", "failed", "duplicate", "unknown"])
@pytest.mark.parametrize("through_api", [False, True])
def test_ineligible_explicit_selection_rejected_without_writes(evidence, status, through_api):
    path, ledger, document_id = evidence
    ledger.update_document(document_id, {"processingStatus": status})
    before = state(ledger)
    if through_api:
        response = create_app({"fab_local_ledger_path": path}).test_client().post(
            "/api/reconciliation/run", json={"bankTransactions": [], "documentIds": [document_id]})
        assert response.status_code == 400
        assert "documentIds" in response.get_json()["error"]
    else:
        with pytest.raises(ValueError, match="documentIds"):
            LocalReconciliationService(ledger).run([], document_ids=[document_id])
    assert state(ledger) == before


def test_linked_duplicate_is_excluded_from_automatic_pool(evidence):
    _, ledger, document_id = evidence
    ledger.update_document(document_id, {"duplicateOfDocumentId": document_id})
    assert ledger.list_reconcilable_documents(RECONCILIABLE_DOCUMENT_STATUSES) == []
    report = LocalReconciliationService(ledger).run([])
    assert report["candidateDocuments"] == 0
    assert report["results"] == []


def test_linked_duplicate_explicit_selection_rejected_even_when_processed(evidence):
    _, ledger, document_id = evidence
    ledger.update_document(document_id, {"duplicateOfDocumentId": document_id})
    before = state(ledger)
    with pytest.raises(ValueError, match="documentIds"):
        LocalReconciliationService(ledger).run([], document_ids=[document_id])
    assert state(ledger) == before


@pytest.mark.parametrize("status", RECONCILIABLE_DOCUMENT_STATUSES)
def test_eligible_explicit_selection_remains_supported(evidence, status):
    _, ledger, document_id = evidence
    ledger.update_document(document_id, {"processingStatus": status})
    assert LocalReconciliationService(ledger).run([], document_ids=[document_id])["candidateDocuments"] == 1


@pytest.mark.parametrize("limit", [1, 100])
@pytest.mark.parametrize("change", [{"processingStatus": "needs_review"}, {"duplicateOfDocumentId": 1}])
def test_gate_after_candidate_limit_rejects_entire_selection(evidence, limit, change):
    _, ledger, document_id = evidence
    blocked = ledger.register_document({"source": "manual", "sourceDocumentId": "synthetic-blocked-tail",
        "originalFilename": "blocked.pdf", "processingStatus": "processed", **change})
    before = state(ledger)
    with pytest.raises(ValueError, match="documentIds"):
        LocalReconciliationService(ledger).run([], document_ids=[document_id, blocked], limit=limit)
    assert state(ledger) == before


@pytest.mark.parametrize("status", ["approved", "reconciled", "ignored"])
def test_terminal_documents_remain_excluded(evidence, status):
    _, ledger, document_id = evidence
    ledger.update_document(document_id, {"reconciliationStatus": status})
    before = ledger.get_document_core(document_id)
    report = LocalReconciliationService(ledger).run([], document_ids=[document_id])
    assert report["candidateDocuments"] == 0
    assert report["results"] == []
    assert ledger.get_document_core(document_id) == before


def test_duplicate_does_not_displace_valid_candidate_at_limit_one(evidence):
    _, ledger, document_id = evidence
    ledger.register_document({"source": "manual", "sourceDocumentId": "synthetic-newer-duplicate",
        "originalFilename": "duplicate.pdf", "processingStatus": "processed", "duplicateOfDocumentId": document_id})
    candidates = ledger.list_reconcilable_documents(RECONCILIABLE_DOCUMENT_STATUSES, limit=1)
    assert [document["id"] for document in candidates] == [document_id]
    assert LocalReconciliationService(ledger).run([], limit=1)["candidateDocuments"] == 1
