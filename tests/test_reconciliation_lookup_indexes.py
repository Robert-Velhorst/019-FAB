import pytest

from src.operations.local_ledger import LocalOperationsLedger


def trace_query(ledger, monkeypatch, operation):
    original = ledger._connect
    queries = []

    def connect():
        connection = original()
        connection.set_trace_callback(lambda sql: queries.append(sql) if sql.startswith("SELECT * FROM") else None)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_connect", connect)
        result = operation()
    with ledger._connection() as connection:
        plans = [row[3] for sql in queries for row in connection.execute("EXPLAIN QUERY PLAN " + sql)]
    return result, plans


@pytest.mark.parametrize("kind,index", [
    ("review", "idx_local_missing_review_match"),
    ("bank", "idx_local_reconciliation_bank_record"),
    ("account", "idx_local_reconciliation_ad_hoc_account"),
])
def test_actual_lookup_query_uses_its_expression_index(tmp_path, monkeypatch, kind, index):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    if kind == "review":
        operation = lambda: ledger.list_missing_receipt_review_items(reconciliation_match_id=1)
    elif kind == "bank":
        operation = lambda: ledger.list_reconciliation_matches(
            bank_transaction_id="synthetic-bank", bank_transaction_record_id=1,
            documentless_only=True, keyset_order=True,
        )
    else:
        operation = lambda: ledger.list_reconciliation_matches(
            bank_transaction_id="synthetic-bank", ad_hoc_bank_only=True,
            ad_hoc_account_identifier="synthetic-account", documentless_only=True,
        )
    _result, plans = trace_query(ledger, monkeypatch, operation)
    assert any("SEARCH" in item and index in item for item in plans), plans
    assert not any("TEMP B-TREE" in item for item in plans), plans


def rows(ledger):
    with ledger._connection() as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return {row[0]: [tuple(item) for item in connection.execute(
            'SELECT * FROM "' + row[0].replace('"', '""') + '" ORDER BY rowid'
        )] for row in tables}


def test_reopening_installs_indexes_without_changing_ledger_rows(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    ledger.create_review_item({"reason": "missing_receipt", "correctedData": {"reconciliationMatchId": 1}})
    with ledger._connection() as connection:
        connection.execute("DROP INDEX IF EXISTS idx_local_missing_review_match")
        connection.execute("DROP INDEX IF EXISTS idx_local_reconciliation_bank_record")
        connection.execute("DROP INDEX IF EXISTS idx_local_reconciliation_ad_hoc_account")
        # Corrupt legacy JSON must remain inspectable, not break creation of a derived index.
        connection.execute("INSERT INTO review_items (reason, status, corrected_data_json, created_at, updated_at) "
                           "VALUES ('missing_receipt', 'pending', '{broken', 'synthetic', 'synthetic')")
        connection.execute("INSERT INTO reconciliation_matches (bank_transaction_id, status, metadata_json, created_at) "
                           "VALUES ('synthetic-corrupt', 'missing_receipt', '{broken', 'synthetic')")
    before = rows(ledger)
    reopened = LocalOperationsLedger(ledger.path)
    assert rows(reopened) == before
    with reopened._connection() as connection:
        indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"idx_local_missing_review_match", "idx_local_reconciliation_bank_record",
                "idx_local_reconciliation_ad_hoc_account"} <= indexes
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def count_work(ledger, monkeypatch, operation):
    original = ledger._connect
    steps = 0

    def progress():
        nonlocal steps
        steps += 1
        return 0

    def connect():
        connection = original()
        connection.set_progress_handler(progress, 1)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_connect", connect)
        result = operation()
    return result, steps


def test_index_reduces_actual_lookup_database_work_on_synthetic_backlog(tmp_path, monkeypatch):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    with ledger.write_transaction():
        for index in range(2001):
            ledger.create_review_item({"reason": "missing_receipt", "correctedData": {"reconciliationMatchId": index + 1}})
    operation = lambda: ledger.list_missing_receipt_review_items(reconciliation_match_id=1)
    with ledger._connection() as connection:
        connection.execute("DROP INDEX IF EXISTS idx_local_missing_review_match")
    baseline, baseline_steps = count_work(ledger, monkeypatch, operation)
    indexed = LocalOperationsLedger(ledger.path)
    result, indexed_steps = count_work(indexed, monkeypatch, lambda: indexed.list_missing_receipt_review_items(reconciliation_match_id=1))
    assert result == baseline
    assert indexed_steps < baseline_steps // 10, (baseline_steps, indexed_steps)
    print(f"Synthetic missing-review lookup SQLite steps: {baseline_steps} -> {indexed_steps}")
