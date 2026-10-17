import sqlite3
import unittest

from src.operations.local_ledger import LocalOperationsLedger


class TestLocalLedgerSqlIdentifiers(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            "CREATE TABLE ledger_rows (id INTEGER PRIMARY KEY, status TEXT, note TEXT)"
        )

    def tearDown(self):
        self.connection.close()

    def test_insert_update_count_and_qualified_status_filter(self):
        row_id = LocalOperationsLedger._insert_with_connection(
            self.connection,
            "ledger_rows",
            {"status": "pending", "note": "original"},
        )
        LocalOperationsLedger._update_with_connection(
            self.connection,
            "ledger_rows",
            row_id,
            {"note": "updated"},
        )
        self.assertEqual(LocalOperationsLedger._count(self.connection, "ledger_rows"), 1)

        where = []
        params = []
        LocalOperationsLedger._append_status_filter(where, params, "r.status", ["pending"])
        result = self.connection.execute(
            f"SELECT note FROM ledger_rows AS r WHERE {where[0]}", params
        ).fetchone()

        self.assertEqual(result["note"], "updated")

    def test_insert_update_count_and_migration_reject_sql_fragments(self):
        attacks = (
            "ledger_rows; DROP TABLE ledger_rows;--",
            "status = 'pending' OR 1=1",
            "status) VALUES ('injected');--",
        )
        for identifier in attacks:
            with self.subTest(identifier=identifier):
                with self.assertRaises(ValueError):
                    LocalOperationsLedger._insert_with_connection(
                        self.connection, identifier, {"status": "pending"}
                    )
                with self.assertRaises(ValueError):
                    LocalOperationsLedger._insert_with_connection(
                        self.connection, "ledger_rows", {identifier: "pending"}
                    )
                with self.assertRaises(ValueError):
                    LocalOperationsLedger._update_with_connection(
                        self.connection, "ledger_rows", 1, {identifier: "pending"}
                    )
                with self.assertRaises(ValueError):
                    LocalOperationsLedger._count(self.connection, identifier)
                with self.assertRaises(ValueError):
                    LocalOperationsLedger._ensure_column(
                        self.connection, "ledger_rows", identifier, "TEXT"
                    )
                with self.assertRaises(ValueError):
                    LocalOperationsLedger._append_status_filter([], [], identifier, "pending")

        self.assertEqual(LocalOperationsLedger._count(self.connection, "ledger_rows"), 0)
        self.assertIsNotNone(
            self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'ledger_rows'"
            ).fetchone()
        )

    def test_insert_requires_at_least_one_non_null_field(self):
        with self.assertRaises(ValueError):
            LocalOperationsLedger._insert_with_connection(
                self.connection, "ledger_rows", {"status": None}
            )


if __name__ == "__main__":
    unittest.main()
