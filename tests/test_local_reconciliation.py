import os
import tempfile
import unittest

from src.operations.local_bank_transactions import LocalBankTransactionImportService
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_reconciliation import LocalReconciliationService


class TestLocalReconciliationService(unittest.TestCase):
    def test_run_audit_event_records_actor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))

            LocalReconciliationService(ledger).run([], actor="fab_hai_api:hai")

            event = ledger.list_audit_events()[0]
            self.assertEqual(event["action"], "local_reconciliation.run_completed")
            self.assertEqual(event["details"]["actor"], "fab_hai_api:hai")

    def test_run_records_candidate_and_review_item(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "receipt-1",
                "originalFilename": "receipt.txt",
                "processingStatus": "processed",
                "vendorName": "Office Shop",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
            })

            summary = LocalReconciliationService(ledger, {
                "reconciliation_match_threshold": 0.9,
            }).run([
                {
                    "id": "tx-1",
                    "date": "2026-06-28",
                    "amount": -42.5,
                    "description": "Office Shop",
                }
            ])

            self.assertEqual(summary["matchedCandidates"], 1)
            self.assertEqual(summary["matchesRecorded"], 1)
            self.assertEqual(summary["reviewItemsCreated"], 1)
            match = ledger.list_reconciliation_matches(document_id=document_id)[0]
            self.assertEqual(match["status"], "candidate")
            self.assertEqual(match["bank_transaction_id"], "tx-1")
            document = ledger.get_document(document_id)
            self.assertEqual(document["reconciliation_status"], "candidate")
            self.assertEqual(document["bookkeeping_record"]["status"], "needs_review")
            self.assertEqual(document["bookkeeping_record"]["reconciliation_status"], "candidate")
            self.assertEqual(ledger.list_review_items(status="pending")[0]["reason"], "reconciliation_candidate")
            self.assertEqual(ledger.list_audit_events()[0]["action"], "local_reconciliation.run_completed")

    def test_missing_receipt_is_recorded_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalReconciliationService(ledger, {})
            transactions = [{"id": "tx-missing", "date": "2026-06-28", "amount": -12.0, "description": "Unknown"}]

            first = service.run(transactions)
            second = service.run(transactions)

            self.assertEqual(first["missingReceipts"], 1)
            self.assertEqual(first["matchesRecorded"], 1)
            self.assertEqual(second["missingReceipts"], 1)
            self.assertEqual(second["matchesRecorded"], 0)
            matches = ledger.list_reconciliation_matches(bank_transaction_id="tx-missing")
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]["status"], "missing_receipt")
            self.assertEqual(ledger.list_review_items(status="pending")[0]["reason"], "missing_receipt")

    def test_resolve_match_marks_document_reconciled_without_changing_processing_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "receipt-2",
                "originalFilename": "receipt.txt",
                "processingStatus": "processed",
                "vendorName": "Office Shop",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
            })
            service = LocalReconciliationService(ledger, {})
            summary = service.run([{
                "id": "tx-2",
                "date": "2026-06-28",
                "amount": -42.5,
                "description": "Office Shop",
            }])
            match_id = summary["results"][0]["reconciliationMatchId"]

            result = service.resolve_match(match_id, "approved", "Confirmed bank match.")
            document = ledger.get_document(document_id)

            self.assertTrue(result["success"])
            self.assertEqual(ledger.get_reconciliation_match(match_id)["status"], "approved")
            self.assertEqual(document["processing_status"], "processed")
            self.assertEqual(document["reconciliation_status"], "reconciled")
            self.assertEqual(document["bookkeeping_record"]["reconciliation_status"], "reconciled")
            self.assertEqual(ledger.list_review_items()[0]["status"], "resolved")

    def test_persisted_bank_transaction_status_tracks_reconciliation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "receipt-persisted-bank",
                "originalFilename": "receipt.txt",
                "processingStatus": "processed",
                "vendorName": "Office Shop",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
            })
            bank_service = LocalBankTransactionImportService(ledger, {})
            bank_service.import_transactions([{
                "id": "tx-persisted-1",
                "date": "2026-06-28",
                "amount": -42.5,
                "description": "Office Shop",
            }])
            transaction = ledger.list_bank_transactions()[0]
            reconciliation_service = LocalReconciliationService(ledger, {})

            summary = reconciliation_service.run(bank_service.transactions_for_reconciliation())
            match_id = summary["results"][0]["reconciliationMatchId"]
            candidate_transaction = ledger.get_bank_transaction(transaction["id"])
            result = reconciliation_service.resolve_match(match_id, "approved", "Confirmed.")
            resolved_transaction = ledger.get_bank_transaction(transaction["id"])

            self.assertEqual(candidate_transaction["reconciliation_status"], "candidate")
            self.assertTrue(result["success"])
            self.assertEqual(resolved_transaction["reconciliation_status"], "reconciled")
            self.assertEqual(resolved_transaction["metadata"]["latestReconciliation"]["documentId"], document_id)
            document = ledger.get_document(document_id)
            self.assertEqual(document["bookkeeping_record"]["reconciliation_status"], "reconciled")
            bank_record = ledger.get_bookkeeping_record_by_bank_transaction(transaction["id"])
            self.assertEqual(bank_record["reconciliation_status"], "reconciled")


if __name__ == "__main__":
    unittest.main()
