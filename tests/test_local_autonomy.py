import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src.operations.local_api import create_app
from src.operations.local_autonomy import LocalAutonomousService
from src.operations.local_autonomy_control import AUTONOMY_RESUME_CONFIRMATION
from src.operations.local_bank_transactions import LocalBankTransactionImportService
from src.operations.local_exports import EXPORT_APPROVAL_PHRASE, LocalExportAttemptService
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_readiness import LocalReadinessService
from src.operations.local_routing import LocalRoutingService
from src.operations.local_wave_control import LocalWaveControlService
from src.utils.rate_limiter import RateLimiter, reset_all_limiters, set_rate_limiter


class TestLocalAutonomousService(unittest.TestCase):
    def test_autonomy_plan_uses_one_consistent_ledger_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
            })
            client = app.test_client()
            original_connect = LocalOperationsLedger._connect
            connection_count = 0

            def counted_connect(ledger):
                nonlocal connection_count
                connection_count += 1
                return original_connect(ledger)

            with patch.object(LocalOperationsLedger, "_connect", counted_connect):
                response = client.get(
                    "/api/autonomy/plan?includeWavePlan=false"
                    "&includeWaveSync=false&includeConnectorSync=false"
                )

            self.assertEqual(response.status_code, 200)
            self.assertLessEqual(connection_count, 1)

    def test_operator_emergency_stop_blocks_cycles_until_confirmed_resume(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()

            engaged = client.post("/api/autonomy/emergency-stop", json={
                "actor": "operator-test",
                "reason": "Investigate an unexpected provider response.",
            })
            plan = client.get("/api/autonomy/plan").get_json()
            blocked_run = client.post("/api/autonomy/run", json={"limit": 1})
            rejected_resume = client.delete("/api/autonomy/emergency-stop", json={
                "actor": "operator-test",
                "reason": "Inspection complete.",
                "confirmation": "resume",
            })
            resumed = client.delete("/api/autonomy/emergency-stop", json={
                "actor": "operator-test",
                "reason": "Inspection complete; current plan reviewed.",
                "confirmation": AUTONOMY_RESUME_CONFIRMATION,
            })

            self.assertEqual(engaged.status_code, 200)
            self.assertEqual(engaged.get_json()["status"], "stopped")
            self.assertEqual(plan["status"], "blocked")
            self.assertTrue(plan["emergencyStop"]["active"])
            self.assertIn("operator_emergency_stop", plan["blockedReasons"])
            self.assertEqual(blocked_run.status_code, 400)
            self.assertEqual(blocked_run.get_json()["status"], "blocked")
            self.assertEqual(rejected_resume.status_code, 400)
            self.assertEqual(rejected_resume.get_json()["status"], "confirmation_required")
            self.assertEqual(resumed.status_code, 200)
            self.assertFalse(resumed.get_json()["control"]["active"])

            ledger = LocalOperationsLedger(ledger_path)
            actions = [event["action"] for event in ledger.list_audit_events(limit=20)]
            self.assertIn("runtime_control.engaged", actions)
            self.assertIn("runtime_control.cleared", actions)

    def test_autonomy_plan_exposes_safe_actions_and_review_gates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_intake_paths": intake_dir,
            })
            client = app.test_client()

            response = client.get("/api/autonomy/plan")
            wave_sync_disabled = client.get("/api/autonomy/plan?includeWaveSync=false")
            connector_sync_disabled = client.get("/api/autonomy/plan?includeConnectorSync=false")
            page = client.get("/")

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["externalSubmission"], "not_executed")
            self.assertEqual(payload["status"], "ready")
            self.assertIn("rescan_intake", payload["runnableActionIds"])
            self.assertIn("plan_wave_daily_reconciliation", payload["runnableActionIds"])
            self.assertIn("refresh_wave_entity_mirror", {action["id"] for action in payload["actions"]})
            self.assertIn("sync_connector_sources", {action["id"] for action in payload["actions"]})
            disabled_action = next(
                action for action in wave_sync_disabled.get_json()["actions"]
                if action["id"] == "refresh_wave_entity_mirror"
            )
            self.assertIn("disabled for this request", disabled_action["blockedReason"])
            disabled_connector_action = next(
                action for action in connector_sync_disabled.get_json()["actions"]
                if action["id"] == "sync_connector_sources"
            )
            self.assertIn("disabled for this request", disabled_connector_action["blockedReason"])
            self.assertIn("review_queue", {action["id"] for action in payload["actions"]})
            self.assertIn("exception_queue", {action["id"] for action in payload["actions"]})
            self.assertIn("approve_export_attempts", {action["id"] for action in payload["actions"]})
            self.assertIn("prepare_period_close_pack", {action["id"] for action in payload["actions"]})
            self.assertIn("prepare_master_ledger_projection", {action["id"] for action in payload["actions"]})
            self.assertEqual(payload["exceptions"]["total"], 0)
            self.assertEqual(payload["closeReadiness"]["status"], "blocked")
            self.assertGreater(payload["counts"]["closeBlockingGates"], 0)
            self.assertIn("Autonomous Cycle", page.data.decode("utf-8"))
            self.assertIn("Cycle lease", page.data.decode("utf-8"))

    def test_autonomy_plan_does_not_reprepare_routes_with_export_attempts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "already-exported-route",
                "originalFilename": "receipt.txt",
                "processingStatus": "reviewed",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "transactionDate": "2026-08-13",
                "totalAmount": 42.5,
            })
            route = LocalRoutingService(ledger).prepare_document_route(document_id)
            LocalExportAttemptService(ledger).prepare_from_routing_attempt(
                route["routingAttemptId"]
            )

            plan = LocalAutonomousService(ledger, {}, intake_paths=[]).plan(
                include_wave_plan=False,
                include_wave_sync=False,
                include_connector_sync=False,
            )
            cycle = LocalAutonomousService(ledger, {}, intake_paths=[]).run_cycle(
                include_wave_plan=False,
                include_wave_sync=False,
                include_connector_sync=False,
            )

            self.assertEqual(plan["counts"]["readyForExportAttempts"], 0)
            self.assertNotIn("prepare_export_attempts", plan["runnableActionIds"])
            self.assertNotIn(
                "prepare_export_attempts",
                {action["id"] for action in cycle["executedActions"]},
            )

    def test_autonomy_records_master_projection_and_wave_plan_only_when_due(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            ledger.upsert_bookkeeping_record({
                "sourceType": "document",
                "status": "validated",
                "targetSystem": "waveapps_business",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "recordDate": "2026-08-13",
                "amount": 42.5,
            })
            service = LocalAutonomousService(ledger, {}, intake_paths=[])

            first = service.run_cycle(include_wave_plan=True, include_wave_sync=False)
            second = service.run_cycle(include_wave_plan=True, include_wave_sync=False)

            first_ids = {action["id"] for action in first["executedActions"]}
            second_ids = {action["id"] for action in second["executedActions"]}
            self.assertIn("prepare_master_ledger_projection", first_ids)
            self.assertIn("plan_wave_daily_reconciliation", first_ids)
            self.assertNotIn("prepare_master_ledger_projection", second_ids)
            self.assertNotIn("plan_wave_daily_reconciliation", second_ids)
            self.assertEqual(
                len([
                    event for event in ledger.list_audit_events(limit=100)
                    if event["action"] == "local_master_ledger.projection_prepared"
                ]),
                1,
            )
            self.assertEqual(
                len([
                    event for event in ledger.list_audit_events(limit=100)
                    if event["action"] == "local_autonomy.wave_daily_plan_prepared"
                ]),
                1,
            )

    def test_autonomy_plan_includes_operating_exception_queue(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            record_id = ledger.upsert_bookkeeping_record({
                "sourceType": "document",
                "status": "needs_review",
                "exportStatus": "blocked_by_review",
                "targetSystem": "waveapps",
                "vendorName": "Unknown Vendor",
                "category": "Manual Review",
                "amount": 10,
                "currency": "EUR",
                "reviewRequired": True,
            })
            service = LocalAutonomousService(
                ledger,
                {"fab_autonomy_ignore_health_blocks": True},
                intake_paths=[],
            )

            plan = service.plan(include_wave_plan=False)
            exception_action = next(action for action in plan["actions"] if action["id"] == "exception_queue")

            self.assertIn("exception_queue", plan["manualActionIds"])
            self.assertEqual(plan["counts"]["operatingExceptions"], plan["exceptions"]["total"])
            self.assertGreaterEqual(plan["exceptions"]["total"], 1)
            self.assertIn("master_ledger_record_review", plan["exceptions"]["byType"])
            self.assertEqual(exception_action["evidence"]["operatingExceptions"], plan["exceptions"]["total"])
            self.assertEqual(
                plan["exceptions"]["topExceptions"][0]["entityId"],
                record_id,
            )
            self.assertEqual(plan["exceptions"]["topExceptions"][0]["entityType"], "bookkeeping_record")
            self.assertIn(
                "open_bookkeeping_record",
                {action["id"] for action in plan["exceptions"]["topExceptions"][0]["actions"]},
            )
            self.assertEqual(
                next(
                    action for action in plan["exceptions"]["topExceptions"][0]["actions"]
                    if action["id"] == "open_bookkeeping_record"
                )["path"],
                f"/bookkeeping-records/{record_id}",
            )

    def test_blocked_ledger_health_allows_local_repair_but_gates_external_execution(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            ledger.upsert_bookkeeping_record({
                "sourceType": "document",
                "status": "needs_review",
                "exportStatus": "blocked_by_review",
                "targetSystem": "waveapps_business",
                "vendorName": "Unknown Vendor",
                "category": "Manual Review",
                "amount": 10,
                "currency": "EUR",
                "reviewRequired": True,
            })
            ledger.upsert_source_account({
                "sourceType": "gmail",
                "sourceIdentifier": "me",
                "label": "Gmail",
                "status": "failed",
            })
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "health-gated-export",
                "originalFilename": "approved-receipt.pdf",
                "documentType": "receipt",
                "processingStatus": "reviewed",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "transactionDate": "2026-08-03",
                "totalAmount": 10,
            })
            route = LocalRoutingService(ledger).prepare_document_route(document_id)
            prepared = LocalExportAttemptService(ledger).prepare_from_routing_attempt(
                route["routingAttemptId"]
            )
            LocalExportAttemptService(ledger).approve_attempt(
                prepared["exportAttemptId"],
                actor="tester",
                confirmation=EXPORT_APPROVAL_PHRASE,
            )
            service = LocalAutonomousService(
                ledger,
                {"fab_autonomy_execute_approved_exports": True},
                intake_paths=[intake_dir],
                intake_extensions=["pdf"],
            )

            plan = service.plan(
                include_connector_sync=False,
                include_wave_plan=False,
                include_wave_sync=False,
            )
            actions = {action["id"]: action for action in plan["actions"]}

            self.assertEqual(plan["health"]["status"], "blocked")
            self.assertEqual(plan["status"], "ready")
            self.assertTrue(plan["canRunAutonomously"])
            self.assertNotIn("operations_health_blocked", plan["blockedReasons"])
            self.assertIn("operations_health_blocked", plan["guardedReasons"])
            self.assertTrue(actions["rescan_intake"]["canRun"])
            self.assertNotIn("execute_approved_exports", actions)

    def test_dashboard_autonomy_panel_surfaces_top_operating_exceptions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            record_id = ledger.upsert_bookkeeping_record({
                "sourceType": "document",
                "status": "needs_review",
                "exportStatus": "blocked_by_review",
                "targetSystem": "waveapps",
                "vendorName": "Unknown Vendor",
                "category": "Manual Review",
                "amount": 10,
                "currency": "EUR",
                "reviewRequired": True,
            })
            app = create_app({
                "fab_local_ledger_path": ledger_path,
                "fab_autonomy_ignore_health_blocks": True,
            })

            html = app.test_client().get("/").data.decode("utf-8")

            self.assertIn("Exceptions", html)
            self.assertIn("master_ledger_record_review", html)
            self.assertIn(f"bookkeeping_record #{record_id}", html)
            self.assertIn(f"/bookkeeping-records/{record_id}", html)
            self.assertIn("Open record", html)
            self.assertIn("Open full exception queue", html)

    def test_autonomy_api_rejects_overlapping_cycle_with_public_lease_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            lease = ledger.acquire_runtime_lease(
                "local_autonomous_cycle",
                "existing-owner",
                ttl_seconds=60,
                metadata={"trigger": "worker"},
            )
            app = create_app({"fab_local_ledger_path": ledger_path})

            response = app.test_client().post("/api/autonomy/run", json={
                "includeWavePlan": False,
                "includeWaveSync": False,
            })

            self.assertTrue(lease["acquired"])
            self.assertEqual(response.status_code, 409)
            payload = response.get_json()
            self.assertFalse(payload["success"])
            self.assertEqual(payload["status"], "already_running")
            self.assertTrue(payload["runtimeLease"]["active"])
            self.assertNotIn("owner_token", payload["runtimeLease"])
            self.assertEqual(ledger.list_workflow_runs(), [])
            self.assertIn(
                "local_autonomy.cycle_skipped_already_running",
                [event["action"] for event in ledger.list_audit_events(limit=10)],
            )
            self.assertTrue(
                ledger.release_runtime_lease("local_autonomous_cycle", "existing-owner")
            )

    def test_autonomy_dry_run_does_not_acquire_runtime_lease(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalAutonomousService(ledger, {}, intake_paths=[])

            result = service.run_cycle(
                include_wave_plan=False,
                include_wave_sync=False,
                dry_run=True,
            )

            self.assertEqual(result["status"], "dry_run")
            self.assertIsNone(ledger.get_runtime_lease("local_autonomous_cycle"))

    def test_autonomy_applies_waiting_trusted_category_without_new_intake(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "trusted-category-waiting",
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
            for reason in (
                "low_confidence_categorization",
                "manual_review_category",
            ):
                ledger.create_review_item({
                    "documentId": document_id,
                    "reason": reason,
                    "details": "Category requires review.",
                })
            service = LocalAutonomousService(ledger, {}, intake_paths=[])

            plan = service.plan(
                include_wave_plan=False,
                include_wave_sync=False,
                include_connector_sync=False,
            )
            result = service.run_cycle(
                include_wave_plan=False,
                include_wave_sync=False,
                include_connector_sync=False,
            )

            self.assertEqual(plan["counts"]["importedDocuments"], 0)
            self.assertEqual(plan["counts"]["trustedCategorySuggestions"], 1)
            self.assertIn("process_imported", plan["runnableActionIds"])
            processing = next(
                action
                for action in result["executedActions"]
                if action["id"] == "process_imported"
            )
            self.assertEqual(
                processing["summary"]["trustedCategoryAutomation"]["readyDocuments"],
                1,
            )
            document = ledger.get_document(document_id)
            self.assertEqual(document["category"], "Construction Materials & Tools")
            self.assertNotEqual(document["processing_status"], "needs_review")
            self.assertEqual(
                ledger.list_review_items(
                    status=("pending", "in_review"),
                    document_id=document_id,
                ),
                [],
            )

    def test_trusted_category_keeps_duplicate_decision_open(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            original_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "trusted-category-original",
                "originalFilename": "original.pdf",
                "processingStatus": "processed",
                "vendorName": "Praxis",
                "category": "Construction Materials & Tools",
            })
            candidate_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "trusted-category-duplicate-candidate",
                "originalFilename": "candidate.pdf",
                "documentType": "receipt",
                "processingStatus": "needs_review",
                "duplicateOfDocumentId": original_id,
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
                    "documentId": candidate_id,
                    "reason": reason,
                    "details": "Review required.",
                })

            result = LocalAutonomousService(
                ledger,
                {},
                intake_paths=[],
            ).run_cycle(
                include_wave_plan=False,
                include_wave_sync=False,
                include_connector_sync=False,
            )

            processing = next(
                action
                for action in result["executedActions"]
                if action["id"] == "process_imported"
            )
            trusted = processing["summary"]["trustedCategoryAutomation"]
            backup = trusted.pop("preMutationBackup")
            self.assertEqual(
                trusted,
                {
                    "candidates": 1,
                    "updatedDocuments": 1,
                    "resolvedReviewItems": 2,
                    "stillNeedsReview": 1,
                    "readyDocuments": 0,
                    "documentIds": [candidate_id],
                    "externalSubmission": "not_executed",
                },
            )
            self.assertEqual(backup["status"], "valid")
            self.assertTrue(backup["backupFilename"].endswith(".zip"))
            self.assertEqual(len(backup["ledgerSha256"]), 64)
            self.assertGreater(backup["ledgerBytes"], 0)
            document = ledger.get_document(candidate_id)
            self.assertEqual(document["category"], "Construction Materials & Tools")
            self.assertEqual(document["processing_status"], "needs_review")
            open_reviews = ledger.list_review_items(
                status=("pending", "in_review"),
                document_id=candidate_id,
            )
            self.assertEqual(
                [(item["reason"], item["status"]) for item in open_reviews],
                [("duplicate_candidate", "pending")],
            )

    def test_autonomy_repairs_legacy_duplicate_cycle_before_processing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            first_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "legacy-cycle-1",
                "originalFilename": "scan-1.pdf",
                "processingStatus": "needs_review",
            })
            second_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "legacy-cycle-2",
                "originalFilename": "scan-2.pdf",
                "processingStatus": "needs_review",
            })
            ledger.update_document(first_id, {"duplicateOfDocumentId": second_id})
            ledger.update_document(second_id, {"duplicateOfDocumentId": first_id})
            for document_id in (first_id, second_id):
                ledger.create_review_item({
                    "documentId": document_id,
                    "reason": "duplicate_candidate",
                    "details": "Legacy duplicate review remains open.",
                })
            service = LocalAutonomousService(
                ledger,
                {"fab_local_backup_dir": os.path.join(temp_dir, "backups")},
                intake_paths=[],
            )

            plan = service.plan(
                include_wave_plan=False,
                include_wave_sync=False,
                include_connector_sync=False,
            )
            result = service.run_cycle(
                include_wave_plan=False,
                include_wave_sync=False,
                include_connector_sync=False,
            )

            self.assertEqual(plan["counts"]["duplicateLinkCycles"], 1)
            self.assertIn("process_imported", plan["runnableActionIds"])
            processing = next(
                action
                for action in result["executedActions"]
                if action["id"] == "process_imported"
            )
            self.assertEqual(
                processing["summary"]["duplicateCycleRepair"]["cyclesRepaired"],
                1,
            )
            self.assertIsNone(ledger.get_document(first_id)["duplicate_of_document_id"])
            self.assertIsNone(ledger.get_document(second_id)["duplicate_of_document_id"])
            self.assertEqual(
                len(ledger.list_review_items(status="pending")),
                2,
            )

    def test_autonomy_reassesses_stale_duplicate_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            first_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "monthly-april",
                "originalFilename": "monthly-april.pdf",
                "documentType": "vendor_invoice",
                "processingStatus": "needs_review",
                "vendorName": "T-Mobile",
                "transactionDate": "2023-04-21",
                "totalAmount": 37.68,
                "extractedData": {
                    "vendor_name": "T-Mobile",
                    "transaction_date": "2023-04-21",
                    "total_amount": 37.68,
                    "invoice_number": "staat",
                },
            })
            second_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "monthly-may",
                "originalFilename": "monthly-may.pdf",
                "documentType": "vendor_invoice",
                "processingStatus": "needs_review",
                "vendorName": "T-Mobile",
                "transactionDate": "2023-05-19",
                "totalAmount": 37.68,
                "extractedData": {
                    "vendor_name": "T-Mobile",
                    "transaction_date": "2023-05-19",
                    "total_amount": 37.68,
                    "invoice_number": "staat",
                },
            })
            ledger.record_duplicate_candidate({
                "documentId": second_id,
                "candidateDocumentId": first_id,
                "matchType": "exact_fingerprint_match",
                "confidenceScore": 1.0,
                "status": "pending",
            })
            ledger.create_review_item({
                "documentId": second_id,
                "reason": "duplicate_candidate",
                "details": "Stale machine-generated match.",
            })
            service = LocalAutonomousService(
                ledger,
                {"fab_local_backup_dir": os.path.join(temp_dir, "backups")},
                intake_paths=[],
            )

            plan = service.plan(
                include_wave_plan=False,
                include_wave_sync=False,
                include_connector_sync=False,
            )
            result = service.run_cycle(
                include_wave_plan=False,
                include_wave_sync=False,
                include_connector_sync=False,
            )

            self.assertEqual(plan["counts"]["duplicateCandidateReassessments"], 1)
            self.assertIn("process_imported", plan["runnableActionIds"])
            processing = next(
                action
                for action in result["executedActions"]
                if action["id"] == "process_imported"
            )
            reassessment = processing["summary"]["duplicateCandidateReassessment"]
            self.assertEqual(reassessment["rejectedPairs"], 1)
            self.assertEqual(reassessment["resolvedReviewItems"], 1)
            self.assertEqual(
                ledger.list_duplicate_candidates(status=("pending", "in_review")),
                [],
            )
            self.assertEqual(
                ledger.list_review_items(status=("pending", "in_review")),
                [],
            )
            self.assertEqual(ledger.get_document(second_id)["processing_status"], "processed")

    def test_autonomy_run_rescans_processes_and_prepares_wave_draft(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            receipt_path = os.path.join(intake_dir, "receipt.txt")
            with open(receipt_path, "w", encoding="utf-8") as handle:
                handle.write("Vendor: Test Vendor\nDate: 2026-06-28\nTotal: EUR 42.50\nOffice supplies\n")

            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            app = create_app({
                "fab_local_ledger_path": ledger_path,
                "fab_local_intake_paths": intake_dir,
                "fab_local_intake_extensions": "txt",
                "categorization_rules": {
                    "Office Supplies": {
                        "keywords": ["office supplies"],
                        "vendors": ["test vendor"],
                    }
                },
            })
            client = app.test_client()

            response = client.post("/api/autonomy/run", json={"includeWavePlan": False})

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            executed_ids = {action["id"] for action in payload["executedActions"]}
            self.assertTrue(payload["success"])
            self.assertEqual(payload["externalSubmission"], "not_executed")
            self.assertTrue(payload["runtimeLease"]["released"])
            self.assertFalse(payload["runtimeLease"]["active"])
            self.assertIn("rescan_intake", executed_ids)
            self.assertIn("process_imported", executed_ids)
            self.assertIn("prepare_wave_drafts", executed_ids)
            self.assertIn("prepare_export_attempts", executed_ids)
            self.assertIn("prepare_master_ledger_projection", executed_ids)
            self.assertEqual(payload["masterLedger"]["totalRows"], 1)
            self.assertEqual(payload["masterLedger"]["readyForApproval"], 1)
            self.assertEqual(len(payload["masterLedger"]["ledgerChecksum"]), 64)
            documents = client.get("/api/documents").get_json()["documents"]
            self.assertEqual(documents[0]["processing_status"], "export_draft_prepared")
            self.assertEqual(documents[0]["vendor_name"], "Test Vendor")
            routing = client.get("/api/routing").get_json()["routingAttempts"]
            self.assertEqual(routing[0]["status"], "draft_prepared")
            self.assertEqual(routing[0]["metadata"]["externalSubmission"], "not_executed")
            export_attempts = client.get("/api/export-attempts").get_json()["exportAttempts"]
            self.assertEqual(len(export_attempts), 1)
            self.assertEqual(export_attempts[0]["status"], "approval_required")
            self.assertEqual(export_attempts[0]["external_submission"], "not_executed")
            audit_actions = [event["action"] for event in client.get("/api/audit").get_json()["auditEvents"]]
            self.assertIn("local_autonomy.cycle_started", audit_actions)
            workflow = client.get(f"/api/workflows/{payload['workflowRunId']}").get_json()
            self.assertEqual(len(workflow["steps"]), 12)
            self.assertEqual(workflow["steps"][0]["step_key"], "sync_connector_sources")
            self.assertEqual(workflow["steps"][0]["status"], "skipped")
            self.assertEqual(workflow["steps"][1]["step_key"], "rescan_intake")
            self.assertEqual(workflow["steps"][1]["status"], "completed")
            self.assertGreaterEqual(workflow["steps"][1]["duration_ms"], 0)
            self.assertEqual(workflow["stepSummary"]["completed"], len(payload["executedActions"]))
            self.assertEqual(workflow["stepSummary"]["skipped"], len(payload["skippedActions"]))
            self.assertIn("local_autonomy.cycle_completed", audit_actions)
            self.assertIn("local_master_ledger.projection_prepared", audit_actions)

    def test_autonomy_failure_marks_failed_step_and_remaining_steps_not_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalAutonomousService(
                ledger,
                {
                    "fab_autonomy_ignore_health_blocks": True,
                    "api_token": "sensitive-value",
                },
                intake_paths=[intake_dir],
            )

            with patch.object(
                service,
                "_run_rescan",
                side_effect=RuntimeError(
                    "sensitive-value was rejected; access_token=unknown-secret"
                ),
            ):
                result = service.run_cycle(
                    include_wave_plan=False,
                    include_wave_sync=False,
                )

            steps = ledger.list_workflow_steps(
                workflow_run_id=result["workflowRunId"],
                limit=100,
            )
            self.assertFalse(result["success"])
            self.assertEqual(result["status"], "failed")
            self.assertNotIn("sensitive-value", result["error"])
            self.assertNotIn("unknown-secret", result["error"])
            self.assertEqual(steps[0]["step_key"], "sync_connector_sources")
            self.assertEqual(steps[0]["status"], "skipped")
            self.assertEqual(steps[1]["step_key"], "rescan_intake")
            self.assertEqual(steps[1]["status"], "failed")
            self.assertNotIn("sensitive-value", steps[1]["error_message"])
            self.assertNotIn("unknown-secret", steps[1]["error_message"])
            self.assertTrue(all(step["status"] == "not_run" for step in steps[2:]))
            self.assertTrue(all(step["finished_at"] for step in steps))

    def test_one_click_cycle_collects_connector_document_before_processing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            scan_path = os.path.join(temp_dir, "scanner-receipt.txt")
            with open(scan_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "Vendor: Test Vendor\nDate: 2026-06-28\n"
                    "Total: EUR 42.50\nOffice supplies\n"
                )
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalAutonomousService(
                ledger,
                {
                    "fab_autonomy_ignore_health_blocks": True,
                    "categorization_rules": {
                        "Office Supplies": {
                            "keywords": ["office supplies"],
                            "vendors": ["test vendor"],
                        }
                    },
                },
                intake_paths=[],
            )

            with patch("src.operations.local_autonomy.LocalConnectorIntakeService") as connector_type:
                connector = connector_type.return_value
                connector.plan.return_value = {
                    "enabledSources": ["gmail"],
                    "syncableSources": ["gmail"],
                    "sources": [{
                        "source": "gmail",
                        "status": "ready",
                        "enabled": True,
                        "canSync": True,
                        "targetSystem": "waveapps_business",
                    }],
                }

                def register_scan(**_kwargs):
                    ledger.register_document({
                        "source": "gmail",
                        "sourceDocumentId": "scanner-message-1_attachment-1",
                        "originalFilename": "scanner-receipt.txt",
                        "mimeType": "text/plain",
                        "storagePath": scan_path,
                        "processingStatus": "imported",
                        "metadata": {"targetSystem": "waveapps_business"},
                    })
                    return {
                        "success": True,
                        "status": "completed",
                        "workflowRunId": 42,
                        "summary": {"registered": 1},
                    }

                connector.sync.side_effect = register_scan
                result = service.run_cycle(
                    include_wave_plan=False,
                    include_wave_sync=False,
                )

            executed_ids = [action["id"] for action in result["executedActions"]]
            self.assertTrue(result["success"])
            self.assertLess(
                executed_ids.index("sync_connector_sources"),
                executed_ids.index("process_imported"),
            )
            connector.sync.assert_called_once_with(
                sources=["gmail"],
                actor="local_autonomy",
                trigger_source="local_autonomous_cycle",
                workflow_metadata={"parentWorkflow": "local_autonomous_cycle"},
            )
            document = ledger.list_documents(limit=1)[0]
            self.assertNotEqual(document["processing_status"], "imported")
            processing = next(
                action for action in result["executedActions"]
                if action["id"] == "process_imported"
            )
            self.assertEqual(processing["summary"]["requested"], 1)

    def test_connector_failure_does_not_suppress_independent_local_work(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            scan_path = os.path.join(intake_dir, "local-receipt.txt")
            with open(scan_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "Vendor: Test Vendor\nDate: 2026-08-03\n"
                    "Total: EUR 42.50\nOffice supplies\n"
                )
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalAutonomousService(
                ledger,
                {
                    "categorization_rules": {
                        "Office Supplies": {
                            "keywords": ["office supplies"],
                            "vendors": ["test vendor"],
                        }
                    },
                },
                intake_paths=[intake_dir],
                intake_extensions=["txt"],
            )

            with patch("src.operations.local_autonomy.LocalConnectorIntakeService") as connector_type:
                connector = connector_type.return_value
                connector.plan.return_value = {
                    "enabledSources": ["gmail"],
                    "syncableSources": ["gmail"],
                    "sources": [{
                        "source": "gmail",
                        "status": "ready",
                        "enabled": True,
                        "canSync": True,
                        "targetSystem": "waveapps_business",
                    }],
                }
                connector.sync.return_value = {
                    "success": False,
                    "status": "failed",
                    "workflowRunId": 42,
                    "summary": {"registered": 0, "failedSources": 1},
                }

                result = service.run_cycle(
                    include_wave_plan=False,
                    include_wave_sync=False,
                )

            executed = {action["id"]: action for action in result["executedActions"]}
            steps = {
                step["step_key"]: step
                for step in ledger.list_workflow_steps(
                    workflow_run_id=result["workflowRunId"],
                    limit=100,
                )
            }

            self.assertTrue(result["success"])
            self.assertEqual(executed["sync_connector_sources"]["status"], "attention_required")
            self.assertEqual(executed["sync_connector_sources"]["summary"]["failedSources"], 1)
            self.assertEqual(steps["sync_connector_sources"]["status"], "blocked")
            self.assertEqual(executed["rescan_intake"]["summary"]["registered"], 1)
            self.assertEqual(executed["process_imported"]["summary"]["requested"], 1)
            self.assertIn("prepare_master_ledger_projection", executed)

    def test_autonomy_surfaces_and_prepares_mijngeldzaken_downstream_routes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "receipt-autonomy-mgz",
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
            service = LocalAutonomousService(
                ledger,
                {"mijngeldzaken_category_mapping": {"Personal": "Huishouden"}},
                intake_paths=[],
            )

            plan = service.plan(include_wave_plan=False)
            result = service.run_cycle(include_wave_plan=False)

            routing_action = next(action for action in plan["actions"] if action["id"] == "prepare_wave_drafts")
            self.assertEqual(routing_action["evidence"]["targetBreakdown"]["mijngeldzaken"], 1)
            self.assertIn("prepare_wave_drafts", plan["runnableActionIds"])
            self.assertTrue(result["success"])
            executed_ids = {action["id"] for action in result["executedActions"]}
            self.assertIn("prepare_wave_drafts", executed_ids)
            self.assertIn("prepare_export_attempts", executed_ids)
            self.assertIn("prepare_master_ledger_projection", executed_ids)
            route_summary = next(action for action in result["executedActions"] if action["id"] == "prepare_wave_drafts")
            export_summary = next(action for action in result["executedActions"] if action["id"] == "prepare_export_attempts")
            master_summary = next(action for action in result["executedActions"] if action["id"] == "prepare_master_ledger_projection")
            self.assertEqual(route_summary["summary"]["targetBreakdown"]["mijngeldzaken"], 1)
            self.assertEqual(export_summary["summary"]["targetBreakdown"]["mijngeldzaken"], 1)
            self.assertEqual(master_summary["summary"]["byTargetSystem"]["mijngeldzaken"]["statuses"]["awaiting_approval"], 1)
            routing_attempt = ledger.list_routing_attempts(status="draft_prepared", limit=1)[0]
            export_attempt = ledger.list_export_attempts(status="approval_required", limit=1)[0]
            self.assertEqual(routing_attempt["target"], "mijngeldzaken:transactions")
            self.assertEqual(export_attempt["target_system"], "mijngeldzaken")
            self.assertEqual(export_attempt["action_id"], "transaction_import_prepare")
            self.assertEqual(export_attempt["payload"]["category"], "Huishouden")

    def test_autonomy_regenerates_stale_mijngeldzaken_export_drafts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            config = {"mijngeldzaken_category_mapping": {"Personal": "Huishouden"}}
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "receipt-autonomy-mgz-stale",
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
            route = LocalRoutingService(ledger, config).prepare_document_route(document_id)
            export_service = LocalExportAttemptService(ledger, config)
            prepared = export_service.prepare_from_routing_attempt(route["routingAttemptId"])
            approved = export_service.approve_attempt(
                prepared["exportAttemptId"],
                actor="tester",
                confirmation=EXPORT_APPROVAL_PHRASE,
            )
            original_checksum = approved["exportAttempt"]["metadata"]["masterLedgerChecksum"]
            ledger.update_document(document_id, {
                "source": "scanner",
                "sourceDocumentId": "receipt-autonomy-mgz-stale",
                "originalFilename": "groceries.txt",
                "documentType": "receipt",
                "processingStatus": "export_draft_prepared",
                "vendorName": "Local Supermarket",
                "category": "Personal",
                "transactionDate": "2026-06-28",
                "totalAmount": 99.99,
                "extractedData": {
                    "vendor_name": "Local Supermarket",
                    "transaction_date": "2026-06-28",
                    "total_amount": 99.99,
                    "description": "Weekly groceries",
                },
                "metadata": {"targetSystem": "mijngeldzaken"},
            })
            service = LocalAutonomousService(ledger, config, intake_paths=[])

            plan = service.plan(include_wave_plan=False)
            result = service.run_cycle(include_wave_plan=False)
            executed_ids = {action["id"] for action in result["executedActions"]}
            regenerate_action = next(
                action for action in result["executedActions"]
                if action["id"] == "regenerate_stale_export_attempts"
            )
            attempt = ledger.get_export_attempt(prepared["exportAttemptId"])

            self.assertIn("regenerate_stale_export_attempts", plan["runnableActionIds"])
            self.assertEqual(plan["counts"]["staleMasterLedgerDrafts"], 1)
            self.assertTrue(result["success"])
            self.assertIn("regenerate_stale_export_attempts", executed_ids)
            self.assertEqual(regenerate_action["summary"]["regenerated"], 1)
            self.assertEqual(attempt["status"], "approval_required")
            self.assertEqual(attempt["external_submission"], "not_executed")
            self.assertEqual(attempt["payload"]["amount"], 99.99)
            self.assertNotEqual(attempt["metadata"]["masterLedgerChecksum"], original_checksum)
            self.assertEqual(result["masterLedger"]["readyForApproval"], 1)
            self.assertEqual(result["masterLedger"]["blockedRows"], 0)
            audit_actions = [event["action"] for event in ledger.list_audit_events(limit=50)]
            self.assertIn("local_export_attempt.regenerated", audit_actions)

    def test_autonomy_run_records_wave_report_snapshots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalAutonomousService(ledger, {}, intake_paths=[])

            result = service.run_cycle(include_wave_plan=True)
            snapshots = ledger.list_wave_report_snapshots(workflow_id="daily_reconciliation_run")

            self.assertTrue(result["success"])
            self.assertIn("plan_wave_daily_reconciliation", {action["id"] for action in result["executedActions"]})
            wave_action = next(action for action in result["executedActions"] if action["id"] == "plan_wave_daily_reconciliation")
            self.assertEqual(wave_action["summary"]["waveReportControls"]["status"], "ready_for_wave_read")
            self.assertGreater(len(snapshots), 0)
            self.assertIn("account-transactions", {snapshot["report_type"] for snapshot in snapshots})
            self.assertEqual(snapshots[0]["external_submission"], "not_executed")

    @patch("src.data_entry.waveapps_entity_sync.requests.post")
    def test_autonomy_refreshes_due_wave_entity_mirror_once(self, mock_post):
        reset_all_limiters()
        set_rate_limiter(
            "waveapps",
            limiter=RateLimiter(calls_per_second=100, calls_per_day=1000, name="WaveApps"),
        )
        self.addCleanup(reset_all_limiters)

        def response_for_request(*args, **kwargs):
            query = kwargs["json"]["query"]
            if "customers(" in query:
                return _wave_entity_response("customers", [{
                    "id": "customer-autonomy-1",
                    "name": "Autonomy Customer",
                    "email": "billing@example.test",
                    "currency": {"code": "EUR"},
                }])
            if "products(" in query:
                return _wave_entity_response("products", [{
                    "id": "product-autonomy-1",
                    "name": "Autonomy Service",
                    "unitPrice": "100.00",
                    "isArchived": False,
                }])
            return _wave_entity_response("invoices", [{
                "id": "invoice-autonomy-1",
                "invoiceNumber": "AUTO-1",
                "status": "DRAFT",
                "invoiceDate": "2026-07-12",
                "dueDate": "2026-08-11",
                "currency": {"code": "EUR"},
                "total": {"value": "100.00"},
            }])

        mock_post.side_effect = response_for_request
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalAutonomousService(ledger, {
                "waveapps_business_access_token": "business-secret-token",
                "waveapps_business_id": "business-1",
                "wave_entity_sync_max_wait_seconds": 0,
            }, intake_paths=[])

            plan = service.plan(include_wave_plan=False)
            result = service.run_cycle(include_wave_plan=False)
            current_plan = service.plan(include_wave_plan=False)
            disabled_plan = service.plan(include_wave_plan=False, include_wave_sync=False)

            self.assertIn("refresh_wave_entity_mirror", plan["runnableActionIds"])
            self.assertEqual(plan["counts"]["waveEntitySyncTargetsDue"], 1)
            self.assertTrue(result["success"])
            action = next(
                item for item in result["executedActions"]
                if item["id"] == "refresh_wave_entity_mirror"
            )
            self.assertEqual(action["status"], "completed")
            self.assertEqual(action["summary"]["entitiesSeen"], 3)
            self.assertEqual(action["summary"]["successfulTargets"], 1)
            self.assertEqual(mock_post.call_count, 3)
            self.assertEqual(len(ledger.list_wave_entities(limit=20)), 3)
            self.assertNotIn("refresh_wave_entity_mirror", current_plan["runnableActionIds"])
            self.assertEqual(current_plan["counts"]["waveEntitySyncTargetsDue"], 0)
            self.assertNotIn("refresh_wave_entity_mirror", disabled_plan["runnableActionIds"])
            disabled_action = next(
                item for item in disabled_plan["actions"]
                if item["id"] == "refresh_wave_entity_mirror"
            )
            self.assertIn("disabled for this request", disabled_action["blockedReason"])
            self.assertNotIn("business-secret-token", str(result))
            audit_actions = [event["action"] for event in ledger.list_audit_events(limit=30)]
            self.assertIn("local_autonomy.wave_entity_mirror_refreshed", audit_actions)

    @patch("src.data_entry.waveapps_entity_sync.requests.post")
    def test_autonomy_holds_failed_wave_sync_inside_retry_window(self, mock_post):
        reset_all_limiters()
        set_rate_limiter(
            "waveapps",
            limiter=RateLimiter(calls_per_second=100, calls_per_day=1000, name="WaveApps"),
        )
        self.addCleanup(reset_all_limiters)
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"errors": [{"message": "temporary provider error"}]}
        mock_post.return_value = response

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalAutonomousService(ledger, {
                "waveapps_business_access_token": "business-secret-token",
                "waveapps_business_id": "business-1",
                "wave_entity_sync_max_wait_seconds": 0,
                "wave_entity_sync_retry_hours": 1,
            }, intake_paths=[])

            result = service.run_cycle(include_wave_plan=False)
            retry_plan = service.plan(include_wave_plan=False)

            action = next(
                item for item in result["executedActions"]
                if item["id"] == "refresh_wave_entity_mirror"
            )
            self.assertTrue(result["success"])
            self.assertEqual(action["status"], "attention_required")
            self.assertEqual(action["summary"]["failedTargets"], 1)
            self.assertNotIn("refresh_wave_entity_mirror", retry_plan["runnableActionIds"])
            target_state = next(
                state for state in next(
                    item for item in retry_plan["actions"]
                    if item["id"] == "refresh_wave_entity_mirror"
                )["evidence"]["targetStates"]
                if state["targetSystem"] == "waveapps_business"
            )
            self.assertEqual(target_state["reason"], "retry_backoff")
            self.assertEqual(mock_post.call_count, 1)

    def test_autonomy_run_prepares_period_close_pack_when_close_ready(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            _capture_zero_activity_wave_result(ledger)
            service = LocalAutonomousService(ledger, {}, intake_paths=[])

            plan = service.plan(include_wave_plan=False)
            result = service.run_cycle(include_wave_plan=False)

            self.assertEqual(plan["closeReadiness"]["status"], "ready")
            self.assertIn("prepare_period_close_pack", plan["runnableActionIds"])
            self.assertTrue(result["success"])
            self.assertIn("prepare_period_close_pack", {action["id"] for action in result["executedActions"]})
            close_action = next(action for action in result["executedActions"] if action["id"] == "prepare_period_close_pack")
            self.assertEqual(close_action["summary"]["status"], "prepared")
            self.assertTrue(close_action["summary"]["closeReadiness"]["canClose"])
            self.assertEqual(close_action["summary"]["externalSubmission"], "not_executed")
            self.assertTrue(os.path.exists(close_action["summary"]["closePackPath"]))
            self.assertEqual(close_action["summary"]["manifest"]["externalSubmission"], "not_executed")
            audit_actions = [event["action"] for event in ledger.list_audit_events(limit=20)]
            self.assertIn("local_close_pack.prepared", audit_actions)
            self.assertIn("local_autonomy.period_close_pack_prepared", audit_actions)

    def test_autonomy_run_uses_persisted_bank_transactions_for_reconciliation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "receipt-autonomy-bank",
                "originalFilename": "receipt.txt",
                "processingStatus": "processed",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
            })
            LocalBankTransactionImportService(ledger, {}).import_transactions([{
                "id": "tx-autonomy-bank",
                "date": "2026-06-28",
                "amount": -42.5,
                "description": "Office Shop",
            }])
            service = LocalAutonomousService(ledger, {"reconciliation_match_threshold": 0.9}, intake_paths=[])

            result = service.run_cycle(include_wave_plan=False)
            transaction = ledger.list_bank_transactions()[0]

            self.assertTrue(result["success"])
            self.assertIn("run_reconciliation", {action["id"] for action in result["executedActions"]})
            self.assertEqual(transaction["reconciliation_status"], "candidate")
            self.assertEqual(ledger.list_reconciliation_matches()[0]["status"], "candidate")

    def test_autonomy_run_refreshes_bank_transaction_records_with_approved_rules(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            ledger.upsert_vendor_category_rule({
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "targetSystem": "waveapps",
                "status": "approved",
            })
            LocalBankTransactionImportService(ledger, {}).import_transactions([{
                "id": "tx-autonomy-rule",
                "date": "2026-06-28",
                "amount": -42.5,
                "description": "Printer paper",
                "counterparty": "Office Shop",
            }])
            service = LocalAutonomousService(ledger, {}, intake_paths=[])

            result = service.run_cycle(include_wave_plan=False)
            records = ledger.list_bookkeeping_records(limit=10)

            self.assertTrue(result["success"])
            self.assertIn("refresh_bank_records", {action["id"] for action in result["executedActions"]})
            self.assertIn("run_reconciliation", {action["id"] for action in result["executedActions"]})
            self.assertIn("prepare_wave_drafts", {action["id"] for action in result["executedActions"]})
            self.assertEqual(records[0]["source_type"], "bank_transaction")
            self.assertEqual(records[0]["category"], "Office Supplies")
            self.assertEqual(records[0]["line_items"][0]["account_name"], "Office Supplies")
            self.assertEqual(records[0]["metadata"]["appliedVendorCategoryRule"]["category"], "Office Supplies")
            self.assertEqual(records[0]["status"], "missing_receipt")
            self.assertEqual(records[0]["export_status"], "blocked_missing_receipt")
            routing_attempts = ledger.list_routing_attempts(status="draft_prepared", limit=10)
            export_attempts = ledger.list_export_attempts(status="approval_required", limit=10)
            self.assertEqual(len(routing_attempts), 0)
            self.assertEqual(len(export_attempts), 0)
            audit_actions = [event["action"] for event in ledger.list_audit_events(limit=20)]
            self.assertIn("local_bookkeeping_records.bank_transactions_refreshed", audit_actions)

    def test_autonomy_can_prepare_bank_record_wave_draft_when_reconciliation_is_not_requested(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            ledger.upsert_vendor_category_rule({
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "targetSystem": "waveapps",
                "status": "approved",
            })
            LocalBankTransactionImportService(ledger, {}).import_transactions([{
                "id": "tx-autonomy-bank-draft",
                "date": "2026-06-28",
                "amount": -42.5,
                "description": "Printer paper",
                "counterparty": "Office Shop",
            }])
            service = LocalAutonomousService(ledger, {}, intake_paths=[])

            result = service.run_cycle(bank_transactions=[], include_wave_plan=False)
            records = ledger.list_bookkeeping_records(limit=10)

            self.assertTrue(result["success"])
            self.assertIn("refresh_bank_records", {action["id"] for action in result["executedActions"]})
            self.assertIn("prepare_wave_drafts", {action["id"] for action in result["executedActions"]})
            self.assertIn("prepare_export_attempts", {action["id"] for action in result["executedActions"]})
            self.assertEqual(records[0]["source_type"], "bank_transaction")
            self.assertEqual(records[0]["export_status"], "awaiting_approval")
            routing_attempts = ledger.list_routing_attempts(status="draft_prepared", limit=10)
            export_attempts = ledger.list_export_attempts(status="approval_required", limit=10)
            self.assertEqual(len(routing_attempts), 1)
            self.assertEqual(routing_attempts[0]["bookkeeping_record_id"], records[0]["id"])
            self.assertEqual(routing_attempts[0]["metadata"]["bookkeepingRecordId"], records[0]["id"])
            self.assertEqual(len(export_attempts), 1)
            self.assertEqual(export_attempts[0]["bookkeeping_record_id"], records[0]["id"])
            self.assertIsNone(export_attempts[0]["document_id"])
            audit_actions = [event["action"] for event in ledger.list_audit_events(limit=30)]
            self.assertIn("local_routing.bank_record_wave_draft_prepared", audit_actions)

    def test_safe_autonomy_cycle_never_executes_approved_exports(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "receipt-autonomy-execute",
                "originalFilename": "receipt.txt",
                "documentType": "receipt",
                "processingStatus": "reviewed",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
            })
            route = LocalRoutingService(ledger).prepare_document_route(document_id)
            export_service = LocalExportAttemptService(ledger)
            prepared = export_service.prepare_from_routing_attempt(route["routingAttemptId"])
            approved = export_service.approve_attempt(
                prepared["exportAttemptId"],
                actor="tester",
                confirmation=EXPORT_APPROVAL_PHRASE,
            )
            service = LocalAutonomousService(
                ledger,
                {
                    "fab_autonomy_execute_approved_exports": True,
                },
                intake_paths=[],
            )

            with patch.object(LocalExportAttemptService, "process_approved_attempts") as execute_exports:
                plan = service.plan(include_wave_plan=False)
                result = service.run_cycle(include_wave_plan=False)

            self.assertEqual(approved["status"], "approved")
            self.assertNotIn("execute_approved_exports", plan["runnableActionIds"])
            self.assertNotIn(
                "execute_approved_exports",
                {action["id"] for action in plan["actions"]},
            )
            self.assertTrue(result["success"])
            execute_exports.assert_not_called()
            self.assertEqual(
                ledger.get_export_attempt(prepared["exportAttemptId"])["status"],
                "approved",
            )
            self.assertEqual(
                ledger.get_export_attempt(prepared["exportAttemptId"])["external_submission"],
                "approved_not_executed",
            )
            audit_actions = [event["action"] for event in ledger.list_audit_events(limit=40)]
            self.assertNotIn("local_backup.created", audit_actions)
            self.assertNotIn("local_autonomy.export_execution_preflight_backup", audit_actions)

    def test_autonomy_blocks_when_remote_exposure_is_unsafe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            readiness = LocalReadinessService(
                {},
                ledger_path=os.path.join(temp_dir, "fab.sqlite3"),
                api_host="0.0.0.0",
                api_token_configured=False,
                intake_paths=[],
                intake_extensions=[],
            )
            service = LocalAutonomousService(
                ledger,
                {},
                readiness=readiness,
                intake_paths=[],
                intake_extensions=[],
            )

            plan = service.plan()
            result = service.run_cycle()

            self.assertEqual(plan["status"], "blocked")
            self.assertIn("remote_exposure_without_token", plan["blockedReasons"])
            self.assertFalse(result["success"])
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(
                ledger.list_audit_events()[0]["action"],
                "local_autonomy.cycle_blocked",
            )

def _capture_zero_activity_wave_result(ledger: LocalOperationsLedger):
    service = LocalWaveControlService()
    plan = service.plan_workflow({
        "workflowId": "daily_reconciliation_run",
        "fromDate": "2026-06-28",
        "toDate": "2026-06-28",
        "accountOption": "-1",
        "contactOption": "0",
    })
    service.record_workflow_report_snapshots(ledger, plan)
    return service.record_report_result(ledger, {
        "workflowId": "daily_reconciliation_run",
        "reportType": "account-transactions",
        "actionId": "report_table_read",
        "result": {
            "rowCount": 0,
            "totalDebits": 0,
            "totalCredits": 0,
        },
    })


def _wave_entity_response(collection, nodes):
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "data": {
            "business": {
                "id": "business-1",
                collection: {
                    "pageInfo": {
                        "currentPage": 1,
                        "totalPages": 1,
                        "totalCount": len(nodes),
                    },
                    "edges": [{"node": node} for node in nodes],
                },
            }
        }
    }
    return response


if __name__ == "__main__":
    unittest.main()
