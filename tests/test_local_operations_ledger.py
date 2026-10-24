import hashlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.operations.local_ledger import (
    LEDGER_SCHEMA_MIGRATIONS,
    LEDGER_SCHEMA_VERSION,
    LocalOperationsLedger,
    SourceScanManifestLimitError,
)


class TestLocalOperationsLedger(unittest.TestCase):
    def test_manifest_limit_cleans_up_previously_committed_chunks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            source_id = ledger.upsert_source_account({
                "sourceType": "local_folder",
                "sourceIdentifier": os.path.join(temp_dir, "receipts"),
                "status": "ready",
            })

            with self.assertRaises(SourceScanManifestLimitError):
                ledger.rebuild_source_scan_manifest(
                    source_id,
                    (f"receipt-{index}.pdf" for index in range(502)),
                    max_entries=501,
                    max_path_bytes=100_000,
                )

            self.assertEqual(ledger.get_source_scan_batch(source_id, None, 10), [])

    def test_credential_url_redaction_is_linear_for_long_scheme_like_text(self):
        secret_url = LocalOperationsLedger._redact_sensitive(
            "https://operator:secret@example.test/path"
        )
        self.assertEqual(secret_url, "https://[REDACTED]@example.test/path")

        adversarial = "a" * 20_000 + "://" + "a" * 20_000
        started = time.perf_counter()
        redacted = LocalOperationsLedger._redact_sensitive(adversarial)
        elapsed = time.perf_counter() - started

        self.assertEqual(redacted, adversarial)
        self.assertLess(elapsed, 1.0)

        long_userinfo = "https://" + ("a" * 20_000) + "@example.test/resource"
        started = time.perf_counter()
        redacted_userinfo = LocalOperationsLedger._redact_sensitive(long_userinfo)
        elapsed = time.perf_counter() - started

        self.assertEqual(redacted_userinfo, "https://[REDACTED]@example.test/resource")
        self.assertLess(elapsed, 1.0)

    def test_error_redaction_bounds_untrusted_diagnostic_input(self):
        with patch.object(
            LocalOperationsLedger,
            "_redact_sensitive",
            side_effect=lambda value: value,
        ) as redact:
            result = LocalOperationsLedger._redact_error_message("x" * 100_000)

        self.assertEqual(len(redact.call_args.args[0]), 16_384)
        self.assertEqual(len(result), 2_000)

    def test_sqlite_sync_mode_matches_deployment_durability_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))

            for profile, expected in (("local", 1), ("windows", 2), ("vm", 2)):
                with self.subTest(profile=profile), patch.dict(
                    os.environ,
                    {"FAB_DEPLOYMENT_PROFILE": profile},
                ):
                    connection = ledger._connect()
                    try:
                        self.assertEqual(
                            connection.execute("PRAGMA synchronous").fetchone()[0],
                            expected,
                        )
                    finally:
                        connection.close()

    def test_persisted_diagnostics_redact_embedded_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            message = (
                "POST https://operator:url-secret@example.test/callback?access_token=query-secret "
                "Authorization: Bearer bearer-secret Cookie: session=cookie-secret "
                'provider={"client_secret":"json-secret"}'
            )

            workflow_id = ledger.create_workflow_run({
                "status": "failed",
                "triggerSource": "test",
                "errorMessage": message,
            })
            step_id = ledger.create_workflow_step({
                "workflowRunId": workflow_id,
                "stepKey": "provider_call",
                "status": "failed",
                "errorMessage": message,
            })
            event_id = ledger.record_audit_event({
                "action": "provider.failed",
                "entityType": "test",
                "details": {
                    "message": message,
                    "authorization": "Bearer object-secret",
                    "nested": {"refreshToken": "nested-secret"},
                },
            })
            snapshot_id = ledger.record_wave_operation_snapshot({
                "workflowId": "workflow-redaction",
                "operationId": "operation-redaction",
                "surface": "transactions",
                "actionId": "transaction_create",
                "safety": "safe_draft",
                "status": "failed",
                "payload": {"accessToken": "payload-secret", "memo": message},
                "plan": {"clientSecret": "plan-secret"},
            })
            sync_run_id = ledger.create_wave_sync_run({
                "targetSystem": "waveapps_business",
                "status": "failed",
                "errorMessage": message,
            })

            persisted = json.dumps({
                "workflow": ledger.get_workflow_run(workflow_id),
                "step": ledger.get_workflow_step(step_id),
                "event": next(item for item in ledger.list_audit_events(limit=20) if item["id"] == event_id),
                "snapshot": ledger.get_wave_operation_snapshot("operation-redaction"),
                "snapshotId": snapshot_id,
                "syncRun": ledger.get_wave_sync_run(sync_run_id),
            })

            self.assertIn("[REDACTED]", persisted)
            for secret in (
                "url-secret",
                "query-secret",
                "bearer-secret",
                "cookie-secret",
                "json-secret",
                "object-secret",
                "nested-secret",
                "payload-secret",
                "plan-secret",
            ):
                self.assertNotIn(secret, persisted)

    def test_read_snapshot_reuses_one_query_only_connection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))

            with patch.object(ledger, "_connect", wraps=ledger._connect) as connect:
                with ledger.read_snapshot():
                    ledger.dashboard_metrics()
                    with ledger.read_snapshot():
                        ledger.list_documents(limit=5)
                    with self.assertRaises(sqlite3.OperationalError):
                        ledger.record_audit_event({
                            "action": "read_snapshot.write_rejected",
                            "entityType": "test",
                        })

            self.assertEqual(connect.call_count, 1)
            event_id = ledger.record_audit_event({
                "action": "read_snapshot.closed",
                "entityType": "test",
            })
            self.assertGreater(event_id, 0)

    def test_schema_history_is_versioned_and_current(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)

            status = ledger.schema_status()
            connection = sqlite3.connect(ledger_path)
            try:
                user_version = connection.execute("PRAGMA user_version").fetchone()[0]
                migrations = connection.execute(
                    "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
                ).fetchall()
            finally:
                connection.close()

            self.assertEqual(status["status"], "current")
            self.assertEqual(status["currentVersion"], LEDGER_SCHEMA_VERSION)
            self.assertTrue(status["historyComplete"])
            self.assertEqual(user_version, LEDGER_SCHEMA_VERSION)
            self.assertEqual([migration[0] for migration in migrations], list(range(1, LEDGER_SCHEMA_VERSION + 1)))
            self.assertTrue(all(len(migration[2]) == 64 for migration in migrations))

    def test_version_one_ledger_gets_verified_backup_before_scan_manifest_upgrade(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            connection = sqlite3.connect(ledger_path)
            try:
                connection.execute("CREATE TABLE legacy_evidence (value TEXT NOT NULL)")
                connection.execute("INSERT INTO legacy_evidence (value) VALUES ('retained')")
                connection.execute(
                    """
                    CREATE TABLE schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        checksum TEXT NOT NULL,
                        applied_at TEXT NOT NULL
                    )
                    """
                )
                migration = LEDGER_SCHEMA_MIGRATIONS[1]
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (1, ?, ?, '2026-08-09T00:00:00Z')",
                    (migration["name"], migration["checksum"]),
                )
                connection.execute("PRAGMA user_version = 1")
                connection.commit()
            finally:
                connection.close()

            ledger = LocalOperationsLedger(ledger_path)
            status = ledger.schema_status()
            backup_dir = os.path.join(temp_dir, "schema_backups")
            manifests = [name for name in os.listdir(backup_dir) if name.endswith(".json")]
            self.assertEqual(len(manifests), 1)
            with open(os.path.join(backup_dir, manifests[0]), "r", encoding="utf-8") as handle:
                backup_manifest = json.load(handle)
            backup_path = os.path.join(backup_dir, backup_manifest["backupFilename"])
            backup = sqlite3.connect(backup_path)
            try:
                retained = backup.execute("SELECT value FROM legacy_evidence").fetchone()[0]
                old_manifest_table = backup.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'source_scan_manifest'"
                ).fetchone()
                integrity = backup.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                backup.close()
            self.assertEqual(status["currentVersion"], LEDGER_SCHEMA_VERSION)
            self.assertTrue(status["historyComplete"])
            self.assertEqual(backup_manifest["sourceSchemaVersion"], 1)
            self.assertEqual(backup_manifest["targetSchemaVersion"], LEDGER_SCHEMA_VERSION)
            self.assertEqual(retained, "retained")
            self.assertIsNone(old_manifest_table)
            self.assertEqual(integrity, "ok")

    def test_existing_unversioned_ledger_gets_verified_pre_migration_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            connection = sqlite3.connect(ledger_path)
            try:
                connection.execute("CREATE TABLE legacy_evidence (value TEXT NOT NULL)")
                connection.execute("INSERT INTO legacy_evidence (value) VALUES ('retained')")
                connection.commit()
            finally:
                connection.close()

            ledger = LocalOperationsLedger(ledger_path)
            backup_dir = os.path.join(temp_dir, "schema_backups")
            manifests = [name for name in os.listdir(backup_dir) if name.endswith(".json")]
            self.assertEqual(len(manifests), 1)
            with open(os.path.join(backup_dir, manifests[0]), "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
            backup_path = os.path.join(backup_dir, manifest["backupFilename"])
            digest = hashlib.sha256(Path(backup_path).read_bytes()).hexdigest()
            backup = sqlite3.connect(backup_path)
            try:
                retained = backup.execute("SELECT value FROM legacy_evidence").fetchone()[0]
                integrity = backup.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                backup.close()

            self.assertEqual(manifest["sourceSchemaVersion"], 0)
            self.assertEqual(manifest["targetSchemaVersion"], LEDGER_SCHEMA_VERSION)
            self.assertEqual(manifest["sha256"], digest)
            self.assertEqual(retained, "retained")
            self.assertEqual(integrity, "ok")
            self.assertTrue(ledger.schema_status()["preMigrationBackupCreated"])
            reopened_status = LocalOperationsLedger(ledger_path).schema_status()
            self.assertTrue(reopened_status["preMigrationBackupCreated"])
            self.assertIsNotNone(reopened_status["lastPreMigrationBackupAt"])
            event = ledger.list_audit_events(limit=1)[0]
            self.assertEqual(event["action"], "local_ledger.pre_migration_backup_created")

    def test_existing_routing_table_is_extended_before_new_index_is_created(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            connection = sqlite3.connect(ledger_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE routing_attempts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        document_id INTEGER,
                        workflow_run_id INTEGER,
                        target TEXT NOT NULL,
                        status TEXT NOT NULL,
                        external_id TEXT,
                        message TEXT,
                        metadata_json TEXT,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO routing_attempts
                      (document_id, workflow_run_id, target, status, created_at)
                    VALUES (1, 2, 'waveapps_business', 'draft_prepared', '2026-08-09')
                    """
                )
                connection.commit()
            finally:
                connection.close()

            ledger = LocalOperationsLedger(ledger_path)
            connection = sqlite3.connect(ledger_path)
            try:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(routing_attempts)")}
                indexes = {row[1] for row in connection.execute("PRAGMA index_list(routing_attempts)")}
                retained = connection.execute(
                    "SELECT target, status FROM routing_attempts WHERE id = 1"
                ).fetchone()
            finally:
                connection.close()

            self.assertIn("bookkeeping_record_id", columns)
            self.assertIn("idx_local_routing_record", indexes)
            self.assertEqual(retained, ("waveapps_business", "draft_prepared"))
            self.assertEqual(ledger.schema_status()["status"], "current")

    def test_future_or_tampered_schema_history_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            future_path = os.path.join(temp_dir, "future.sqlite3")
            connection = sqlite3.connect(future_path)
            try:
                connection.execute("CREATE TABLE retained (value TEXT)")
                connection.execute("PRAGMA user_version = 99")
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(RuntimeError, "newer than supported"):
                LocalOperationsLedger(future_path)

            tampered_path = os.path.join(temp_dir, "tampered.sqlite3")
            LocalOperationsLedger(tampered_path)
            connection = sqlite3.connect(tampered_path)
            try:
                connection.execute("UPDATE schema_migrations SET checksum = 'tampered'")
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(RuntimeError, "failed checksum validation"):
                LocalOperationsLedger(tampered_path)

            incomplete_path = os.path.join(temp_dir, "incomplete.sqlite3")
            LocalOperationsLedger(incomplete_path)
            connection = sqlite3.connect(incomplete_path)
            try:
                connection.execute("DELETE FROM schema_migrations")
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(RuntimeError, "history does not match"):
                LocalOperationsLedger(incomplete_path)

    def test_existing_ledger_gets_workflow_recovery_linkage_columns_and_indexes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            connection = sqlite3.connect(ledger_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE workflow_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        status TEXT NOT NULL,
                        trigger_source TEXT NOT NULL,
                        documents_imported INTEGER NOT NULL DEFAULT 0,
                        documents_processed INTEGER NOT NULL DEFAULT 0,
                        documents_needing_review INTEGER NOT NULL DEFAULT 0,
                        error_message TEXT,
                        metadata_json TEXT,
                        started_at TEXT,
                        finished_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.commit()
            finally:
                connection.close()

            ledger = LocalOperationsLedger(ledger_path)
            source_id = ledger.create_workflow_run({"status": "failed", "triggerSource": "test"})
            child_id = ledger.create_workflow_run({
                "status": "completed",
                "triggerSource": "test_recovery",
                "metadata": {
                    "recovery": {
                        "sourceWorkflowRunId": source_id,
                        "rootWorkflowRunId": source_id,
                    }
                },
            })

            connection = sqlite3.connect(ledger_path)
            try:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(workflow_runs)")}
                indexes = {row[1] for row in connection.execute("PRAGMA index_list(workflow_runs)")}
            finally:
                connection.close()

            self.assertIn("recovery_source_workflow_run_id", columns)
            self.assertIn("recovery_root_workflow_run_id", columns)
            self.assertIn("idx_local_workflow_recovery_source", indexes)
            self.assertIn("idx_local_workflow_recovery_root", indexes)
            self.assertEqual(ledger.get_workflow_recovery_child(source_id)["id"], child_id)

    def test_workflow_steps_are_ordered_filterable_and_redacted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            workflow_run_id = ledger.create_workflow_run({
                "status": "running",
                "triggerSource": "test_cycle",
            })
            first_step_id = ledger.create_workflow_step({
                "workflowRunId": workflow_run_id,
                "stepKey": "collect",
                "stage": "collect",
                "status": "pending",
                "stepOrder": 1,
                "metadata": {"apiToken": "must-not-persist", "scope": "receipts"},
            })
            ledger.create_workflow_step({
                "workflowRunId": workflow_run_id,
                "stepKey": "extract",
                "stage": "extract_validate",
                "status": "skipped",
                "stepOrder": 2,
                "durationMs": -10,
            })

            ledger.update_workflow_step(first_step_id, {
                "status": "completed",
                "startedAt": "2026-07-13T08:00:00Z",
                "finishedAt": "2026-07-13T08:00:01Z",
                "durationMs": 1000,
                "metadata": {"access_token": "must-not-persist", "registered": 2},
            })

            detail = ledger.get_workflow_run_with_steps(workflow_run_id)
            completed = ledger.list_workflow_steps(status="completed")
            metrics = ledger.dashboard_metrics()

            self.assertEqual([step["step_key"] for step in detail["steps"]], ["collect", "extract"])
            self.assertEqual(detail["step_count"], 2)
            self.assertEqual(completed[0]["duration_ms"], 1000)
            self.assertEqual(completed[0]["metadata"]["access_token"], "<redacted>")
            self.assertEqual(detail["steps"][1]["duration_ms"], 0)
            self.assertEqual(metrics["workflow_runs"], 1)
            self.assertEqual(metrics["workflow_steps"], 2)

    def test_runtime_lease_is_atomic_owner_checked_and_redacted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))

            first = ledger.acquire_runtime_lease(
                "autonomy",
                "owner-one",
                ttl_seconds=60,
                metadata={"trigger": "worker", "accessToken": "secret"},
            )
            blocked = ledger.acquire_runtime_lease("autonomy", "owner-two", ttl_seconds=60)

            self.assertTrue(first["acquired"])
            self.assertTrue(first["lease"]["active"])
            self.assertNotIn("owner_token", first["lease"])
            self.assertEqual(first["lease"]["metadata"]["accessToken"], "<redacted>")
            self.assertFalse(blocked["acquired"])
            self.assertEqual(blocked["status"], "already_held")
            self.assertFalse(ledger.release_runtime_lease("autonomy", "owner-two"))
            connection = sqlite3.connect(ledger.path)
            try:
                connection.execute(
                    "UPDATE runtime_leases SET expires_at = ? WHERE lease_name = ?",
                    ("2000-01-01T00:00:00+00:00", "autonomy"),
                )
                connection.commit()
            finally:
                connection.close()
            recovered = ledger.acquire_runtime_lease("autonomy", "owner-two", ttl_seconds=60)
            self.assertTrue(recovered["acquired"])
            self.assertFalse(ledger.release_runtime_lease("autonomy", "owner-one"))
            self.assertTrue(ledger.release_runtime_lease("autonomy", "owner-two"))
            self.assertIsNone(ledger.get_runtime_lease("autonomy"))
            self.assertTrue(
                ledger.acquire_runtime_lease("autonomy", "owner-three", ttl_seconds=60)["acquired"]
            )

    def test_runtime_lease_heartbeat_extends_only_an_active_owned_lease(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            acquired = ledger.acquire_runtime_lease("archive", "owner-one", ttl_seconds=60)
            before = acquired["lease"]["expiresAt"]

            self.assertTrue(ledger.heartbeat_runtime_lease("archive", "owner-one", ttl_seconds=120))
            after = ledger.get_runtime_lease("archive")["expiresAt"]
            self.assertGreater(after, before)
            self.assertFalse(ledger.heartbeat_runtime_lease("archive", "owner-two", ttl_seconds=120))

            connection = sqlite3.connect(ledger.path)
            try:
                connection.execute(
                    "UPDATE runtime_leases SET expires_at = ? WHERE lease_name = ?",
                    ("2000-01-01T00:00:00+00:00", "archive"),
                )
                connection.commit()
            finally:
                connection.close()
            self.assertFalse(ledger.heartbeat_runtime_lease("archive", "owner-one", ttl_seconds=120))
            self.assertTrue(ledger.acquire_runtime_lease("archive", "owner-two", ttl_seconds=60)["acquired"])

    def test_runtime_lease_can_be_force_released_after_owned_service_shutdown(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            ledger.acquire_runtime_lease(
                "local_connector_intake",
                "stopped-worker",
                ttl_seconds=21600,
                metadata={"actor": "local_worker"},
            )

            released = ledger.force_release_runtime_lease(
                "local_connector_intake",
                actor="Stop-FAB.ps1",
                reason="owned_services_stopped",
            )

            self.assertTrue(released)
            self.assertIsNone(ledger.get_runtime_lease("local_connector_intake"))
            event = ledger.list_audit_events(limit=1)[0]
            self.assertEqual(event["action"], "runtime_lease.force_released")
            self.assertEqual(event["entity_id"], "local_connector_intake")
            self.assertEqual(event["details"]["actor"], "Stop-FAB.ps1")
            self.assertEqual(event["details"]["reason"], "owned_services_stopped")
            self.assertEqual(event["details"]["externalSubmission"], "not_executed")
            self.assertFalse(ledger.force_release_runtime_lease(
                "local_connector_intake",
                actor="Stop-FAB.ps1",
                reason="owned_services_stopped",
            ))

    def test_runtime_leases_can_be_listed_by_literal_name_prefix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            ledger.acquire_runtime_lease("hai_command:request-1", "owner-1")
            ledger.acquire_runtime_lease("haiXcommand:request-2", "owner-2")
            ledger.acquire_runtime_lease("local_autonomous_cycle", "owner-3")

            leases = ledger.list_runtime_leases(name_prefix="hai_command:")

            self.assertEqual(
                [lease["leaseName"] for lease in leases],
                ["hai_command:request-1"],
            )
            self.assertNotIn("owner_token", leases[0])

    def test_source_accounts_are_upserted_and_counted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))

            source_id = ledger.upsert_source_account({
                "sourceType": "local_folder",
                "sourceIdentifier": os.path.join(temp_dir, "sort-out"),
                "label": "sort-out",
                "status": "ready",
                "documentsSeen": 2,
                "documentsImported": 1,
                "duplicatesDetected": 1,
            })
            same_source_id = ledger.upsert_source_account({
                "sourceType": "local_folder",
                "sourceIdentifier": os.path.join(temp_dir, "sort-out"),
                "label": "sort-out",
                "status": "ready",
                "documentsSeen": 3,
                "documentsImported": 2,
            })
            document_id = ledger.register_document({
                "sourceAccountId": source_id,
                "source": "local_folder",
                "sourceDocumentId": "receipt-1",
                "originalFilename": "receipt.pdf",
            })

            sources = ledger.list_source_accounts(source_type="local_folder")
            document = ledger.get_document(document_id)

            self.assertEqual(source_id, same_source_id)
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0]["documents_seen"], 5)
            self.assertEqual(sources[0]["documents_imported"], 3)
            self.assertEqual(sources[0]["duplicates_detected"], 1)
            self.assertEqual(document["source_account_id"], source_id)

    def test_extracted_fields_are_replaced_and_returned_with_document_detail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-fields",
                "originalFilename": "receipt.pdf",
            })

            ledger.replace_extracted_fields(document_id, [
                {
                    "fieldName": "vendor_name",
                    "value": "Field Vendor",
                    "confidenceScore": 0.92,
                    "provenance": {"source": "ocr"},
                },
                {
                    "fieldName": "total_amount",
                    "value": 42.5,
                    "confidenceScore": 0.9,
                    "provenance": {"source": "regex"},
                },
            ])
            ledger.replace_extracted_fields(document_id, [
                {
                    "fieldName": "vendor_name",
                    "value": "Corrected Vendor",
                    "confidenceScore": 0.95,
                    "provenance": {"source": "rerun"},
                },
            ])

            fields = ledger.list_extracted_fields(document_id=document_id)
            detail = ledger.get_document(document_id)

            self.assertEqual(len(fields), 1)
            self.assertEqual(fields[0]["field_name"], "vendor_name")
            self.assertEqual(fields[0]["field_value"], "Corrected Vendor")
            self.assertEqual(fields[0]["normalized_value"], "Corrected Vendor")
            self.assertEqual(detail["extracted_fields"][0]["provenance"]["source"], "rerun")

    def test_wave_report_snapshots_are_upserted_and_filterable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))

            snapshot_id = ledger.record_wave_report_snapshot({
                "operationId": "wave:report:1",
                "workflowId": "daily_reconciliation_run",
                "reportType": "account-transactions",
                "reportSection": "detailed_reporting",
                "actionId": "report_table_read",
                "fromDate": "2026-06-28",
                "toDate": "2026-06-28",
                "basis": "accrual",
                "accountOption": "-1",
                "accountName": "All Accounts",
                "contactOption": "0",
                "contactName": "All Contacts",
                "cashMode": "1",
                "metadata": {"token": "secret", "source": "plan"},
            })
            same_snapshot_id = ledger.record_wave_report_snapshot({
                "operationId": "wave:report:1",
                "workflowId": "daily_reconciliation_run",
                "reportType": "account-transactions",
                "actionId": "report_export",
                "format": "csv",
                "status": "planned",
                "rowCount": 0,
            })

            snapshots = ledger.list_wave_report_snapshots(report_type="account-transactions")
            metrics = ledger.dashboard_metrics()

            self.assertEqual(snapshot_id, same_snapshot_id)
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0]["action_id"], "report_export")
            self.assertEqual(snapshots[0]["export_format"], "csv")
            self.assertEqual(snapshots[0]["row_count"], 0)
            self.assertEqual(snapshots[0]["metadata"]["token"], "<redacted>")
            self.assertEqual(metrics["wave_report_snapshots"], 1)

    def test_wave_operation_snapshots_are_upserted_filterable_and_counted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))

            snapshot_id = ledger.record_wave_operation_snapshot({
                "operationId": "wave:operation:1",
                "workflowId": "daily_reconciliation_run",
                "surface": "transactions",
                "actionId": "transaction_add",
                "mode": "prepare",
                "safety": "safe_draft",
                "status": "planned",
                "planStatus": "planned",
                "requiresConfirmation": True,
                "requiresCredentials": False,
                "requiredFields": ["amount", "date", "vendor_name"],
                "missingFields": [],
                "payload": {"transactionId": "tx-1"},
                "metadata": {"token": "secret"},
            })
            same_snapshot_id = ledger.record_wave_operation_snapshot({
                "operationId": "wave:operation:1",
                "workflowId": "daily_reconciliation_run",
                "surface": "transactions",
                "actionId": "transaction_add",
                "status": "completed",
                "planStatus": "ready",
                "requiresConfirmation": False,
                "payload": {"transactionId": "tx-1", "amount": 42.5},
            })
            snapshots = ledger.list_wave_operation_snapshots(surface="transactions")
            filtered = ledger.list_wave_operation_snapshots(safety="safe_draft", status="completed")
            metrics = ledger.dashboard_metrics()

            self.assertEqual(snapshot_id, same_snapshot_id)
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0]["operation_id"], "wave:operation:1")
            self.assertEqual(snapshots[0]["required_fields"], ["amount", "date", "vendor_name"])
            self.assertEqual(snapshots[0]["metadata"]["token"], "<redacted>")
            self.assertEqual(len(filtered), 1)
            self.assertEqual(filtered[0]["plan_status"], "ready")
            self.assertEqual(metrics["wave_operation_snapshots"], 1)

    def test_vendor_category_rule_status_updates_are_governed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))

            rule_id = ledger.upsert_vendor_category_rule({
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "targetSystem": "waveapps_business",
                "status": "suggested",
            })
            approved_rule = ledger.update_vendor_category_rule_status(
                rule_id,
                "approved",
                resolution="Verified from recurring receipts.",
                actor="test",
            )
            same_rule_id = ledger.upsert_vendor_category_rule({
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "targetSystem": "waveapps_business",
                "status": "suggested",
            })

            self.assertEqual(rule_id, same_rule_id)
            rule = ledger.get_vendor_category_rule(rule_id)
            metrics = ledger.dashboard_metrics()

            self.assertEqual(approved_rule["status"], "approved")
            self.assertEqual(rule["status"], "approved")
            self.assertEqual(rule["usage_count"], 2)
            self.assertEqual(rule["metadata"]["statusHistory"][0]["to"], "approved")
            self.assertEqual(metrics["suggested_vendor_rules"], 0)

    def test_conflicting_approved_vendor_rules_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            approved_id = ledger.upsert_vendor_category_rule({
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "targetSystem": "waveapps_business",
                "status": "approved",
            })
            suggested_id = ledger.upsert_vendor_category_rule({
                "vendorName": " OFFICE SHOP ",
                "category": "Equipment",
                "targetSystem": "waveapps_business",
                "status": "suggested",
            })

            with self.assertRaisesRegex(ValueError, "Conflicting approved"):
                ledger.update_vendor_category_rule_status(suggested_id, "approved")
            with self.assertRaisesRegex(ValueError, "Conflicting approved"):
                ledger.upsert_vendor_category_rule({
                    "vendorName": "office shop",
                    "category": "Equipment",
                    "targetSystem": "waveapps_business",
                    "status": "approved",
                })

            self.assertEqual(ledger.get_vendor_category_rule(approved_id)["status"], "approved")
            self.assertEqual(ledger.get_vendor_category_rule(suggested_id)["status"], "suggested")

    def test_duplicate_candidates_are_upserted_listed_and_resolved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            original_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "original",
                "originalFilename": "original.pdf",
            })
            duplicate_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "copy",
                "originalFilename": "copy.pdf",
                "duplicateOfDocumentId": original_id,
            })

            candidate_id = ledger.record_duplicate_candidate({
                "documentId": duplicate_id,
                "candidateDocumentId": original_id,
                "matchType": "exact_content_hash",
                "confidenceScore": 1.0,
                "evidence": {"token": "secret", "contentSha256": "abc"},
            })
            same_candidate_id = ledger.record_duplicate_candidate({
                "documentId": duplicate_id,
                "candidateDocumentId": original_id,
                "matchType": "exact_content_hash",
                "status": "in_review",
                "confidenceScore": 0.99,
            })

            self.assertEqual(candidate_id, same_candidate_id)
            self.assertEqual(ledger.get_duplicate_candidate(candidate_id)["id"], candidate_id)
            self.assertEqual(ledger.dashboard_metrics()["duplicate_candidates"], 1)
            self.assertEqual(ledger.dashboard_metrics()["open_duplicate_candidates"], 1)
            candidates = ledger.list_duplicate_candidates(status="in_review", document_id=duplicate_id)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["evidence"]["token"], "<redacted>")
            detail = ledger.get_document(duplicate_id)
            self.assertEqual(detail["duplicate_candidates"][0]["candidate_document_id"], original_id)

            resolved = ledger.resolve_duplicate_candidates_for_document(duplicate_id, "rejected", "Not the same receipt.")

            self.assertEqual(resolved, 1)
            self.assertEqual(ledger.dashboard_metrics()["open_duplicate_candidates"], 0)
            self.assertEqual(ledger.list_duplicate_candidates()[0]["status"], "rejected")

    def test_resolves_one_duplicate_candidate_without_closing_other_pairs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_ids = [
                ledger.register_document({
                    "source": "gmail",
                    "sourceDocumentId": f"candidate-{index}",
                    "originalFilename": f"candidate-{index}.pdf",
                })
                for index in range(3)
            ]
            first_id = ledger.record_duplicate_candidate({
                "documentId": document_ids[2],
                "candidateDocumentId": document_ids[0],
                "matchType": "fuzzy_document_match",
                "status": "pending",
                "evidence": {"original": True},
            })
            second_id = ledger.record_duplicate_candidate({
                "documentId": document_ids[2],
                "candidateDocumentId": document_ids[1],
                "matchType": "fuzzy_document_match",
                "status": "pending",
            })

            self.assertTrue(
                ledger.resolve_duplicate_candidate(
                    first_id,
                    "rejected",
                    "Pair no longer matches.",
                    evidence={"reassessed": True},
                )
            )

            candidates = {
                item["id"]: item
                for item in ledger.list_duplicate_candidates(limit=10)
            }
            self.assertEqual(candidates[first_id]["status"], "rejected")
            self.assertTrue(candidates[first_id]["evidence"]["original"])
            self.assertTrue(candidates[first_id]["evidence"]["reassessed"])
            self.assertEqual(
                candidates[first_id]["evidence"]["resolution"],
                "Pair no longer matches.",
            )
            self.assertEqual(candidates[second_id]["status"], "pending")

    def test_document_groups_are_persisted_with_members_and_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            first_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-page-1",
                "originalFilename": "scan_001.pdf",
            })
            second_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-page-2",
                "originalFilename": "scan_002.pdf",
            })

            group_id = ledger.upsert_document_group({
                "groupKey": "scanner:scan",
                "groupType": "scanner_batch",
                "title": "Scanner batch",
                "status": "candidate",
                "primaryDocumentId": first_id,
                "confidenceScore": 0.82,
                "metadata": {"token": "secret"},
            })
            same_group_id = ledger.upsert_document_group({
                "groupKey": "scanner:scan",
                "status": "needs_review",
                "confidenceScore": 0.9,
            })
            ledger.add_document_to_group(group_id, first_id, {"role": "primary", "sortOrder": 0})
            ledger.add_document_to_group(group_id, second_id, {"role": "page", "sortOrder": 1})

            self.assertEqual(group_id, same_group_id)
            groups = ledger.list_document_groups(status="needs_review")
            detail = ledger.get_document(first_id)
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0]["member_count"], 2)
            self.assertEqual(groups[0]["metadata"]["token"], "<redacted>")
            self.assertEqual(detail["document_groups"][0]["members"][0]["document_id"], first_id)
            self.assertEqual(ledger.dashboard_metrics()["document_groups"], 1)
            self.assertEqual(ledger.dashboard_metrics()["open_document_groups"], 1)

            removed = ledger.remove_document_from_group(group_id, second_id, "Wrong page.")
            ledger.update_document_group_status(group_id, "split", "Split during review.")

            self.assertEqual(removed, 1)
            updated_group = ledger.get_document_group(group_id)
            self.assertEqual(updated_group["status"], "split")
            self.assertEqual(updated_group["member_count"], 1)
            self.assertEqual(ledger.dashboard_metrics()["open_document_groups"], 0)

    def test_register_document_is_idempotent_by_source_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))

            first_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-001",
                "originalFilename": "receipt.pdf",
                "processingStatus": "imported",
                "duplicateFingerprint": "fingerprint-1",
            })
            second_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-001",
                "originalFilename": "receipt.pdf",
                "processingStatus": "validated",
                "vendorName": "Vendor",
            })
            ledger.create_review_item({
                "documentId": first_id,
                "reason": "low_confidence",
                "status": "pending",
            })
            ledger.record_audit_event({
                "action": "workflow.document.imported",
                "entityType": "bookkeeping_document",
                "entityId": str(first_id),
            })

            self.assertEqual(first_id, second_id)
            self.assertEqual(
                ledger.dashboard_metrics(),
                {
                    "documents": 1,
                    "pending_review": 1,
                    "duplicates": 0,
                    "duplicate_candidates": 0,
                    "open_duplicate_candidates": 0,
                    "document_groups": 0,
                    "open_document_groups": 0,
                    "suggested_vendor_rules": 0,
                    "unreconciled_documents": 1,
                    "failed_documents": 0,
                    "bank_statement_imports": 0,
                    "bank_transactions": 0,
                    "unreconciled_bank_transactions": 0,
                    "bookkeeping_records": 0,
                    "bookkeeping_record_line_items": 0,
                    "bookkeeping_records_needing_review": 0,
                    "export_ready_records": 0,
                    "export_attempts": 0,
                    "export_attempts_needing_approval": 0,
                    "approved_export_attempts": 0,
                    "attention_export_attempts": 0,
                    "supervised_export_attempts": 0,
                    "deferred_export_attempts": 0,
                    "executed_export_attempts": 0,
                    "wave_report_snapshots": 0,
                    "wave_operation_snapshots": 0,
                    "wave_sync_runs": 0,
                    "wave_entities": 0,
                    "wave_entities_missing_downstream": 0,
                    "financial_report_runs": 0,
                    "financial_report_runs_needing_attention": 0,
                    "notifications": 0,
                    "unread_notifications": 0,
                    "active_notifications": 0,
                    "notification_preferences": 0,
                    "compliance_assessments": 0,
                    "open_compliance_findings": 0,
                    "blocking_compliance_findings": 0,
                    "retention_records": 0,
                    "workflow_runs": 0,
                    "workflow_steps": 0,
                    "failed_workflow_steps": 0,
                    "audit_events": 1,
                },
            )

    def test_dashboard_counts_unknown_wave_outcomes_as_attention(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            ledger.upsert_export_attempt({
                "targetSystem": "waveapps_business",
                "status": "failed",
                "externalSubmission": "unknown",
                "message": "Provider outcome is unknown.",
            })

            self.assertEqual(ledger.dashboard_metrics()["attention_export_attempts"], 1)

    def test_bookkeeping_records_are_upserted_and_filterable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-record-1",
                "originalFilename": "receipt.txt",
                "processingStatus": "processed",
            })

            record_id = ledger.upsert_bookkeeping_record({
                "documentId": document_id,
                "sourceType": "document",
                "recordType": "expense",
                "status": "ready_to_route",
                "targetSystem": "waveapps_business",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "recordDate": "2026-06-28",
                "amount": 42.5,
                "vatAmount": 7.38,
                "currency": "EUR",
                "reviewRequired": False,
                "exportStatus": "ready",
                "reconciliationStatus": "candidate",
                "metadata": {"token": "secret", "source": "test"},
            })
            same_record_id = ledger.upsert_bookkeeping_record({
                "documentId": document_id,
                "status": "export_draft_prepared",
                "exportStatus": "draft_prepared",
                "metadata": {"apiKey": "secret", "source": "rerun"},
            })
            line_count = ledger.replace_bookkeeping_record_line_items(record_id, [
                {
                    "itemName": "Printer paper",
                    "description": "A4 paper",
                    "quantity": 2,
                    "unitPrice": 17.56,
                    "amount": 35.12,
                    "taxAmount": 7.38,
                    "taxRate": 21,
                    "taxCode": "BTW 21%",
                    "category": "Office Supplies",
                    "accountName": "Office Supplies",
                    "metadata": {"token": "secret"},
                }
            ])

            with patch.object(
                ledger,
                "_bookkeeping_record_with_line_items",
                side_effect=AssertionError("per-record line-item lookup"),
            ):
                records = ledger.list_bookkeeping_records(export_status="draft_prepared")
            detail = ledger.get_document(document_id)
            lines = ledger.list_bookkeeping_record_line_items(bookkeeping_record_id=record_id)
            metrics = ledger.dashboard_metrics()

            self.assertEqual(record_id, same_record_id)
            self.assertEqual(line_count, 1)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "export_draft_prepared")
            self.assertEqual(records[0]["vendor_name"], "Office Shop")
            self.assertEqual(records[0]["line_item_count"], 1)
            self.assertEqual(records[0]["line_items"][0]["tax_code"], "BTW 21%")
            self.assertEqual(records[0]["line_items"][0]["metadata"]["token"], "<redacted>")
            self.assertEqual(lines[0]["account_name"], "Office Supplies")
            self.assertEqual(records[0]["metadata"]["apiKey"], "<redacted>")
            self.assertEqual(detail["bookkeeping_record"]["id"], record_id)
            self.assertEqual(detail["bookkeeping_record"]["line_items"][0]["item_name"], "Printer paper")
            self.assertEqual(metrics["bookkeeping_records"], 1)
            self.assertEqual(metrics["bookkeeping_record_line_items"], 1)

    def test_vendor_and_category_directories_summarize_records_documents_and_rules(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "vendor-dir-doc",
                "originalFilename": "receipt.txt",
                "processingStatus": "processed",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
            })
            ledger.upsert_bookkeeping_record({
                "documentId": document_id,
                "sourceType": "document",
                "status": "ready_to_route",
                "targetSystem": "waveapps_business",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "recordDate": "2026-06-28",
                "amount": 42.5,
                "currency": "EUR",
                "exportStatus": "ready",
            })
            ledger.upsert_bookkeeping_record({
                "bankTransactionId": 99,
                "sourceType": "bank_transaction",
                "status": "needs_review",
                "targetSystem": "mijngeldzaken",
                "vendorName": "Office Shop",
                "category": "Travel",
                "recordDate": "2026-06-29",
                "amount": 10.0,
                "currency": "EUR",
                "reviewRequired": True,
            })
            ledger.upsert_vendor_category_rule({
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "targetSystem": "waveapps_business",
                "status": "approved",
            })
            ledger.upsert_vendor_category_rule({
                "vendorName": "Office Shop",
                "category": "Travel",
                "targetSystem": "mijngeldzaken",
                "status": "suggested",
            })

            vendors = ledger.list_vendor_summaries()
            categories = ledger.list_category_summaries()
            office_vendor = next(item for item in vendors if item["vendorName"] == "Office Shop")
            office_category = next(item for item in categories if item["category"] == "Office Supplies")
            travel_category = next(item for item in categories if item["category"] == "Travel")

            self.assertEqual(office_vendor["recordCount"], 2)
            self.assertEqual(office_vendor["documentCount"], 1)
            self.assertEqual(office_vendor["bankTransactionCount"], 1)
            self.assertEqual(office_vendor["amountByCurrency"]["EUR"], 52.5)
            self.assertEqual(office_vendor["reviewRequiredCount"], 1)
            self.assertEqual(office_vendor["exportReadyCount"], 1)
            self.assertEqual(office_vendor["ruleCount"], 2)
            self.assertEqual(office_vendor["approvedRuleCount"], 1)
            self.assertEqual(office_vendor["suggestedRuleCount"], 1)
            self.assertTrue(office_vendor["needsAttention"])
            self.assertIn("Office Supplies", {item["value"] for item in office_vendor["categories"]})
            self.assertIn("mijngeldzaken", {item["value"] for item in office_vendor["targetSystems"]})
            self.assertEqual(office_category["recordCount"], 1)
            self.assertEqual(office_category["documentCount"], 1)
            self.assertEqual(office_category["amountByCurrency"]["EUR"], 42.5)
            self.assertIn("Office Shop", {item["value"] for item in office_category["vendors"]})
            self.assertEqual(travel_category["reviewRequiredCount"], 1)
            self.assertTrue(travel_category["needsAttention"])

    def test_export_attempts_are_idempotent_filterable_and_counted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-export-ledger",
                "originalFilename": "receipt.txt",
                "processingStatus": "export_draft_prepared",
            })
            record_id = ledger.upsert_bookkeeping_record({
                "documentId": document_id,
                "status": "export_draft_prepared",
                "exportStatus": "draft_prepared",
            })
            routing_id = ledger.create_routing_attempt({
                "documentId": document_id,
                "target": "waveapps:transactions",
                "status": "draft_prepared",
                "metadata": {"operation": {"operation_id": "op-ledger-1"}},
            })

            export_id = ledger.upsert_export_attempt({
                "bookkeepingRecordId": record_id,
                "documentId": document_id,
                "routingAttemptId": routing_id,
                "targetSystem": "waveapps",
                "targetAccount": "Office Supplies",
                "actionId": "transaction_add",
                "surface": "transactions",
                "operationId": "op-ledger-1",
                "payload": {"token": "secret", "amount": 42.5},
                "metadata": {"apiKey": "secret", "source": "test"},
            })
            same_export_id = ledger.upsert_export_attempt({
                "routingAttemptId": routing_id,
                "operationId": "op-ledger-1",
                "status": "approved",
                "approvalRequired": False,
                "externalSubmission": "approved_not_executed",
                "result": {"credential": "hidden"},
            })

            exports = ledger.list_export_attempts(status="approved")
            detail = ledger.get_document(document_id)
            metrics = ledger.dashboard_metrics()

            self.assertEqual(export_id, same_export_id)
            self.assertEqual(len(exports), 1)
            self.assertEqual(exports[0]["payload"]["token"], "<redacted>")
            self.assertEqual(exports[0]["metadata"]["apiKey"], "<redacted>")
            self.assertEqual(exports[0]["result"]["credential"], "<redacted>")
            self.assertEqual(detail["export_attempts"][0]["id"], export_id)
            self.assertEqual(metrics["export_attempts"], 1)
            self.assertEqual(metrics["export_attempts_needing_approval"], 0)
            self.assertEqual(metrics["approved_export_attempts"], 1)

    def test_bank_transactions_are_imported_idempotently_and_filterable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))

            import_id = ledger.create_bank_statement_import({
                "source": "wave_export",
                "accountIdentifier": "wave-checking",
                "filename": "account-transactions.csv",
                "format": "csv",
                "status": "running",
                "rowsSeen": 2,
            })
            transaction_id = ledger.upsert_bank_transaction({
                "importId": import_id,
                "accountIdentifier": "wave-checking",
                "transactionId": "tx-bank-1",
                "transactionDate": "2026-06-28",
                "amount": -42.5,
                "currency": "EUR",
                "description": "Office Shop",
                "counterparty": "Office Shop",
                "duplicateFingerprint": "fingerprint-1",
                "metadata": {"apiToken": "secret", "source": "csv"},
            })
            same_transaction_id = ledger.upsert_bank_transaction({
                "importId": import_id,
                "accountIdentifier": "wave-checking",
                "transactionId": "tx-bank-1",
                "transactionDate": "2026-06-28",
                "amount": -42.5,
                "currency": "EUR",
                "description": "Office Shop updated",
                "reconciliationStatus": "candidate",
            })
            ledger.update_bank_statement_import(import_id, {
                "status": "completed",
                "rowsImported": 1,
                "duplicates": 1,
            })

            transactions = ledger.list_bank_transactions(
                account_identifier="wave-checking",
                reconciliation_status="candidate",
            )
            imports = ledger.list_bank_statement_imports(account_identifier="wave-checking")
            metrics = ledger.dashboard_metrics()

            self.assertEqual(transaction_id, same_transaction_id)
            self.assertEqual(len(transactions), 1)
            self.assertEqual(transactions[0]["description"], "Office Shop updated")
            self.assertEqual(transactions[0]["metadata"]["apiToken"], "<redacted>")
            self.assertEqual(imports[0]["status"], "completed")
            self.assertEqual(imports[0]["rows_imported"], 1)
            self.assertEqual(imports[0]["duplicates"], 1)
            self.assertEqual(metrics["bank_statement_imports"], 1)
            self.assertEqual(metrics["bank_transactions"], 1)
            self.assertEqual(metrics["unreconciled_bank_transactions"], 1)

    def test_list_review_items_accepts_open_status_collection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-002",
                "originalFilename": "receipt.pdf",
            })
            pending_id = ledger.create_review_item({
                "documentId": document_id,
                "reason": "pending_check",
                "status": "pending",
            })
            in_review_id = ledger.create_review_item({
                "documentId": document_id,
                "reason": "active_check",
                "status": "in_review",
            })
            ledger.create_review_item({
                "documentId": document_id,
                "reason": "closed_check",
                "status": "resolved",
            })

            open_items = ledger.list_review_items(status=("pending", "in_review"))
            first_page = ledger.list_review_items(status=("pending", "in_review"), limit=1)
            second_page = ledger.list_review_items(status=("pending", "in_review"), limit=1, offset=1)

            self.assertEqual({item["id"] for item in open_items}, {pending_id, in_review_id})
            self.assertEqual(
                {first_page[0]["id"], second_page[0]["id"]},
                {pending_id, in_review_id},
            )

    def test_create_review_item_reuses_open_document_reason(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "review-idempotent",
                "originalFilename": "receipt.pdf",
            })
            first_id = ledger.create_review_item({
                "documentId": document_id,
                "reason": "manual_review_category",
                "details": "Initial details.",
            })
            repeated_id = ledger.create_review_item({
                "documentId": document_id,
                "reason": "manual_review_category",
                "details": "Refreshed details.",
            })

            self.assertEqual(repeated_id, first_id)
            open_items = ledger.list_review_items(
                status=("pending", "in_review"),
                document_id=document_id,
            )
            self.assertEqual(len(open_items), 1)
            self.assertEqual(open_items[0]["details"], "Refreshed details.")

    def test_create_review_item_recovers_when_another_worker_wins_insert_race(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "review-insert-race",
                "originalFilename": "receipt.pdf",
            })
            original_insert = ledger._insert_with_connection
            raced = False

            def simulate_competing_insert(connection, table, values):
                nonlocal raced
                if table == "review_items" and not raced:
                    raced = True
                    competitor = LocalOperationsLedger(ledger_path)
                    competitor.create_review_item({
                        "documentId": document_id,
                        "reason": "manual_review_category",
                        "details": "Competing worker details.",
                    })
                    raise sqlite3.IntegrityError("simulated unique-index race")
                return original_insert(connection, table, values)

            with patch.object(ledger, "_insert_with_connection", side_effect=simulate_competing_insert):
                review_id = ledger.create_review_item({
                    "documentId": document_id,
                    "reason": "manual_review_category",
                    "details": "Current worker details.",
                })

            open_items = ledger.list_review_items(
                status=("pending", "in_review"),
                document_id=document_id,
            )
            self.assertEqual(len(open_items), 1)
            self.assertEqual(open_items[0]["id"], review_id)
            self.assertEqual(open_items[0]["details"], "Current worker details.")

    def test_existing_ledger_deduplicates_open_review_rows_before_unique_index(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "legacy-review-duplicates",
                "originalFilename": "receipt.pdf",
            })
            first_id = ledger.create_review_item({
                "documentId": document_id,
                "reason": "manual_review_category",
                "details": "Older review gate.",
            })
            connection = sqlite3.connect(ledger_path)
            try:
                connection.execute("DROP INDEX idx_local_review_open_document_reason")
                cursor = connection.execute(
                    """
                    INSERT INTO review_items
                      (document_id, reason, details, status, created_at, updated_at)
                    VALUES (?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        document_id,
                        "manual_review_category",
                        "Newest review gate.",
                        "2026-07-22T12:00:00+00:00",
                        "2026-07-22T12:00:00+00:00",
                    ),
                )
                newest_id = int(cursor.lastrowid)
                connection.commit()
            finally:
                connection.close()

            migrated = LocalOperationsLedger(ledger_path)
            open_items = migrated.list_review_items(
                status=("pending", "in_review"),
                document_id=document_id,
            )
            all_items = migrated.list_review_items(document_id=document_id)

            self.assertEqual([item["id"] for item in open_items], [newest_id])
            older = next(item for item in all_items if item["id"] == first_id)
            self.assertEqual(older["status"], "resolved")
            self.assertEqual(older["corrected_data"]["supersededByReviewItemId"], newest_id)
            audit = migrated.list_audit_events(limit=1)[0]
            self.assertEqual(audit["action"], "local_ledger.open_reviews_deduplicated")
            self.assertEqual(audit["details"]["supersededReviewItemIds"], [first_id])
            connection = sqlite3.connect(ledger_path)
            try:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO review_items
                          (document_id, reason, details, status, created_at, updated_at)
                        VALUES (?, ?, ?, 'pending', ?, ?)
                        """,
                        (
                            document_id,
                            "manual_review_category",
                            "Should be blocked.",
                            "2026-07-22T13:00:00+00:00",
                            "2026-07-22T13:00:00+00:00",
                        ),
                    )
            finally:
                connection.close()

    def test_reconciliation_matches_can_be_filtered_and_resolved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-reconcile",
                "originalFilename": "receipt.pdf",
                "processingStatus": "processed",
            })
            match_id = ledger.create_reconciliation_match({
                "documentId": document_id,
                "bankTransactionId": "tx-1",
                "status": "candidate",
                "confidenceScore": 0.98,
                "amountDifference": 0,
            })

            matches = ledger.list_reconciliation_matches(document_id=document_id, bank_transaction_id="tx-1")
            ledger.update_reconciliation_match(match_id, {"status": "approved", "matchedAt": "2026-06-28T00:00:00Z"})

            self.assertEqual(matches[0]["id"], match_id)
            self.assertEqual(ledger.get_reconciliation_match(match_id)["status"], "approved")

    def test_existing_ledger_gets_reconciliation_and_content_hash_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            connection = sqlite3.connect(ledger_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE bookkeeping_documents (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source TEXT NOT NULL,
                        source_document_id TEXT,
                        original_filename TEXT NOT NULL,
                        mime_type TEXT,
                        storage_path TEXT,
                        document_type TEXT NOT NULL DEFAULT 'unknown',
                        processing_status TEXT NOT NULL DEFAULT 'imported',
                        duplicate_fingerprint TEXT,
                        duplicate_of_document_id INTEGER,
                        vendor_name TEXT,
                        category TEXT,
                        transaction_date TEXT,
                        total_amount REAL,
                        vat_amount REAL,
                        confidence_score REAL,
                        ocr_text TEXT,
                        extracted_data_json TEXT,
                        metadata_json TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(source, source_document_id)
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO bookkeeping_documents
                      (source, source_document_id, original_filename, processing_status,
                       duplicate_fingerprint, metadata_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "scanner",
                        "legacy-content-hash",
                        "legacy-content.pdf",
                        "processed",
                        "semantic-fingerprint",
                        '{"contentSha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}',
                        "2026-07-22T00:00:00Z",
                        "2026-07-22T00:00:00Z",
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "legacy-1",
                "originalFilename": "legacy.pdf",
                "processingStatus": "processed",
            })

            self.assertEqual(ledger.get_document(document_id)["reconciliation_status"], "not_started")
            migrated = ledger.get_document_by_source("scanner", "legacy-content-hash")
            self.assertEqual(
                migrated["content_sha256"],
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            )

    def test_false_source_revision_repair_requires_identical_unprocessed_bytes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            content_hash = "b" * 64
            parent_id = ledger.register_document({
                "source": "google_drive",
                "sourceDocumentId": "drive-file-1",
                "originalFilename": "receipt.pdf",
                "processingStatus": "processed",
                "duplicateFingerprint": "semantic-fingerprint",
                "contentSha256": content_hash,
                "metadata": {"contentSha256": content_hash},
            })
            child_id = ledger.register_document({
                "source": "google_drive",
                "sourceDocumentId": f"drive-file-1:revision:{content_hash[:16]}",
                "originalFilename": "receipt.pdf",
                "processingStatus": "needs_review",
                "duplicateFingerprint": content_hash,
                "contentSha256": content_hash,
                "metadata": {
                    "contentSha256": content_hash,
                    "sourceRevision": {
                        "revisionOfDocumentId": parent_id,
                        "baseSourceDocumentId": "drive-file-1",
                    },
                },
            })
            ledger.create_review_item({
                "documentId": child_id,
                "reason": "source_revision_detected",
                "details": "False revision caused by legacy fingerprint reuse.",
            })

            result = ledger.repair_false_source_revisions(actor="test")

            self.assertEqual(result["repaired"], 1)
            self.assertIsNotNone(ledger.get_document(parent_id))
            self.assertIsNone(ledger.get_document(child_id))
            self.assertEqual(ledger.list_review_items(document_id=child_id), [])
            event = ledger.list_audit_events(limit=1)[0]
            self.assertEqual(event["action"], "local_ledger.false_source_revision_repaired")
            self.assertFalse(event["details"]["localEvidenceDeleted"])

    def test_audit_event_details_redact_nested_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))

            ledger.record_audit_event({
                "action": "test.secret_redaction",
                "entityType": "test",
                "details": {
                    "accessToken": "top-secret",
                    "nested": {"password": "also-secret", "status": "failed"},
                    "error": "provider failed?access_token=query-secret Authorization: Bearer header-secret",
                },
            })

            event = ledger.list_audit_events(limit=1)[0]
            self.assertEqual(event["details"]["accessToken"], "<redacted>")
            self.assertEqual(event["details"]["nested"]["password"], "<redacted>")
            self.assertEqual(event["details"]["nested"]["status"], "failed")
            self.assertNotIn("query-secret", event["details"]["error"])
            self.assertNotIn("header-secret", event["details"]["error"])
            self.assertIn("[REDACTED]", event["details"]["error"])

    def test_compact_document_snapshot_filters_and_orders_review_items(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            first_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "compact-first",
                "originalFilename": "first.pdf",
                "processingStatus": "needs_review",
            })
            second_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "compact-second",
                "originalFilename": "second.pdf",
                "processingStatus": "processed",
            })
            older_id = ledger.create_review_item({
                "documentId": first_id,
                "reason": "older-open-review",
                "details": "First open item.",
            })
            newer_id = ledger.create_review_item({
                "documentId": first_id,
                "reason": "newer-open-review",
                "details": "Second open item.",
            })
            resolved_id = ledger.create_review_item({
                "documentId": first_id,
                "reason": "resolved-review",
                "details": "Resolved item.",
            })
            ledger.resolve_review_item(
                resolved_id,
                status="resolved",
                resolution="Already handled.",
            )

            documents = ledger.get_documents_with_review_items(
                [first_id, second_id, first_id, "invalid"],
                review_status=("pending", "in_review"),
            )

            self.assertEqual(set(documents), {first_id, second_id})
            self.assertEqual(documents[second_id]["review_items"], [])
            self.assertEqual(
                [item["id"] for item in documents[first_id]["review_items"]],
                [newer_id, older_id],
            )
            self.assertNotIn(
                resolved_id,
                [item["id"] for item in documents[first_id]["review_items"]],
            )

    def test_exception_entity_context_loads_compact_related_counts_and_steps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "exception-context-document",
                "originalFilename": "receipt.pdf",
                "processingStatus": "needs_review",
            })
            for reason in ("first_review", "second_review"):
                ledger.create_review_item({
                    "documentId": document_id,
                    "reason": reason,
                    "details": "Review evidence.",
                })
            workflow_id = ledger.create_workflow_run({
                "status": "failed",
                "triggerSource": "test",
            })
            second_step_id = ledger.create_workflow_step({
                "workflowRunId": workflow_id,
                "stepKey": "second",
                "stepOrder": 2,
                "status": "failed",
            })
            first_step_id = ledger.create_workflow_step({
                "workflowRunId": workflow_id,
                "stepKey": "first",
                "stepOrder": 1,
                "status": "completed",
            })
            source_account_id = ledger.upsert_source_account({
                "sourceType": "gmail",
                "sourceIdentifier": "scanner@example.test",
                "label": "Scanner inbox",
                "status": "ready",
            })

            context = ledger.get_exception_entity_context({
                "bookkeeping_document": [document_id, document_id, "invalid"],
                "workflow_run": [workflow_id],
                "source_account": [source_account_id],
                "unsupported": [1],
            })

            self.assertEqual(
                context["bookkeeping_document"][document_id]["review_item_count"],
                2,
            )
            self.assertEqual(
                [step["id"] for step in context["workflow_run"][workflow_id]["steps"]],
                [first_step_id, second_step_id],
            )
            self.assertEqual(
                context["source_account"][source_account_id]["label"],
                "Scanner inbox",
            )
            self.assertNotIn("unsupported", context)


if __name__ == "__main__":
    unittest.main()
