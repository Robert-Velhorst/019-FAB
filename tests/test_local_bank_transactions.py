import os
import tempfile
import unittest
from unittest.mock import patch

from src.operations.local_bank_transactions import (
    MAX_BANK_STATEMENT_BYTES,
    LocalBankTransactionImportService,
)
from src.operations.local_ledger import LocalOperationsLedger


class TestLocalBankTransactionImportService(unittest.TestCase):
    def test_import_transactions_is_idempotent_and_reconciliation_ready(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalBankTransactionImportService(ledger, {})

            first = service.import_transactions([
                {
                    "id": "tx-json-1",
                    "date": "2026-06-28",
                    "amount": "-42.50",
                    "description": "Office Shop",
                    "counterparty": "Office Shop",
                }
            ], account_identifier="wave-checking", source="wave_report", filename="account-transactions.json")
            second = service.import_transactions([
                {
                    "id": "tx-json-1",
                    "date": "2026-06-28",
                    "amount": "-42.50",
                    "description": "Office Shop",
                    "counterparty": "Office Shop",
                }
            ], account_identifier="wave-checking", source="wave_report", filename="account-transactions.json")

            transactions = ledger.list_bank_transactions(account_identifier="wave-checking")
            reconciliation_batch = service.transactions_for_reconciliation()

            self.assertEqual(first["rowsImported"], 1)
            self.assertEqual(first["duplicates"], 0)
            self.assertEqual(second["rowsImported"], 0)
            self.assertEqual(second["duplicates"], 1)
            self.assertEqual(len(transactions), 1)
            self.assertEqual(transactions[0]["transaction_id"], "tx-json-1")
            self.assertEqual(transactions[0]["amount"], -42.5)
            self.assertEqual(reconciliation_batch[0]["id"], "tx-json-1")
            self.assertEqual(reconciliation_batch[0]["ledgerBankTransactionId"], transactions[0]["id"])
            self.assertEqual(ledger.list_audit_events()[0]["action"], "local_bank_transactions.import_completed")

    def test_import_csv_statement_maps_debit_credit_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalBankTransactionImportService(ledger, {})

            result = service.import_statement_text(
                "Date;Description;Debit;Credit;Name;Reference\n"
                "28-06-2026;Office Shop;42,50;;Office Shop;csv-1\n"
                "29-06-2026;Client payment;;100,00;Client BV;csv-2\n",
                format="csv",
                account_identifier="nl-bank",
                source="bank_csv",
            )

            transactions = ledger.list_bank_transactions(account_identifier="nl-bank", limit=10)
            by_id = {transaction["transaction_id"]: transaction for transaction in transactions}

            self.assertEqual(result["rowsImported"], 2)
            self.assertEqual(by_id["csv-1"]["transaction_date"], "2026-06-28")
            self.assertEqual(by_id["csv-1"]["amount"], -42.5)
            self.assertEqual(by_id["csv-2"]["amount"], 100.0)

    def test_exact_reimport_preserves_final_reconciliation_and_original_provenance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalBankTransactionImportService(ledger, {})
            row = {
                "id": "stable-bank-id",
                "date": "2026-06-28",
                "amount": "-42.50",
                "currency": "EUR",
                "description": "Office supplies",
                "counterparty": "Office Shop",
            }
            first = service.import_transactions([row], account_identifier="checking")
            transaction = ledger.list_bank_transactions(account_identifier="checking")[0]
            ledger.update_bank_transaction(transaction["id"], {
                "status": "approved",
                "reconciliationStatus": "reconciled",
            })

            second = service.import_transactions([row], account_identifier="checking")
            preserved = ledger.get_bank_transaction(transaction["id"])

            self.assertEqual(second["duplicates"], 1)
            self.assertEqual(second["rowsImported"], 0)
            self.assertEqual(preserved["status"], "approved")
            self.assertEqual(preserved["reconciliation_status"], "reconciled")
            self.assertEqual(preserved["import_id"], first["bankStatementImportId"])

    def test_changed_facts_under_existing_bank_identity_are_quarantined(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalBankTransactionImportService(ledger, {})
            service.import_transactions([{
                "id": "stable-bank-id",
                "date": "2026-06-28",
                "amount": "-42.50",
                "currency": "EUR",
                "description": "Office supplies",
                "counterparty": "Office Shop",
            }], account_identifier="checking")
            existing = ledger.list_bank_transactions(account_identifier="checking")[0]
            ledger.update_bank_transaction(existing["id"], {
                "status": "approved",
                "reconciliationStatus": "reconciled",
            })

            result = service.import_transactions([{
                "id": "stable-bank-id",
                "date": "2026-06-28",
                "amount": "-99.00",
                "currency": "EUR",
                "description": "Office supplies",
                "counterparty": "Office Shop",
            }], account_identifier="checking")
            preserved = ledger.get_bank_transaction(existing["id"])
            import_record = ledger.list_bank_statement_imports(account_identifier="checking")[0]

            self.assertEqual(result["status"], "needs_review")
            self.assertEqual(result["identityConflicts"], 1)
            self.assertEqual(result["skipped"], 1)
            self.assertEqual(result["identityConflictRows"], [{
                "row": 1,
                "transactionId": "stable-bank-id",
                "bankTransactionId": existing["id"],
            }])
            self.assertEqual(preserved["amount"], -42.5)
            self.assertEqual(preserved["status"], "approved")
            self.assertEqual(preserved["reconciliation_status"], "reconciled")
            self.assertEqual(import_record["status"], "needs_review")
            self.assertEqual(
                ledger.list_audit_events(limit=1)[0]["action"],
                "local_bank_transactions.import_needs_review",
            )

    def test_import_camt_statement_normalizes_debit_entry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalBankTransactionImportService(ledger, {})

            result = service.import_statement_text(
                """
                <Document>
                  <BkToCstmrStmt>
                    <Stmt>
                      <Ntry>
                        <Amt Ccy="EUR">42.50</Amt>
                        <CdtDbtInd>DBIT</CdtDbtInd>
                        <BookgDt><Dt>2026-06-28</Dt></BookgDt>
                        <AcctSvcrRef>camt-1</AcctSvcrRef>
                        <NtryDtls><TxDtls><RltdPties><Cdtr><Nm>Office Shop</Nm></Cdtr></RltdPties><RmtInf><Ustrd>Office supplies</Ustrd></RmtInf></TxDtls></NtryDtls>
                      </Ntry>
                    </Stmt>
                  </BkToCstmrStmt>
                </Document>
                """,
                format="camt",
                account_identifier="camt-account",
            )

            transaction = ledger.list_bank_transactions(account_identifier="camt-account")[0]

            self.assertEqual(result["rowsImported"], 1)
            self.assertEqual(transaction["transaction_id"], "camt-1")
            self.assertEqual(transaction["amount"], -42.5)
            self.assertEqual(transaction["counterparty"], "Office Shop")

    def test_import_mt940_statement_extracts_amount_date_and_description(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalBankTransactionImportService(ledger, {})

            result = service.import_statement_text(
                ":20:START\n"
                ":61:2606280628D42,50NTRFNONREF\n"
                ":86:Office Shop payment\n",
                format="mt940",
                account_identifier="mt940-account",
            )

            transaction = ledger.list_bank_transactions(account_identifier="mt940-account")[0]

            self.assertEqual(result["rowsImported"], 1)
            self.assertEqual(transaction["transaction_date"], "2026-06-28")
            self.assertEqual(transaction["amount"], -42.5)
            self.assertIn("Office Shop", transaction["description"])

    def test_generated_ids_preserve_identical_legitimate_rows_and_remain_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalBankTransactionImportService(ledger, {})
            rows = [
                {
                    "date": "2026-06-28",
                    "amount": "-12.50",
                    "description": "Transit fare",
                    "counterparty": "Transit BV",
                },
                {
                    "date": "2026-06-28",
                    "amount": "-12.50",
                    "description": "Transit fare",
                    "counterparty": "Transit BV",
                },
            ]

            first = service.import_transactions(rows, account_identifier="checking")
            second = service.import_transactions(rows, account_identifier="checking")
            transactions = ledger.list_bank_transactions(account_identifier="checking")

            self.assertEqual(first["rowsImported"], 2)
            self.assertEqual(first["duplicates"], 0)
            self.assertEqual(second["rowsImported"], 0)
            self.assertEqual(second["duplicates"], 2)
            self.assertEqual(len(transactions), 2)
            generated_ids = sorted(item["transaction_id"] for item in transactions)
            self.assertTrue(generated_ids[0].startswith("generated:"))
            self.assertEqual(generated_ids[1], f"{generated_ids[0]}:2")

    def test_empty_and_invalid_rows_are_not_reported_as_completed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalBankTransactionImportService(ledger, {})

            empty = service.import_transactions([], actor="fab_dashboard:4")
            invalid = service.import_transactions([
                {"date": "not-a-date", "amount": "-20", "description": "Invalid date"},
                {"date": "2026-06-28", "description": "Missing amount"},
            ])

            self.assertEqual(empty["status"], "empty")
            self.assertEqual(empty["rowsImported"], 0)
            self.assertEqual(invalid["status"], "empty")
            self.assertEqual(invalid["skipped"], 2)
            self.assertEqual(ledger.list_bank_transactions(), [])
            self.assertEqual(
                ledger.list_audit_events(limit=10)[-1]["details"]["actor"],
                "fab_dashboard:4",
            )

    def test_unexpected_row_failure_rolls_back_entire_import_batch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalBankTransactionImportService(ledger, {})
            original_upsert = ledger.upsert_bank_transaction
            calls = 0

            def fail_on_second_row(payload):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("simulated database write failure")
                return original_upsert(payload)

            with patch.object(ledger, "upsert_bank_transaction", side_effect=fail_on_second_row):
                with self.assertRaisesRegex(RuntimeError, "simulated database write failure"):
                    service.import_transactions([
                        {"id": "tx-1", "date": "2026-06-28", "amount": -10},
                        {"id": "tx-2", "date": "2026-06-29", "amount": -20},
                    ], account_identifier="checking")

            self.assertEqual(ledger.list_bank_transactions(account_identifier="checking"), [])
            self.assertEqual(ledger.list_bank_statement_imports(account_identifier="checking"), [])
            self.assertEqual(ledger.list_audit_events(), [])

    def test_statement_bytes_support_windows_bank_exports_and_reject_binary_or_oversized_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalBankTransactionImportService(ledger, {})

            result = service.import_statement_bytes(
                (
                    "Datum;Omschrijving;Bedrag;Valuta\n"
                    "28-06-2026;Café aankoop;-4,25;EUR\n"
                ).encode("cp1252"),
                format="csv",
                filename="bank.csv",
            )

            self.assertEqual(result["rowsImported"], 1)
            self.assertEqual(ledger.list_bank_transactions()[0]["description"], "Café aankoop")
            with self.assertRaisesRegex(ValueError, "text-based"):
                service.import_statement_bytes(b"date\x00amount", format="csv")
            with self.assertRaisesRegex(ValueError, "text-based"):
                service.import_statement_bytes(b"date,amount\n\x01,-20", format="csv")
            with self.assertRaisesRegex(ValueError, "transaction date column"):
                service.import_statement_bytes(b"description,amount\nLunch,-20", format="csv")
            with self.assertRaisesRegex(ValueError, "amount, debit, or credit column"):
                service.import_statement_bytes(b"date,description\n2026-06-28,Lunch", format="csv")
            with self.assertRaisesRegex(ValueError, "import limit"):
                service.import_statement_bytes(b"x" * (MAX_BANK_STATEMENT_BYTES + 1), format="csv")

    def test_malformed_camt_returns_a_validation_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalBankTransactionImportService(ledger, {})

            with self.assertRaisesRegex(ValueError, "not valid XML"):
                service.import_statement_text("<Document>", format="camt")


if __name__ == "__main__":
    unittest.main()
