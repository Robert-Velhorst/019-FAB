import hashlib
import os
import tempfile
import unittest
from unittest.mock import patch

from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_processing import (
    LocalDocumentProcessor,
    _read_text_file,
    STORED_OCR_REASSESSMENT_VERSION,
    duplicate_candidate_reassessment_plan,
    duplicate_link_cycles,
)
from src.document_processors.document_resource_limits import (
    MAX_SOURCE_DOCUMENT_BYTES,
    MAX_TEXT_DOCUMENT_BYTES,
)
from src.operations.local_intake import LocalFolderIntake


class StaticCategorizer:
    def __init__(self, category="Office Supplies", confidence_score=0.91):
        self.category = category
        self.confidence_score = confidence_score

    def categorize(self, processed_data):
        return {"category": self.category, "confidence_score": self.confidence_score}


class StaticValidator:
    def __init__(self, valid=True, reason=""):
        self.valid = valid
        self.reason = reason

    def validate_receipt(self, processed_data):
        return {
            "is_valid": self.valid,
            "errors": [self.reason] if self.reason else [],
            "warnings": [],
            "reason": self.reason,
            "blocking": not self.valid,
        }


class RaisingPipeline:
    def process_document(self, path):
        raise RuntimeError("OCR engine unavailable")


class StaticPipeline:
    def __init__(self):
        self.paths = []

    def process_document(self, path):
        self.paths.append(path)
        return {
            "document_path": path,
            "ocr_text": "Vendor: Retry Vendor\nDate: 2026-06-28\nTotal: EUR 42.50\n",
            "language": "en",
            "ocr_strategy": "illumination_normalized_fallback",
            "ocr_fallback_pages": 1,
            "preprocessing": {
                "applied": True,
                "reason": None,
                "deskewAngle": -2.5,
            },
            "extracted_data": {
                "vendor_name": "Retry Vendor",
                "transaction_date": "2026-06-28",
                "total_amount": 42.5,
                "currency": "EUR",
            },
        }


