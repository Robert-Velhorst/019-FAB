import os
import tempfile
import unittest
from unittest.mock import patch

from src.data_entry.mijngeldzaken_artifacts import MijngeldzakenArtifactStore
from src.operations.local_exports import EXPORT_APPROVAL_PHRASE, LocalExportAttemptService
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_routing import LocalRoutingService
from src.run_approved_postings import run_approved_postings
from src.security.local_secret_store import LocalSecretStore
from src.worker.scheduler import FabWorker


class TestOperationsExportWorker(unittest.TestCase):
    def test_failed_source_leaves_worker_queue_without_preparing_provider_artifact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir)
            ledger = LocalOperationsLedger(config["fab_local_ledger_path"])
            document_id, attempt_id = self._prepare_approved_mijngeldzaken_export(ledger, config)
            ledger.update_document(document_id, {"processingStatus": "failed"})
            service = LocalExportAttemptService(ledger, config)

            first = service.process_approved_attempts(create_backup=False)
            second = service.process_approved_attempts(create_backup=False)

            self.assertFalse(first["success"])
            self.assertEqual(first["processed"][0]["status"], "blocked_processing")
            self.assertEqual(second["count"], 0)
            self.assertEqual(ledger.get_export_attempt(attempt_id)["status"], "attention_required")
            self.assertFalse(os.path.exists(config["mijngeldzaken_export_dir"]))

    def _config(self, temp_dir):
        return {
            "fab_local_ledger_enabled": True,
            "fab_local_ledger_path": os.path.join(temp_dir, "fab-operations.sqlite3"),
            "fab_local_backup_dir": os.path.join(temp_dir, "backups"),
            "fab_local_report_dir": os.path.join(temp_dir, "reports"),
            "mijngeldzaken_export_dir": os.path.join(temp_dir, "mijngeldzaken-exports"),
            "mijngeldzaken_category_mapping": {"Personal": "Huishouden"},
            "fab_autonomy_execute_approved_exports": True,
            "worker_run_once": True,
            "worker_process_approved_postings": True,
            "worker_process_due_retries": True,
            "worker_create_scheduled_backups": True,
            "backup_require_complete_source_evidence": False,
            "worker_generate_scheduled_reports": True,
            "report_schedule_frequency": "monthly",
            "report_schedule_period_mode": "current_year_to_date",
            "worker_process_legacy_postings": False,
        }

    def _prepare_approved_mijngeldzaken_export(self, ledger, config, suffix="1"):
        document_id = ledger.register_document({
            "source": "scanner",
            "sourceDocumentId": f"worker-mgz-{suffix}",
            "originalFilename": f"receipt-{suffix}.txt",
            "documentType": "receipt",
            "processingStatus": "reviewed",
            "vendorName": "Local Supermarket",
            "category": "Personal",
            "transactionDate": "2026-07-10",
            "totalAmount": 31.25,
            "extractedData": {
                "vendor_name": "Local Supermarket",
                "transaction_date": "2026-07-10",
                "total_amount": 31.25,
                "description": "Weekly groceries",
            },
            "metadata": {"targetSystem": "mijngeldzaken"},
        })
        route = LocalRoutingService(ledger, config).prepare_document_route(document_id)
        service = LocalExportAttemptService(ledger, config)
        prepared = service.prepare_from_routing_attempt(route["routingAttemptId"])
        service.approve_attempt(
            prepared["exportAttemptId"],
            actor="tester",
            confirmation=EXPORT_APPROVAL_PHRASE,
        )
        return document_id, prepared["exportAttemptId"]

    def test_repeated_scheduled_empty_folder_cycles_reduce_workflow_growth_by_95_percent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            config = {
                **self._config(temp_dir),
                "fab_local_intake_paths": intake_dir,
                "worker_include_wave_plan": False,
                "worker_include_wave_sync": False,
                "worker_audit_heartbeat_seconds": 86400,
            }
            worker = FabWorker(config)
            ledger = worker.operations_ledger

            for _ in range(20):
                worker._run_local_autonomy()

            workflow_runs = ledger.list_workflow_runs(limit=100)
            audit_events = ledger.list_audit_events(limit=500)
            inner_events = [
                event for event in audit_events
                if str(event.get("action") or "").startswith("local_autonomy.")
            ]
            worker_events = [
                event for event in audit_events
                if event.get("action") == "local_worker.autonomy_cycle"
            ]

            self.assertLessEqual(len(workflow_runs), 1)
            self.assertLessEqual(len(inner_events), 3)
            self.assertLessEqual(len(worker_events), 2)

    def test_worker_uses_operations_ledger_and_does_not_open_legacy_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir)
            ledger = LocalOperationsLedger(config["fab_local_ledger_path"])
            document_id, export_attempt_id = self._prepare_approved_mijngeldzaken_export(
                ledger,
                config,
            )
            worker = FabWorker(config)

            with patch.object(worker, "install_signal_handlers"):
                worker.run()

            self.assertIsNone(worker.database)
            attempt = ledger.get_export_attempt(export_attempt_id)
            self.assertEqual(attempt["status"], "supervision_required")
            self.assertEqual(attempt["external_submission"], "not_executed")
            self.assertTrue(os.path.isfile(attempt["metadata"]["supervisedArtifact"]["path"]))
            reviews = ledger.list_review_items(document_id=document_id)
            self.assertEqual(reviews[0]["reason"], "mijngeldzaken_supervision_required")
            audit_actions = {event["action"] for event in ledger.list_audit_events(limit=100)}
            self.assertIn("local_worker.autonomy_cycle", audit_actions)
            self.assertIn("local_worker.connector_intake_cycle", audit_actions)
            self.assertIn("local_autonomy.cycle_completed", audit_actions)
            self.assertIn("local_worker.approved_export_cycle", audit_actions)
            self.assertIn("local_worker.scheduled_backup_cycle", audit_actions)
            self.assertIn("local_worker.scheduled_report_cycle", audit_actions)
            self.assertIn("local_worker.compliance_cycle", audit_actions)
            self.assertIn("local_worker.notification_cycle", audit_actions)
            self.assertIn("local_reporting.scheduled_report_prepared", audit_actions)
            self.assertIn("local_export_attempt.batch_execution_preflight_backup", audit_actions)
            self.assertIn("local_export_attempt.supervision_required", audit_actions)
            self.assertEqual(ledger.list_export_attempts(status="approved"), [])
            self.assertEqual(len(ledger.list_financial_report_runs()), 1)
            self.assertEqual(len(ledger.list_compliance_assessments()), 1)

    def test_worker_does_not_repeat_export_preparation_completed_by_autonomy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir)
            worker = FabWorker(config)

            with patch("src.worker.scheduler.LocalAutonomousService") as autonomy_type, patch(
                "src.worker.scheduler.LocalExportAttemptService"
            ) as export_type:
                autonomy_type.return_value.run_cycle.return_value = {
                    "success": True,
                    "status": "completed",
                    "executedActions": [{"id": "prepare_export_attempts", "status": "completed"}],
                    "skippedActions": [],
                }
                export_type.return_value.process_approved_attempts.return_value = {
                    "success": True,
                    "status": "completed",
                    "processed": [],
                    "count": 0,
                }

                worker._run_local_autonomy()
                worker._process_operations_exports()

            export_type.return_value.prepare_ready_exports.assert_not_called()
            export_type.return_value.process_approved_attempts.assert_called_once_with(
                limit=20,
                actor="local_worker",
            )

    def test_worker_falls_back_to_export_preparation_when_autonomy_did_not_run_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir)
            worker = FabWorker(config)

            with patch("src.worker.scheduler.LocalExportAttemptService") as export_type:
                export_type.return_value.prepare_ready_exports.return_value = {
                    "requested": 0,
                    "prepared": 0,
                    "alreadyPrepared": 0,
                    "blocked": 0,
                    "changed": False,
                    "auditRecorded": False,
                    "externalSubmission": "not_executed",
                }
                export_type.return_value.process_approved_attempts.return_value = {
                    "success": True,
                    "status": "completed",
                    "processed": [],
                    "count": 0,
                }

                worker._process_operations_exports()

            export_type.return_value.prepare_ready_exports.assert_called_once_with(limit=25)

    def test_worker_coalesces_proven_idle_cycles_without_workflow_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir)
            config.update({
                "worker_sync_source_connectors": False,
                "worker_include_wave_plan": False,
                "worker_include_wave_sync": False,
            })
            worker = FabWorker(config)

            worker._run_local_autonomy()

            ledger = LocalOperationsLedger(config["fab_local_ledger_path"])
            workflows = ledger.list_workflow_runs(
                trigger_source="local_autonomous_cycle",
                limit=1,
            )
            audit_events = ledger.list_audit_events(limit=20)

            self.assertEqual(workflows, [])
            self.assertEqual(
                len([
                    event for event in audit_events
                    if event.get("action") == "local_worker.autonomy_cycle"
                ]),
                1,
            )

    def test_worker_coalesces_unchanged_cycle_audits_but_records_state_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir)
            worker = FabWorker(config)

            worker._record_audit(
                "scheduled_backup_cycle",
                {"success": True, "status": "not_due", "nextDueAt": "2026-08-14T00:00:00Z"},
                "Backup is current",
            )
            worker._record_audit(
                "scheduled_backup_cycle",
                {"success": True, "status": "not_due", "nextDueAt": "2026-08-14T00:00:00Z"},
                "Backup is current",
            )
            worker._record_audit(
                "scheduled_backup_cycle",
                {"success": False, "status": "failed", "nextDueAt": "2026-08-14T00:00:00Z"},
                "Backup failed",
            )

            ledger = LocalOperationsLedger(config["fab_local_ledger_path"])
            events = [
                event for event in ledger.list_audit_events(limit=10)
                if event["action"] == "local_worker.scheduled_backup_cycle"
            ]
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["details"]["status"], "failed")
            self.assertEqual(events[1]["details"]["status"], "not_due")

    def test_worker_stage_failure_does_not_suppress_local_autonomy_or_exports(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir)
            config["freshdesk_api_key"] = "super-secret-value"
            worker = FabWorker(config)

            with patch.object(worker, "install_signal_handlers"), patch.object(
                worker,
                "_sync_source_connectors",
                side_effect=RuntimeError("api_key=super-secret-value connector unavailable"),
            ), patch.object(worker, "_run_local_autonomy") as autonomy, patch.object(
                worker,
                "_process_scheduled_reports",
            ) as scheduled_reports, patch.object(
                worker,
                "_process_operations_exports",
            ) as exports:
                worker.run()

            autonomy.assert_called_once()
            scheduled_reports.assert_called_once()
            exports.assert_called_once()
            ledger = LocalOperationsLedger(config["fab_local_ledger_path"])
            audit_actions = [event["action"] for event in ledger.list_audit_events(limit=30)]
            self.assertIn("local_worker.stage_failed", audit_actions)
            self.assertIn("local_worker.cycle_finished_with_error", audit_actions)
            audit_payload = str(ledger.list_audit_events(limit=30))
            self.assertNotIn("super-secret-value", audit_payload)
            self.assertIn("[REDACTED]", audit_payload)

    def test_connector_failure_does_not_suppress_autonomy_or_exports(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir)
            worker = FabWorker(config)

            with patch.object(worker, "install_signal_handlers"), patch.object(
                worker,
                "_sync_source_connectors",
                side_effect=RuntimeError("gmail unavailable"),
            ), patch.object(worker, "_run_local_autonomy") as autonomy, patch.object(
                worker,
                "_process_scheduled_reports",
            ), patch.object(worker, "_process_operations_exports") as exports:
                worker.run()

            autonomy.assert_called_once()
            exports.assert_called_once()
            ledger = LocalOperationsLedger(config["fab_local_ledger_path"])
            failed_stages = [
                event for event in ledger.list_audit_events(limit=30)
                if event["action"] == "local_worker.stage_failed"
            ]
            self.assertEqual(failed_stages[0]["details"]["stage"], "connector_intake")

    def test_worker_syncs_only_syncable_sources_not_held_by_recovery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = FabWorker(self._config(temp_dir))
            worker._recovery_held_connector_sources = {"gmail"}

            with patch("src.worker.scheduler.LocalConnectorIntakeService") as service_type:
                service = service_type.return_value
                service.plan.return_value = {
                    "syncableSources": ["gmail", "google_drive"],
                }
                service.sync.return_value = {
                    "success": True,
                    "status": "completed",
                    "workflowRunId": 1,
                    "summary": {},
                    "externalSubmission": "not_executed",
                }

                worker._sync_source_connectors()

            service.sync.assert_called_once_with(
                sources=["google_drive"],
                actor="local_worker",
            )

    def test_retired_legacy_pipeline_configuration_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir)
            config["worker_run_legacy_workflow"] = True

            with self.assertRaisesRegex(ValueError, "was retired"):
                FabWorker(config)

    def test_worker_refreshes_wave_settings_saved_after_startup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir)
            config.update({
                "fab_local_secret_store_path": os.path.join(temp_dir, "credentials", "secrets.enc"),
                "fab_local_secret_key_path": os.path.join(temp_dir, "credentials", "secrets.key"),
                "worker_run_local_autonomy": False,
                "worker_sync_source_connectors": False,
                "worker_recover_workflows": False,
                "worker_create_scheduled_backups": False,
                "worker_generate_scheduled_reports": False,
                "worker_refresh_notifications": False,
                "worker_assess_compliance": False,
                "worker_archive_verified_drive_sources": False,
                "worker_process_approved_postings": False,
            })
            worker = FabWorker(config)
            LocalSecretStore(config).update_wave_target("waveapps_business", {
                "access_token": "saved-after-worker-started",
                "business_id": "business-1",
            })

            with patch.object(worker, "install_signal_handlers"), patch.object(
                worker,
                "_process_operations_exports",
            ):
                worker.run()

            self.assertEqual(
                worker.config["waveapps_business_access_token"],
                "saved-after-worker-started",
            )
            self.assertEqual(worker.config["waveapps_business_id"], "business-1")

    def test_scheduled_backup_failure_does_not_suppress_later_worker_stages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir)
            worker = FabWorker(config)

            with patch.object(worker, "install_signal_handlers"), patch(
                "src.worker.scheduler.LocalBackupService.run_due",
                side_effect=RuntimeError("backup disk unavailable"),
            ), patch.object(worker, "_run_local_autonomy") as autonomy, patch.object(
                worker,
                "_process_scheduled_reports",
            ) as scheduled_reports, patch.object(
                worker,
                "_process_operations_exports",
            ) as exports:
                worker.run()

            autonomy.assert_called_once()
            scheduled_reports.assert_called_once()
            exports.assert_called_once()
            ledger = LocalOperationsLedger(config["fab_local_ledger_path"])
            failed_stages = [
                event for event in ledger.list_audit_events(limit=30)
                if event["action"] == "local_worker.stage_failed"
            ]
            self.assertEqual(failed_stages[0]["details"]["stage"], "scheduled_backup")

    def test_scheduled_report_failure_does_not_suppress_export_stage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir)
            worker = FabWorker(config)

            with patch.object(worker, "install_signal_handlers"), patch.object(
                worker,
                "_run_local_autonomy",
            ), patch.object(
                worker,
                "_process_scheduled_reports",
                side_effect=RuntimeError("report disk unavailable"),
            ), patch.object(worker, "_process_operations_exports") as exports:
                worker.run()

            exports.assert_called_once()
            ledger = LocalOperationsLedger(config["fab_local_ledger_path"])
            failed_stages = [
                event for event in ledger.list_audit_events(limit=30)
                if event["action"] == "local_worker.stage_failed"
            ]
            self.assertEqual(failed_stages[0]["details"]["stage"], "scheduled_reports")
            self.assertIn(
                "local_worker.cycle_finished_with_error",
                {event["action"] for event in ledger.list_audit_events(limit=30)},
            )

    def test_notification_failure_does_not_suppress_export_stage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir)
            worker = FabWorker(config)

            with patch.object(worker, "install_signal_handlers"), patch.object(
                worker,
                "_run_local_autonomy",
            ), patch.object(
                worker,
                "_process_scheduled_reports",
            ), patch.object(
                worker,
                "_refresh_notifications",
                side_effect=RuntimeError("notification store unavailable"),
            ), patch.object(worker, "_process_operations_exports") as exports:
                worker.run()

            exports.assert_called_once()
            ledger = LocalOperationsLedger(config["fab_local_ledger_path"])
            failed_stages = [
                event for event in ledger.list_audit_events(limit=30)
                if event["action"] == "local_worker.stage_failed"
            ]
            self.assertEqual(failed_stages[0]["details"]["stage"], "notifications")

    def test_compliance_failure_does_not_suppress_notifications_or_exports(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir)
            worker = FabWorker(config)

            with patch.object(worker, "install_signal_handlers"), patch.object(
                worker,
                "_run_local_autonomy",
            ), patch.object(
                worker,
                "_process_scheduled_reports",
            ), patch.object(
                worker,
                "_assess_compliance",
                side_effect=RuntimeError("compliance assessment unavailable"),
            ), patch.object(worker, "_refresh_notifications") as notifications, patch.object(
                worker,
                "_process_operations_exports",
            ) as exports:
                worker.run()

            notifications.assert_called_once()
            exports.assert_called_once()
            ledger = LocalOperationsLedger(config["fab_local_ledger_path"])
            failed_stages = [
                event for event in ledger.list_audit_events(limit=30)
                if event["action"] == "local_worker.stage_failed"
            ]
            self.assertEqual(failed_stages[0]["details"]["stage"], "compliance")

    def test_manual_runner_uses_operations_ledger_and_respects_execution_flag(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir)
            config["fab_autonomy_execute_approved_exports"] = False
            ledger = LocalOperationsLedger(config["fab_local_ledger_path"])
            _, export_attempt_id = self._prepare_approved_mijngeldzaken_export(ledger, config)

            skipped = run_approved_postings(config)
            status_after_skip = ledger.get_export_attempt(export_attempt_id)["status"]
            config["fab_autonomy_execute_approved_exports"] = True
            executed = run_approved_postings(config)

            self.assertEqual(skipped["sourceOfTruth"], "local_operations_ledger")
            self.assertEqual(skipped["execution"]["status"], "skipped")
            self.assertEqual(status_after_skip, "approved")
            self.assertEqual(executed["sourceOfTruth"], "local_operations_ledger")
            self.assertEqual(executed["execution"]["count"], 1)
            self.assertEqual(
                ledger.get_export_attempt(export_attempt_id)["status"],
                "supervision_required",
            )

    def test_artifact_store_sanitizes_filename_and_writes_checksum_bound_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MijngeldzakenArtifactStore({"mijngeldzaken_export_dir": temp_dir})

            artifact = store.write_text("../private import.csv", "Datum,Bedrag\n2026-07-10,31.25\n")

            self.assertTrue(
                os.path.samefile(os.path.dirname(artifact["path"]), temp_dir)
            )
            self.assertNotIn("..", artifact["filename"])
            self.assertIn(artifact["sha256"][:12], artifact["filename"])
            self.assertTrue(os.path.isfile(artifact["path"]))
            self.assertEqual(
                [name for name in os.listdir(temp_dir) if name.endswith(".tmp")],
                [],
            )

    def test_approved_batch_does_not_execute_when_preflight_backup_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir)
            ledger = LocalOperationsLedger(config["fab_local_ledger_path"])
            _, export_attempt_id = self._prepare_approved_mijngeldzaken_export(ledger, config)
            service = LocalExportAttemptService(ledger, config)

            with patch(
                "src.operations.local_backup.LocalBackupService.create_backup",
                return_value={"success": False, "status": "failed", "error": "disk full"},
            ):
                result = service.process_approved_attempts(actor="test-worker")

            self.assertFalse(result["success"])
            self.assertEqual(result["status"], "pre_execution_backup_failed")
            self.assertEqual(result["count"], 0)
            self.assertEqual(ledger.get_export_attempt(export_attempt_id)["status"], "approved")
            self.assertFalse(os.path.exists(config["mijngeldzaken_export_dir"]))
            audit_actions = {event["action"] for event in ledger.list_audit_events(limit=50)}
            self.assertIn("local_export_attempt.batch_execution_blocked_backup", audit_actions)


if __name__ == "__main__":
    unittest.main()
