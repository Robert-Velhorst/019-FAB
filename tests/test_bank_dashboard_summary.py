import os
import tempfile
import unittest
from unittest.mock import patch

from src.operations.local_api import create_app
from src.operations.local_ledger import LocalOperationsLedger


class TestBankDashboardSummary(unittest.TestCase):
    def test_empty_summary_and_missing_or_overflowed_amounts(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalOperationsLedger(os.path.join(directory, "ledger.sqlite3"))
            self.assertEqual(ledger.bank_transaction_summary(), {
                "complete": True, "scope": "all_matching_transactions",
                "transactionCount": 0, "unreconciledCount": 0,
                "unreconciledAmountByCurrency": {},
            })
            ledger.upsert_bank_transaction({"transactionId": "unknown", "accountIdentifier": "missing"})
            ledger.upsert_bank_transaction({"transactionId": "large-1", "accountIdentifier": "overflow", "amount": 1e308})
            ledger.upsert_bank_transaction({"transactionId": "large-2", "accountIdentifier": "overflow", "amount": 1e308})
            for account in ("missing", "overflow"):
                with self.subTest(account=account):
                    summary = ledger.bank_transaction_summary(account_identifier=account)
                    self.assertFalse(summary["complete"])
                    self.assertEqual(summary["unreconciledAmountByCurrency"], {})

    def test_summary_covers_all_transactions_without_loading_private_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ledger.sqlite3")
            app = create_app({"fab_local_ledger_path": path})
            ledger = LocalOperationsLedger(path)
            with ledger.write_transaction():
                for index in range(300):
                    ledger.upsert_bank_transaction({
                        "transactionId": f"old-{index}", "accountIdentifier": "business",
                        "amount": -0.1, "currency": "eur", "date": "2020-01-01",
                        "description": "PRIVATE-TRANSACTION-DETAIL",
                    })
                for state in ("approved", "reconciled", "ignored"):
                    ledger.upsert_bank_transaction({
                        "transactionId": state, "accountIdentifier": "business",
                        "amount": 999, "reconciliationStatus": state,
                    })
                ledger.upsert_bank_transaction({
                    "transactionId": "usd", "accountIdentifier": "business",
                    "amount": 2.05, "currency": "USD",
                })
                ledger.upsert_bank_transaction({
                    "transactionId": "gbp", "accountIdentifier": "personal",
                    "amount": -3.25, "currency": "GBP",
                })

            client = app.test_client()
            with patch.object(LocalOperationsLedger, "list_bank_transactions", side_effect=AssertionError("detail rows loaded")):
                response = client.get("/api/bank-transactions?view=summary&limit=1")
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["bankTransactions"], [])
            self.assertEqual(payload["summary"], {
                "complete": True, "scope": "all_matching_transactions",
                "transactionCount": 305, "unreconciledCount": 302,
                "unreconciledAmountByCurrency": {"EUR": 30.0, "USD": 2.05, "GBP": 3.25},
            })
            self.assertNotIn("PRIVATE-TRANSACTION-DETAIL", response.get_data(as_text=True))

            filtered = client.get(
                "/api/bank-transactions?view=summary&accountIdentifier=business&reconciliationStatus=approved",
            ).get_json()["summary"]
            self.assertEqual(filtered["transactionCount"], 1)
            self.assertEqual(filtered["unreconciledCount"], 0)
            self.assertEqual(filtered["unreconciledAmountByCurrency"], {})
            details = client.get("/api/bank-transactions?limit=2").get_json()
            self.assertEqual(len(details["bankTransactions"]), 2)

            batch = client.get("/api/control-center/resources").get_json()
            bank = batch["resources"]["bankTransactions"]
            self.assertTrue(bank["ok"])
            self.assertEqual(bank["value"]["summary"], payload["summary"])
            self.assertEqual(bank["value"]["bankTransactions"], [])


if __name__ == "__main__":
    unittest.main()