class TestLocalDocumentProcessor(unittest.TestCase):
    def test_processing_read_loads_only_open_reviews_and_document_facts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "local_folder",
                "sourceDocumentId": "processing-read-1",
                "originalFilename": "receipt.txt",
                "mimeType": "text/plain",
                "storagePath": os.path.join(temp_dir, "receipt.txt"),
                "documentType": "text",
                "processingStatus": "imported",
            })
            ledger.create_review_item({
                "documentId": document_id,
                "reason": "needs_review",
                "details": "Open review",
            })

            document = ledger.get_document_for_processing(document_id)

            self.assertEqual([item["reason"] for item in document["review_items"]], ["needs_review"])
            self.assertNotIn("export_attempts", document)
            self.assertNotIn("audit_events", document)

    def test_processing_rejects_bytes_changed_since_intake(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            source_path = os.path.join(intake_dir, "receipt.txt")
            with open(source_path, "w", encoding="utf-8") as handle:
                handle.write("Original registered receipt")
            LocalFolderIntake(ledger).rescan([intake_dir])
            document_id = ledger.list_documents(limit=1)[0]["id"]
            with open(source_path, "w", encoding="utf-8") as handle:
                handle.write("Changed receipt content")
            pipeline = StaticPipeline()

            result = LocalDocumentProcessor(
                ledger,
                processor_pipeline=pipeline,
                categorizer=StaticCategorizer(),
                validator=StaticValidator(),
            ).process_document(document_id)

            self.assertEqual(result["status"], "needs_review")
            self.assertEqual(result["reviewReasons"], ["source_file_hash_mismatch"])
            self.assertEqual(pipeline.paths, [])
            self.assertEqual(ledger.get_document(document_id)["processing_status"], "needs_review")

    def test_processing_snapshot_enforces_binary_document_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, "oversized.pdf")
            with open(source_path, "wb") as handle:
                handle.truncate(MAX_SOURCE_DOCUMENT_BYTES + 1)
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "oversized-binary",
                "originalFilename": "oversized.pdf",
                "mimeType": "application/pdf",
                "storagePath": source_path,
                "documentType": "pdf",
                "processingStatus": "imported",
            })
            pipeline = StaticPipeline()

            result = LocalDocumentProcessor(
                ledger,
                processor_pipeline=pipeline,
                categorizer=StaticCategorizer(),
                validator=StaticValidator(),
            ).process_document(document_id)

            self.assertEqual(result["status"], "needs_review")
            self.assertEqual(result["reviewReasons"], ["document_exceeds_limit"])
            self.assertEqual(pipeline.paths, [])

    def test_processing_snapshot_enforces_text_limit_before_copy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, "oversized.txt")
            with open(source_path, "wb") as handle:
                handle.truncate(MAX_TEXT_DOCUMENT_BYTES + 1)
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "oversized-text",
                "originalFilename": "oversized.txt",
                "mimeType": "text/plain",
                "storagePath": source_path,
                "documentType": "text",
                "processingStatus": "imported",
            })
            pipeline = StaticPipeline()

            result = LocalDocumentProcessor(
                ledger,
                processor_pipeline=pipeline,
                categorizer=StaticCategorizer(),
                validator=StaticValidator(),
            ).process_document(document_id)

            self.assertEqual(result["status"], "needs_review")
            self.assertEqual(result["reviewReasons"], ["text_document_exceeds_limit"])
            self.assertEqual(pipeline.paths, [])

    def test_text_reader_bounds_bytes_even_if_file_changes_after_intake(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "receipt.txt")
            with open(path, "wb") as handle:
                handle.truncate(MAX_TEXT_DOCUMENT_BYTES + 1)

            with self.assertRaisesRegex(ValueError, "text_document_exceeds_limit"):
                _read_text_file(path)

    def test_process_text_document_updates_ledger_fields_and_audit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = os.path.join(temp_dir, "receipt.txt")
            with open(receipt_path, "w", encoding="utf-8") as handle:
                handle.write("Vendor: Test Vendor\nDate: 2026-06-28\nTotal: EUR 42.50\nVAT: EUR 7.38\n")
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "local_folder",
                "sourceDocumentId": "text-1",
                "originalFilename": "receipt.txt",
                "mimeType": "text/plain",
                "storagePath": receipt_path,
                "documentType": "text",
                "processingStatus": "imported",
            })

            result = LocalDocumentProcessor(
                ledger,
                categorizer=StaticCategorizer(),
                validator=StaticValidator(),
            ).process_document(document_id)

            self.assertEqual(result["status"], "processed")
            self.assertIsNotNone(result["bookkeepingRecordId"])
            document = ledger.get_document(document_id)
            self.assertEqual(document["processing_status"], "processed")
            self.assertEqual(document["vendor_name"], "Test Vendor")
            self.assertEqual(document["category"], "Office Supplies")
            self.assertEqual(document["total_amount"], 42.5)
            self.assertIn("Vendor: Test Vendor", document["ocr_text"])
            fields = {field["field_name"]: field for field in document["extracted_fields"]}
            self.assertEqual(fields["vendor_name"]["field_value"], "Test Vendor")
            self.assertEqual(fields["total_amount"]["field_value"], 42.5)
            self.assertEqual(fields["vat_amount"]["field_value"], 7.38)
            self.assertEqual(fields["category"]["field_value"], "Office Supplies")
            self.assertEqual(fields["vendor_name"]["provenance"]["extractionSource"], "local_text_regex")
            self.assertEqual(document["bookkeeping_record"]["status"], "ready_to_route")
            self.assertEqual(document["bookkeeping_record"]["export_status"], "ready")
            self.assertEqual(ledger.list_audit_events()[0]["action"], "local_processing.document_processed")

    def test_freshdesk_ticket_description_is_forced_to_non_posting_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            description_path = os.path.join(temp_dir, "freshdesk-description.txt")
            with open(description_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "Subject: Invoice 2026-42\n"
                    "Vendor: Example Supplier\n"
                    "Date: 2026-07-20\n"
                    "Total: EUR 125.00\n"
                )
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "freshdesk",
                "sourceDocumentId": "freshdesk_42_description",
                "originalFilename": "freshdesk-42-description.txt",
                "mimeType": "text/plain",
                "storagePath": description_path,
                "documentType": "text",
                "processingStatus": "imported",
                "metadata": {
                    "providerMetadata": {
                        "evidence_role": "ticket_description",
                        "posting_eligible": False,
                        "profile_id": "scan_to_folder_v1",
                        "source_provenance": {
                            "repository": (
                                "Noodzakelijk-Online/"
                                "025-Scan-to-folder-automation"
                            ),
                            "auditedCommit": (
                                "e3078d92c214aa3b17d98a8687f16e73f52f71ba"
                            ),
                        },
                    },
                },
            })

            result = LocalDocumentProcessor(
                ledger,
                categorizer=StaticCategorizer(),
                validator=StaticValidator(),
            ).process_document(document_id)

            document = ledger.get_document(document_id)
            classification = document["metadata"]["processing"][
                "documentTypeClassification"
            ]
            self.assertEqual(result["status"], "processed")
            self.assertEqual(document["document_type"], "supporting_document")
            self.assertEqual(document["category"], "Supporting Evidence")
            self.assertFalse(classification["postingEligible"])
            self.assertEqual(
                classification["classifier"],
                "provider_evidence_policy_v1",
            )
            self.assertEqual(document["review_items"], [])
            self.assertEqual(
                document["bookkeeping_record"]["record_type"],
                "supporting_document",
            )
            self.assertEqual(
                document["bookkeeping_record"]["status"],
                "supporting_evidence",
            )
            self.assertEqual(
                document["bookkeeping_record"]["export_status"],
                "not_applicable",
            )

    def test_new_document_uses_trusted_exact_vendor_category_during_processing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = os.path.join(temp_dir, "praxis.txt")
            with open(receipt_path, "w", encoding="utf-8") as handle:
                handle.write("Vendor: Praxis\nDate: 2026-06-28\nTOTAAL EUR 42.50\n")
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "praxis-new",
                "originalFilename": "praxis.pdf",
                "mimeType": "application/pdf",
                "storagePath": receipt_path,
                "documentType": "receipt",
                "processingStatus": "imported",
            })

            result = LocalDocumentProcessor(
                ledger,
                categorizer=StaticCategorizer(
                    category="Manual Review",
                    confidence_score=0.1,
                ),
                validator=StaticValidator(),
            ).process_document(document_id)

            self.assertEqual(result["status"], "processed")
            self.assertEqual(result["category"], "Construction Materials & Tools")
            self.assertEqual(result["reviewReasons"], [])
            self.assertEqual(
                result["appliedTrustedCategorySuggestion"]["automationPolicy"],
                "builtin_exact_vendor_taxonomy_v1",
            )
            document = ledger.get_document(document_id)
            self.assertEqual(document["category"], "Construction Materials & Tools")
            self.assertGreaterEqual(document["confidence_score"], 0.95)
            self.assertEqual(
                document["metadata"]["processing"]["appliedTrustedCategorySuggestion"]["source"],
                "fab_builtin_vendor_taxonomy_v1",
            )
            category_field = next(
                field
                for field in document["extracted_fields"]
                if field["field_name"] == "category"
            )
            self.assertEqual(
                category_field["provenance"]["fieldSource"],
                "trusted_category_automation",
            )
            self.assertEqual(
                category_field["provenance"]["policy"],
                "builtin_exact_vendor_taxonomy_v1",
            )

    def test_vendor_invoice_type_is_persisted_and_routes_as_bill_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            invoice_path = os.path.join(temp_dir, "invoice.txt")
            with open(invoice_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "FACTUUR\nVendor: Test Vendor\nDate: 2026-06-28\n"
                    "Total: EUR 42.50\nInvoice number: INV-0042\n"
                )
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "scanner-invoice-1",
                "originalFilename": "scan.pdf",
                "mimeType": "application/pdf",
                "storagePath": invoice_path,
                "documentType": "pdf",
                "processingStatus": "imported",
            })

            result = LocalDocumentProcessor(
                ledger,
                categorizer=StaticCategorizer(),
                validator=StaticValidator(),
            ).process_document(document_id)

            document = ledger.get_document(document_id)
            self.assertEqual(result["status"], "processed")
            self.assertEqual(document["document_type"], "vendor_invoice")
            self.assertEqual(document["bookkeeping_record"]["record_type"], "bill")
            fields = {field["field_name"]: field for field in document["extracted_fields"]}
            self.assertEqual(fields["document_type"]["field_value"], "vendor_invoice")
            self.assertEqual(
                fields["document_type"]["provenance"]["fieldSource"],
                "document_type_classifier",
            )

    def test_credit_note_is_reviewed_as_a_signed_expense_reversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credit_path = os.path.join(temp_dir, "credit-note.txt")
            with open(credit_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "CREDITNOTA\nVendor: Test Vendor\nDate: 2026-06-28\n"
                    "Total: EUR 42.50\nVAT: EUR 7.38\n"
                )
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "scanner-credit-1",
                "originalFilename": "credit-note.pdf",
                "mimeType": "application/pdf",
                "storagePath": credit_path,
                "documentType": "pdf",
                "processingStatus": "imported",
            })

            result = LocalDocumentProcessor(
                ledger,
                categorizer=StaticCategorizer(),
                validator=StaticValidator(),
            ).process_document(document_id)

            document = ledger.get_document(document_id)
            record = document["bookkeeping_record"]
            self.assertEqual(result["status"], "needs_review")
            self.assertIn("credit_note_posting_review", result["reviewReasons"])
            self.assertNotIn("non_posting_document_type", result["reviewReasons"])
            self.assertEqual(document["document_type"], "credit_note")
            self.assertEqual(document["category"], "Office Supplies")
            self.assertEqual(document["total_amount"], 42.5)
            self.assertEqual(record["record_type"], "expense")
            self.assertEqual(record["amount"], -42.5)
            self.assertEqual(record["vat_amount"], -7.38)
            self.assertEqual(record["metadata"]["postingDirection"], "credit")
            self.assertEqual(record["metadata"]["evidenceAmount"], 42.5)
            self.assertTrue(record["review_required"])
            self.assertEqual(record["export_status"], "blocked_by_review")

    def test_dutch_refund_uses_positive_evidence_and_negative_ledger_direction(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credit_path = os.path.join(temp_dir, "praxis-refund.pdf")
            source_bytes = b"%PDF-1.7\nretained refund source\n"
            with open(credit_path, "wb") as handle:
                handle.write(source_bytes)
            source_hash_before = hashlib.sha256(source_bytes).hexdigest()
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "scanner-refund-1",
                "originalFilename": "praxis-refund.pdf",
                "mimeType": "application/pdf",
                "storagePath": credit_path,
                "documentType": "pdf",
                "processingStatus": "needs_review",
                "ocrText": (
                    "Praxis\nTERUGBETALING\nDatum: 07/07/2023\n"
                    "Terug (Vpay) 25,00"
                ),
            })

            result = LocalDocumentProcessor(
                ledger,
                categorizer=StaticCategorizer(),
                processor_pipeline=RaisingPipeline(),
            ).process_document(document_id, reuse_stored_ocr=True)

            document = ledger.get_document(document_id)
            record = document["bookkeeping_record"]
            normalization = document["metadata"]["processing"][
                "creditNoteAmountNormalization"
            ]
            total_evidence = document["metadata"]["processing"]["fieldEvidence"][
                "total_amount"
            ]
            self.assertEqual(result["status"], "needs_review")
            self.assertIn("credit_note_posting_review", result["reviewReasons"])
            self.assertNotIn("validation_failed", result["reviewReasons"])
            self.assertEqual(document["document_type"], "credit_note")
            self.assertEqual(document["total_amount"], 25.0)
            self.assertEqual(record["amount"], -25.0)
            self.assertEqual(record["metadata"]["postingDirection"], "credit")
            self.assertEqual(
                normalization["policy"],
                "credit_note_absolute_evidence_amount",
            )
            self.assertEqual(total_evidence["observedValue"], -25.0)
            self.assertEqual(total_evidence["normalizedValue"], 25.0)
            with open(credit_path, "rb") as handle:
                self.assertEqual(
                    hashlib.sha256(handle.read()).hexdigest(),
                    source_hash_before,
                )

    def test_order_confirmation_is_never_auto_ready_to_post(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            confirmation_path = os.path.join(temp_dir, "confirmation.txt")
            with open(confirmation_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "Order confirmation\nVendor: Test Vendor\nDate: 2026-06-28\n"
                    "Total: EUR 42.50\n"
                )
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "scanner-order-1",
                "originalFilename": "scan.pdf",
                "mimeType": "application/pdf",
                "storagePath": confirmation_path,
                "documentType": "pdf",
                "processingStatus": "imported",
            })

            result = LocalDocumentProcessor(
                ledger,
                categorizer=StaticCategorizer(),
                validator=StaticValidator(),
            ).process_document(document_id)

            self.assertEqual(result["status"], "needs_review")
            self.assertIn("non_posting_document_type", result["reviewReasons"])
            document = ledger.get_document(document_id)
            self.assertEqual(document["document_type"], "order_confirmation")
            self.assertEqual(document["bookkeeping_record"]["record_type"], "supporting_document")
            self.assertEqual(document["bookkeeping_record"]["export_status"], "not_applicable")
            self.assertIsNone(document["bookkeeping_record"]["amount"])
            self.assertEqual(document["bookkeeping_record"]["metadata"]["evidenceAmount"], 42.5)
            self.assertFalse(
                document["bookkeeping_record"]["metadata"]["exportReadiness"]["readyForWaveDraft"]
            )

    def test_document_type_backfill_is_audited_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "historic-receipt-1",
                "originalFilename": "historic-scan.pdf",
                "mimeType": "application/pdf",
                "documentType": "pdf",
                "processingStatus": "needs_review",
                "vendorName": "Historic Vendor",
                "category": "Manual Review",
                "transactionDate": "2026-06-28",
                "totalAmount": 12.0,
                "ocrText": "Ontvangstbewijs\nTotaal EUR 12,00",
                "extractedData": {
                    "vendor_name": "Historic Vendor",
                    "transaction_date": "2026-06-28",
                    "total_amount": 12.0,
                },
            })
            processor = LocalDocumentProcessor(
                ledger,
                categorizer=StaticCategorizer(),
                validator=StaticValidator(),
            )

            first = processor.backfill_document_types()
            second = processor.backfill_document_types()

            self.assertEqual(first["evaluated"], 1)
            self.assertEqual(first["classified"], 1)
            self.assertEqual(first["externalSubmission"], "not_executed")
            self.assertEqual(second["evaluated"], 0)
            self.assertEqual(second["alreadyClassified"], 1)
            document = ledger.get_document(document_id)
            self.assertEqual(document["document_type"], "receipt")
            self.assertEqual(document["extracted_data"]["document_type"], "receipt")
            self.assertEqual(document["bookkeeping_record"]["record_type"], "expense")
            actions = [event["action"] for event in ledger.list_audit_events(limit=20)]
            self.assertEqual(actions.count("local_processing.document_type_backfilled"), 1)
            self.assertEqual(actions.count("local_processing.document_type_backfill_completed"), 1)

    def test_credit_note_backfill_replaces_stale_supporting_evidence_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "historic-credit-1",
                "originalFilename": "historic-credit.pdf",
                "mimeType": "application/pdf",
                "documentType": "credit_note",
                "processingStatus": "needs_review",
                "vendorName": "Historic Vendor",
                "category": "Supporting Evidence",
                "transactionDate": "2026-06-28",
                "totalAmount": 12.0,
                "ocrText": "CREDITNOTA\nTotaal EUR 12,00",
                "extractedData": {
                    "document_type": "credit_note",
                    "vendor_name": "Historic Vendor",
                    "transaction_date": "2026-06-28",
                    "total_amount": 12.0,
                },
                "metadata": {
                    "processing": {
                        "documentTypeClassification": {
                            "documentType": "credit_note",
                            "classifier": "deterministic_financial_document_type_v3",
                            "postingEligible": False,
                            "reviewRequired": True,
                        },
                        "reviewReasons": ["non_posting_document_type"],
                    },
                },
            })
            old_review_id = ledger.create_review_item({
                "documentId": document_id,
                "reason": "non_posting_document_type",
                "details": "Legacy classifier treated this credit note as evidence.",
            })

            result = LocalDocumentProcessor(ledger).backfill_document_types()

            self.assertEqual(result["evaluated"], 1)
            document = ledger.get_document(document_id)
            self.assertEqual(document["category"], "Manual Review")
            self.assertEqual(document["bookkeeping_record"]["amount"], -12.0)
            self.assertEqual(document["bookkeeping_record"]["record_type"], "expense")
            open_reasons = {
                item["reason"]
                for item in document["review_items"]
                if item["status"] in {"pending", "in_review"}
            }
            self.assertEqual(open_reasons, {"credit_note_posting_review"})
            old_review = ledger.get_review_item(old_review_id)
            self.assertEqual(old_review["status"], "resolved")

    def test_document_type_backfill_queues_non_posting_review_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "historic-order-1",
                "originalFilename": "historic-order.pdf",
                "mimeType": "application/pdf",
                "documentType": "pdf",
                "processingStatus": "needs_review",
                "ocrText": "Order confirmation\nTotal EUR 12.00",
                "extractedData": {"total_amount": 12.0},
            })
            processor = LocalDocumentProcessor(
                ledger,
                categorizer=StaticCategorizer(),
                validator=StaticValidator(),
            )

            first = processor.backfill_document_types()
            second = processor.backfill_document_types()

            self.assertEqual(first["reviewQueued"], 1)
            self.assertEqual(second["reviewQueued"], 0)
            reviews = [
                item for item in ledger.list_review_items(document_id=document_id, limit=20)
                if item["reason"] == "non_posting_document_type"
            ]
            self.assertEqual(len(reviews), 1)
            document = ledger.get_document(document_id)
            self.assertEqual(document["document_type"], "order_confirmation")
            self.assertEqual(document["category"], "Supporting Evidence")
            self.assertEqual(document["bookkeeping_record"]["record_type"], "supporting_document")
            self.assertEqual(document["bookkeeping_record"]["export_status"], "not_applicable")

    def test_government_evidence_backfill_clears_transaction_review_reasons(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "historic-uwv-statement-1",
                "originalFilename": "historic-uwv.pdf",
                "mimeType": "application/pdf",
                "documentType": "pdf",
                "processingStatus": "needs_review",
                "category": "Manual Review",
                "confidenceScore": 0.1,
                "totalAmount": 821.98,
                "ocrText": (
                    "uwv Betaalspecificatie\nWajong Uitkering\n"
                    "Netto te ontvangen EUR 821,98"
                ),
                "extractedData": {
                    "vendor_name": "uwv Betaalspecificatie",
                    "total_amount": 821.98,
                },
            })
            for reason in (
                "validation_failed",
                "low_confidence_categorization",
                "manual_review_category",
                "sensitive_government_document",
            ):
                ledger.create_review_item({
                    "documentId": document_id,
                    "reason": reason,
                    "details": "Historic processing gate",
                })

            result = LocalDocumentProcessor(ledger).backfill_document_types()

            self.assertEqual(result["classified"], 1)
            document = ledger.get_document(document_id)
            self.assertEqual(document["document_type"], "government_correspondence")
            self.assertEqual(document["category"], "Supporting Evidence")
            self.assertEqual(document["bookkeeping_record"]["record_type"], "supporting_document")
            self.assertEqual(document["bookkeeping_record"]["export_status"], "not_applicable")
            self.assertIsNone(document["bookkeeping_record"]["amount"])
            self.assertEqual(document["bookkeeping_record"]["metadata"]["evidenceAmount"], 821.98)
            open_reasons = {
                item["reason"]
                for item in ledger.list_review_items(
                    status=("pending", "in_review"),
                    document_id=document_id,
                )
            }
            self.assertEqual(
                open_reasons,
                {"non_posting_document_type", "sensitive_government_document"},
            )

    def test_process_text_document_queues_review_for_validation_and_sensitive_terms(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            letter_path = os.path.join(temp_dir, "belastingdienst.txt")
            with open(letter_path, "w", encoding="utf-8") as handle:
                handle.write("Belastingdienst\nVendor: Government\nTotal: EUR 0.00\n")
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "local_folder",
                "sourceDocumentId": "letter-1",
                "originalFilename": "belastingdienst.txt",
                "mimeType": "text/plain",
                "storagePath": letter_path,
                "documentType": "text",
                "processingStatus": "imported",
            })

            result = LocalDocumentProcessor(
                ledger,
                categorizer=StaticCategorizer(category="Manual Review", confidence_score=0.1),
                validator=StaticValidator(valid=False, reason="Missing transaction date"),
            ).process_document(document_id)

            self.assertEqual(result["status"], "needs_review")
            self.assertIsNotNone(result["bookkeepingRecordId"])
            self.assertIn("validation_failed", result["reviewReasons"])
            self.assertIn("low_confidence_categorization", result["reviewReasons"])
            self.assertIn("manual_review_category", result["reviewReasons"])
            self.assertIn("sensitive_government_document", result["reviewReasons"])
            review_reasons = {item["reason"] for item in ledger.list_review_items(status="pending")}
            self.assertIn("sensitive_government_document", review_reasons)
            document = ledger.get_document(document_id)
            self.assertEqual(document["processing_status"], "needs_review")
            self.assertEqual(document["bookkeeping_record"]["status"], "needs_review")
            self.assertEqual(document["bookkeeping_record"]["review_required"], 1)

    def test_reprocessing_resolves_machine_review_reasons_that_are_no_longer_active(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = os.path.join(temp_dir, "receipt.txt")
            with open(receipt_path, "w", encoding="utf-8") as handle:
                handle.write("Vendor: Test Vendor\nDate: 2026-06-28\nTotal: EUR 42.50\n")
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "local_folder",
                "sourceDocumentId": "reprocess-1",
                "originalFilename": "receipt.txt",
                "mimeType": "text/plain",
                "storagePath": receipt_path,
                "documentType": "text",
                "processingStatus": "imported",
            })

            first = LocalDocumentProcessor(
                ledger,
                categorizer=StaticCategorizer(category="Manual Review", confidence_score=0.1),
                validator=StaticValidator(valid=False, reason="Missing required field"),
            ).process_document(document_id)
            second = LocalDocumentProcessor(
                ledger,
                categorizer=StaticCategorizer(),
                validator=StaticValidator(),
            ).process_document(document_id)

            self.assertEqual(first["status"], "needs_review")
            self.assertEqual(second["status"], "processed")
            self.assertEqual(len(second["resolvedReviewItemIds"]), 3)
            self.assertEqual(
                ledger.list_review_items(status=("pending", "in_review"), document_id=document_id),
                [],
            )
            resolved_reasons = {
                item["reason"]
                for item in ledger.list_review_items(status="resolved", document_id=document_id)
            }
            self.assertEqual(
                resolved_reasons,
                {"validation_failed", "low_confidence_categorization", "manual_review_category"},
            )

    def test_approved_vendor_rule_overrides_low_confidence_category(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = os.path.join(temp_dir, "receipt.txt")
            with open(receipt_path, "w", encoding="utf-8") as handle:
                handle.write("Vendor: Office Shop\nDate: 2026-06-28\nTotal: EUR 42.50\n")
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "local_folder",
                "sourceDocumentId": "rule-approved",
                "originalFilename": "receipt.txt",
                "mimeType": "text/plain",
                "storagePath": receipt_path,
                "documentType": "text",
                "processingStatus": "imported",
            })
            rule_id = ledger.upsert_vendor_category_rule({
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "targetSystem": "none",
                "confidenceScore": 0.98,
                "status": "approved",
            })

            result = LocalDocumentProcessor(
                ledger,
                categorizer=StaticCategorizer(category="Manual Review", confidence_score=0.1),
                validator=StaticValidator(),
            ).process_document(document_id)

            self.assertEqual(result["status"], "processed")
            self.assertEqual(result["category"], "Office Supplies")
            self.assertEqual(result["appliedVendorCategoryRule"]["ruleId"], rule_id)
            self.assertEqual(result["reviewReasons"], [])
            document = ledger.get_document(document_id)
            self.assertEqual(document["category"], "Office Supplies")
            self.assertEqual(document["metadata"]["processing"]["appliedVendorCategoryRule"]["ruleId"], rule_id)
            fields = {field["field_name"]: field for field in document["extracted_fields"]}
            self.assertEqual(fields["category"]["provenance"]["fieldSource"], "approved_vendor_rule")
            self.assertEqual(fields["category"]["provenance"]["ruleId"], rule_id)
            self.assertEqual(document["bookkeeping_record"]["status"], "ready_to_route")
            audit = ledger.list_audit_events()[0]
            self.assertEqual(audit["action"], "local_processing.document_processed")
            self.assertEqual(audit["details"]["appliedVendorCategoryRule"]["ruleId"], rule_id)

    def test_suggested_vendor_rule_does_not_override_processing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = os.path.join(temp_dir, "receipt.txt")
            with open(receipt_path, "w", encoding="utf-8") as handle:
                handle.write("Vendor: Office Shop\nDate: 2026-06-28\nTotal: EUR 42.50\n")
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "local_folder",
                "sourceDocumentId": "rule-suggested",
                "originalFilename": "receipt.txt",
                "mimeType": "text/plain",
                "storagePath": receipt_path,
                "documentType": "text",
                "processingStatus": "imported",
            })
            ledger.upsert_vendor_category_rule({
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "targetSystem": "none",
                "status": "suggested",
            })

            result = LocalDocumentProcessor(
                ledger,
                categorizer=StaticCategorizer(category="Manual Review", confidence_score=0.1),
                validator=StaticValidator(),
            ).process_document(document_id)

            self.assertEqual(result["status"], "needs_review")
            self.assertIsNone(result["appliedVendorCategoryRule"])
            self.assertIn("low_confidence_categorization", result["reviewReasons"])
            self.assertIn("manual_review_category", result["reviewReasons"])
            document = ledger.get_document(document_id)
            self.assertEqual(document["category"], "Manual Review")
            fields = {field["field_name"]: field for field in document["extracted_fields"]}
            self.assertEqual(fields["category"]["provenance"]["fieldSource"], "categorizer")

    def test_process_imported_batches_only_imported_documents(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = os.path.join(temp_dir, "receipt.txt")
            with open(receipt_path, "w", encoding="utf-8") as handle:
                handle.write("Vendor: Test Vendor\nDate: 2026-06-28\nTotal: 42.50\n")
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            ledger.register_document({
                "source": "local_folder",
                "sourceDocumentId": "text-1",
                "originalFilename": "receipt.txt",
                "mimeType": "text/plain",
                "storagePath": receipt_path,
                "documentType": "text",
                "processingStatus": "imported",
            })
            ledger.register_document({
                "source": "local_folder",
                "sourceDocumentId": "done-1",
                "originalFilename": "done.txt",
                "processingStatus": "processed",
            })

            summary = LocalDocumentProcessor(
                ledger,
                categorizer=StaticCategorizer(),
                validator=StaticValidator(),
            ).process_imported()

            self.assertEqual(summary["requested"], 1)
            self.assertEqual(summary["processed"], 1)
            self.assertEqual(summary["needsReview"], 0)
            self.assertEqual(ledger.dashboard_metrics()["documents"], 2)

    def test_reprocess_incomplete_only_retries_blank_review_documents_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            blank_path = os.path.join(temp_dir, "blank.pdf")
            complete_path = os.path.join(temp_dir, "complete.pdf")
            for path in (blank_path, complete_path):
                with open(path, "wb") as handle:
                    handle.write(b"test document bytes")
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            blank_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "blank-review",
                "originalFilename": "blank.pdf",
                "mimeType": "application/pdf",
                "storagePath": blank_path,
                "documentType": "pdf",
                "processingStatus": "needs_review",
                "ocrText": "",
            })
            ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "complete-review",
                "originalFilename": "complete.pdf",
                "mimeType": "application/pdf",
                "storagePath": complete_path,
                "documentType": "pdf",
                "processingStatus": "needs_review",
                "ocrText": "Already extracted",
            })
            pipeline = StaticPipeline()

            summary = LocalDocumentProcessor(
                ledger,
                processor_pipeline=pipeline,
                categorizer=StaticCategorizer(category="Manual Review", confidence_score=0.1),
                validator=StaticValidator(valid=False, reason="review required"),
            ).reprocess_incomplete(limit=25, actor="test-operator", create_backup=False)

            self.assertEqual(summary["candidates"], 1)
            self.assertEqual(summary["requested"], 1)
            self.assertEqual(summary["reprocessed"], 1)
            self.assertEqual(summary["ocrRecovered"], 1)
            self.assertEqual(summary["needsReview"], 1)
            self.assertEqual(summary["skippedMissingSource"], 0)
            self.assertEqual(summary["externalSubmission"], "not_executed")
            self.assertFalse(summary["sourceFilesModified"])
            self.assertEqual(len(pipeline.paths), 1)
            self.assertNotEqual(pipeline.paths[0], blank_path)
            self.assertTrue(pipeline.paths[0].endswith(".pdf"))
            self.assertFalse(os.path.exists(pipeline.paths[0]))
            self.assertTrue(os.path.isfile(blank_path))
            document = ledger.get_document(blank_id)
            self.assertEqual(document["processing_status"], "needs_review")
            self.assertIn("Vendor: Retry Vendor", document["ocr_text"])
            self.assertEqual(document["metadata"]["processing"]["ocrStrategy"], "illumination_normalized_fallback")
            self.assertEqual(
                document["metadata"]["processing"]["preprocessing"],
                {"applied": True, "reason": None, "deskewAngle": -2.5},
            )
            recovery = document["metadata"]["processing"]["ocrRecovery"]
            self.assertEqual(recovery["version"], "illumination_normalization_v1")
            self.assertEqual(recovery["status"], "recovered")
            self.assertEqual(recovery["actor"], "test-operator")
            self.assertEqual(ledger.list_audit_events()[0]["action"], "local_processing.incomplete_ocr_reprocessed")

    def test_reprocess_incomplete_does_not_consume_missing_source_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "missing-review",
                "originalFilename": "missing.pdf",
                "mimeType": "application/pdf",
                "storagePath": os.path.join(temp_dir, "missing.pdf"),
                "documentType": "pdf",
                "processingStatus": "needs_review",
                "ocrText": "",
            })

            summary = LocalDocumentProcessor(ledger).reprocess_incomplete(
                actor="test-operator",
                create_backup=False,
            )

            self.assertEqual(summary["candidates"], 1)
            self.assertEqual(summary["requested"], 0)
            self.assertEqual(summary["skipped"], 1)
            self.assertEqual(summary["skippedMissingSource"], 1)
            document = ledger.get_document(document_id)
            metadata = document.get("metadata") or {}
            self.assertNotIn("ocrRecovery", metadata.get("processing") or {})

    def test_reprocess_review_queue_reuses_retained_ocr_once_and_clears_stale_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = os.path.join(temp_dir, "action.pdf")
            with open(receipt_path, "wb") as handle:
                handle.write(b"retained source")
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "action-review",
                "originalFilename": "action.pdf",
                "mimeType": "application/pdf",
                "storagePath": receipt_path,
                "documentType": "receipt",
                "processingStatus": "needs_review",
                "ocrText": (
                    "ACTION\n12-07-2023\nTOTAAL 1.98\n"
                    "BTW-SPECIFICATIE BTW Excl. Incl.\nTOTAAL 0.16 1.82 1.98"
                ),
                "vendorName": "ACTION",
                "category": "Manual Review",
                "transactionDate": "2023-07-12",
                "totalAmount": 0.16,
            })
            for reason in (
                "validation_failed",
                "low_confidence_categorization",
                "manual_review_category",
            ):
                ledger.create_review_item({
                    "documentId": document_id,
                    "reason": reason,
                    "details": "Machine review gate.",
                })

            processor = LocalDocumentProcessor(ledger, processor_pipeline=RaisingPipeline())
            first = processor.reprocess_review_queue(
                actor="test-operator",
                create_backup=False,
            )
            second = processor.reprocess_review_queue(
                actor="test-operator",
                create_backup=False,
            )

            self.assertEqual(first["requested"], 1)
            self.assertEqual(first["reprocessed"], 1)
            self.assertEqual(first["resolvedReviewItems"], 1)
            self.assertFalse(first["ocrRerun"])
            self.assertEqual(second["requested"], 0)
            self.assertEqual(second["skippedPreviouslyAttempted"], 1)
            document = ledger.get_document(document_id)
            self.assertEqual(document["total_amount"], 1.98)
            self.assertEqual(
                document["metadata"]["processing"]["storedOcrReassessment"]["version"],
                STORED_OCR_REASSESSMENT_VERSION,
            )
            open_reasons = {
                item["reason"]
                for item in document["review_items"]
                if item["status"] in {"pending", "in_review"}
            }
            self.assertEqual(
                open_reasons,
                {"low_confidence_categorization", "manual_review_category"},
            )

    def test_reprocess_review_queue_recovers_header_vendor_without_rerunning_ocr(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = os.path.join(temp_dir, "action-ocr.pdf")
            with open(receipt_path, "wb") as handle:
                handle.write(b"retained source")
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "action-ocr-review",
                "originalFilename": "action-ocr.pdf",
                "mimeType": "application/pdf",
                "storagePath": receipt_path,
                "documentType": "receipt",
                "processingStatus": "needs_review",
                "ocrText": "AAGTION\n1385 Arnhem\n12-07-2023\nTOTAAL 12,00",
                "vendorName": "AAGTION",
                "category": "Manual Review",
                "transactionDate": "2023-07-12",
                "totalAmount": 12.0,
            })
            for reason in (
                "validation_failed",
                "low_confidence_categorization",
                "manual_review_category",
            ):
                ledger.create_review_item({
                    "documentId": document_id,
                    "reason": reason,
                    "details": "Machine review gate.",
                })

            summary = LocalDocumentProcessor(
                ledger,
                processor_pipeline=RaisingPipeline(),
            ).reprocess_review_queue(
                actor="test-operator",
                create_backup=False,
            )

            self.assertEqual(summary["requested"], 1)
            self.assertEqual(summary["resolvedReviewItems"], 1)
            document = ledger.get_document(document_id)
            self.assertEqual(document["vendor_name"], "Action")
            self.assertEqual(
                document["metadata"]["processing"]["fieldConfidences"]["vendor_name"],
                0.85,
            )
            self.assertEqual(
                document["metadata"]["processing"]["fieldEvidence"]["vendor_name"]["source"],
                "receipt_header_vendor_pattern",
            )
            vendor_field = next(
                field
                for field in document["extracted_fields"]
                if field["field_name"] == "vendor_name"
            )
            self.assertEqual(vendor_field["confidence_score"], 0.85)
            self.assertEqual(
                vendor_field["provenance"]["evidence"]["source"],
                "receipt_header_vendor_pattern",
            )
            self.assertEqual(
                {
                    item["reason"]
                    for item in document["review_items"]
                    if item["status"] in {"pending", "in_review"}
                },
                {"low_confidence_categorization", "manual_review_category"},
            )

    def test_reprocess_review_queue_refreshes_pending_duplicate_without_clearing_gate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = os.path.join(temp_dir, "hornbach-duplicate.pdf")
            source_bytes = b"retained duplicate source"
            with open(receipt_path, "wb") as handle:
                handle.write(source_bytes)
            source_mtime = os.stat(receipt_path).st_mtime_ns
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            canonical_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "canonical",
                "originalFilename": "canonical.pdf",
                "documentType": "receipt",
                "processingStatus": "processed",
                "vendorName": "Different Vendor",
                "transactionDate": "2023-07-12",
                "totalAmount": 15.60,
            })
            document_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "hornbach-duplicate-review",
                "originalFilename": "hornbach-duplicate.pdf",
                "mimeType": "application/pdf",
                "storagePath": receipt_path,
                "documentType": "receipt",
                "processingStatus": "needs_review",
                "ocrText": (
                    "~ Eri is altijd iets te doen.\n"
                    "Hornbach Bouwmarkt B.V.\n"
                    "BTW-nummer: NL8075.08.093.B.01\n"
                    "Totaal [4] EUR 15,60\n"
                    "BRUTO BTW NETTO\n"
                    "21% 15,60 2,71 12,89"
                ),
                "vendorName": "~ Eri is altijd iets te doen.",
                "category": "Manual Review",
                "totalAmount": 15.60,
                "vatAmount": 8075.08,
                "duplicateOfDocumentId": canonical_id,
            })
            duplicate_candidate_id = ledger.record_duplicate_candidate({
                "documentId": document_id,
                "candidateDocumentId": canonical_id,
                "matchType": "exact_content_match",
                "confidenceScore": 1.0,
                "status": "pending",
            })
            for reason in (
                "duplicate_candidate",
                "validation_failed",
                "low_confidence_categorization",
                "manual_review_category",
            ):
                ledger.create_review_item({
                    "documentId": document_id,
                    "reason": reason,
                    "details": "Machine review gate.",
                })

            summary = LocalDocumentProcessor(
                ledger,
                config={
                    "fab_local_backup_dir": os.path.join(temp_dir, "backups"),
                },
                processor_pipeline=RaisingPipeline(),
                categorizer=StaticCategorizer(
                    category="Construction Materials & Tools",
                    confidence_score=0.99,
                ),
                validator=StaticValidator(),
            ).reprocess_review_queue(actor="test-operator")

            self.assertEqual(summary["requested"], 1)
            self.assertEqual(summary["reprocessed"], 1)
            self.assertEqual(summary["backup"]["status"], "valid")
            self.assertEqual(len(summary["backup"]["ledgerSha256"]), 64)
            self.assertFalse(summary["sourceFilesModified"])
            document = ledger.get_document(document_id)
            self.assertEqual(document["vendor_name"], "Hornbach Bouwmarkt B.V.")
            self.assertIsNone(document["vat_amount"])
            self.assertEqual(
                document["category"],
                "Construction Materials & Tools",
            )
            self.assertEqual(document["processing_status"], "needs_review")
            self.assertEqual(document["duplicate_of_document_id"], canonical_id)
            self.assertEqual(
                {
                    item["reason"]
                    for item in document["review_items"]
                    if item["status"] in {"pending", "in_review"}
                },
                {"duplicate_candidate"},
            )
            self.assertEqual(
                ledger.list_duplicate_candidates(
                    status="pending",
                    document_id=document_id,
                )[0]["id"],
                duplicate_candidate_id,
            )
            with open(receipt_path, "rb") as handle:
                self.assertEqual(
                    hashlib.sha256(handle.read()).hexdigest(),
                    hashlib.sha256(source_bytes).hexdigest(),
                )
            self.assertEqual(os.stat(receipt_path).st_mtime_ns, source_mtime)

    def test_reprocess_review_queue_audits_resolved_and_new_review_items(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = os.path.join(temp_dir, "mantel-review.pdf")
            with open(receipt_path, "wb") as handle:
                handle.write(b"retained source")
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "mantel-original",
                "originalFilename": "mantel-original.pdf",
                "documentType": "receipt",
                "processingStatus": "processed",
                "vendorName": "Mantel",
                "transactionDate": "2023-07-08",
                "totalAmount": 24.95,
                "extractedData": {
                    "vendor_name": "Mantel",
                    "transaction_date": "2023-07-08",
                    "total_amount": 24.95,
                },
            })
            document_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "mantel-review",
                "originalFilename": "mantel-review.pdf",
                "mimeType": "application/pdf",
                "storagePath": receipt_path,
                "documentType": "receipt",
                "processingStatus": "needs_review",
                "ocrText": "mantel A\\\n08-07-2023\nTOTAAL 24,95",
                "vendorName": "mantel A\\",
                "category": "Manual Review",
                "transactionDate": "2023-07-08",
                "totalAmount": 24.95,
            })
            prior_review_id = ledger.create_review_item({
                "documentId": document_id,
                "reason": "validation_failed",
                "details": "Stale extraction requires review.",
            })

            summary = LocalDocumentProcessor(
                ledger,
                processor_pipeline=RaisingPipeline(),
                categorizer=StaticCategorizer(confidence_score=0.99),
                validator=StaticValidator(),
            ).reprocess_review_queue(
                actor="test-operator",
                create_backup=False,
            )

            self.assertEqual(summary["resolvedReviewItems"], 1)
            self.assertEqual(summary["openedReviewItems"], 1)
            self.assertEqual(
                summary["documents"][0]["resolvedReviewItemIds"],
                [prior_review_id],
            )
            self.assertEqual(
                summary["documents"][0]["openedReviewItems"],
                1,
            )
            document = ledger.get_document(document_id)
            self.assertEqual(document["processing_status"], "needs_review")
            self.assertEqual(
                {
                    item["reason"]
                    for item in document["review_items"]
                    if item["status"] in {"pending", "in_review"}
                },
                {"duplicate_candidate"},
            )

    def test_reprocess_review_queue_fails_closed_when_backup_is_invalid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = os.path.join(temp_dir, "blocked-review.pdf")
            with open(receipt_path, "wb") as handle:
                handle.write(b"retained source")
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "blocked-review",
                "originalFilename": "blocked-review.pdf",
                "mimeType": "application/pdf",
                "storagePath": receipt_path,
                "documentType": "receipt",
                "processingStatus": "needs_review",
                "ocrText": "ACTION\n12-07-2023\nTOTAAL 1,98",
                "vendorName": "Unchanged Vendor",
                "category": "Manual Review",
                "totalAmount": 0.16,
            })
            ledger.create_review_item({
                "documentId": document_id,
                "reason": "validation_failed",
                "details": "Machine review gate.",
            })
            processor = LocalDocumentProcessor(
                ledger,
                config={
                    "fab_local_backup_dir": os.path.join(temp_dir, "backups"),
                },
                processor_pipeline=RaisingPipeline(),
            )

            with patch(
                "src.operations.local_processing.LocalBackupService.inspect_backup",
                return_value={"success": False, "status": "invalid"},
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "validated, checksum-bound",
                ):
                    processor.reprocess_review_queue(actor="test-operator")

            document = ledger.get_document(document_id)
            self.assertEqual(document["vendor_name"], "Unchanged Vendor")
            self.assertEqual(document["total_amount"], 0.16)
            self.assertNotIn(
                "storedOcrReassessment",
                (document.get("metadata") or {}).get("processing") or {},
            )
            self.assertEqual(
                ledger.list_audit_events(limit=1)[0]["action"],
                "local_processing.review_queue_reassessment_blocked",
            )

    def test_reprocess_review_queue_keeps_implausible_transaction_year_blocked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = os.path.join(temp_dir, "future-year.pdf")
            with open(receipt_path, "wb") as handle:
                handle.write(b"retained source")
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "future-year-review",
                "originalFilename": "future-year.pdf",
                "mimeType": "application/pdf",
                "storagePath": receipt_path,
                "documentType": "receipt",
                "processingStatus": "needs_review",
                "ocrText": "Example Shop\n2823-07-16\nTOTAAL 59,60",
                "vendorName": "Example Shop",
                "category": "Manual Review",
                "transactionDate": "2823-07-16",
                "totalAmount": 59.60,
            })
            ledger.create_review_item({
                "documentId": document_id,
                "reason": "validation_failed",
                "details": "Machine review gate.",
            })

            summary = LocalDocumentProcessor(
                ledger,
                processor_pipeline=RaisingPipeline(),
            ).reprocess_review_queue(
                actor="test-operator",
                create_backup=False,
            )

            self.assertEqual(summary["requested"], 1)
            self.assertEqual(summary["resolvedReviewItems"], 0)
            document = ledger.get_document(document_id)
            self.assertEqual(document["processing_status"], "needs_review")
            self.assertEqual(document["transaction_date"], "")
            self.assertIn(
                "Missing required field: transaction_date",
                "; ".join(
                    document["metadata"]["processing"]["validation"]["errors"]
                ),
            )
            open_reasons = {
                item["reason"]
                for item in document["review_items"]
                if item["status"] in {"pending", "in_review"}
            }
            self.assertIn("validation_failed", open_reasons)

    def test_reprocess_review_queue_clears_unsupported_stale_amounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = os.path.join(temp_dir, "ambiguous.pdf")
            with open(receipt_path, "wb") as handle:
                handle.write(b"retained source")
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "ambiguous-review",
                "originalFilename": "ambiguous.pdf",
                "mimeType": "application/pdf",
                "storagePath": receipt_path,
                "documentType": "receipt",
                "processingStatus": "needs_review",
                "ocrText": (
                    "Praxis\nBTW DETAIL BTW Excl. Incl.\n"
                    "21% 1,59 unreadable\nTOTAAL 1,59 1,51 16"
                ),
                "vendorName": "Praxis",
                "category": "Manual Review",
                "totalAmount": 1.59,
                "vatAmount": 50.0,
            })
            ledger.create_review_item({
                "documentId": document_id,
                "reason": "validation_failed",
                "details": "Unsupported stale amount.",
            })

            LocalDocumentProcessor(
                ledger,
                processor_pipeline=RaisingPipeline(),
            ).reprocess_review_queue(create_backup=False)

            document = ledger.get_document(document_id)
            self.assertIsNone(document["total_amount"])
            self.assertIsNone(document["vat_amount"])

    def test_process_text_document_flags_fuzzy_duplicate_candidate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first_path = os.path.join(temp_dir, "first.txt")
            second_path = os.path.join(temp_dir, "second.txt")
            with open(first_path, "w", encoding="utf-8") as handle:
                handle.write("Vendor: Office Shop\nDate: 2026-06-28\nTotal: EUR 42.50\n")
            with open(second_path, "w", encoding="utf-8") as handle:
                handle.write("Supplier: Office Shop\nDate: 2026-06-28\nAmount: EUR 42.50\nDifferent scan bytes\n")
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            original_id = ledger.register_document({
                "source": "local_folder",
                "sourceDocumentId": "first",
                "originalFilename": "first.txt",
                "mimeType": "text/plain",
                "storagePath": first_path,
                "documentType": "text",
                "processingStatus": "processed",
                "vendorName": "Office Shop",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
                "extractedData": {
                    "vendor_name": "Office Shop",
                    "transaction_date": "2026-06-28",
                    "total_amount": 42.5,
                },
            })
            duplicate_id = ledger.register_document({
                "source": "local_folder",
                "sourceDocumentId": "second",
                "originalFilename": "second.txt",
                "mimeType": "text/plain",
                "storagePath": second_path,
                "documentType": "text",
                "processingStatus": "imported",
            })

            result = LocalDocumentProcessor(
                ledger,
                categorizer=StaticCategorizer(),
                validator=StaticValidator(),
            ).process_document(duplicate_id)

            self.assertEqual(result["status"], "needs_review")
            self.assertIn("duplicate_candidate", result["reviewReasons"])
            document = ledger.get_document(duplicate_id)
            self.assertIsNone(document["duplicate_of_document_id"])
            self.assertEqual(document["processing_status"], "needs_review")
            candidates = ledger.list_duplicate_candidates(status="pending", document_id=duplicate_id)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["candidate_document_id"], original_id)
            self.assertGreaterEqual(candidates[0]["confidence_score"], 0.9)
            self.assertEqual(ledger.list_review_items(status="pending")[0]["reason"], "duplicate_candidate")

    def test_duplicate_reassessment_rejects_false_reference_and_canonicalizes_pair(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))

            def register(source_id, vendor, date, amount, invoice):
                return ledger.register_document({
                    "source": "gmail",
                    "sourceDocumentId": source_id,
                    "originalFilename": f"{source_id}.pdf",
                    "documentType": "vendor_invoice",
                    "processingStatus": "needs_review",
                    "vendorName": vendor,
                    "transactionDate": date,
                    "totalAmount": amount,
                    "category": "Manual Review",
                    "extractedData": {
                        "vendor_name": vendor,
                        "transaction_date": date,
                        "total_amount": amount,
                        "invoice_number": invoice,
                    },
                })

            april_id = register("tmobile-april", "T-Mobile", "2023-04-21", 37.68, "staat")
            may_id = register("tmobile-may", "T-Mobile", "2023-05-19", 37.68, "staat")
            stale_id = ledger.record_duplicate_candidate({
                "documentId": may_id,
                "candidateDocumentId": april_id,
                "matchType": "exact_fingerprint_match",
                "confidenceScore": 1.0,
                "status": "pending",
            })
            ledger.create_review_item({
                "documentId": may_id,
                "reason": "duplicate_candidate",
                "details": "Stale recurring-charge match.",
            })
            ledger.create_review_item({
                "documentId": may_id,
                "reason": "manual_review_category",
                "details": "Category still requires review.",
            })

            canonical_id = register(
                "same-receipt-first",
                "Praxis",
                "2023-06-12",
                6.39,
                None,
            )
            subject_id = register(
                "same-receipt-second",
                "Praxis",
                "2023-06-12",
                6.39,
                None,
            )
            forward_id = ledger.record_duplicate_candidate({
                "documentId": canonical_id,
                "candidateDocumentId": subject_id,
                "matchType": "exact_fingerprint_match",
                "confidenceScore": 1.0,
                "status": "pending",
            })
            reverse_id = ledger.record_duplicate_candidate({
                "documentId": subject_id,
                "candidateDocumentId": canonical_id,
                "matchType": "fuzzy_document_match",
                "confidenceScore": 0.93,
                "status": "pending",
            })
            for document_id in (canonical_id, subject_id):
                ledger.create_review_item({
                    "documentId": document_id,
                    "reason": "duplicate_candidate",
                    "details": "Reciprocal duplicate evidence.",
                })

            summary = LocalDocumentProcessor(ledger).reassess_duplicate_candidates(
                actor="test-reassessment",
                create_backup=False,
            )

            self.assertEqual(summary["candidatePairs"], 2)
            self.assertEqual(summary["rejectedPairs"], 1)
            self.assertEqual(summary["retainedPairs"], 1)
            self.assertFalse(summary["sourceFilesModified"])
            self.assertFalse(summary["confirmedDuplicateLinksModified"])
            candidates_by_id = {
                item["id"]: item
                for item in ledger.list_duplicate_candidates(limit=20)
            }
            self.assertEqual(candidates_by_id[stale_id]["status"], "rejected")
            self.assertEqual(
                {
                    item["reason"]
                    for item in ledger.list_review_items(
                        status=("pending", "in_review"),
                        document_id=may_id,
                    )
                },
                {"manual_review_category"},
            )
            self.assertEqual(ledger.get_document(may_id)["processing_status"], "needs_review")

            open_candidates = ledger.list_duplicate_candidates(
                status=("pending", "in_review"),
                limit=20,
            )
            self.assertEqual(len(open_candidates), 1)
            self.assertEqual(open_candidates[0]["document_id"], subject_id)
            self.assertEqual(open_candidates[0]["candidate_document_id"], canonical_id)
            self.assertEqual(open_candidates[0]["match_type"], "exact_fingerprint_match")
            self.assertEqual(candidates_by_id[forward_id]["status"], "rejected")
            self.assertEqual(candidates_by_id[reverse_id]["status"], "rejected")
            self.assertEqual(
                ledger.list_review_items(
                    status=("pending", "in_review"),
                    document_id=canonical_id,
                ),
                [],
            )
            self.assertEqual(
                {
                    item["reason"]
                    for item in ledger.list_review_items(
                        status=("pending", "in_review"),
                        document_id=subject_id,
                    )
                },
                {"duplicate_candidate"},
            )

    def test_duplicate_reassessment_plan_uses_one_compact_document_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            canonical_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "monthly-charge-april",
                "originalFilename": "april.pdf",
                "documentType": "vendor_invoice",
                "processingStatus": "needs_review",
                "vendorName": "T-Mobile",
                "transactionDate": "2023-04-21",
                "totalAmount": 37.68,
                "extractedData": {"invoice_number": "april-statement"},
            })
            subject_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "monthly-charge-may",
                "originalFilename": "may.pdf",
                "documentType": "vendor_invoice",
                "processingStatus": "needs_review",
                "vendorName": "T-Mobile",
                "transactionDate": "2023-05-19",
                "totalAmount": 37.68,
                "extractedData": {"invoice_number": "may-statement"},
            })
            ledger.record_duplicate_candidate({
                "documentId": subject_id,
                "candidateDocumentId": canonical_id,
                "matchType": "fuzzy_document_match",
                "confidenceScore": 0.95,
                "status": "pending",
            })
            ledger.create_review_item({
                "documentId": subject_id,
                "reason": "duplicate_candidate",
                "details": "Review stale recurring-charge evidence.",
            })

            with patch.object(
                ledger,
                "get_documents_with_review_items",
                wraps=ledger.get_documents_with_review_items,
            ) as compact_snapshot, patch.object(
                ledger,
                "get_document",
                side_effect=AssertionError("full document history must not be loaded"),
            ) as full_document:
                plans = duplicate_candidate_reassessment_plan(ledger)

            compact_snapshot.assert_called_once()
            full_document.assert_not_called()
            self.assertEqual(len(plans), 1)
            self.assertEqual(plans[0]["pair"], [canonical_id, subject_id])
            self.assertEqual(plans[0]["action"], "reject")
            self.assertEqual(
                plans[0]["subjectDocument"]["review_items"][0]["reason"],
                "duplicate_candidate",
            )

    def test_duplicate_reassessment_clears_only_disproven_pending_pair_link(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            canonical_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "recurring-charge-april",
                "originalFilename": "april.pdf",
                "documentType": "vendor_invoice",
                "processingStatus": "needs_review",
                "vendorName": "T-Mobile",
                "transactionDate": "2023-04-21",
                "totalAmount": 37.68,
                "extractedData": {
                    "vendor_name": "T-Mobile",
                    "transaction_date": "2023-04-21",
                    "total_amount": 37.68,
                    "invoice_number": "april-statement",
                },
            })
            subject_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "recurring-charge-may",
                "originalFilename": "may.pdf",
                "documentType": "vendor_invoice",
                "processingStatus": "needs_review",
                "vendorName": "T-Mobile",
                "transactionDate": "2023-05-19",
                "totalAmount": 37.68,
                "duplicateOfDocumentId": canonical_id,
                "extractedData": {
                    "vendor_name": "T-Mobile",
                    "transaction_date": "2023-05-19",
                    "total_amount": 37.68,
                    "invoice_number": "may-statement",
                },
            })
            candidate_id = ledger.record_duplicate_candidate({
                "documentId": subject_id,
                "candidateDocumentId": canonical_id,
                "matchType": "fuzzy_document_match",
                "confidenceScore": 0.95,
                "status": "pending",
            })
            duplicate_review_id = ledger.create_review_item({
                "documentId": subject_id,
                "reason": "duplicate_candidate",
                "details": "Provisional duplicate link requires review.",
            })
            manual_review_id = ledger.create_review_item({
                "documentId": subject_id,
                "reason": "manual_review_category",
                "details": "Category remains a separate decision.",
            })
            confirmed_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "confirmed-duplicate",
                "originalFilename": "confirmed.pdf",
                "documentType": "vendor_invoice",
                "processingStatus": "duplicate",
                "vendorName": "T-Mobile",
                "transactionDate": "2023-05-19",
                "totalAmount": 37.68,
                "duplicateOfDocumentId": canonical_id,
            })
            confirmed_candidate_id = ledger.record_duplicate_candidate({
                "documentId": confirmed_id,
                "candidateDocumentId": canonical_id,
                "matchType": "fuzzy_document_match",
                "confidenceScore": 0.95,
                "status": "pending",
            })

            summary = LocalDocumentProcessor(
                ledger,
            ).reassess_duplicate_candidates(
                actor="test-operator",
                create_backup=False,
            )

            self.assertEqual(summary["candidatePairs"], 1)
            self.assertEqual(summary["rejectedPairs"], 1)
            self.assertEqual(summary["pendingDuplicateLinksCleared"], 1)
            self.assertEqual(summary["resolvedReviewItems"], 1)
            self.assertFalse(summary["confirmedDuplicateLinksModified"])
            subject = ledger.get_document(subject_id)
            self.assertIsNone(subject["duplicate_of_document_id"])
            self.assertEqual(subject["processing_status"], "needs_review")
            self.assertEqual(
                {
                    item["id"]: item["status"]
                    for item in subject["review_items"]
                },
                {
                    duplicate_review_id: "resolved",
                    manual_review_id: "pending",
                },
            )
            candidates = {
                item["id"]: item
                for item in ledger.list_duplicate_candidates(limit=20)
            }
            self.assertEqual(candidates[candidate_id]["status"], "rejected")
            self.assertEqual(
                candidates[confirmed_candidate_id]["status"],
                "pending",
            )
            confirmed = ledger.get_document(confirmed_id)
            self.assertEqual(
                confirmed["duplicate_of_document_id"],
                canonical_id,
            )
            self.assertEqual(confirmed["processing_status"], "duplicate")
            audit_event = next(
                event
                for event in ledger.list_audit_events(limit=20)
                if event["action"]
                == "local_processing.duplicate_candidate_reassessed"
            )
            self.assertEqual(
                audit_event["details"][
                    "clearedPendingDuplicateLinkDocumentIds"
                ],
                [subject_id],
            )

    def test_duplicate_cycle_repair_clears_links_and_preserves_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            paths = []
            document_ids = []
            for index in range(3):
                path = os.path.join(temp_dir, f"scan-{index}.pdf")
                with open(path, "wb") as handle:
                    handle.write(f"retained evidence {index}".encode("ascii"))
                paths.append(path)
                document_ids.append(ledger.register_document({
                    "source": "gmail",
                    "sourceDocumentId": f"cycle-{index}",
                    "originalFilename": f"scan-{index}.pdf",
                    "storagePath": path,
                    "documentType": "receipt",
                    "processingStatus": "needs_review",
                }))
            for index, document_id in enumerate(document_ids):
                candidate_id = document_ids[(index + 1) % len(document_ids)]
                ledger.update_document(document_id, {
                    "duplicateOfDocumentId": candidate_id,
                })
                ledger.record_duplicate_candidate({
                    "documentId": document_id,
                    "candidateDocumentId": candidate_id,
                    "matchType": "legacy_fuzzy_document_match",
                    "confidenceScore": 0.95,
                    "status": "pending",
                })
                ledger.create_review_item({
                    "documentId": document_id,
                    "reason": "duplicate_candidate",
                    "details": "Compare retained source documents.",
                })
            source_state = {}
            for path in paths:
                with open(path, "rb") as handle:
                    source_state[path] = (handle.read(), os.stat(path).st_mtime_ns)

            self.assertEqual(duplicate_link_cycles(ledger), [sorted(document_ids)])
            summary = LocalDocumentProcessor(ledger).repair_duplicate_cycles(
                actor="test-operator",
                create_backup=False,
            )

            self.assertEqual(summary["cyclesFound"], 1)
            self.assertEqual(summary["cyclesRepaired"], 1)
            self.assertEqual(summary["documentsCleared"], 3)
            self.assertEqual(summary["reviewItemsCreated"], 0)
            self.assertEqual(duplicate_link_cycles(ledger), [])
            self.assertEqual(
                len(ledger.list_duplicate_candidates(status="pending")),
                3,
            )
            self.assertEqual(
                len(ledger.list_review_items(status="pending")),
                3,
            )
            for document_id in document_ids:
                document = ledger.get_document(document_id)
                self.assertIsNone(document["duplicate_of_document_id"])
                self.assertEqual(document["processing_status"], "needs_review")
            for path, (contents, modified_at) in source_state.items():
                with open(path, "rb") as handle:
                    self.assertEqual(handle.read(), contents)
                self.assertEqual(os.stat(path).st_mtime_ns, modified_at)
            audit_actions = {
                event["action"] for event in ledger.list_audit_events(limit=20)
            }
            self.assertIn("local_processing.duplicate_cycle_repaired", audit_actions)
            self.assertIn(
                "local_processing.duplicate_cycle_repair_completed",
                audit_actions,
            )

    def test_duplicate_matching_never_uses_an_existing_duplicate_as_canonical(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            current_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "current-document",
                "originalFilename": "current.pdf",
                "documentType": "receipt",
                "vendorName": "Praxis",
                "transactionDate": "2023-07-07",
                "totalAmount": -25.0,
                "processingStatus": "needs_review",
            })
            reverse_duplicate_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "reverse-duplicate",
                "originalFilename": "reverse.pdf",
                "documentType": "receipt",
                "vendorName": "Praxis",
                "transactionDate": "2023-07-07",
                "totalAmount": -25.0,
                "duplicateOfDocumentId": current_id,
                "processingStatus": "needs_review",
            })

            current = ledger.get_document(current_id)
            result = LocalDocumentProcessor(ledger)._duplicate_match(
                current,
                {
                    "vendor_name": "Praxis",
                    "transaction_date": "2023-07-07",
                    "total_amount": -25.0,
                },
                {"ocr_text": "Praxis\n07-07-2023\nTerugbetaling 25,00"},
            )

            self.assertFalse(result["is_duplicate"])
            self.assertEqual(
                ledger.get_document(reverse_duplicate_id)["duplicate_of_document_id"],
                current_id,
            )

    def test_retry_failed_reprocesses_document_and_resolves_failure_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = os.path.join(temp_dir, "receipt.pdf")
            with open(receipt_path, "wb") as handle:
                handle.write(b"receipt bytes")
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "local_folder",
                "sourceDocumentId": "retry-failed",
                "originalFilename": "receipt.pdf",
                "mimeType": "application/pdf",
                "storagePath": receipt_path,
                "documentType": "receipt",
                "processingStatus": "imported",
            })

            failed = LocalDocumentProcessor(
                ledger,
                processor_pipeline=RaisingPipeline(),
                categorizer=StaticCategorizer(),
                validator=StaticValidator(),
            ).process_document(document_id)
            retried = LocalDocumentProcessor(
                ledger,
                processor_pipeline=StaticPipeline(),
                categorizer=StaticCategorizer(),
                validator=StaticValidator(),
            ).retry_failed(actor="tester")

            self.assertEqual(failed["status"], "failed")
            self.assertEqual(retried["requested"], 1)
            self.assertEqual(retried["retried"], 1)
            self.assertEqual(retried["processed"], 1)
            self.assertEqual(retried["documents"][0]["retryCount"], 1)
            document = ledger.get_document(document_id)
            self.assertEqual(document["processing_status"], "processed")
            self.assertEqual(document["vendor_name"], "Retry Vendor")
            self.assertEqual(document["metadata"]["processing"]["retryCount"], 1)
            self.assertEqual(document["metadata"]["processing"]["retryHistory"][0]["previousError"], "OCR engine unavailable")
            failure_reviews = [
                item for item in document["review_items"]
                if item["reason"] == "processing_failed"
            ]
            self.assertEqual(failure_reviews[0]["status"], "resolved")
            audit_actions = [event["action"] for event in ledger.list_audit_events(limit=20)]
            self.assertIn("local_processing.retry_started", audit_actions)
            self.assertIn("local_processing.processing_failed_review_resolved", audit_actions)
            self.assertIn("local_processing.retry_failed_completed", audit_actions)

    def test_trusted_category_automation_resolves_only_category_gates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            guarded_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "praxis-review",
                "originalFilename": "praxis.pdf",
                "mimeType": "application/pdf",
                "documentType": "receipt",
                "processingStatus": "needs_review",
                "vendorName": "Praxis",
                "category": "Manual Review",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
                "confidenceScore": 0.1,
            })
            ready_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "tmobile-review",
                "originalFilename": "tmobile.pdf",
                "mimeType": "application/pdf",
                "documentType": "receipt",
                "processingStatus": "needs_review",
                "vendorName": "T-Mobile",
                "category": "Manual Review",
                "transactionDate": "2026-06-27",
                "totalAmount": 35.0,
                "confidenceScore": 0.1,
                "metadata": {
                    "processing": {
                        "documentTypeClassification": {
                            "documentType": "unknown",
                            "postingEligible": False,
                            "reviewRequired": False,
                        },
                    },
                },
            })
            for document_id in (guarded_id, ready_id):
                for reason in (
                    "low_confidence_categorization",
                    "manual_review_category",
                ):
                    ledger.create_review_item({
                        "documentId": document_id,
                        "reason": reason,
                        "details": "Category requires review.",
                    })
            ledger.create_review_item({
                "documentId": guarded_id,
                "reason": "validation_failed",
                "details": "VAT evidence requires review.",
            })
            duplicate_guarded_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "duplicate-praxis-review",
                "originalFilename": "duplicate-praxis.pdf",
                "mimeType": "application/pdf",
                "documentType": "receipt",
                "processingStatus": "needs_review",
                "vendorName": "Praxis",
                "category": "Manual Review",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
                "confidenceScore": 0.1,
            })
            for reason in (
                "low_confidence_categorization",
                "manual_review_category",
                "duplicate_candidate",
            ):
                ledger.create_review_item({
                    "documentId": duplicate_guarded_id,
                    "reason": reason,
                    "details": "Duplicate evidence requires review.",
                })
            confirmed_duplicate_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "confirmed-duplicate-praxis-review",
                "originalFilename": "confirmed-duplicate-praxis.pdf",
                "mimeType": "application/pdf",
                "documentType": "receipt",
                "processingStatus": "needs_review",
                "vendorName": "Praxis",
                "category": "Manual Review",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
                "confidenceScore": 0.1,
                "duplicateOfDocumentId": guarded_id,
            })
            for reason in (
                "low_confidence_categorization",
                "manual_review_category",
            ):
                ledger.create_review_item({
                    "documentId": confirmed_duplicate_id,
                    "reason": reason,
                    "details": "Confirmed duplicate stays unchanged.",
                })

            summary = LocalDocumentProcessor(
                ledger,
            ).apply_trusted_category_suggestions()

            self.assertEqual(summary["candidates"], 3)
            self.assertEqual(summary["updatedDocuments"], 3)
            self.assertEqual(summary["resolvedReviewItems"], 6)
            self.assertEqual(summary["stillNeedsReview"], 2)
            self.assertEqual(summary["readyDocuments"], 1)
            self.assertEqual(summary["externalSubmission"], "not_executed")
            self.assertEqual(summary["preMutationBackup"]["status"], "valid")
            self.assertEqual(len(summary["preMutationBackup"]["ledgerSha256"]), 64)
            duplicate_guarded = ledger.get_document(duplicate_guarded_id)
            self.assertEqual(
                duplicate_guarded["category"],
                "Construction Materials & Tools",
            )
            self.assertEqual(duplicate_guarded["processing_status"], "needs_review")
            self.assertEqual(
                {
                    item["reason"]
                    for item in duplicate_guarded["review_items"]
                    if item["status"] in {"pending", "in_review"}
                },
                {"duplicate_candidate"},
            )
            confirmed_duplicate = ledger.get_document(confirmed_duplicate_id)
            self.assertEqual(confirmed_duplicate["category"], "Manual Review")
            self.assertEqual(confirmed_duplicate["processing_status"], "needs_review")

            guarded = ledger.get_document(guarded_id)
            ready = ledger.get_document(ready_id)
            self.assertEqual(guarded["category"], "Construction Materials & Tools")
            self.assertEqual(guarded["processing_status"], "needs_review")
            self.assertEqual(
                guarded["metadata"]["processing"]["reviewReasons"],
                ["validation_failed"],
            )
            self.assertEqual(ready["category"], "Telecommunications")
            self.assertEqual(ready["processing_status"], "processed")
            self.assertGreaterEqual(ready["confidence_score"], 0.95)
            self.assertEqual(
                {
                    item["reason"]
                    for item in guarded["review_items"]
                    if item["status"] in {"pending", "in_review"}
                },
                {"validation_failed"},
            )
            self.assertEqual(
                [
                    item
                    for item in ready["review_items"]
                    if item["status"] in {"pending", "in_review"}
                ],
                [],
            )
            category_field = next(
                field
                for field in ready["extracted_fields"]
                if field["field_name"] == "category"
            )
            self.assertEqual(category_field["field_value"], "Telecommunications")
            self.assertEqual(
                category_field["provenance"]["policy"],
                "builtin_exact_vendor_taxonomy_v1",
            )
            audit_actions = {
                event["action"] for event in ledger.list_audit_events(limit=20)
            }
            self.assertIn("local_processing.trusted_category_applied", audit_actions)
            self.assertIn(
                "local_processing.trusted_category_batch_completed",
                audit_actions,
            )

    def test_trusted_category_automation_fails_closed_without_verified_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "backup-blocked-praxis",
                "originalFilename": "praxis.pdf",
                "documentType": "receipt",
                "processingStatus": "needs_review",
                "vendorName": "Praxis",
                "category": "Manual Review",
            })
            for reason in (
                "low_confidence_categorization",
                "manual_review_category",
            ):
                ledger.create_review_item({
                    "documentId": document_id,
                    "reason": reason,
                    "details": "Category requires review.",
                })

            with patch(
                "src.operations.local_processing.LocalBackupService.create_backup",
                side_effect=OSError("simulated backup failure"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "pre-mutation backup could not be verified",
                ):
                    LocalDocumentProcessor(
                        ledger,
                    ).apply_trusted_category_suggestions()

            document = ledger.get_document(document_id)
            self.assertEqual(document["category"], "Manual Review")
            self.assertEqual(document["processing_status"], "needs_review")
            self.assertEqual(
                {
                    item["reason"]
                    for item in ledger.list_review_items(
                        status=("pending", "in_review"),
                        document_id=document_id,
                    )
                },
                {
                    "low_confidence_categorization",
                    "manual_review_category",
                },
            )
            blocked = next(
                event
                for event in ledger.list_audit_events(limit=10)
                if event["action"] == "local_processing.trusted_category_batch_blocked"
            )
            self.assertEqual(
                blocked["details"]["reason"],
                "pre_mutation_backup_failed",
            )
            self.assertEqual(
                blocked["details"]["externalSubmission"],
                "not_executed",
            )


if __name__ == "__main__":
    unittest.main()
