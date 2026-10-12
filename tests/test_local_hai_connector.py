import io
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from src.operations.local_api import create_app
from src.operations.local_hai_connector import LocalHaiConnector
from src.operations.local_ledger import LocalOperationsLedger


class TestLocalHaiConnector(unittest.TestCase):
    def test_hai_token_is_scoped_to_declared_connector_routes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_api_token": "operator-token-for-tests",
                "fab_hai_api_token": "hai-token-for-tests",
                "fab_hai_connector_enabled": True,
                "fab_hai_allowed_commands": "refresh_notifications",
            })
            client = app.test_client()
            hai_headers = {"Authorization": "Bearer hai-token-for-tests"}
            operator_headers = {"Authorization": "Bearer operator-token-for-tests"}

            manifest = client.get("/api/hai/manifest", headers=hai_headers)
            plan = client.post(
                "/api/hai/commands/plan",
                headers=hai_headers,
                json={"commandId": "refresh_notifications", "payload": {}},
            )
            forbidden_read = client.get("/api/health", headers=hai_headers)
            forbidden_mutation = client.post(
                "/api/autonomy/emergency-stop",
                headers=hai_headers,
                json={"reason": "must not be reachable through the HAI token"},
            )
            operator_read = client.get("/api/health", headers=operator_headers)

            self.assertEqual(manifest.status_code, 200)
            self.assertEqual(plan.status_code, 200)
            self.assertEqual(forbidden_read.status_code, 403)
            self.assertEqual(forbidden_mutation.status_code, 403)
            self.assertEqual(
                forbidden_read.get_json()["error"],
                "HAI credential is not permitted for this route",
            )
            self.assertEqual(operator_read.status_code, 200)

    def test_manifest_is_discoverable_but_execution_is_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            connector = LocalHaiConnector(ledger, executors={"refresh_notifications": lambda payload, actor: {}})

            manifest = connector.manifest()
            result = connector.execute("request-1", "refresh_notifications")

            self.assertEqual(manifest["status"], "prepared_disabled")
            self.assertEqual(manifest["sourceOfTruth"], "fab_local_ledger")
            self.assertEqual(manifest["transport"], "loopback_local_http")
            self.assertEqual(manifest["authentication"], "loopback_origin_controls")
            self.assertIn("submit_to_wave", manifest["excludedCapabilities"])
            self.assertFalse(result["success"])
            self.assertEqual(result["status"], "connector_disabled")

    def test_execution_requires_allowlist_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            executions = []
            connector = LocalHaiConnector(
                ledger,
                {
                    "fab_hai_connector_enabled": True,
                    "fab_hai_allowed_commands": "refresh_notifications",
                },
                executors={
                    "refresh_notifications": lambda payload, actor: executions.append(actor) or {
                        "status": "refreshed",
                    }
                },
            )

            first = connector.execute("request-2", "refresh_notifications", actor="hai-controller")
            second = connector.execute("request-2", "refresh_notifications", actor="different-actor")
            blocked = connector.execute("request-3", "run_reconciliation")

            self.assertEqual(first["status"], "completed")
            self.assertEqual(second["status"], "already_executed")
            self.assertEqual(second["result"], {"status": "refreshed"})
            self.assertEqual(executions, ["hai-controller"])
            self.assertEqual(blocked["status"], "not_allowed")
            self.assertEqual(
                ledger.find_audit_event(
                    "hai.command.completed",
                    "hai_command_request",
                    "request-2",
                )["details"]["commandId"],
                "refresh_notifications",
            )

    def test_concurrent_duplicate_request_executes_only_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            started = threading.Event()
            release = threading.Event()
            executions = []

            def executor(payload, actor):
                executions.append(actor)
                started.set()
                self.assertTrue(release.wait(timeout=10))
                return {"status": "refreshed"}

            connector = LocalHaiConnector(
                ledger,
                {
                    "fab_hai_connector_enabled": True,
                    "fab_hai_allowed_commands": "refresh_notifications",
                },
                executors={"refresh_notifications": executor},
            )
            first_result = {}

            def run_first():
                first_result.update(connector.execute(
                    "concurrent-request-1",
                    "refresh_notifications",
                    actor="first-controller",
                ))

            thread = threading.Thread(target=run_first)
            thread.start()
            self.assertTrue(started.wait(timeout=10))
            concurrent = connector.execute(
                "concurrent-request-1",
                "refresh_notifications",
                actor="second-controller",
            )
            release.set()
            thread.join(timeout=10)
            replay = connector.execute("concurrent-request-1", "refresh_notifications")

            self.assertFalse(thread.is_alive())
            self.assertEqual(first_result["status"], "completed")
            self.assertEqual(concurrent["status"], "execution_in_progress")
            self.assertEqual(replay["status"], "already_executed")
            self.assertEqual(executions, ["first-controller"])

    def test_request_id_is_bound_to_command_and_normalized_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            connector = LocalHaiConnector(
                ledger,
                {
                    "fab_hai_connector_enabled": True,
                    "fab_hai_allowed_commands": "process_imported,refresh_notifications",
                },
                executors={
                    "process_imported": lambda payload, actor: payload,
                    "refresh_notifications": lambda payload, actor: {},
                },
            )

            completed = connector.execute(
                "bound-request-1",
                "process_imported",
                {"limit": 1},
            )
            changed_payload = connector.execute(
                "bound-request-1",
                "process_imported",
                {"limit": 2},
            )
            changed_command = connector.execute(
                "bound-request-1",
                "refresh_notifications",
            )

            self.assertEqual(completed["status"], "completed")
            self.assertEqual(changed_payload["status"], "idempotency_conflict")
            self.assertEqual(changed_command["status"], "idempotency_conflict")

    def test_hai_can_engage_but_cannot_clear_operator_emergency_stop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            app = create_app({
                "fab_local_ledger_path": ledger_path,
                "fab_hai_connector_enabled": True,
                "fab_hai_allowed_commands": "engage_emergency_stop",
            })
            client = app.test_client()

            result = client.post("/api/hai/commands/execute", json={
                "requestId": "stop-request-1",
                "commandId": "engage_emergency_stop",
                "actor": "hai-controller",
                "payload": {"reason": "Operator attention requested by HAI."},
            })
            manifest = client.get("/api/hai/manifest").get_json()

            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.get_json()["result"]["status"], "stopped")
            self.assertTrue(
                LocalOperationsLedger(ledger_path)
                .get_runtime_control("autonomy_emergency_stop")["active"]
            )
            self.assertIn("clear_emergency_stop", manifest["excludedCapabilities"])

    def test_manifest_reports_bearer_transport_when_api_token_is_configured(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            connector = LocalHaiConnector(ledger, {"fab_local_api_token": "configured-secret"})

            manifest = connector.manifest()

            self.assertEqual(manifest["transport"], "authenticated_local_http")
            self.assertEqual(manifest["authentication"], "bearer_token")
            self.assertNotIn("configured-secret", str(manifest))

    def test_maintenance_mode_disables_all_hai_execution_but_keeps_recovery_status_readable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            connector = LocalHaiConnector(
                ledger,
                {
                    "fab_hai_connector_enabled": True,
                    "fab_hai_allowed_commands": "refresh_notifications",
                    "fab_maintenance_mode": True,
                },
                executors={"refresh_notifications": lambda payload, actor: {}},
            )

            manifest = connector.manifest()
            status = connector.status()
            plan = connector.plan("refresh_notifications")

            self.assertEqual(manifest["status"], "maintenance")
            self.assertTrue(manifest["configuredEnabled"])
            self.assertFalse(manifest["enabled"])
            self.assertTrue(manifest["maintenanceMode"])
            self.assertTrue(all(not command["allowed"] for command in manifest["commands"]))
            self.assertEqual(status["allowedCommandIds"], [])
            self.assertEqual(plan["status"], "maintenance_locked")
            self.assertIn(
                "backup_recovery_status",
                {resource["resourceId"] for resource in manifest["resources"]},
            )
            self.assertIn("restore_backups", manifest["excludedCapabilities"])

    def test_payload_validation_rejects_unknown_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            connector = LocalHaiConnector(
                ledger,
                {
                    "fab_hai_connector_enabled": True,
                    "fab_hai_allowed_commands": "run_reconciliation",
                },
                executors={"run_reconciliation": lambda payload, actor: payload},
            )

            result = connector.plan("run_reconciliation", {"bankTransactions": []})

            self.assertEqual(result["status"], "invalid")
            self.assertIn("bankTransactions", result["error"])

            empty_array = connector.plan("run_reconciliation", [])
            self.assertEqual(empty_array["status"], "invalid")
            self.assertEqual(empty_array["error"], "payload must be an object.")

    def test_compliance_dates_follow_manifest_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            connector = LocalHaiConnector(
                ledger,
                {
                    "fab_hai_connector_enabled": True,
                    "fab_hai_allowed_commands": "assess_compliance",
                },
                executors={"assess_compliance": lambda payload, actor: payload},
            )

            malformed = connector.plan("assess_compliance", {"fromDate": "09-08-2026"})
            reversed_period = connector.plan(
                "assess_compliance",
                {"fromDate": "2026-08-10", "toDate": "2026-08-09"},
            )

            self.assertEqual(malformed["status"], "invalid")
            self.assertIn("YYYY-MM-DD", malformed["error"])
            self.assertEqual(reversed_period["status"], "invalid")
            self.assertIn("on or before", reversed_period["error"])

    def test_request_ids_and_executor_failures_do_not_leak_exception_details(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            executions = []

            def fail_executor(payload, actor):
                executions.append(actor)
                raise RuntimeError("provider token and financial detail")

            connector = LocalHaiConnector(
                ledger,
                {
                    "fab_hai_connector_enabled": True,
                    "fab_hai_allowed_commands": "refresh_notifications",
                },
                executors={"refresh_notifications": fail_executor},
            )

            invalid = connector.execute("unsafe request id", "refresh_notifications")
            failed = connector.execute("safe-request-001", "refresh_notifications")
            repeated = connector.execute("safe-request-001", "refresh_notifications")
            audit = ledger.find_audit_event(
                "hai.command.failed",
                "hai_command_request",
                "safe-request-001",
            )

            self.assertEqual(invalid["status"], "invalid_request")
            self.assertEqual(failed["errorCode"], "executor_failed")
            self.assertEqual(failed["errorType"], "RuntimeError")
            self.assertEqual(repeated["status"], "previously_failed")
            self.assertEqual(executions, ["hai"])
            self.assertNotIn("provider token", str(failed))
            self.assertEqual(audit["details"]["errorType"], "RuntimeError")
            self.assertNotIn("provider token", str(audit))

    def test_api_exposes_manifest_plan_and_audited_execution(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            app = create_app({
                "fab_local_ledger_path": ledger_path,
                "fab_hai_connector_enabled": True,
                "fab_hai_allowed_commands": "process_imported",
            })
            client = app.test_client()

            manifest = client.get("/api/hai/manifest")
            plan = client.post("/api/hai/commands/plan", json={
                "commandId": "process_imported",
                "payload": {"limit": 1},
            })
            executed = client.post("/api/hai/commands/execute", json={
                "requestId": "api-request-1",
                "commandId": "process_imported",
                "actor": "dashboard-test",
                "payload": {"limit": 1},
            })
            conflicting = client.post("/api/hai/commands/execute", json={
                "requestId": "api-request-1",
                "commandId": "process_imported",
                "actor": "dashboard-test",
                "payload": {"limit": 2},
            })
            invalid_nested_payload = client.post("/api/hai/commands/plan", json={
                "commandId": "process_imported",
                "payload": [],
            })

            self.assertEqual(manifest.status_code, 200)
            self.assertEqual(len(manifest.get_json()["commands"]), 14)
            self.assertIn(
                "reprocess_review_queue",
                {
                    command["commandId"]
                    for command in manifest.get_json()["commands"]
                },
            )
            resources = {
                item["resourceId"] for item in manifest.get_json()["resources"]
            }
            self.assertEqual(len(resources), 8)
            self.assertIn("backup_recovery_status", resources)
            self.assertIn("google_drive_binary_relay", resources)
            self.assertIn("wave_attachment_work_orders", resources)
            self.assertIn("wave_attachment_binary_readback", resources)
            self.assertIn("wave_receipt_executor_status", resources)
            self.assertIn("wave_receipt_executor_session", resources)
            self.assertIn("wave_receipt_executor_claim", resources)
            self.assertIn("wave_receipt_executor_release", resources)
            self.assertEqual(plan.status_code, 200)
            self.assertEqual(plan.get_json()["status"], "ready")
            self.assertEqual(executed.status_code, 200)
            self.assertEqual(executed.get_json()["status"], "completed")
            self.assertEqual(executed.get_json()["externalSubmission"], "not_executed")
            self.assertEqual(conflicting.status_code, 409)
            self.assertEqual(conflicting.get_json()["status"], "idempotency_conflict")
            self.assertEqual(invalid_nested_payload.status_code, 400)
            self.assertEqual(invalid_nested_payload.get_json()["status"], "invalid")

    def test_hai_token_cannot_spoof_audit_actor_on_allowed_routes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_api_token": "operator-token-for-tests",
                "fab_hai_api_token": "hai-token-for-tests",
            })
            client = app.test_client()
            headers = {"Authorization": "Bearer hai-token-for-tests"}
            operator_headers = {"Authorization": "Bearer operator-token-for-tests"}
            with (
                patch("src.operations.local_api.LocalWaveReceiptExecutorService") as executor,
                patch("src.operations.local_api.DriveWaveDeliveryService") as delivery,
            ):
                executor.return_value.register.return_value = {"success": True}
                executor.return_value.claim_next.return_value = {"success": True}
                executor.return_value.release.return_value = {"success": True}
                delivery.return_value.record_attachment_readback.return_value = {"success": True}

                responses = [
                    client.post(
                        "/api/wave/receipt-executor/session",
                        headers=headers,
                        json={"actor": "operator_admin", "executorId": "executor-1"},
                    ),
                    client.post(
                        "/api/wave/receipt-executor/claim",
                        headers=headers,
                        json={"actor": "operator_admin"},
                    ),
                    client.post(
                        "/api/wave/receipt-executor/release",
                        headers=headers,
                        json={"actor": "operator_admin", "documentId": 1},
                    ),
                    client.post(
                        "/api/drive-wave/documents/1/attachment-readback",
                        headers=headers,
                        data={
                            "actor": "operator_admin",
                            "evidence": "{}",
                            "executorId": "executor-1",
                            "sessionId": "session-1",
                            "attachment": (io.BytesIO(b"receipt"), "receipt.pdf"),
                        },
                        content_type="multipart/form-data",
                    ),
                ]
                operator_response = client.post(
                    "/api/wave/receipt-executor/session",
                    headers=operator_headers,
                    json={"actor": "wave_browser_executor"},
                )

            self.assertEqual(
                [response.status_code for response in responses],
                [200, 200, 200, 200],
            )
            self.assertEqual(operator_response.status_code, 200)
            self.assertEqual(
                executor.return_value.register.call_args_list[0].kwargs["actor"],
                "fab_hai_api:hai",
            )
            self.assertEqual(
                executor.return_value.register.call_args_list[1].kwargs["actor"],
                "wave_browser_executor",
            )
            self.assertEqual(
                executor.return_value.claim_next.call_args.kwargs["actor"],
                "fab_hai_api:hai",
            )
            self.assertEqual(
                executor.return_value.release.call_args.kwargs["actor"],
                "fab_hai_api:hai",
            )
            self.assertEqual(
                [call.kwargs["actor"] for call in executor.return_value.release.call_args_list],
                ["fab_hai_api:hai", "fab_hai_api:hai"],
            )
            self.assertEqual(
                delivery.return_value.record_attachment_readback.call_args.kwargs["actor"],
                "fab_hai_api:hai",
            )


if __name__ == "__main__":
    unittest.main()
