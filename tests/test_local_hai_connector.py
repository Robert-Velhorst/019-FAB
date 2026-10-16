import io
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from src.operations.local_api import HAI_ROUTE_RULES, create_app
from src.operations.local_exports import EXPORT_APPROVAL_PHRASE, LocalExportAttemptService
from src.operations.local_hai_connector import LocalHaiConnector
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_routing import LocalRoutingService


class TestLocalHaiConnector(unittest.TestCase):
    def test_hai_token_is_denied_from_every_unlisted_api_method(self):
        expected_rules = (
            ("GET", r"^/api/hai/(?:manifest|status)$"),
            ("POST", r"^/api/hai/commands/(?:plan|execute)$"),
            ("GET", r"^/api/backups$"),
            ("POST", r"^/api/connectors/google-drive/relay$"),
            ("GET", r"^/api/drive-wave/work-orders$"),
            ("GET", r"^/api/wave/receipt-executor/status$"),
            ("POST", r"^/api/wave/receipt-executor/(?:session|claim|release)$"),
        )
        self.assertEqual(
            tuple((method, pattern.pattern) for method, pattern in HAI_ROUTE_RULES),
            expected_rules,
        )
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
            checked = 0
            skipped_mutations = 0
            methods_to_check = ("POST", "PUT", "PATCH", "DELETE", "GET", "HEAD")

            for rule in app.url_map.iter_rules():
                if not rule.rule.startswith("/api/"):
                    continue
                values = {}
                for name, converter in rule._converters.items():
                    converter_name = type(converter).__name__.lower()
                    values[name] = (
                        1 if "integer" in converter_name or "float" in converter_name
                        else "00000000-0000-0000-0000-000000000001" if "uuid" in converter_name
                        else "audit-probe"
                    )
                _, path = rule.build(values)
                allowed_methods = set(rule.methods or ()) - {"OPTIONS"}
                for method in methods_to_check:
                    if method not in allowed_methods:
                        continue
                    allowed = any(
                        method == allowed_method and pattern.fullmatch(path)
                        for allowed_method, pattern in HAI_ROUTE_RULES
                    )
                    if allowed and method != "GET":
                        skipped_mutations += 1
                        continue
                    response = client.open(path, method=method, headers=hai_headers)
                    checked += 1
                    if allowed:
                        self.assertNotEqual(response.status_code, 403, f"{method} {path}")
                    else:
                        self.assertEqual(response.status_code, 403, f"{method} {path}")

            self.assertGreater(checked, 100)
            self.assertGreater(skipped_mutations, 0)

    def test_hai_safe_cycle_preserves_its_trigger_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_api_token": "operator-token-for-tests",
                "fab_hai_api_token": "hai-token-for-tests",
                "fab_hai_connector_enabled": True,
                "fab_hai_allowed_commands": "run_safe_cycle",
            })
            client = app.test_client()
            with patch("src.operations.local_api._autonomy_service") as autonomy:
                autonomy.return_value.run_cycle.return_value = {
                    "success": True,
                    "status": "completed",
                }
                response = client.post(
                    "/api/hai/commands/execute",
                    headers={"Authorization": "Bearer hai-token-for-tests"},
                    json={
                        "requestId": "hai-safe-cycle-1",
                        "commandId": "run_safe_cycle",
                        "payload": {"limit": 1},
                    },
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                autonomy.return_value.run_cycle.call_args.kwargs["trigger_source"],
                "hai_connector",
            )

    def test_hai_reconciliation_actor_is_persisted_in_ledger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            app = create_app({
                "fab_local_ledger_path": ledger_path,
                "fab_local_api_token": "operator-token-for-tests",
                "fab_hai_api_token": "hai-token-for-tests",
                "fab_hai_connector_enabled": True,
                "fab_hai_allowed_commands": "run_reconciliation",
            })
            response = app.test_client().post(
                "/api/hai/commands/execute",
                headers={"Authorization": "Bearer hai-token-for-tests"},
                json={
                    "requestId": "hai-reconciliation-1",
                    "commandId": "run_reconciliation",
                    "payload": {},
                },
            )

            self.assertEqual(response.status_code, 200)
            event = next(
                item
                for item in LocalOperationsLedger(ledger_path).list_audit_events()
                if item["action"] == "local_reconciliation.run_completed"
            )
            self.assertEqual(event["details"]["actor"], "fab_hai_api:hai")

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
            manifest_body = manifest.get_json()
            self.assertEqual(manifest_body["haiAuthentication"], "dedicated_bearer_token")
            self.assertTrue(manifest_body["dedicatedHaiTokenConfigured"])
            self.assertTrue(manifest_body["remoteAuthenticationConfigured"])
            self.assertNotIn("hai-token-for-tests", str(manifest_body))
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
            self.assertEqual(manifest["haiAuthentication"], "loopback_origin_controls")
            self.assertFalse(manifest["dedicatedHaiTokenConfigured"])
            self.assertFalse(manifest["remoteAuthenticationConfigured"])
            self.assertIn("submit_to_wave", manifest["excludedCapabilities"])
            self.assertFalse(result["success"])
            self.assertEqual(result["status"], "connector_disabled")

    def test_blocked_plan_reports_command_disallowed_when_connector_cannot_execute(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            policies = (
                (
                    {"fab_hai_connector_enabled": False, "fab_hai_allowed_commands": "refresh_notifications"},
                    "connector_disabled",
                ),
                (
                    {
                        "fab_hai_connector_enabled": True,
                        "fab_hai_allowed_commands": "refresh_notifications",
                        "fab_maintenance_mode": True,
                    },
                    "maintenance_locked",
                ),
            )

            for config, expected_status in policies:
                with self.subTest(expected_status=expected_status):
                    connector = LocalHaiConnector(ledger, config)

                    result = connector.plan("refresh_notifications")

                    self.assertEqual(result["status"], expected_status)
                    self.assertFalse(result["command"]["allowed"])

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

    def test_hai_archive_external_status_is_audited_and_preserved_on_replay(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            app = create_app({
                "fab_local_ledger_path": ledger_path,
                "fab_hai_connector_enabled": True,
                "fab_hai_allowed_commands": "archive_verified_drive_sources",
            })
            client = app.test_client()
            with patch("src.operations.local_api.DriveWaveDeliveryService.archive_ready") as archive:
                archive.return_value = {
                    "success": True,
                    "status": "completed",
                    "archived": 1,
                    "externalSubmission": "executed",
                }
                first = client.post("/api/hai/commands/execute", json={
                    "requestId": "hai-drive-archive-result",
                    "commandId": "archive_verified_drive_sources",
                    "payload": {"dryRun": False},
                })
                replay = client.post("/api/hai/commands/execute", json={
                    "requestId": "hai-drive-archive-result",
                    "commandId": "archive_verified_drive_sources",
                    "payload": {"dryRun": False},
                })

            ledger = LocalOperationsLedger(ledger_path)
            event = ledger.find_audit_event(
                action="hai.command.completed",
                entity_type="hai_command_request",
                entity_id="hai-drive-archive-result",
            )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.get_json()["externalSubmission"], "executed")
            self.assertEqual(replay.get_json()["status"], "already_executed")
            self.assertEqual(replay.get_json()["externalSubmission"], "executed")
            self.assertEqual(event["details"]["externalSubmission"], "executed")
            archive.assert_called_once()

    def test_hai_archive_exception_reports_indeterminate_effect_on_replay(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            app = create_app({
                "fab_local_ledger_path": ledger_path,
                "fab_hai_connector_enabled": True,
                "fab_hai_allowed_commands": "archive_verified_drive_sources",
            })
            client = app.test_client()
            with patch(
                "src.operations.local_api.DriveWaveDeliveryService.archive_ready",
                side_effect=RuntimeError("post-move error"),
            ) as archive:
                first = client.post("/api/hai/commands/execute", json={
                    "requestId": "hai-drive-archive-error",
                    "commandId": "archive_verified_drive_sources",
                    "payload": {"dryRun": False},
                })
                replay = client.post("/api/hai/commands/execute", json={
                    "requestId": "hai-drive-archive-error",
                    "commandId": "archive_verified_drive_sources",
                    "payload": {"dryRun": False},
                })

            ledger = LocalOperationsLedger(ledger_path)
            event = ledger.find_audit_event(
                action="hai.command.failed",
                entity_type="hai_command_request",
                entity_id="hai-drive-archive-error",
            )
            self.assertEqual(first.get_json()["status"], "failed")
            self.assertEqual(first.get_json()["externalSubmission"], "unknown")
            self.assertEqual(replay.get_json()["status"], "previously_failed")
            self.assertEqual(replay.get_json()["externalSubmission"], "unknown")
            self.assertEqual(event["details"]["externalSubmission"], "unknown")
            archive.assert_called_once()

    def test_executor_failure_result_is_not_reported_as_completed_or_reexecuted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            executions = []

            def executor(payload, actor):
                executions.append(actor)
                return {
                    "success": False,
                    "status": "blocked",
                    "reason": "source_evidence_changed",
                    "externalSubmission": "not_executed",
                }

            connector = LocalHaiConnector(
                ledger,
                {
                    "fab_hai_connector_enabled": True,
                    "fab_hai_allowed_commands": "archive_verified_drive_sources",
                },
                executors={"archive_verified_drive_sources": executor},
            )

            first = connector.execute(
                "hai-blocked-executor-result",
                "archive_verified_drive_sources",
                {"dryRun": False},
            )
            replay = connector.execute(
                "hai-blocked-executor-result",
                "archive_verified_drive_sources",
                {"dryRun": False},
            )

            event = ledger.find_audit_event(
                "hai.command.failed",
                "hai_command_request",
                "hai-blocked-executor-result",
            )
            self.assertFalse(first["success"])
            self.assertEqual(first["status"], "blocked")
            self.assertEqual(first["result"]["status"], "blocked")
            self.assertEqual(first["externalSubmission"], "not_executed")
            self.assertEqual(replay["status"], "previously_failed")
            self.assertEqual(replay["result"]["status"], "blocked")
            self.assertEqual(replay["externalSubmission"], "not_executed")
            self.assertEqual(event["details"]["externalSubmission"], "not_executed")
            self.assertEqual(executions, ["hai"])

    def test_idempotent_result_replay_rechecks_current_connector_policy(self):
        revoked_policies = (
            ({"fab_hai_connector_enabled": False, "fab_hai_allowed_commands": "refresh_notifications"}, "connector_disabled"),
            ({"fab_hai_connector_enabled": True, "fab_hai_allowed_commands": "run_reconciliation"}, "not_allowed"),
            ({"fab_hai_connector_enabled": True, "fab_hai_allowed_commands": "refresh_notifications", "fab_maintenance_mode": True}, "maintenance_locked"),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            for index, (config, expected_status) in enumerate(revoked_policies):
                request_id = f"revoked-policy-{index}"
                enabled = LocalHaiConnector(
                    ledger,
                    {"fab_hai_connector_enabled": True, "fab_hai_allowed_commands": "refresh_notifications"},
                    executors={"refresh_notifications": lambda payload, actor: {"sensitiveResult": "private"}},
                )
                first = enabled.execute(request_id, "refresh_notifications")
                revoked = LocalHaiConnector(
                    ledger,
                    config,
                    executors={"refresh_notifications": lambda payload, actor: {"sensitiveResult": "private"}},
                )

                replay = revoked.execute(request_id, "refresh_notifications")

                self.assertEqual(first["status"], "completed")
                self.assertEqual(replay["status"], expected_status)
                self.assertNotIn("result", replay)

            allowed_without_executor = LocalHaiConnector(
                ledger,
                {"fab_hai_connector_enabled": True, "fab_hai_allowed_commands": "refresh_notifications"},
            )
            replay = allowed_without_executor.execute("revoked-policy-0", "refresh_notifications")
            self.assertEqual(replay["status"], "already_executed")

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
            self.assertEqual(manifest["haiAuthentication"], "operator_bearer_token")
            self.assertFalse(manifest["dedicatedHaiTokenConfigured"])
            self.assertFalse(manifest["remoteAuthenticationConfigured"])
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

    def test_compliance_dates_and_target_system_enforce_manifest_types(self):
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

            for value in ("20260615", "2026-W25-1", 20260615):
                with self.subTest(fromDate=value):
                    result = connector.plan("assess_compliance", {"fromDate": value})
                    self.assertEqual(result["status"], "invalid")

            for value in (123, ["wave"], {"name": "wave"}):
                with self.subTest(targetSystem=value):
                    result = connector.plan("assess_compliance", {"targetSystem": value})
                    self.assertEqual(result["status"], "invalid")

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
            self.assertEqual(len(resources), 7)
            self.assertIn("backup_recovery_status", resources)
            self.assertIn("google_drive_binary_relay", resources)
            self.assertIn("wave_attachment_work_orders", resources)
            self.assertNotIn("wave_attachment_binary_readback", resources)
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

    def test_hai_safe_cycle_never_executes_approved_wave_exports(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "hai-approved-export",
                "originalFilename": "approved-receipt.pdf",
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
            export_service.approve_attempt(
                prepared["exportAttemptId"],
                actor="operator-test",
                confirmation=EXPORT_APPROVAL_PHRASE,
            )
            app = create_app({
                "fab_local_ledger_path": ledger_path,
                "fab_hai_connector_enabled": True,
                "fab_hai_allowed_commands": "run_safe_cycle",
                "fab_autonomy_execute_approved_exports": True,
            })
            client = app.test_client()

            with patch.object(LocalExportAttemptService, "process_approved_attempts") as execute_exports:
                execute_exports.return_value = {
                    "success": True,
                    "status": "completed",
                    "eligibleCount": 0,
                    "processed": [],
                    "deferredNotDue": 0,
                    "preExecutionBackup": None,
                }
                response = client.post("/api/hai/commands/execute", json={
                    "requestId": "hai-safe-cycle-approved-export",
                    "commandId": "run_safe_cycle",
                    "payload": {"limit": 10},
                })

            self.assertEqual(response.status_code, 200, response.get_json())
            result = response.get_json()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["externalSubmission"], "not_executed")
            self.assertNotIn(
                "execute_approved_exports",
                result["result"]["plan"]["runnableActionIds"],
            )
            execute_exports.assert_not_called()
            self.assertEqual(
                LocalOperationsLedger(ledger_path).get_export_attempt(
                    prepared["exportAttemptId"]
                )["status"],
                "approved",
            )

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
                patch("src.operations.local_api.DriveRelayIntakeService") as drive_relay,
                patch("src.operations.local_api.LocalHaiConnector") as hai_connector,
            ):
                executor.return_value.register.return_value = {"success": True}
                executor.return_value.claim_next.return_value = {"success": True}
                executor.return_value.release.return_value = {"success": True}
                delivery.return_value.record_attachment_readback.return_value = {"success": True}
                drive_relay.return_value.status.return_value = {"maxBytes": 2048}
                drive_relay.return_value.ingest.return_value = {"success": True}
                hai_connector.return_value.execute.return_value = {"success": True}

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
                    client.post(
                        "/api/connectors/google-drive/relay",
                        headers=headers,
                        data={
                            "metadata": '{"actor":"operator_admin"}',
                            "file": (io.BytesIO(b"receipt"), "receipt.pdf"),
                        },
                        content_type="multipart/form-data",
                    ),
                ]
                operator_response = client.post(
                    "/api/wave/receipt-executor/session",
                    headers=operator_headers,
                    json={"actor": "fab_hai_api:hai\r\nforged"},
                )
                hai_execute_response = client.post(
                    "/api/hai/commands/execute",
                    headers=headers,
                    json={
                        "requestId": "hai-request-actor-test",
                        "commandId": "refresh_notifications",
                        "actor": "operator_admin",
                    },
                )
                operator_execute_response = client.post(
                    "/api/hai/commands/execute",
                    headers=operator_headers,
                    json={
                        "requestId": "operator-request-actor-test",
                        "commandId": "refresh_notifications",
                        "actor": "hai-controller",
                    },
                )

            self.assertEqual(
                [response.status_code for response in responses],
                [200, 200, 200, 403, 201],
            )
            self.assertEqual(operator_response.status_code, 200)
            self.assertEqual(hai_execute_response.status_code, 200)
            self.assertEqual(operator_execute_response.status_code, 200)
            self.assertEqual(
                [call.kwargs["actor"] for call in hai_connector.return_value.execute.call_args_list],
                ["fab_hai_api:hai", "fab_hai_api:operator"],
            )
            self.assertEqual(
                executor.return_value.register.call_args_list[0].kwargs["actor"],
                "fab_hai_api:hai",
            )
            self.assertEqual(
                executor.return_value.register.call_args_list[1].kwargs["actor"],
                "fab_local_api:operator_claim:fab_hai_api:hai forged",
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
                ["fab_hai_api:hai"],
            )
            delivery.return_value.record_attachment_readback.assert_not_called()
            self.assertEqual(
                drive_relay.return_value.ingest.call_args.kwargs["actor"],
                "fab_hai_api:hai",
            )

    def test_operator_actor_claim_is_bound_for_direct_json_audit_routes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_api_token": "operator-token-for-tests",
            })
            client = app.test_client()
            with patch("src.operations.local_api.LocalNotificationService") as notifications:
                notifications.return_value.refresh.return_value = {"success": True}
                response = client.post(
                    "/api/notifications/refresh",
                    headers={"Authorization": "Bearer operator-token-for-tests"},
                    json={"actor": "fab_hai_api:hai\r\nforged"},
                )

            self.assertEqual(response.status_code, 200)
            notifications.return_value.refresh.assert_called_once_with(
                actor="fab_local_api:operator_claim:fab_hai_api:hai forged",
            )

if __name__ == "__main__":
    unittest.main()
