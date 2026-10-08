import json

import pytest

from src.operations.local_api import create_app
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_reconciliation import LocalReconciliationService
from src.reconciliation.automated_reconciliation import AutomatedReconciliation


def transaction(**changes):
    return {"id": "synthetic-input", "amount": -4.28, "date": "2026-09-30", **changes}


def state(ledger):
    with ledger._connection() as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return {row[0]: [tuple(item) for item in connection.execute(
            'SELECT * FROM "' + row[0].replace('"', '""') + '" ORDER BY rowid'
        )] for row in tables}


@pytest.fixture
def ledger(tmp_path):
    return LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))


@pytest.mark.parametrize("item", [None, [], "invalid", transaction(amount=float("nan")),
    transaction(amount=float("inf")), transaction(amount=True), transaction(amount=None),
    transaction(date=None), transaction(date="not-a-date"),
    transaction(metadata={"nested": float("nan")})])
def test_bad_direct_transaction_is_rejected_before_any_ledger_write(ledger, item):
    before = state(ledger)
    with pytest.raises(ValueError):
        LocalReconciliationService(ledger).run([transaction(id="valid"), item])
    assert state(ledger) == before


@pytest.mark.parametrize("kind", ["batch", "bytes", "depth", "cycle", "keys", "wide", "unicode", "large_key"])
def test_oversized_or_non_json_input_is_rejected_before_matching(ledger, kind):
    payload = [transaction()]
    if kind == "batch":
        payload *= 501
    elif kind == "bytes":
        payload[0]["description"] = "x" * (4 * 1024 * 1024 + 1)
    elif kind == "depth":
        nested = "synthetic"
        for _ in range(35):
            nested = {"child": nested}
        payload[0]["metadata"] = nested
    elif kind == "cycle":
        payload[0]["metadata"] = payload
    elif kind == "keys":
        payload[0]["metadata"] = {1: "integer-key", "1": "string-key"}
    elif kind == "wide":
        payload[0]["metadata"] = [0] * 50_000
    elif kind == "unicode":
        payload[0]["description"] = chr(0x1F600) * (1024 * 1024 + 1)
    else:
        payload[0]["metadata"] = {"x" * (4 * 1024 * 1024 + 1): "synthetic"}

    class NeverMatch:
        def reconcile(self, *_args):
            raise AssertionError("Invalid payload reached matching")

    before = state(ledger)
    with pytest.raises(ValueError):
        LocalReconciliationService(ledger, reconciler=NeverMatch()).run(payload)
    assert state(ledger) == before


def test_caller_mutation_cannot_change_the_captured_request(ledger):
    payload = [transaction(metadata={"reference": "original"})]

    class ChangingCaller:
        def reconcile(self, captured, documents):
            payload[0]["amount"] = -99
            payload[0]["metadata"]["reference"] = "changed"
            return AutomatedReconciliation({}).reconcile(captured, documents)

    result = LocalReconciliationService(ledger, reconciler=ChangingCaller()).run(payload)
    match = ledger.get_reconciliation_match(result["results"][0]["reconciliationMatchId"])
    assert match["metadata"]["bankTransaction"]["amount"] == -4.28
    assert match["metadata"]["bankTransaction"]["metadata"]["reference"] == "original"


def test_reconciler_mutation_of_captured_input_is_rejected_atomically(ledger):
    class ChangingCaptured:
        def reconcile(self, captured, documents):
            captured[0]["amount"] = -99
            return AutomatedReconciliation({}).reconcile(captured, documents)

    before = state(ledger)
    with pytest.raises(ValueError):
        LocalReconciliationService(ledger, reconciler=ChangingCaptured()).run([transaction()])
    assert state(ledger) == before


@pytest.mark.parametrize("body", ['{broken', '[]', '{"bankTransactions":null}',
                               '{"bankTransactions":false}', '{"bankTransactions":[{"amount":NaN}]}'])
def test_json_api_rejects_invalid_body_instead_of_running_default_pool(ledger, body):
    app = create_app({"fab_local_ledger_path": ledger.path})
    response = app.test_client().post("/api/reconciliation/run", data=body, content_type="application/json")
    assert response.status_code == 400
    assert not ledger.list_reconciliation_matches()


def test_form_reports_bad_transaction_without_creating_review(ledger):
    app = create_app({"fab_local_ledger_path": ledger.path})
    with app.test_client() as client:
        response = client.post("/reconciliation/run", data={"bankTransactionsJson": json.dumps([transaction(amount=True)])})
        assert response.status_code == 302
        with client.session_transaction() as session:
            assert "error" in session["fab_last_reconciliation_summary"]
    assert not ledger.list_review_items()


def test_exact_batch_boundary_is_accepted_without_truncating(ledger):
    class Observed:
        def reconcile(self, captured, _documents):
            assert len(captured) == 500
            assert captured[-1]["id"] == "synthetic-499"
            return []

    result = LocalReconciliationService(ledger, reconciler=Observed()).run([
        transaction(id=f"synthetic-{index}") for index in range(500)
    ])
    assert result["requestedTransactions"] == 500
