import os
import tempfile
import unittest

from src.operations.local_bookkeeping_records import LocalBookkeepingRecordService
from src.operations.local_ledger import LocalOperationsLedger


class TestLocalBookkeepingRecordService(unittest.TestCase):
    def test_current_document_block_overrides_old_prepared_route(self):
        for processing_status, expected in (
            ("failed", "blocked_processing"),
            ("duplicate", "blocked_duplicate"),
            ("needs_review", "blocked_by_review"),
        ):
            with self.subTest(status=processing_status), tempfile.TemporaryDirectory() as temp_dir:
                ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
                document_id = ledger.register_document({
                    "source": "scanner", "sourceDocumentId": "old-draft",
                    "processingStatus": "export_draft_prepared",
                    "vendorName": "Office Shop", "category": "Office Supplies",
                    "transactionDate": "2026-06-28", "totalAmount": 42.5,
                })
                route_id = ledger.create_routing_attempt({
                    "documentId": document_id, "target": "waveapps_business",
                    "status": "draft_prepared",
                })
                service = LocalBookkeepingRecordService(ledger)
                service.upsert_from_document(document_id)
                ledger.update_document(document_id, {"processingStatus": processing_status})

                result = service.upsert_from_document(document_id)
                record = ledger.get_bookkeeping_record(result["recordId"])

                self.assertEqual(record["export_status"], expected)
                self.assertTrue(record["review_required"])
                self.assertFalse(record["metadata"]["exportReadiness"]["readyForWaveDraft"])
                self.assertEqual(ledger.get_routing_attempt(route_id)["status"], "draft_prepared")
                self.assertEqual(record["amount"], 42.5)

    def test_upsert_from_document_creates_export_ready_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-record-ready",
                "originalFilename": "receipt.txt",
                "documentType": "receipt",
                "processingStatus": "processed",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
                "vatAmount": 7.38,
                "confidenceScore": 0.94,
                "extractedData": {
                    "currency": "EUR",
                    "description": "Printer paper",
                    "line_items": [
                        {
                            "description": "Paper",
                            "quantity": 2,
                            "unit_price": 17.56,
                            "amount": 35.12,
                            "vat_amount": 7.38,
                            "tax_code": "BTW 21%",
                            "account_name": "Office Supplies",
                        }
                    ],
                },
                "metadata": {
                    "targetSystem": "waveapps_business",
                    "targetAccount": "Office Supplies",
                },
            })
            service = LocalBookkeepingRecordService(ledger, {})

            first = service.upsert_from_document(document_id)
            second = service.upsert_from_document(document_id)

            self.assertTrue(first["success"])
            self.assertEqual(first["recordId"], second["recordId"])
            record = ledger.get_bookkeeping_record(first["recordId"])
            self.assertEqual(record["status"], "ready_to_route")
            self.assertEqual(record["export_status"], "ready")
            self.assertEqual(record["target_system"], "waveapps_business")
            self.assertEqual(record["vendor_name"], "Office Shop")
            self.assertEqual(record["amount"], 42.5)
            self.assertEqual(record["review_required"], 0)
            self.assertEqual(record["line_item_count"], 1)
            self.assertEqual(record["line_items"][0]["description"], "Paper")
            self.assertEqual(record["line_items"][0]["account_name"], "Office Supplies")
            self.assertEqual(record["line_items"][0]["tax_code"], "BTW 21%")
            self.assertEqual(record["metadata"]["lineItemCount"], 1)
            self.assertTrue(record["metadata"]["exportReadiness"]["readyForWaveDraft"])
            self.assertEqual(ledger.dashboard_metrics()["export_ready_records"], 1)
            self.assertEqual(ledger.dashboard_metrics()["bookkeeping_record_line_items"], 1)

    def test_missing_document_fields_create_review_required_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-record-review",
                "originalFilename": "unknown.txt",
                "processingStatus": "processed",
                "category": "Manual Review",
                "confidenceScore": 0.2,
            })

            result = LocalBookkeepingRecordService(ledger, {}).upsert_from_document(document_id)
            record = ledger.get_bookkeeping_record(result["recordId"])

            self.assertEqual(record["status"], "needs_review")
            self.assertEqual(record["review_required"], 1)
            self.assertEqual(record["export_status"], "blocked_by_review")
            self.assertEqual(record["line_item_count"], 1)
            self.assertEqual(record["line_items"][0]["source"], "document_total")
            self.assertIn("vendorName", record["metadata"]["missingFields"])
            self.assertIn("amount", record["metadata"]["missingFields"])

    def test_extractor_total_alias_becomes_a_reconciled_line_amount(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-total-alias",
                "originalFilename": "receipt.pdf",
                "documentType": "receipt",
                "processingStatus": "processed",
                "vendorName": "Praxis",
                "category": "Construction Materials & Tools",
                "transactionDate": "2026-06-28",
                "totalAmount": 25.10,
                "extractedData": {
                    "currency": "EUR",
                    "line_items": [{"description": "Hardware", "total": 25.10}],
                },
                "metadata": {
                    "targetSystem": "waveapps_business",
                    "targetAccount": "Construction Materials & Tools",
                },
            })

            result = LocalBookkeepingRecordService(ledger, {}).upsert_from_document(document_id)
            record = ledger.get_bookkeeping_record(result["recordId"])

            self.assertEqual(record["line_item_count"], 1)
            self.assertEqual(record["line_items"][0]["amount"], 25.10)
            self.assertEqual(record["line_items"][0]["source"], "extracted_line_item")

    def test_mismatched_extracted_lines_fall_back_to_one_document_total(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-mismatched-lines",
                "originalFilename": "receipt.pdf",
                "documentType": "receipt",
                "processingStatus": "processed",
                "vendorName": "Praxis",
                "category": "Construction Materials & Tools",
                "transactionDate": "2026-06-28",
                "totalAmount": 25.10,
                "extractedData": {
                    "currency": "EUR",
                    "line_items": [
                        {"description": "OCR gross column", "total": 28.10},
                        {"description": "OCR VAT column", "total": 4.36},
                    ],
                },
                "metadata": {
                    "targetSystem": "waveapps_business",
                    "targetAccount": "Construction Materials & Tools",
                },
            })

            result = LocalBookkeepingRecordService(ledger, {}).upsert_from_document(document_id)
            record = ledger.get_bookkeeping_record(result["recordId"])
            line_item = record["line_items"][0]

            self.assertEqual(record["line_item_count"], 1)
            self.assertEqual(line_item["amount"], 25.10)
            self.assertEqual(line_item["source"], "document_total")
            self.assertEqual(
                line_item["metadata"]["fallbackReason"],
                "extracted_line_total_mismatch",
            )
            self.assertEqual(line_item["metadata"]["evidenceLineItemCount"], 2)

    def test_credit_note_lines_follow_negative_ledger_direction(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "credit-note-lines",
                "originalFilename": "credit-note.pdf",
                "documentType": "credit_note",
                "processingStatus": "reviewed",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.50,
                "vatAmount": 7.38,
                "extractedData": {
                    "document_type": "credit_note",
                    "currency": "EUR",
                    "line_items": [{
                        "description": "Returned paper",
                        "amount": 35.12,
                        "taxAmount": 7.38,
                    }],
                },
            })

            result = LocalBookkeepingRecordService(ledger, {}).upsert_from_document(document_id)
            record = ledger.get_bookkeeping_record(result["recordId"])
            line_item = record["line_items"][0]

            self.assertEqual(record["amount"], -42.5)
            self.assertEqual(record["vat_amount"], -7.38)
            self.assertEqual(line_item["amount"], -35.12)
            self.assertEqual(line_item["tax_amount"], -7.38)
            self.assertTrue(line_item["metadata"]["postingDirectionNormalized"])

    def test_impossible_legacy_vat_is_suppressed_but_preserved_as_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "legacy-btw-number-tax",
                "originalFilename": "receipt.pdf",
                "documentType": "receipt",
                "processingStatus": "processed",
                "vendorName": "Hornbach",
                "category": "Tools",
                "transactionDate": "2026-06-28",
                "totalAmount": 59.6,
                "vatAmount": 8075.08,
                "confidenceScore": 0.94,
            })
            service = LocalBookkeepingRecordService(ledger, {})
            initial_record_id = ledger.upsert_bookkeeping_record({
                "documentId": document_id,
                "sourceType": "document",
                "recordType": "expense",
                "status": "ready_to_route",
                "exportStatus": "ready",
                "amount": 59.6,
                "vatAmount": 8075.08,
                "reviewRequired": False,
            })

            first = service.upsert_from_document(document_id)
            second = service.upsert_from_document(document_id)

            self.assertEqual(first["recordId"], initial_record_id)
            self.assertEqual(first["financialFieldIssues"][0]["reason"], "vat_exceeds_total_ratio")
            record = ledger.get_bookkeeping_record(initial_record_id)
            self.assertEqual(record["status"], "needs_review")
            self.assertEqual(record["export_status"], "blocked_invalid_financial_fields")
            self.assertEqual(record["review_required"], 1)
            self.assertIsNone(record["vat_amount"])
            self.assertEqual(record["metadata"]["evidenceVatAmount"], 8075.08)
            self.assertEqual(record["line_items"][0]["tax_amount"], None)
            actions = [event["action"] for event in ledger.list_audit_events(limit=20)]
            self.assertEqual(
                actions.count("local_bookkeeping_records.invalid_financial_fields_suppressed"),
                1,
            )
            self.assertEqual(second["financialFieldIssues"][0]["reason"], "vat_exceeds_total_ratio")

    def test_dutch_record_date_is_normalized_without_losing_source_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "dutch-date",
                "originalFilename": "receipt.pdf",
                "documentType": "receipt",
                "processingStatus": "processed",
                "vendorName": "Example Shop",
                "category": "Office Supplies",
                "transactionDate": "21.06.23",
                "totalAmount": 12.5,
                "confidenceScore": 0.94,
            })

            result = LocalBookkeepingRecordService(ledger, {}).upsert_from_document(document_id)
            record = ledger.get_bookkeeping_record(result["recordId"])

            self.assertEqual(record["record_date"], "2023-06-21")
            self.assertEqual(record["metadata"]["evidenceRecordDate"], "21.06.23")
            self.assertEqual(record["metadata"]["financialFieldIssues"], [])

    def test_impossible_record_date_is_suppressed_and_blocks_export(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "future-date",
                "originalFilename": "receipt.pdf",
                "documentType": "receipt",
                "processingStatus": "processed",
                "vendorName": "Example Shop",
                "category": "Office Supplies",
                "transactionDate": "3038-06-10",
                "totalAmount": 12.5,
                "confidenceScore": 0.94,
            })
            initial_record_id = ledger.upsert_bookkeeping_record({
                "documentId": document_id,
                "sourceType": "document",
                "recordType": "expense",
                "status": "ready_to_route",
                "exportStatus": "ready",
                "recordDate": "3038-06-10",
                "amount": 12.5,
                "reviewRequired": False,
            })

            result = LocalBookkeepingRecordService(ledger, {}).upsert_from_document(document_id)
            record = ledger.get_bookkeeping_record(initial_record_id)

            self.assertEqual(result["financialFieldIssues"][0]["reason"], "implausible_record_date_year")
            self.assertIsNone(record["record_date"])
            self.assertEqual(record["status"], "needs_review")
            self.assertEqual(record["export_status"], "blocked_invalid_financial_fields")
            self.assertEqual(record["metadata"]["evidenceRecordDate"], "3038-06-10")

    def test_impossible_line_item_tax_is_suppressed_and_blocks_export(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "line-item-tax",
                "originalFilename": "receipt.pdf",
                "documentType": "receipt",
                "processingStatus": "processed",
                "vendorName": "Example Shop",
                "category": "Office Supplies",
                "transactionDate": "2026-06-28",
                "totalAmount": 20.0,
                "vatAmount": 2.0,
                "confidenceScore": 0.94,
                "extractedData": {
                    "line_items": [{
                        "description": "Paper",
                        "amount": 10.0,
                        "tax_amount": 50.0,
                    }],
                },
            })

            result = LocalBookkeepingRecordService(ledger, {}).upsert_from_document(document_id)
            record = ledger.get_bookkeeping_record(result["recordId"])

            self.assertEqual(
                result["financialFieldIssues"][0]["field"],
                "lineItems[0].taxAmount",
            )
            self.assertEqual(record["status"], "needs_review")
            self.assertEqual(record["export_status"], "blocked_invalid_financial_fields")
            self.assertIsNone(record["line_items"][0]["tax_amount"])
            self.assertEqual(
                record["line_items"][0]["metadata"]["financialFieldIssue"]["evidenceValue"],
                50.0,
            )

    def test_non_posting_document_becomes_supporting_evidence_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "policy-record",
                "originalFilename": "policy.pdf",
                "documentType": "receipt",
                "processingStatus": "reviewed",
                "vendorName": "Insurer",
                "category": "Insurance",
                "transactionDate": "2026-06-28",
                "totalAmount": 6100000,
            })
            service = LocalBookkeepingRecordService(ledger, {})
            initial = service.upsert_from_document(document_id)
            self.assertEqual(ledger.get_bookkeeping_record(initial["recordId"])["amount"], 6100000.0)
            ledger.update_document(document_id, {
                "documentType": "insurance_policy",
                "category": "Supporting Evidence",
            })

            result = service.upsert_from_document(document_id)
            record = ledger.get_bookkeeping_record(result["recordId"])

            self.assertEqual(record["record_type"], "supporting_document")
            self.assertEqual(record["status"], "supporting_evidence")
            self.assertEqual(record["export_status"], "not_applicable")
            self.assertIsNone(record["amount"])
            self.assertEqual(record["metadata"]["evidenceAmount"], 6100000.0)
            self.assertEqual(record["line_item_count"], 0)
            self.assertFalse(record["metadata"]["exportReadiness"]["readyForWaveDraft"])

    def test_resolve_record_applies_corrections_and_audit_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            record_id = ledger.upsert_bookkeeping_record({
                "sourceType": "document",
                "status": "needs_review",
                "exportStatus": "blocked_by_review",
                "targetSystem": "waveapps",
                "vendorName": "Unknown",
                "category": "Manual Review",
                "amount": 10,
                "reviewRequired": True,
                "metadata": {"source": "test"},
            })
            ledger.replace_bookkeeping_record_line_items(record_id, [{
                "description": "Unclear receipt",
                "amount": 10,
                "category": "Manual Review",
                "accountName": "Manual Review",
                "source": "document_total",
            }])
            service = LocalBookkeepingRecordService(ledger, {})

            result = service.resolve_record(
                record_id,
                status="approved",
                resolution="Corrected normalized record.",
                corrections={
                    "vendorName": "Office Shop",
                    "category": "Office Supplies",
                    "amount": "42.50",
                    "vatAmount": "7.38",
                    "targetAccount": "Office Supplies",
                },
                actor="unit-test",
            )
            record = ledger.get_bookkeeping_record(record_id)
            audit = ledger.list_audit_events()[0]

            self.assertTrue(result["success"])
            self.assertEqual(result["externalSubmission"], "not_executed")
            self.assertEqual(record["status"], "ready_to_route")
            self.assertEqual(record["export_status"], "ready")
            self.assertEqual(record["review_required"], 0)
            self.assertEqual(record["vendor_name"], "Office Shop")
            self.assertEqual(record["category"], "Office Supplies")
            self.assertEqual(record["amount"], 42.5)
            self.assertEqual(record["vat_amount"], 7.38)
            self.assertEqual(record["line_items"][0]["amount"], 42.5)
            self.assertEqual(record["line_items"][0]["tax_amount"], 7.38)
            self.assertEqual(record["line_items"][0]["account_name"], "Office Supplies")
            self.assertEqual(record["metadata"]["resolutionHistory"][0]["actor"], "unit-test")
            self.assertEqual(record["metadata"]["resolutionHistory"][0]["fromStatus"], "needs_review")
            self.assertEqual(record["metadata"]["lastResolution"]["toStatus"], "ready_to_route")
            self.assertEqual(audit["action"], "local_bookkeeping_records.record.resolve")
            self.assertEqual(audit["details"]["externalSubmission"], "not_executed")

    def test_upsert_from_bank_transaction_creates_missing_receipt_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            transaction_id = ledger.upsert_bank_transaction({
                "accountIdentifier": "wave-checking",
                "transactionId": "tx-missing-record",
                "transactionDate": "2026-06-28",
                "amount": -12.5,
                "currency": "EUR",
                "description": "Unknown supplier",
                "reconciliationStatus": "missing_receipt",
            })

            result = LocalBookkeepingRecordService(ledger, {}).upsert_from_bank_transaction(transaction_id)
            record = ledger.get_bookkeeping_record(result["recordId"])

            self.assertEqual(record["source_type"], "bank_transaction")
            self.assertEqual(record["bank_transaction_id"], transaction_id)
            self.assertEqual(record["status"], "missing_receipt")
            self.assertEqual(record["export_status"], "blocked_missing_receipt")
            self.assertEqual(record["reconciliation_status"], "missing_receipt")
            self.assertEqual(record["line_items"][0]["source"], "bank_transaction")

    def test_upsert_from_bank_transaction_applies_approved_vendor_rule(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            rule_id = ledger.upsert_vendor_category_rule({
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "targetSystem": "waveapps",
                "confidenceScore": 0.97,
                "status": "approved",
            })
            transaction_id = ledger.upsert_bank_transaction({
                "accountIdentifier": "wave-checking",
                "transactionId": "tx-rule-record",
                "transactionDate": "2026-06-28",
                "amount": -42.5,
                "currency": "EUR",
                "description": "Printer paper",
                "counterparty": "Office Shop",
                "reconciliationStatus": "not_started",
            })

            result = LocalBookkeepingRecordService(ledger, {}).upsert_from_bank_transaction(transaction_id)
            record = ledger.get_bookkeeping_record(result["recordId"])

            self.assertEqual(record["category"], "Office Supplies")
            self.assertEqual(record["target_account"], "Office Supplies")
            self.assertEqual(record["confidence_score"], 0.97)
            self.assertEqual(record["metadata"]["appliedVendorCategoryRule"]["ruleId"], rule_id)
            self.assertEqual(record["line_items"][0]["category"], "Office Supplies")
            self.assertEqual(record["line_items"][0]["account_name"], "Office Supplies")
            self.assertEqual(record["line_items"][0]["metadata"]["appliedVendorCategoryRule"]["ruleId"], rule_id)
            audit_actions = [event["action"] for event in ledger.list_audit_events()]
            self.assertIn("local_bookkeeping_records.vendor_category_rule.applied", audit_actions)

    def test_upsert_from_bank_transaction_ignores_suggested_vendor_rule(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            ledger.upsert_vendor_category_rule({
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "targetSystem": "waveapps",
                "status": "suggested",
            })
            transaction_id = ledger.upsert_bank_transaction({
                "accountIdentifier": "wave-checking",
                "transactionId": "tx-rule-suggested-record",
                "transactionDate": "2026-06-28",
                "amount": -42.5,
                "currency": "EUR",
                "description": "Printer paper",
                "counterparty": "Office Shop",
                "reconciliationStatus": "not_started",
            })

            result = LocalBookkeepingRecordService(ledger, {}).upsert_from_bank_transaction(transaction_id)
            record = ledger.get_bookkeeping_record(result["recordId"])

            self.assertIsNone(record["category"])
            self.assertIsNone(record["metadata"]["appliedVendorCategoryRule"])
            self.assertEqual(record["line_items"][0]["account_name"], "wave-checking")

    def test_refresh_bank_transactions_updates_all_records_without_audit_spam(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            ledger.upsert_vendor_category_rule({
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "targetSystem": "waveapps",
                "status": "approved",
            })
            transaction_id = ledger.upsert_bank_transaction({
                "accountIdentifier": "wave-checking",
                "transactionId": "tx-refresh-record",
                "transactionDate": "2026-06-28",
                "amount": -42.5,
                "currency": "EUR",
                "description": "Printer paper",
                "counterparty": "Office Shop",
                "reconciliationStatus": "not_started",
            })
            service = LocalBookkeepingRecordService(ledger, {})

            first = service.refresh_bank_transactions()
            second = service.refresh_bank_transactions()
            record = ledger.get_bookkeeping_record_by_bank_transaction(transaction_id)
            rule_audits = [
                event for event in ledger.list_audit_events(limit=20)
                if event["action"] == "local_bookkeeping_records.vendor_category_rule.applied"
            ]

            self.assertEqual(first["updated"], 1)
            self.assertEqual(first["ruleApplied"], 1)
            self.assertEqual(second["updated"], 1)
            self.assertEqual(second["ruleApplied"], 1)
            self.assertEqual(record["category"], "Office Supplies")
            self.assertEqual(len(rule_audits), 1)


if __name__ == "__main__":
    unittest.main()
