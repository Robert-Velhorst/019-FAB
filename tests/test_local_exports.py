import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from src.operations.local_exports import (
    EXPORT_APPROVAL_PHRASE,
    EXPORT_REJECTION_PHRASE,
    EXPORT_RESULT_CONFIRMATION_PHRASE,
    LocalExportAttemptService,
)
from src.operations.local_bookkeeping_records import LocalBookkeepingRecordService
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_master_ledger import LocalMasterLedgerService
from src.operations.local_routing import LocalRoutingService


class _FakeWaveExecutor:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return dict(self.result)


class TestLocalExportAttemptService(unittest.TestCase):
    def test_source_processing_blocks_old_draft_approval_and_execution(self):
        scenarios = [
            (stage, status, expected)
            for stage in ("approval", "execution", "deferred")
            for status, expected in (
                ("failed", "blocked_processing"),
                ("duplicate", "blocked_duplicate"),
                ("needs_review", "blocked_by_review"),
            )
        ]
        for stage, status, expected in scenarios:
            with self.subTest(stage=stage, status=status), tempfile.TemporaryDirectory() as temp_dir:
                ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
                source_path = os.path.join(temp_dir, "receipt.txt")
                source_bytes = b"Synthetic receipt evidence"
                with open(source_path, "wb") as source:
                    source.write(source_bytes)
                document_id = ledger.register_document({
                    "source": "scanner", "sourceDocumentId": "reprocessing-export",
                    "originalFilename": "receipt.txt", "storagePath": source_path,
                    "documentType": "receipt", "processingStatus": "reviewed",
                    "vendorName": "Office Shop", "category": "Office Supplies",
                    "transactionDate": "2026-06-28", "totalAmount": 42.5,
                })
                route = LocalRoutingService(ledger).prepare_document_route(document_id)
                executor = _FakeWaveExecutor({"status": "success", "external_id": "must-not-send"})
                service = LocalExportAttemptService(ledger, wave_executor=executor)
                prepared = service.prepare_from_routing_attempt(route["routingAttemptId"])
                attempt_id = prepared["exportAttemptId"]
                original_payload = ledger.get_export_attempt(attempt_id)["payload"]
                if stage != "approval":
                    approved = service.approve_attempt(attempt_id, confirmation=EXPORT_APPROVAL_PHRASE)
                    self.assertEqual(approved["status"], "approved")
                if stage == "deferred":
                    ledger.update_export_attempt(attempt_id, {"status": "deferred"})
                # Do not refresh the normalized record: the final gate must read the source.
                ledger.update_document(document_id, {"processingStatus": status})

                result = (service.execute_attempt(attempt_id) if stage != "approval"
                          else service.approve_attempt(attempt_id, confirmation=EXPORT_APPROVAL_PHRASE))

                self.assertEqual(result["status"], expected)
                self.assertFalse(result["success"])
                self.assertEqual(result["externalSubmission"], "not_executed")
                self.assertEqual(executor.calls, [])
                attempt = ledger.get_export_attempt(attempt_id)
                self.assertNotEqual(attempt["status"], "execution_in_progress")
                self.assertNotIn(attempt["status"], {"approved", "deferred"})
                self.assertEqual(attempt["payload"], original_payload)
                record = ledger.get_bookkeeping_record_by_document(document_id)
                self.assertEqual(record["export_status"], expected)
                self.assertEqual(attempt["approval_required"], 1)
                audit = [event for event in ledger.list_audit_events(limit=50)
                         if event["action"] == "local_export_attempt.source_processing_blocked"]
                self.assertEqual(len(audit), 1)
                self.assertEqual(audit[0]["details"]["reason"], expected)
                with open(source_path, "rb") as source:
                    self.assertEqual(source.read(), source_bytes)
                if stage == "execution" and status == "failed":
                    ledger.update_document(document_id, {"processingStatus": "reviewed"})
                    self.assertEqual(service.execute_attempt(attempt_id)["status"], "not_approved")
                    self.assertEqual(executor.calls, [])
                    reapproved = service.approve_attempt(attempt_id, confirmation=EXPORT_APPROVAL_PHRASE)
                    self.assertEqual(reapproved["status"], "approved")
                    self.assertEqual(service.execute_attempt(attempt_id)["status"], "executed")
                    self.assertEqual(len(executor.calls), 1)

    def _register_mijngeldzaken_document(self, ledger):
        return ledger.register_document({
            "source": "scanner",
            "sourceDocumentId": "scan-export-mgz",
            "originalFilename": "groceries.txt",
            "documentType": "receipt",
            "processingStatus": "reviewed",
            "vendorName": "Local Supermarket",
            "category": "Personal",
            "transactionDate": "2026-06-28",
            "totalAmount": 42.5,
            "extractedData": {
                "vendor_name": "Local Supermarket",
                "transaction_date": "2026-06-28",
                "total_amount": 42.5,
                "description": "Weekly groceries",
            },
            "metadata": {"targetSystem": "mijngeldzaken"},
        })

    def _change_mijngeldzaken_document_amount(self, ledger, document_id, amount=99.99):
        ledger.update_document(document_id, {
            "source": "scanner",
            "sourceDocumentId": "scan-export-mgz",
            "originalFilename": "groceries.txt",
            "documentType": "receipt",
            "processingStatus": "export_draft_prepared",
            "vendorName": "Local Supermarket",
            "category": "Personal",
            "transactionDate": "2026-06-28",
            "totalAmount": amount,
            "extractedData": {
                "vendor_name": "Local Supermarket",
                "transaction_date": "2026-06-28",
                "total_amount": amount,
                "description": "Weekly groceries",
            },
            "metadata": {"targetSystem": "mijngeldzaken"},
        })

    def _prepare_mijngeldzaken_export(self, ledger):
        route_config = {"mijngeldzaken_category_mapping": {"Personal": "Huishouden"}}
        document_id = self._register_mijngeldzaken_document(ledger)
        route = LocalRoutingService(ledger, route_config).prepare_document_route(document_id)
        service = LocalExportAttemptService(ledger)
        prepared = service.prepare_from_routing_attempt(route["routingAttemptId"])
        return document_id, service, prepared

    def test_prepare_approve_and_record_export_result_without_silent_submission(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-export-1",
                "originalFilename": "receipt.txt",
                "documentType": "receipt",
                "processingStatus": "reviewed",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
                "extractedData": {
                    "line_items": [{
                        "description": "Printer paper",
                        "amount": 42.5,
                        "account_name": "Office Supplies",
                        "tax_code": "BTW 21%",
                    }]
                },
            })
            route = LocalRoutingService(ledger).prepare_document_route(document_id)
            service = LocalExportAttemptService(ledger)

            prepared = service.prepare_from_routing_attempt(route["routingAttemptId"])
            blocked_approval = service.approve_attempt(prepared["exportAttemptId"], confirmation="wrong")
            approved = service.approve_attempt(
                prepared["exportAttemptId"],
                actor="tester",
                confirmation=EXPORT_APPROVAL_PHRASE,
            )
            blocked_result = service.record_result(
                prepared["exportAttemptId"],
                "executed",
                confirmation="wrong",
            )
            recorded = service.record_result(
                prepared["exportAttemptId"],
                "executed",
                external_id="wave-tx-1",
                result={"waveId": "wave-tx-1"},
                actor="tester",
                confirmation=EXPORT_RESULT_CONFIRMATION_PHRASE,
            )

            self.assertTrue(prepared["success"])
            self.assertEqual(prepared["status"], "approval_required")
            self.assertEqual(prepared["exportAttempt"]["external_submission"], "not_executed")
            self.assertEqual(blocked_approval["status"], "requires_confirmation")
            self.assertTrue(approved["success"])
            self.assertEqual(approved["exportAttempt"]["external_submission"], "approved_not_executed")
            self.assertEqual(blocked_result["status"], "requires_confirmation")
            self.assertTrue(recorded["success"])
            self.assertEqual(recorded["externalSubmission"], "executed")
            document = ledger.get_document(document_id)
            self.assertEqual(document["export_attempts"][0]["external_id"], "wave-tx-1")
            self.assertEqual(document["bookkeeping_record"]["status"], "routed")
            self.assertEqual(document["bookkeeping_record"]["export_status"], "executed")
            self.assertEqual(ledger.dashboard_metrics()["executed_export_attempts"], 1)

    def test_prepare_ready_exports_is_idempotent_and_approval_gated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-export-2",
                "originalFilename": "receipt.txt",
                "processingStatus": "reviewed",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
            })
            LocalRoutingService(ledger).prepare_document_route(document_id)
            service = LocalExportAttemptService(ledger)

            first = service.prepare_ready_exports()
            first_updated_at = ledger.list_export_attempts()[0]["updated_at"]
            second = service.prepare_ready_exports()

            self.assertEqual(first["requested"], 1)
            self.assertEqual(first["prepared"], 1)
            self.assertTrue(first["changed"])
            self.assertTrue(first["auditRecorded"])
            self.assertEqual(second["prepared"], 0)
            self.assertEqual(second["alreadyPrepared"], 1)
            self.assertFalse(second["changed"])
            self.assertFalse(second["auditRecorded"])
            exports = ledger.list_export_attempts()
            self.assertEqual(len(exports), 1)
            self.assertEqual(exports[0]["status"], "approval_required")
            self.assertEqual(exports[0]["external_submission"], "not_executed")
            self.assertEqual(exports[0]["updated_at"], first_updated_at)
            self.assertEqual(ledger.dashboard_metrics()["export_attempts_needing_approval"], 1)
            prepared_events = [
                event for event in ledger.list_audit_events(limit=20)
                if event["action"] == "local_export_attempt.prepared"
            ]
            self.assertEqual(len(prepared_events), 1)
            batch_events = [
                event for event in ledger.list_audit_events(limit=20)
                if event["action"] == "local_export_attempt.batch_prepare_completed"
            ]
            self.assertEqual(len(batch_events), 1)

    def test_prepare_ready_exports_preserves_approved_attempts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-export-preserve",
                "originalFilename": "receipt.txt",
                "documentType": "receipt",
                "processingStatus": "reviewed",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
            })
            route = LocalRoutingService(ledger).prepare_document_route(document_id)
            service = LocalExportAttemptService(ledger)
            prepared = service.prepare_from_routing_attempt(route["routingAttemptId"])
            approved = service.approve_attempt(
                prepared["exportAttemptId"],
                actor="tester",
                confirmation=EXPORT_APPROVAL_PHRASE,
            )

            repeated = service.prepare_ready_exports()
            service.prepare_ready_exports()

            self.assertEqual(approved["status"], "approved")
            self.assertEqual(repeated["prepared"], 0)
            self.assertEqual(repeated["alreadyPrepared"], 1)
            attempts = ledger.list_export_attempts(limit=10)
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["status"], "approved")
            self.assertEqual(attempts[0]["external_submission"], "approved_not_executed")
            audit_actions = [event["action"] for event in ledger.list_audit_events(limit=20)]
            self.assertIn("local_export_attempt.prepare_preserved", audit_actions)
            preserved_events = [
                event for event in ledger.list_audit_events(limit=30)
                if event["action"] == "local_export_attempt.prepare_preserved"
            ]
            self.assertEqual(len(preserved_events), 1)

    def test_reject_export_attempt_requires_confirmation_and_preserves_rejected_attempt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id, service, prepared = self._prepare_mijngeldzaken_export(ledger)

            blocked = service.reject_attempt(prepared["exportAttemptId"], confirmation="wrong")
            rejected = service.reject_attempt(
                prepared["exportAttemptId"],
                actor="tester",
                confirmation=EXPORT_REJECTION_PHRASE,
                resolution="Not a Category A ledger item.",
            )
            repeated = service.prepare_ready_exports()

            self.assertEqual(blocked["status"], "requires_confirmation")
            self.assertEqual(blocked["confirmationPhrase"], EXPORT_REJECTION_PHRASE)
            self.assertTrue(rejected["success"])
            self.assertEqual(rejected["status"], "rejected")
            self.assertEqual(rejected["externalSubmission"], "rejected_not_executed")
            attempt = ledger.get_export_attempt(prepared["exportAttemptId"])
            self.assertEqual(attempt["status"], "rejected")
            self.assertFalse(attempt["approval_required"])
            self.assertEqual(attempt["external_submission"], "rejected_not_executed")
            document = ledger.get_document(document_id)
            self.assertEqual(document["bookkeeping_record"]["status"], "export_rejected")
            self.assertEqual(document["bookkeeping_record"]["export_status"], "rejected_not_executed")
            self.assertEqual(repeated["prepared"], 0)
            self.assertEqual(repeated["alreadyPrepared"], 1)
            self.assertEqual(len(ledger.list_export_attempts(limit=10)), 1)
            self.assertEqual(ledger.list_export_attempts(limit=10)[0]["status"], "rejected")
            audit_actions = [event["action"] for event in ledger.list_audit_events(limit=30)]
            self.assertIn("local_export_attempt.rejected", audit_actions)
            self.assertIn("local_export_attempt.prepare_preserved", audit_actions)

    def test_reject_approved_export_attempt_cancels_external_execution(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id, service, prepared = self._prepare_mijngeldzaken_export(ledger)
            service.approve_attempt(
                prepared["exportAttemptId"],
                actor="tester",
                confirmation=EXPORT_APPROVAL_PHRASE,
            )

            rejected = service.reject_attempt(
                prepared["exportAttemptId"],
                actor="tester",
                confirmation=EXPORT_REJECTION_PHRASE,
                resolution="Cancel approval before MGZ import.",
            )

            self.assertTrue(rejected["success"])
            self.assertEqual(rejected["exportAttempt"]["status"], "rejected")
            self.assertEqual(rejected["exportAttempt"]["external_submission"], "rejected_not_executed")
            document = ledger.get_document(document_id)
            self.assertEqual(document["bookkeeping_record"]["export_status"], "rejected_not_executed")

    def test_execute_approved_export_attempt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-export-exec",
                "originalFilename": "receipt.txt",
                "documentType": "receipt",
                "processingStatus": "reviewed",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
                "extractedData": {
                    "line_items": [{
                        "description": "Printer paper",
                        "amount": 42.5,
                        "account_name": "Office Supplies",
                        "tax_code": "BTW 21%",
                    }]
                },
            })
            route = LocalRoutingService(ledger).prepare_document_route(document_id)
            wave_executor = _FakeWaveExecutor({
                "status": "success",
                "message": "Wave transaction created.",
                "external_id": "wave-api-tx-1",
            })
            service = LocalExportAttemptService(ledger, wave_executor=wave_executor)

            prepared = service.prepare_from_routing_attempt(route["routingAttemptId"])
            cannot_execute = service.execute_attempt(prepared["exportAttemptId"])
            approved = service.approve_attempt(
                prepared["exportAttemptId"],
                actor="tester",
                confirmation=EXPORT_APPROVAL_PHRASE,
            )
            executed = service.execute_attempt(prepared["exportAttemptId"], actor="tester")
            repeated = service.execute_attempt(prepared["exportAttemptId"], actor="tester")

            self.assertEqual(prepared["success"], True)
            self.assertEqual(cannot_execute["status"], "not_approved")
            self.assertEqual(approved["status"], "approved")
            self.assertEqual(executed["status"], "executed")
            self.assertEqual(executed["executionStatus"], "success")
            self.assertEqual(executed["externalSubmission"], "executed")
            self.assertEqual(repeated["status"], "already_executed")
            self.assertEqual(len(wave_executor.calls), 1)
            self.assertEqual(wave_executor.calls[0]["idempotency_key"], prepared["operationId"])
            record = ledger.get_document(document_id)
            self.assertEqual(record["bookkeeping_record"]["export_status"], "executed")
            self.assertEqual(record["bookkeeping_record"]["status"], "routed")
            attempt = ledger.get_export_attempt(prepared["exportAttemptId"])
            self.assertEqual(attempt["external_submission"], "executed")
            self.assertEqual(attempt["external_id"], "wave-api-tx-1")

    def test_wave_execution_without_unambiguous_target_opens_review_and_stays_unsubmitted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-wave-ambiguous",
                "originalFilename": "receipt.txt",
                "documentType": "receipt",
                "processingStatus": "reviewed",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "transactionDate": "2026-07-10",
                "totalAmount": 42.5,
            })
            route = LocalRoutingService(ledger).prepare_document_route(document_id)
            service = LocalExportAttemptService(ledger)
            prepared = service.prepare_from_routing_attempt(route["routingAttemptId"])
            service.approve_attempt(
                prepared["exportAttemptId"],
                actor="tester",
                confirmation=EXPORT_APPROVAL_PHRASE,
            )

            result = service.execute_attempt(prepared["exportAttemptId"], actor="tester")

            self.assertFalse(result["success"])
            self.assertEqual(result["status"], "attention_required")
            self.assertEqual(result["externalSubmission"], "not_executed")
            attempt = ledger.get_export_attempt(prepared["exportAttemptId"])
            self.assertEqual(attempt["status"], "attention_required")
            self.assertEqual(attempt["external_submission"], "not_executed")
            review = ledger.get_review_item(result["reviewItemId"])
            self.assertEqual(review["reason"], "wave_target_ambiguous")
            self.assertIn(str(prepared["exportAttemptId"]), review["details"])
            record = ledger.get_document(document_id)["bookkeeping_record"]
            self.assertEqual(record["status"], "export_attention_required")
            self.assertEqual(record["export_status"], "attention_required")

            reapproved = service.approve_attempt(
                prepared["exportAttemptId"],
                actor="tester",
                confirmation=EXPORT_APPROVAL_PHRASE,
                resolution="Configured an explicit Wave target.",
            )

            self.assertEqual(reapproved["status"], "approved")
            self.assertEqual(reapproved["resolvedReviewIds"], [review["id"]])
            self.assertEqual(ledger.get_review_item(review["id"])["status"], "resolved")

    def test_wave_quota_deferral_is_durable_and_not_retried_early(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-wave-rate-limit",
                "originalFilename": "receipt.txt",
                "documentType": "receipt",
                "processingStatus": "reviewed",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "transactionDate": "2026-07-10",
                "totalAmount": 42.5,
            })
            route = LocalRoutingService(ledger).prepare_document_route(document_id)
            wave_executor = _FakeWaveExecutor({
                "status": "rate_limited",
                "message": "Wave quota unavailable.",
                "retry_after_seconds": 600,
            })
            service = LocalExportAttemptService(
                ledger,
                {"fab_autonomy_execute_approved_exports": True},
                wave_executor=wave_executor,
            )
            prepared = service.prepare_from_routing_attempt(route["routingAttemptId"])
            service.approve_attempt(
                prepared["exportAttemptId"],
                actor="tester",
                confirmation=EXPORT_APPROVAL_PHRASE,
            )

            deferred = service.execute_attempt(prepared["exportAttemptId"], actor="tester")
            repeated = service.execute_attempt(prepared["exportAttemptId"], actor="tester")
            batch = service.process_approved_attempts(force=True, create_backup=False)

            self.assertTrue(deferred["success"])
            self.assertEqual(deferred["status"], "deferred")
            self.assertEqual(deferred["externalSubmission"], "not_executed")
            self.assertEqual(repeated["status"], "retry_deferred")
            self.assertEqual(batch["count"], 0)
            self.assertEqual(batch["deferredNotDue"], 1)
            self.assertEqual(len(wave_executor.calls), 1)
            attempt = ledger.get_export_attempt(prepared["exportAttemptId"])
            self.assertEqual(attempt["status"], "deferred")
            self.assertEqual(attempt["metadata"]["retry"]["attemptCount"], 1)
            self.assertIsNotNone(attempt["metadata"]["retry"]["nextRetryAt"])

            metadata = dict(attempt["metadata"])
            metadata["retry"] = dict(metadata["retry"])
            metadata["retry"]["nextRetryAt"] = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat()
            ledger.update_export_attempt(prepared["exportAttemptId"], {"metadata": metadata})
            wave_executor.result = {
                "status": "success",
                "message": "Wave transaction created after quota reset.",
                "external_id": "wave-retry-tx-1",
            }

            retried = service.execute_attempt(prepared["exportAttemptId"], actor="tester")

            self.assertEqual(retried["status"], "executed")
            self.assertEqual(retried["externalSubmission"], "executed")
            self.assertEqual(len(wave_executor.calls), 2)
            self.assertEqual(
                ledger.get_export_attempt(prepared["exportAttemptId"])["external_id"],
                "wave-retry-tx-1",
            )

    def test_approved_wave_action_without_verified_api_executor_is_not_marked_queued(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-wave-bill-unsupported",
                "originalFilename": "vendor-invoice.pdf",
                "documentType": "vendor_invoice",
                "processingStatus": "reviewed",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "transactionDate": "2026-07-10",
                "totalAmount": 42.5,
                "extractedData": {
                    "vendor_name": "Office Shop",
                    "transaction_date": "2026-07-10",
                    "total_amount": 42.5,
                    "line_items": [{
                        "description": "Printer paper",
                        "amount": 42.5,
                        "category": "Office Supplies",
                    }],
                },
            })
            config = {
                "waveapps_default_target": "waveapps_business",
                "waveapps_business_access_token": "secret-store-token",
                "waveapps_business_id": "business-1",
            }
            route = LocalRoutingService(ledger, config).prepare_document_route(document_id)
            service = LocalExportAttemptService(ledger, config)
            prepared = service.prepare_from_routing_attempt(route["routingAttemptId"])
            service.approve_attempt(
                prepared["exportAttemptId"],
                actor="tester",
                confirmation=EXPORT_APPROVAL_PHRASE,
            )

            result = service.execute_attempt(prepared["exportAttemptId"], actor="tester")

            self.assertEqual(prepared["exportAttempt"]["action_id"], "bill_create")
            self.assertFalse(result["success"])
            self.assertEqual(result["status"], "attention_required")
            self.assertEqual(result["externalSubmission"], "not_executed")
            attempt = ledger.get_export_attempt(prepared["exportAttemptId"])
            self.assertEqual(attempt["status"], "attention_required")
            self.assertNotEqual(attempt["external_submission"], "queued")
            review = ledger.get_review_item(result["reviewItemId"])
            self.assertEqual(review["reason"], "wave_executor_unavailable")

    def test_execute_approved_mijngeldzaken_export_prepares_supervised_artifact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = self._register_mijngeldzaken_document(ledger)
            config = {
                "mijngeldzaken_category_mapping": {"Personal": "Huishouden"},
                "mijngeldzaken_export_dir": os.path.join(temp_dir, "mijngeldzaken-exports"),
            }
            route = LocalRoutingService(
                ledger,
                config,
            ).prepare_document_route(document_id)
            service = LocalExportAttemptService(ledger, config)

            prepared = service.prepare_from_routing_attempt(route["routingAttemptId"])
            json_artifact = service.artifact_for_attempt(prepared["exportAttemptId"], export_format="json")
            csv_artifact = service.artifact_for_attempt(prepared["exportAttemptId"], export_format="csv")
            approved = service.approve_attempt(
                prepared["exportAttemptId"],
                actor="tester",
                confirmation=EXPORT_APPROVAL_PHRASE,
            )
            executed = service.execute_attempt(prepared["exportAttemptId"], actor="tester")
            preserved = service.prepare_from_routing_attempt(route["routingAttemptId"])
            blocked_regeneration = service.regenerate_attempt(prepared["exportAttemptId"], actor="tester")

            self.assertTrue(prepared["success"])
            self.assertEqual(prepared["exportAttempt"]["target_system"], "mijngeldzaken")
            self.assertEqual(prepared["exportAttempt"]["action_id"], "transaction_import_prepare")
            master_draft = prepared["exportAttempt"]["metadata"]["masterLedgerDraft"]
            self.assertEqual(master_draft["draftType"], "transaction_import")
            self.assertEqual(master_draft["targetSystem"], "mijngeldzaken")
            self.assertEqual(master_draft["exportFormat"], "csv")
            self.assertEqual(master_draft["importRow"]["Datum"], "2026-06-28")
            self.assertEqual(master_draft["importRow"]["Omschrijving"], "Weekly groceries")
            self.assertEqual(master_draft["importRow"]["Tegenpartij"], "Local Supermarket")
            self.assertEqual(master_draft["importRow"]["Bedrag"], 42.5)
            self.assertEqual(master_draft["importRow"]["Categorie"], "Huishouden")
            self.assertEqual(master_draft["importRow"]["Valuta"], "EUR")
            self.assertEqual(master_draft["sourceProof"]["documentId"], document_id)
            self.assertEqual(len(master_draft["checksum"]), 64)
            self.assertEqual(
                prepared["exportAttempt"]["metadata"]["masterLedgerChecksum"],
                master_draft["checksum"],
            )
            self.assertTrue(json_artifact["success"])
            self.assertEqual(json_artifact["artifact"]["format"], "json")
            self.assertEqual(json_artifact["artifact"]["checksum"], master_draft["checksum"])
            self.assertTrue(csv_artifact["success"])
            self.assertEqual(csv_artifact["artifact"]["format"], "csv")
            self.assertIn("Datum,Omschrijving,Tegenpartij,Bedrag,Categorie,Rekening,Valuta,FAB Document ID", csv_artifact["artifact"]["content"])
            self.assertIn("2026-06-28,Weekly groceries,Local Supermarket,42.5,Huishouden,Huishouden,EUR", csv_artifact["artifact"]["content"])
            self.assertEqual(csv_artifact["artifact"]["externalSubmission"], "not_executed")
            artifact_events = [
                event for event in ledger.list_audit_events(limit=20)
                if event["action"] == "local_export_attempt.artifact_prepared"
            ]
            self.assertEqual({event["details"]["format"] for event in artifact_events}, {"json", "csv"})
            self.assertTrue(all(event["details"]["checksum"] == master_draft["checksum"] for event in artifact_events))
            self.assertTrue(all(event["details"]["externalSubmission"] == "not_executed" for event in artifact_events))
            self.assertEqual(approved["status"], "approved")
            self.assertEqual(
                approved["exportAttempt"]["metadata"]["approval"]["masterLedgerChecksum"],
                master_draft["checksum"],
            )
            self.assertEqual(executed["status"], "supervision_required")
            self.assertEqual(executed["executionStatus"], "supervised_action_required")
            self.assertEqual(executed["externalSubmission"], "not_executed")
            self.assertTrue(os.path.isfile(executed["artifact"]["path"]))
            self.assertEqual(len(executed["artifact"]["sha256"]), 64)
            self.assertEqual(preserved["status"], "already_prepared")
            self.assertEqual(preserved["exportAttempt"]["status"], "supervision_required")
            self.assertEqual(blocked_regeneration["status"], "supervision_in_progress")
            self.assertEqual(
                executed["artifact"]["masterLedgerChecksum"],
                master_draft["checksum"],
            )
            self.assertIn("MijnGeldzaken", executed["exportAttempt"]["message"])
            self.assertEqual(
                executed["exportAttempt"]["metadata"]["lastExecution"]["masterLedgerChecksum"],
                master_draft["checksum"],
            )
            self.assertEqual(
                executed["exportAttempt"]["result"]["masterLedgerChecksum"],
                master_draft["checksum"],
            )
            self.assertEqual(
                executed["exportAttempt"]["result"]["status"],
                "supervised_action_required",
            )
            self.assertEqual(
                executed["exportAttempt"]["metadata"]["masterLedgerChecksum"],
                master_draft["checksum"],
            )
            document = ledger.get_document(document_id)
            self.assertEqual(document["bookkeeping_record"]["export_status"], "supervision_required")
            self.assertEqual(document["bookkeeping_record"]["status"], "export_supervision_required")
            self.assertEqual(
                document["bookkeeping_record"]["metadata"]["latestExport"]["details"]["masterLedgerChecksum"],
                master_draft["checksum"],
            )
            executed_events = [
                event for event in ledger.list_audit_events(limit=30)
                if event["action"] == "local_export_attempt.supervision_required"
            ]
            self.assertEqual(executed_events[0]["details"]["masterLedgerChecksum"], master_draft["checksum"])
            reviews = ledger.list_review_items(document_id=document_id)
            self.assertEqual(reviews[0]["reason"], "mijngeldzaken_supervision_required")
            self.assertEqual(reviews[0]["status"], "pending")

            recorded = service.record_result(
                prepared["exportAttemptId"],
                status="executed",
                external_id="mgz-supervised-import",
                result={"artifactSha256": executed["artifact"]["sha256"]},
                actor="tester",
                confirmation=EXPORT_RESULT_CONFIRMATION_PHRASE,
            )

            self.assertTrue(recorded["success"])
            self.assertEqual(recorded["status"], "executed")
            self.assertEqual(recorded["resolvedReviewIds"], [reviews[0]["id"]])
            self.assertEqual(ledger.get_review_item(reviews[0]["id"])["status"], "resolved")
            self.assertEqual(
                ledger.get_export_attempt(prepared["exportAttemptId"])["external_submission"],
                "executed",
            )

    def test_export_execution_uses_atomic_operations_ledger_claim(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = self._register_mijngeldzaken_document(ledger)
            config = {
                "mijngeldzaken_category_mapping": {"Personal": "Huishouden"},
                "mijngeldzaken_export_dir": os.path.join(temp_dir, "exports"),
            }
            route = LocalRoutingService(ledger, config).prepare_document_route(document_id)
            service = LocalExportAttemptService(ledger, config)
            prepared = service.prepare_from_routing_attempt(route["routingAttemptId"])
            service.approve_attempt(
                prepared["exportAttemptId"],
                actor="tester",
                confirmation=EXPORT_APPROVAL_PHRASE,
            )

            claim = ledger.claim_export_attempt(prepared["exportAttemptId"])
            duplicate_execution = service.execute_attempt(prepared["exportAttemptId"], actor="second-worker")

            self.assertEqual(claim["status"], "claimed")
            self.assertEqual(duplicate_execution["status"], "already_claimed")
            self.assertEqual(duplicate_execution["currentStatus"], "execution_in_progress")
            self.assertFalse(os.path.exists(config["mijngeldzaken_export_dir"]))

    def test_mijngeldzaken_export_approval_blocks_stale_master_ledger_draft(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id, service, prepared = self._prepare_mijngeldzaken_export(ledger)
            original_checksum = prepared["exportAttempt"]["metadata"]["masterLedgerChecksum"]

            self._change_mijngeldzaken_document_amount(ledger, document_id)
            blocked = service.approve_attempt(
                prepared["exportAttemptId"],
                actor="tester",
                confirmation=EXPORT_APPROVAL_PHRASE,
            )

            self.assertFalse(blocked["success"])
            self.assertEqual(blocked["status"], "stale_master_ledger_draft")
            self.assertEqual(blocked["externalSubmission"], "not_executed")
            self.assertEqual(blocked["storedChecksum"], original_checksum)
            self.assertNotEqual(blocked["currentChecksum"], original_checksum)
            self.assertEqual(blocked["currentDraft"]["importRow"]["Bedrag"], 99.99)
            attempt = ledger.get_export_attempt(prepared["exportAttemptId"])
            self.assertEqual(attempt["status"], "approval_required")
            stale_events = [
                event for event in ledger.list_audit_events(limit=30)
                if event["action"] == "local_export_attempt.master_ledger_stale"
            ]
            self.assertEqual(stale_events[0]["details"]["stage"], "approval")
            self.assertEqual(stale_events[0]["details"]["storedChecksum"], original_checksum)
            self.assertEqual(stale_events[0]["details"]["externalSubmission"], "not_executed")

    def test_mijngeldzaken_execution_blocks_stale_master_ledger_draft_after_approval(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id, service, prepared = self._prepare_mijngeldzaken_export(ledger)
            approved = service.approve_attempt(
                prepared["exportAttemptId"],
                actor="tester",
                confirmation=EXPORT_APPROVAL_PHRASE,
            )
            original_checksum = approved["exportAttempt"]["metadata"]["masterLedgerChecksum"]

            self._change_mijngeldzaken_document_amount(ledger, document_id)
            blocked = service.execute_attempt(prepared["exportAttemptId"], actor="tester")

            self.assertFalse(blocked["success"])
            self.assertEqual(blocked["status"], "stale_master_ledger_draft")
            self.assertEqual(blocked["externalSubmission"], "not_executed")
            self.assertEqual(blocked["storedChecksum"], original_checksum)
            self.assertNotEqual(blocked["currentChecksum"], original_checksum)
            attempt = ledger.get_export_attempt(prepared["exportAttemptId"])
            self.assertEqual(attempt["status"], "approved")
            self.assertEqual(attempt["external_submission"], "approved_not_executed")
            self.assertIsNone(attempt.get("result"))
            stale_events = [
                event for event in ledger.list_audit_events(limit=30)
                if event["action"] == "local_export_attempt.master_ledger_stale"
            ]
            self.assertEqual(stale_events[0]["details"]["stage"], "execution")

    def test_mijngeldzaken_record_result_blocks_stale_master_ledger_draft_after_approval(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id, service, prepared = self._prepare_mijngeldzaken_export(ledger)
            approved = service.approve_attempt(
                prepared["exportAttemptId"],
                actor="tester",
                confirmation=EXPORT_APPROVAL_PHRASE,
            )
            original_checksum = approved["exportAttempt"]["metadata"]["masterLedgerChecksum"]

            self._change_mijngeldzaken_document_amount(ledger, document_id)
            blocked = service.record_result(
                prepared["exportAttemptId"],
                "queued",
                actor="tester",
                confirmation=EXPORT_RESULT_CONFIRMATION_PHRASE,
            )

            self.assertFalse(blocked["success"])
            self.assertEqual(blocked["status"], "stale_master_ledger_draft")
            self.assertEqual(blocked["externalSubmission"], "not_executed")
            self.assertEqual(blocked["storedChecksum"], original_checksum)
            self.assertNotEqual(blocked["currentChecksum"], original_checksum)
            attempt = ledger.get_export_attempt(prepared["exportAttemptId"])
            self.assertEqual(attempt["status"], "approved")
            self.assertEqual(attempt["external_submission"], "approved_not_executed")
            self.assertIsNone(attempt.get("result"))
            stale_events = [
                event for event in ledger.list_audit_events(limit=30)
                if event["action"] == "local_export_attempt.master_ledger_stale"
            ]
            self.assertEqual(stale_events[0]["details"]["stage"], "result")

    def test_regenerate_stale_mijngeldzaken_export_attempt_refreshes_checksum(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id, service, prepared = self._prepare_mijngeldzaken_export(ledger)
            original_checksum = prepared["exportAttempt"]["metadata"]["masterLedgerChecksum"]
            self._change_mijngeldzaken_document_amount(ledger, document_id)

            stale_projection = LocalMasterLedgerService(
                ledger,
                {"mijngeldzaken_category_mapping": {"Personal": "Huishouden"}},
            ).project()
            regenerated = service.regenerate_attempt(prepared["exportAttemptId"], actor="tester")
            refreshed_projection = LocalMasterLedgerService(
                ledger,
                {"mijngeldzaken_category_mapping": {"Personal": "Huishouden"}},
            ).project()
            approved = service.approve_attempt(
                prepared["exportAttemptId"],
                actor="tester",
                confirmation=EXPORT_APPROVAL_PHRASE,
            )

            self.assertEqual(stale_projection["rows"][0]["downstreamStatus"], "stale_master_ledger_draft")
            self.assertTrue(regenerated["success"])
            self.assertEqual(regenerated["status"], "regenerated")
            self.assertEqual(regenerated["externalSubmission"], "not_executed")
            self.assertNotEqual(regenerated["masterLedgerChecksum"], original_checksum)
            attempt = regenerated["exportAttempt"]
            self.assertEqual(attempt["status"], "approval_required")
            self.assertEqual(attempt["external_submission"], "not_executed")
            self.assertEqual(attempt["payload"]["amount"], 99.99)
            self.assertEqual(attempt["metadata"]["masterLedgerDraft"]["importRow"]["Bedrag"], 99.99)
            self.assertEqual(attempt["metadata"]["regenerationHistory"][0]["fromChecksum"], original_checksum)
            self.assertEqual(refreshed_projection["rows"][0]["downstreamStatus"], "awaiting_approval")
            self.assertEqual(refreshed_projection["summary"]["blockedRows"], 0)
            self.assertEqual(approved["status"], "approved")
            audit_actions = [event["action"] for event in ledger.list_audit_events(limit=40)]
            self.assertIn("local_export_attempt.regenerated", audit_actions)

    def test_bank_transaction_record_export_flow_without_source_document(self):
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
                "transactionId": "tx-export-bank-record",
                "transactionDate": "2026-06-28",
                "amount": -42.5,
                "currency": "EUR",
                "description": "Printer paper",
                "counterparty": "Office Shop",
                "reconciliationStatus": "not_started",
            })
            record_result = LocalBookkeepingRecordService(ledger, {}).upsert_from_bank_transaction(transaction_id)
            route = LocalRoutingService(ledger).prepare_bookkeeping_record_route(record_result["recordId"])
            service = LocalExportAttemptService(ledger)

            prepared = service.prepare_from_routing_attempt(route["routingAttemptId"])
            approved = service.approve_attempt(
                prepared["exportAttemptId"],
                actor="tester",
                confirmation=EXPORT_APPROVAL_PHRASE,
            )
            recorded = service.record_result(
                prepared["exportAttemptId"],
                "executed",
                external_id="wave-bank-tx-1",
                result={"waveId": "wave-bank-tx-1"},
                actor="tester",
                confirmation=EXPORT_RESULT_CONFIRMATION_PHRASE,
            )

            self.assertTrue(prepared["success"])
            self.assertIsNone(prepared["documentId"])
            self.assertEqual(prepared["bookkeepingRecordId"], record_result["recordId"])
            self.assertEqual(prepared["exportAttempt"]["bookkeeping_record_id"], record_result["recordId"])
            self.assertIsNone(prepared["exportAttempt"]["document_id"])
            self.assertEqual(prepared["exportAttempt"]["status"], "approval_required")
            self.assertEqual(approved["status"], "approved")
            self.assertEqual(recorded["externalSubmission"], "executed")
            record = ledger.get_bookkeeping_record(record_result["recordId"])
            self.assertEqual(record["status"], "routed")
            self.assertEqual(record["export_status"], "executed")
            self.assertEqual(record["metadata"]["latestExport"]["details"]["externalId"], "wave-bank-tx-1")


if __name__ == "__main__":
    unittest.main()
