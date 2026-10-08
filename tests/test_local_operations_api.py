import base64
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.operations.local_backup import (
    FULL_RESTORE_CONFIRMATION_PHRASE,
    LocalBackupService,
    RESTORE_CONFIRMATION_PHRASE,
    RESTORE_MODE_FULL,
)
from src.operations.local_api import create_app
from src.operations.local_autonomy import LocalAutonomousService
from src.operations.local_bookkeeping_records import LocalBookkeepingRecordService
from src.operations.local_exports import EXPORT_APPROVAL_PHRASE, EXPORT_REJECTION_PHRASE, EXPORT_RESULT_CONFIRMATION_PHRASE
from src.operations.local_gmail_auth import LocalGmailAuthorizationCoordinator
from src.operations.local_google_drive_auth import LocalGoogleDriveAuthorizationCoordinator
from src.operations.local_health import LocalOperationsHealth
from src.operations.local_intake import LocalFolderIntake
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_master_ledger import LocalMasterLedgerService
from src.operations.local_wave_setup import LocalWaveSetupService
from src.utils.rate_limiter import RateLimiter, reset_all_limiters, set_rate_limiter
from src.utils.runtime_identity import local_instance_id


class TestLocalOperationsApi(unittest.TestCase):
    def test_api_exposes_failed_source_block_without_provider_dispatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            reset_all_limiters()
            self.addCleanup(reset_all_limiters)
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner", "sourceDocumentId": "failed-export-api",
                "originalFilename": "receipt.txt", "documentType": "receipt",
                "processingStatus": "reviewed", "vendorName": "Office Shop",
                "category": "Office Supplies", "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
            })
            client = create_app({"fab_local_ledger_path": ledger_path}).test_client()
            route = client.post(f"/api/documents/{document_id}/route", json={}).get_json()
            prepared = client.post(f"/api/routing/{route['routingAttemptId']}/export-attempt", json={})
            self.assertEqual(prepared.status_code, 200)
            attempt_id = prepared.get_json()["exportAttemptId"]
            approved = client.post(f"/api/export-attempts/{attempt_id}/approve", json={
                "confirmation": EXPORT_APPROVAL_PHRASE,
            })
            self.assertEqual(approved.status_code, 200)
            ledger.update_document(document_id, {"processingStatus": "failed"})

            with patch("src.data_entry.waveapps_api_executor.WaveappsApiExecutor.execute") as dispatch:
                response = client.post(f"/api/export-attempts/{attempt_id}/execute", json={})
                retry_approval = client.post(f"/api/export-attempts/{attempt_id}/approve", json={
                    "confirmation": EXPORT_APPROVAL_PHRASE,
                })

            dispatch.assert_not_called()
            for result in (response, retry_approval):
                self.assertEqual(result.status_code, 400)
                self.assertEqual(result.get_json()["status"], "blocked_processing")
                self.assertEqual(result.get_json()["externalSubmission"], "not_executed")
            attempt = client.get(f"/api/export-attempts/{attempt_id}").get_json()
            self.assertEqual(attempt["status"], "attention_required")
            records = client.get("/api/bookkeeping-records").get_json()["bookkeepingRecords"]
            self.assertEqual(records[0]["export_status"], "blocked_processing")
            self.assertFalse(records[0]["metadata"]["exportReadiness"]["readyForWaveDraft"])

    def test_control_center_batch_reads_fixed_resources_in_one_snapshot(self):
        expected_resources = {
            "autonomy",
            "reviewQueue",
            "exceptions",
            "health",
            "closeReadiness",
            "driveWaveStatus",
            "driveWaveWorkOrders",
            "reportRuns",
            "compliance",
            "recovery",
            "liveness",
            "waveSetup",
            "workflows",
            "masterLedger",
            "metrics",
            "notifications",
            "settings",
            "bankTransactions",
            "reconciliation",
            "activity",
            "sources",
            "waveReceiptExecutor",
            "sourceReadiness",
            "driveAuthorization",
            "haiStatus",
            "haiManifest",
            "gmailAuthorization",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
            })
            client = app.test_client()
            original_connect = LocalOperationsLedger._connect
            original_project = LocalMasterLedgerService.project
            original_health = LocalOperationsHealth.summarize
            connection_count = 0
            projection_count = 0
            health_count = 0
            wave_setup_count = 0
            metrics_count = 0

            def counted_connect(ledger):
                nonlocal connection_count
                connection_count += 1
                return original_connect(ledger)

            def counted_project(service, *args, **kwargs):
                nonlocal projection_count
                projection_count += 1
                return original_project(service, *args, **kwargs)

            def counted_health(service, *args, **kwargs):
                nonlocal health_count
                health_count += 1
                return original_health(service, *args, **kwargs)

            original_wave_setup = LocalWaveSetupService.status

            def counted_wave_setup(service, *args, **kwargs):
                nonlocal wave_setup_count
                wave_setup_count += 1
                return original_wave_setup(service, *args, **kwargs)

            original_metrics = LocalOperationsLedger.dashboard_metrics

            def counted_metrics(ledger):
                nonlocal metrics_count
                metrics_count += 1
                return original_metrics(ledger)

            with patch.object(LocalOperationsLedger, "_connect", counted_connect), patch.object(
                LocalMasterLedgerService,
                "project",
                counted_project,
            ), patch.object(LocalOperationsHealth, "summarize", counted_health), patch.object(
                LocalWaveSetupService,
                "status",
                counted_wave_setup,
            ), patch.object(LocalOperationsLedger, "dashboard_metrics", counted_metrics):
                response = client.get("/api/control-center/resources")

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["version"], 1)
            self.assertEqual(set(payload["resources"]), expected_resources)
            self.assertTrue(all(
                envelope["ok"] and isinstance(envelope["value"], dict)
                for envelope in payload["resources"].values()
            ))
            self.assertLessEqual(connection_count, 1)
            self.assertLessEqual(projection_count, 1)
            self.assertLessEqual(health_count, 1)
            self.assertLessEqual(wave_setup_count, 1)
            self.assertLessEqual(metrics_count, 1)
            review_categories = payload["resources"]["reviewQueue"]["value"]["categoryOptions"]
            wave_categories = [
                item["category"]
                for item in payload["resources"]["waveSetup"]["value"]["categoryIntents"]
            ]
            self.assertEqual(review_categories, wave_categories)

    @staticmethod
    def _operator_session_ticket(token, payload):
        encoded_payload = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ).decode("ascii").rstrip("=")
        signature = base64.urlsafe_b64encode(
            hmac.new(token.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256).digest()
        ).decode("ascii").rstrip("=")
        return f"{encoded_payload}.{signature}"

    def test_operator_session_handoff_authenticates_once_and_records_bounded_audit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            token = "test-local-api-token"
            now = int(time.time())
            app = create_app({
                "fab_local_ledger_path": ledger_path,
                "fab_local_api_token": token,
                "fab_local_api_base_url": "https://fab-api.example",
            })
            client = app.test_client()
            ticket = self._operator_session_ticket(token, {
                "actor": "fab_dashboard:local_operator",
                "aud": "fab-local-operator-session",
                "exp": now + 45,
                "iat": now,
                "nonce": "fixed_operator_nonce_1234",
                "next": "/#reports",
                "v": 1,
            })

            response = client.get(
                f"/operator/session/bootstrap?ticket={ticket}",
                base_url="https://fab-api.example",
            )
            authenticated = client.get("/api/live", base_url="https://fab-api.example")
            replay = client.get(
                f"/operator/session/bootstrap?ticket={ticket}",
                base_url="https://fab-api.example",
            )

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["Location"], "/#reports")
            self.assertIn("Secure", response.headers["Set-Cookie"])
            self.assertIn("HttpOnly", response.headers["Set-Cookie"])
            self.assertIn("SameSite=Lax", response.headers["Set-Cookie"])
            self.assertNotIn(token, response.headers["Set-Cookie"])
            self.assertEqual(authenticated.status_code, 200)
            self.assertEqual(replay.status_code, 409)

            audit = LocalOperationsLedger(ledger_path).list_audit_events(limit=1)[0]
            self.assertEqual(audit["action"], "local_api.operator_session_bootstrapped")
            self.assertEqual(audit["details"]["actor"], "fab_dashboard:local_operator")
            self.assertEqual(audit["details"]["targetPath"], "/")
            self.assertEqual(audit["details"]["externalSubmission"], "not_executed")
            self.assertNotIn("nonce", str(audit).lower())
            self.assertNotIn("ticket", str(audit).lower())
            self.assertNotIn(token, str(audit))

    def test_managed_handoff_stops_working_when_parent_is_revoked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            token = "D7pH8sAk3C5nZ9wL2tB6eQ0rV1mF4yUj"
            now = int(time.time())
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_api_token": token,
                "fab_operator_session_validation_url": "http://127.0.0.1:3000/api/fab/operator-session/status",
            })
            client = app.test_client()
            ticket = self._operator_session_ticket(token, {
                "actor": "fab_dashboard:admin:test", "aud": "fab-local-operator-session",
                "exp": now + 45, "iat": now, "nonce": "parent_session_nonce_1234",
                "next": "/api/live", "v": 2,
                "parent": {"id": "a" * 32, "exp": now + 600},
            })
            with patch("src.operations.local_api.parent_session_active", return_value=True) as validation:
                response = client.get(f"/operator/session/bootstrap?ticket={ticket}")
                self.assertEqual(response.status_code, 302)
                self.assertEqual(client.get("/api/live").status_code, 200)
                self.assertEqual(validation.call_count, 2)
            with patch("src.operations.local_api.parent_session_active", return_value=False):
                self.assertEqual(client.get("/api/live").status_code, 401)
                self.assertEqual(client.get("/api/live", headers={"Authorization": f"Bearer {token}"}).status_code, 200)

    def test_managed_validation_rejects_legacy_cookie_and_unbound_handoff(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            token = "D7pH8sAk3C5nZ9wL2tB6eQ0rV1mF4yUj"
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_api_token": token,
                "fab_operator_session_validation_url": "http://127.0.0.1:3000/api/fab/operator-session/status",
            })
            client = app.test_client()
            with client.session_transaction() as browser_session:
                browser_session["fab_local_api_authenticated"] = True
            self.assertEqual(client.get("/api/live").status_code, 401)
            now = int(time.time())
            ticket = self._operator_session_ticket(token, {
                "actor": "fab_dashboard:admin:test", "aud": "fab-local-operator-session",
                "exp": now + 45, "iat": now, "nonce": "unbound_parent_nonce_1234",
                "next": "/api/live", "v": 1,
            })
            self.assertEqual(client.get(f"/operator/session/bootstrap?ticket={ticket}").status_code, 401)
            self.assertEqual(client.post("/login", data={"token": token}).status_code, 302)
            self.assertEqual(client.get("/api/live").status_code, 200)

    def test_operator_session_handoff_rejects_expired_tampered_and_unsafe_tickets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            token = "test-local-api-token"
            now = int(time.time())
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_api_token": token,
            })
            client = app.test_client()
            baseline = {
                "actor": "fab_dashboard:local_operator",
                "aud": "fab-local-operator-session",
                "exp": now + 45,
                "iat": now,
                "nonce": "fixed_operator_nonce_9876",
                "next": "/api/audit?limit=10",
                "v": 1,
            }
            expired = self._operator_session_ticket(token, {
                **baseline,
                "exp": now - 1,
                "iat": now - 45,
            })
            unsafe = self._operator_session_ticket(token, {
                **baseline,
                "nonce": "different_operator_nonce_1234",
                "next": "//evil.example",
            })
            valid = self._operator_session_ticket(token, baseline)
            encoded_payload, encoded_signature = valid.split(".")
            base64url_alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            canonical_tail = base64url_alphabet.index(encoded_signature[-1])
            equivalent_noncanonical_tail = base64url_alphabet[(canonical_tail & ~3) | 1]
            tampered = (
                f"{encoded_payload}.{encoded_signature[:-1]}{equivalent_noncanonical_tail}"
            )

            self.assertEqual(client.get(f"/operator/session/bootstrap?ticket={expired}").status_code, 401)
            self.assertEqual(client.get(f"/operator/session/bootstrap?ticket={unsafe}").status_code, 401)
            self.assertEqual(client.get(f"/operator/session/bootstrap?ticket={tampered}").status_code, 401)
            self.assertEqual(client.get("/api/live").status_code, 401)

    def test_health_api_bounds_issue_details_without_hiding_totals(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            for index in range(3):
                ledger.register_document({
                    "source": "scanner",
                    "sourceDocumentId": f"failed-api-health-{index}",
                    "originalFilename": f"failed-api-health-{index}.pdf",
                    "processingStatus": "failed",
                })
            client = create_app({
                "fab_local_ledger_path": ledger_path,
                "fab_health_api_issue_limit": 2,
            }).test_client()

            default_health = client.get("/api/health").get_json()["operations"]
            smaller_health = client.get("/api/health?issueLimit=1").get_json()["operations"]
            complete_health = client.get("/api/health?issueLimit=500").get_json()["operations"]

            self.assertEqual(default_health["issueCount"], 3)
            self.assertEqual(default_health["issueTypeCounts"]["failed_document"], 3)
            self.assertEqual(default_health["issuesReturned"], 2)
            self.assertTrue(default_health["issuesTruncated"])
            self.assertEqual(smaller_health["issuesReturned"], 1)
            self.assertEqual(smaller_health["severityCounts"]["high"], 3)
            self.assertEqual(complete_health["issuesReturned"], 3)
            self.assertFalse(complete_health["issuesTruncated"])

    def test_health_api_coalesces_identical_reads_without_caching_internal_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "health-cache-first",
                "originalFilename": "first.pdf",
                "processingStatus": "failed",
            })
            client = create_app({
                "fab_local_ledger_path": ledger_path,
                "fab_health_api_issue_limit": 1,
                "fab_health_cache_ttl_seconds": 30,
            }).test_client()

            first = client.get("/api/health")
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "health-cache-second",
                "originalFilename": "second.pdf",
                "processingStatus": "failed",
            })
            cached = client.get("/api/health")
            different_projection = client.get("/api/health?issueLimit=2")

            self.assertEqual(first.headers["X-FAB-Health-Cache"], "miss")
            self.assertEqual(cached.headers["X-FAB-Health-Cache"], "hit")
            self.assertEqual(cached.headers["Cache-Control"], "no-store, max-age=0")
            self.assertEqual(first.get_json()["operations"]["issueCount"], 1)
            self.assertEqual(cached.get_json()["operations"]["issueCount"], 1)
            self.assertEqual(different_projection.headers["X-FAB-Health-Cache"], "miss")
            self.assertEqual(different_projection.get_json()["operations"]["issueCount"], 2)
            self.assertEqual(different_projection.get_json()["operations"]["issuesReturned"], 2)

            uncached_client = create_app({
                "fab_local_ledger_path": ledger_path,
                "fab_health_cache_ttl_seconds": 0,
            }).test_client()
            uncached_first = uncached_client.get("/api/health")
            uncached_second = uncached_client.get("/api/health")

            self.assertEqual(uncached_first.headers["X-FAB-Health-Cache"], "disabled")
            self.assertEqual(uncached_second.headers["X-FAB-Health-Cache"], "disabled")
            self.assertEqual(uncached_first.headers["X-FAB-Health-Cache-TTL"], "0")

    def test_api_errors_use_stable_envelope_and_request_ids(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_api_token": "test-api-token",
            })
            client = app.test_client()

            unauthorized = client.get(
                "/api/live",
                headers={"X-Request-ID": "fab-test-request-001"},
            )
            missing = client.get(
                "/api/not-a-route",
                headers={
                    "Authorization": "Bearer test-api-token",
                    "X-Request-ID": "fab-test-request-002",
                },
            )
            unsafe_id = client.get(
                "/api/live",
                headers={"X-Request-ID": "invalid request id value"},
            )

            self.assertEqual(unauthorized.status_code, 401)
            self.assertEqual(unauthorized.headers["X-Request-ID"], "fab-test-request-001")
            self.assertEqual(unauthorized.get_json(), {
                "error": "Unauthorized",
                "errorCode": "unauthorized",
                "message": "Unauthorized",
                "requestId": "fab-test-request-001",
                "status": "error",
                "success": False,
            })
            self.assertEqual(missing.status_code, 404)
            self.assertEqual(missing.get_json()["errorCode"], "not_found")
            self.assertEqual(missing.get_json()["requestId"], "fab-test-request-002")
            self.assertRegex(unsafe_id.headers["X-Request-ID"], r"^[0-9a-f]{24}$")

    def test_api_rejects_malformed_and_non_object_json_mutations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
            }).test_client()

            malformed = client.post(
                "/api/autonomy/run",
                data=b'{"limit":',
                content_type="application/json",
            )
            non_object = client.post("/api/autonomy/run", json=[{"limit": 1}])
            null_body = client.post("/api/autonomy/run", data="null", content_type="application/json")

            self.assertEqual(malformed.status_code, 400)
            self.assertEqual(malformed.get_json()["errorCode"], "invalid_request")
            self.assertEqual(malformed.get_json()["message"], "Request body must contain valid JSON")
            self.assertEqual(non_object.status_code, 400)
            self.assertEqual(non_object.get_json()["message"], "Request JSON body must be an object")
            self.assertEqual(null_body.status_code, 400)
            self.assertEqual(null_body.get_json()["message"], "Request JSON body must be an object")

    def test_api_rejects_oversized_request_before_route_execution(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_api_max_request_bytes": 64,
            }).test_client()

            response = client.post(
                "/api/autonomy/run",
                json={"reason": "x" * 128},
            )

            self.assertEqual(response.status_code, 413)
            self.assertEqual(response.get_json()["errorCode"], "payload_too_large")

    def test_api_list_limits_are_positive_and_bounded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
            })
            with patch.object(
                LocalOperationsLedger,
                "list_audit_events",
                return_value=[],
            ) as list_events:
                client = app.test_client()
                self.assertEqual(client.get("/api/audit?limit=-1").status_code, 200)
                self.assertEqual(list_events.call_args.kwargs["limit"], 1)
                self.assertEqual(client.get("/api/audit?limit=999999").status_code, 200)
                self.assertEqual(list_events.call_args.kwargs["limit"], 500)

    def test_unhandled_api_error_is_generic_and_correlated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            app = create_app({"fab_local_ledger_path": ledger_path})

            @app.get("/api/test-unhandled-error")
            def test_unhandled_error():
                raise RuntimeError("financial implementation detail")

            response = app.test_client().get(
                "/api/test-unhandled-error",
                headers={"X-Request-ID": "fab-test-error-001"},
            )

            self.assertEqual(response.status_code, 500)
            self.assertEqual(response.headers["X-Request-ID"], "fab-test-error-001")
            self.assertEqual(response.get_json()["errorCode"], "internal_error")
            self.assertEqual(response.get_json()["message"], "Internal server error")
            self.assertNotIn("financial implementation detail", response.get_data(as_text=True))
            audit = LocalOperationsLedger(ledger_path).find_audit_event(
                "local_api.unhandled_exception",
                "api_request",
                "fab-test-error-001",
            )
            self.assertEqual(audit["details"]["errorType"], "RuntimeError")
            self.assertEqual(audit["details"]["endpoint"], "test_unhandled_error")
            self.assertNotIn("financial implementation detail", str(audit))

    def test_local_ledger_uses_wal_and_allows_reads_during_worker_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            writer = sqlite3.connect(ledger_path, timeout=1.0)
            try:
                self.assertEqual(
                    writer.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                    "wal",
                )
                writer.execute("BEGIN IMMEDIATE")
                writer.execute(
                    """
                    INSERT INTO runtime_controls (
                        control_name, active, reason, updated_by, metadata_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("concurrency-test", 1, "test", "test", "{}", "2026-08-09", "2026-08-09"),
                )

                metrics = ledger.dashboard_metrics()

                self.assertEqual(metrics["documents"], 0)
            finally:
                writer.rollback()
                writer.close()

    def test_review_api_rejects_invalid_financial_correction(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "invalid-api-amount",
                "originalFilename": "invoice.pdf",
                "documentType": "vendor_invoice",
                "processingStatus": "needs_review",
                "vendorName": "Vendor",
                "category": "Manual Review",
                "transactionDate": "2023-06-18",
                "totalAmount": 12.0,
            })
            review_id = ledger.create_review_item({
                "documentId": document_id,
                "reason": "manual_review_category",
                "details": "Confirm values.",
            })
            app = create_app({"fab_local_ledger_path": ledger_path})

            response = app.test_client().post(
                f"/api/review/{review_id}/resolve",
                json={
                    "status": "approved",
                    "resolution": "Invalid signed correction.",
                    "corrections": {"totalAmount": -12.0},
                },
            )

            self.assertEqual(response.status_code, 400)
            self.assertEqual(
                response.get_json()["status"],
                "invalid_financial_correction",
            )
            self.assertEqual(ledger.get_review_item(review_id)["status"], "pending")

    def test_workflow_run_api_and_dashboard_expose_step_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            workflow_run_id = ledger.create_workflow_run({
                "status": "completed_with_errors",
                "triggerSource": "connector_intake",
                "startedAt": "2026-07-13T08:00:00Z",
                "finishedAt": "2026-07-13T08:00:02Z",
            })
            ledger.create_workflow_step({
                "workflowRunId": workflow_run_id,
                "stepKey": "source:gmail",
                "stage": "collect",
                "status": "failed",
                "stepOrder": 1,
                "durationMs": 2000,
                "errorMessage": "Provider timeout",
            })
            ledger.create_workflow_step({
                "workflowRunId": workflow_run_id,
                "stepKey": "source:google_drive",
                "stage": "collect",
                "status": "not_run",
                "stepOrder": 2,
                "durationMs": 0,
            })
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()

            listing = client.get("/api/workflows?triggerSource=connector_intake").get_json()
            detail = client.get(f"/api/workflows/{workflow_run_id}").get_json()
            missing = client.get("/api/workflows/999999")
            html = client.get("/").data.decode("utf-8")

            self.assertEqual(listing["workflowRuns"][0]["id"], workflow_run_id)
            self.assertEqual(detail["steps"][0]["step_key"], "source:gmail")
            self.assertEqual(detail["stepSummary"]["failed"], 1)
            self.assertEqual(detail["stepSummary"]["not_run"], 1)
            self.assertEqual(missing.status_code, 404)
            self.assertIn("Workflow Runs", html)
            self.assertIn("source:gmail", html)
            self.assertIn("Not run", html)
            self.assertIn(f"/api/workflows/{workflow_run_id}", html)

    def test_workflow_recovery_api_and_dashboard_resume_only_safe_steps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            config = {
                "fab_local_ledger_path": ledger_path,
                "fab_local_intake_paths": intake_dir,
                "fab_autonomy_ignore_health_blocks": True,
            }
            autonomy = LocalAutonomousService(
                ledger,
                config,
                intake_paths=[intake_dir],
            )
            with patch.object(autonomy, "_run_rescan", side_effect=RuntimeError("scan failed")):
                failed = autonomy.run_cycle(
                    include_wave_plan=False,
                    include_wave_sync=False,
                )
            app = create_app(config)
            client = app.test_client()

            plan_response = client.get(
                f"/api/workflows/{failed['workflowRunId']}/recovery-plan"
            )
            before_html = client.get("/").data.decode("utf-8")
            retry_response = client.post(
                f"/api/workflows/{failed['workflowRunId']}/retry",
                json={"actor": "tester"},
            )
            retry_payload = retry_response.get_json()
            recovered = client.get(
                f"/api/workflows/{retry_payload['workflowRunId']}"
            ).get_json()
            repeated = client.post(
                f"/api/workflows/{failed['workflowRunId']}/retry",
                json={"actor": "tester"},
            )
            form_result = client.post(
                f"/workflows/{failed['workflowRunId']}/retry",
                follow_redirects=True,
            )
            missing = client.get("/api/workflows/999999/recovery-plan")

            self.assertEqual(plan_response.status_code, 200)
            self.assertTrue(plan_response.get_json()["canRetry"])
            self.assertEqual(plan_response.get_json()["retryActionId"], "rescan_intake")
            self.assertEqual(
                plan_response.get_json()["continuationStepKeys"],
                [
                    "process_imported",
                    "prepare_wave_drafts",
                    "prepare_export_attempts",
                    "prepare_master_ledger_projection",
                    "prepare_period_close_pack",
                ],
            )
            self.assertIn(
                f"/workflows/{failed['workflowRunId']}/retry",
                before_html,
            )
            self.assertIn("Resume safe workflow", before_html)
            self.assertEqual(retry_response.status_code, 200)
            self.assertTrue(retry_payload["success"])
            self.assertTrue(retry_payload["runtimeLease"]["released"])
            self.assertEqual(recovered["trigger_source"], "local_autonomous_recovery")
            self.assertEqual(len(recovered["steps"]), 6)
            self.assertEqual(recovered["steps"][0]["attempt"], 2)
            self.assertEqual(recovered["steps"][0]["step_key"], "rescan_intake")
            self.assertNotIn(
                "execute_approved_exports",
                {step["step_key"] for step in recovered["steps"]},
            )
            self.assertEqual(repeated.status_code, 409)
            self.assertEqual(repeated.get_json()["status"], "superseded")
            self.assertEqual(form_result.status_code, 200)
            self.assertIn("Last workflow recovery", form_result.data.decode("utf-8"))
            self.assertIn("superseded", form_result.data.decode("utf-8"))
            self.assertEqual(missing.status_code, 404)
            started = next(
                event
                for event in ledger.list_audit_events(limit=100)
                if event["action"] == "local_workflow_recovery.started"
            )
            self.assertEqual(started["details"]["actor"], "tester")
            self.assertEqual(started["details"]["externalSubmission"], "not_executed")

    def test_compliance_assessment_findings_retention_and_dashboard(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "compliance-api",
                "originalFilename": "vat-error.pdf",
                "processingStatus": "reviewed",
            })
            ledger.upsert_bookkeeping_record({
                "documentId": document_id,
                "recordType": "expense",
                "status": "ready_to_route",
                "targetSystem": "waveapps_business",
                "targetAccount": "Office",
                "category": "Office",
                "recordDate": "2026-07-05",
                "amount": 10,
                "vatAmount": 12,
                "currency": "EUR",
                "reconciliationStatus": "reconciled",
            })
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()

            first = client.post("/api/compliance/assessments", json={
                "fromDate": "2026-07-01",
                "toDate": "2026-07-31",
                "actor": "tester",
            })
            second = client.post("/api/compliance/assessments", json={
                "fromDate": "2026-07-01",
                "toDate": "2026-07-31",
                "actor": "tester",
            })

            self.assertEqual(first.status_code, 200)
            self.assertTrue(first.get_json()["created"])
            self.assertEqual(first.get_json()["assessment"]["status"], "blocked")
            self.assertEqual(first.get_json()["filingStatus"], "not_filed")
            self.assertEqual(first.get_json()["externalFiling"], "not_executed")
            self.assertEqual(second.get_json()["status"], "already_current")
            assessment_id = first.get_json()["assessment"]["id"]
            detail = client.get(f"/api/compliance/assessments/{assessment_id}").get_json()
            self.assertEqual(detail["assessment"]["statutory_status"], "provisional")
            finding_id = next(
                finding["id"] for finding in detail["findings"]
                if finding["code"] == "vat_exceeds_gross"
            )
            invalid = client.patch(
                f"/api/compliance/findings/{finding_id}/status",
                json={"status": "resolved"},
            )
            self.assertEqual(invalid.status_code, 400)
            acknowledged = client.patch(
                f"/api/compliance/findings/{finding_id}/status",
                json={"status": "acknowledged", "actor": "tester"},
            )
            self.assertEqual(acknowledged.status_code, 200)
            self.assertEqual(acknowledged.get_json()["finding"]["status"], "acknowledged")
            retention = client.get("/api/compliance/retention").get_json()
            self.assertFalse(retention["deletionAuthorized"])
            self.assertEqual(len(retention["retentionRecords"]), 1)
            html = client.get("/").data.decode("utf-8")
            self.assertIn("VAT & Compliance", html)
            self.assertIn("No tax filing is performed", html)
            self.assertIn("vat_exceeds_gross", html)

    def test_notification_center_refresh_preferences_and_status_actions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "notification-api",
                "originalFilename": "failed.pdf",
                "processingStatus": "failed",
            })
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()

            refreshed = client.post("/api/notifications/refresh", json={"actor": "tester"})
            self.assertEqual(refreshed.status_code, 200)
            self.assertEqual(refreshed.get_json()["created"], 1)
            self.assertEqual(refreshed.get_json()["externalDelivery"], "not_executed")
            inbox = client.get("/api/notifications?status=unread").get_json()
            self.assertEqual(inbox["summary"]["unread"], 1)
            notification_id = inbox["notifications"][0]["id"]
            self.assertEqual(inbox["notifications"][0]["event_type"], "failed_document")

            acknowledged = client.patch(
                f"/api/notifications/{notification_id}/status",
                json={"status": "acknowledged", "actor": "tester"},
            )
            self.assertEqual(acknowledged.status_code, 200)
            self.assertEqual(acknowledged.get_json()["notification"]["status"], "acknowledged")

            preference = client.post("/api/notification-preferences", json={
                "eventType": "failed_document",
                "enabled": False,
                "inAppEnabled": False,
                "minimumSeverity": "high",
            })
            self.assertEqual(preference.status_code, 200)
            self.assertEqual(preference.get_json()["preference"]["external_delivery"], "disabled")
            self.assertFalse(preference.get_json()["preference"]["enabled"])

            page = client.get("/")
            html = page.data.decode("utf-8")
            self.assertIn("Notification Center", html)
            self.assertIn("Document processing failed", html)
            self.assertIn("External delivery", html)
            self.assertIn("acknowledged", html)

    def test_local_api_exposes_dashboard_documents_review_and_audit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-1",
                "originalFilename": "receipt.pdf",
                "processingStatus": "needs_review",
                "vendorName": "Vendor",
            })
            review_id = ledger.create_review_item({
                "documentId": document_id,
                "reason": "low_confidence",
                "details": "Confirm category.",
            })
            ledger.record_audit_event({
                "action": "workflow.document.imported",
                "entityType": "bookkeeping_document",
                "entityId": str(document_id),
            })

            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()

            health = client.get("/api/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.get_json()["service"], "fab-ledger-api")
            live = client.get("/api/live")
            self.assertEqual(live.status_code, 200)
            self.assertEqual(live.get_json()["status"], "ok")
            self.assertNotIn("operations", live.get_json())
            self.assertEqual(
                health.get_json()["instanceId"],
                local_instance_id(Path(__file__).resolve().parents[1]),
            )
            self.assertNotIn("instanceRoot", health.get_json())
            self.assertFalse(health.get_json()["authRequired"])

            dashboard = client.get("/api/dashboard")
            self.assertEqual(dashboard.get_json()["documents"], 1)
            self.assertEqual(dashboard.get_json()["pending_review"], 1)

            close_readiness = client.get("/api/close-readiness?workflowId=daily_reconciliation_run&fromDate=2026-06-28&toDate=2026-06-28")
            self.assertEqual(close_readiness.status_code, 200)
            self.assertEqual(close_readiness.get_json()["status"], "blocked")
            self.assertFalse(close_readiness.get_json()["canClose"])

            documents = client.get("/api/documents?status=needs_review")
            self.assertEqual(documents.status_code, 200)
            self.assertEqual(documents.get_json()["documents"][0]["vendor_name"], "Vendor")

            detail = client.get(f"/api/documents/{document_id}")
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.get_json()["review_items"][0]["id"], review_id)
            self.assertEqual(detail.get_json()["audit_events"][0]["action"], "workflow.document.imported")

            review = client.get("/api/review?status=pending")
            self.assertEqual(review.status_code, 200)
            review_payload = review.get_json()
            self.assertEqual(review_payload["reviewItems"][0]["reason"], "low_confidence")
            self.assertEqual(review_payload["summary"]["documents"], 1)
            self.assertEqual(review_payload["summary"]["postingBlockedDocuments"], 1)
            self.assertEqual(review_payload["summary"]["postingBlockedReviewItems"], 1)
            self.assertEqual(review_payload["summary"]["evidenceOnlyDocuments"], 0)
            self.assertEqual(review_payload["summary"]["evidenceOnlyReviewItems"], 0)
            self.assertEqual(review_payload["summary"]["duplicateCandidates"], 0)
            batch_capability = review_payload["capabilities"]["exactVendorCategoryBatch"]
            self.assertTrue(batch_capability["enabled"])
            self.assertTrue(batch_capability["requiresExplicitApproval"])
            self.assertEqual(batch_capability["propagatedFields"], ["category", "targetSystem"])
            self.assertEqual(
                batch_capability["preservedReviewGates"],
                ["duplicate_candidate", "validation_failed"],
            )
            self.assertEqual(review_payload["workItems"][0]["documentId"], document_id)
            self.assertEqual(review_payload["workItems"][0]["document"]["vendorName"], "Vendor")
            self.assertTrue(review_payload["workItems"][0]["document"]["postingEligible"])
            self.assertEqual(review_payload["workItems"][0]["reviewItems"][0]["id"], review_id)

            resolved = client.post(
                f"/api/review/{review_id}/resolve",
                json={"status": "resolved", "resolution": "Approved category."},
            )
            self.assertEqual(resolved.status_code, 200)
            self.assertTrue(resolved.get_json()["success"])

            repeated = client.post(
                f"/api/review/{review_id}/resolve",
                json={"status": "resolved", "resolution": "Repeated stale submission."},
            )
            self.assertEqual(repeated.status_code, 409)
            self.assertEqual(repeated.get_json()["status"], "already_resolved")

            audit = client.get("/api/audit")
            self.assertEqual(audit.status_code, 200)
            self.assertEqual(audit.get_json()["auditEvents"][0]["action"], "local_review.review_item.resolve")

    def test_review_queue_uses_batched_document_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "batched-review",
                "originalFilename": "receipt.pdf",
                "documentType": "receipt",
                "processingStatus": "needs_review",
                "vendorName": "Vendor",
                "totalAmount": 12.1,
            })
            ledger.create_review_item({
                "documentId": document_id,
                "reason": "manual_review_category",
                "details": "Confirm category.",
            })
            client = create_app({"fab_local_ledger_path": ledger_path}).test_client()

            with (
                patch.object(
                    LocalOperationsLedger,
                    "get_document",
                    side_effect=AssertionError("per-document review lookup"),
                ),
                patch.object(
                    LocalOperationsLedger,
                    "get_bookkeeping_record_by_document",
                    side_effect=AssertionError("per-document record lookup"),
                ),
            ):
                response = client.get("/api/review?status=open")

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["summary"]["documents"], 1)
            self.assertEqual(payload["workItems"][0]["documentId"], document_id)
            self.assertEqual(payload["workItems"][0]["document"]["vendorName"], "Vendor")

    def test_review_queue_pages_complete_work_items_with_global_safety_totals(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            grouped_document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "grouped-review",
                "originalFilename": "grouped.pdf",
                "documentType": "vendor_invoice",
                "processingStatus": "needs_review",
                "vendorName": "Vendor",
            })
            evidence_document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "evidence-review",
                "originalFilename": "statement.pdf",
                "documentType": "bank_statement",
                "processingStatus": "needs_review",
            })
            suggestion_document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "suggestion-review",
                "originalFilename": "telecom.pdf",
                "documentType": "receipt",
                "processingStatus": "needs_review",
                "vendorName": "T-Mobile",
                "category": "Manual Review",
            })
            ledger.create_review_item({
                "documentId": grouped_document_id,
                "reason": "validation_failed",
                "details": "Confirm required fields.",
            })
            ledger.create_review_item({
                "documentId": grouped_document_id,
                "reason": "manual_review_category",
                "details": "Confirm category.",
            })
            ledger.create_review_item({
                "documentId": evidence_document_id,
                "reason": "non_posting_document_type",
                "details": "Retain as evidence.",
            })
            ledger.create_review_item({
                "documentId": suggestion_document_id,
                "reason": "duplicate_candidate",
                "details": "Compare duplicate evidence.",
            })
            ledger.create_review_item({
                "reason": "manual_review",
                "details": "Unlinked legacy review.",
            })
            client = create_app({"fab_local_ledger_path": ledger_path}).test_client()

            first = client.get("/api/review?status=open&limit=2&offset=0").get_json()
            second = client.get("/api/review?status=open&limit=2&offset=2").get_json()

            self.assertEqual(first["summary"], second["summary"])
            self.assertEqual(first["summary"]["reviewItems"], 5)
            self.assertEqual(first["summary"]["workItems"], 4)
            self.assertEqual(first["summary"]["documents"], 3)
            self.assertEqual(first["summary"]["postingBlockedDocuments"], 2)
            self.assertEqual(first["summary"]["postingBlockedReviewItems"], 4)
            self.assertEqual(first["summary"]["evidenceOnlyDocuments"], 1)
            self.assertEqual(first["summary"]["evidenceOnlyReviewItems"], 1)
            self.assertEqual(first["summary"]["duplicateCandidates"], 1)
            self.assertEqual(first["summary"]["categorySuggestions"], 1)
            self.assertIsNotNone(first["summary"]["oldestReviewCreatedAt"])
            self.assertIsNotNone(first["summary"]["newestReviewCreatedAt"])
            self.assertEqual(first["pagination"], {
                "scope": "work_items",
                "offset": 0,
                "limit": 2,
                "returned": 2,
                "total": 4,
                "hasMore": True,
                "nextOffset": 2,
                "previousOffset": None,
            })
            self.assertEqual(second["pagination"], {
                "scope": "work_items",
                "offset": 2,
                "limit": 2,
                "returned": 2,
                "total": 4,
                "hasMore": False,
                "nextOffset": None,
                "previousOffset": 0,
            })
            combined = first["workItems"] + second["workItems"]
            self.assertEqual(len({item["id"] for item in combined}), 4)
            grouped = next(
                item for item in combined
                if item["documentId"] == grouped_document_id
            )
            self.assertEqual(len(grouped["reviewItems"]), 2)

            bounded = client.get("/api/review?status=open&limit=1&offset=-50").get_json()
            self.assertEqual(bounded["pagination"]["offset"], 0)
            self.assertEqual(bounded["pagination"]["returned"], 1)
            lean_page = client.get(
                "/api/review?status=open&limit=2&offset=2"
                "&view=summary&includeSummary=false&includeCategoryOptions=false"
            ).get_json()
            self.assertEqual(lean_page["summary"], {})
            self.assertEqual(lean_page["categoryOptions"], [])
            self.assertEqual(lean_page["reviewItems"], [])
            self.assertEqual(lean_page["pagination"]["total"], 4)
            self.assertEqual(lean_page["pagination"]["returned"], 2)

    def test_review_summary_projection_omits_private_and_duplicate_payloads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "private-source-id",
                "originalFilename": "receipt.pdf",
                "documentType": "receipt",
                "processingStatus": "needs_review",
                "vendorName": "Vendor",
                "ocrText": "Review evidence " * 200,
            })
            ledger.create_review_item({
                "documentId": document_id,
                "reason": "manual_review_category",
                "details": "Confirm category.",
                "correctedData": {"privateModelTrace": "must-not-leave-api"},
            })
            client = create_app({"fab_local_ledger_path": ledger_path}).test_client()

            full = client.get("/api/review?status=open").get_json()
            compact = client.get("/api/review?status=open&view=summary").get_json()

            self.assertEqual(len(full["reviewItems"]), 1)
            self.assertEqual(compact["reviewItems"], [])
            self.assertNotIn("sourceDocumentId", compact["workItems"][0]["document"])
            self.assertNotIn(
                "correctedData",
                compact["workItems"][0]["reviewItems"][0],
            )
            self.assertNotIn("must-not-leave-api", json.dumps(compact))
            self.assertEqual(compact["summary"], full["summary"])

    def test_review_api_exposes_identity_evidence_and_resolves_one_duplicate_pair(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            canonical_id = ledger.register_document({
                "source": "google_drive",
                "sourceDocumentId": "canonical-receipt",
                "originalFilename": "canonical.pdf",
                "documentType": "receipt",
                "processingStatus": "processed",
                "vendorName": "Praxis",
                "transactionDate": "2026-06-02",
                "totalAmount": 31.12,
                "ocrText": "Transactie nr.: 10632674",
            })
            alternate_id = ledger.register_document({
                "source": "google_drive",
                "sourceDocumentId": "alternate-receipt",
                "originalFilename": "alternate.pdf",
                "documentType": "receipt",
                "processingStatus": "processed",
                "vendorName": "Praxis",
                "transactionDate": "2026-06-02",
                "totalAmount": 31.12,
                "ocrText": "Transactie nr.: 10639999",
            })
            subject_id = ledger.register_document({
                "source": "google_drive",
                "sourceDocumentId": "subject-receipt",
                "originalFilename": "subject.pdf",
                "documentType": "receipt",
                "processingStatus": "needs_review",
                "vendorName": "Praxis",
                "transactionDate": "2026-06-02",
                "totalAmount": 31.12,
                "ocrText": "Transaction: 10632674",
            })
            selected_candidate_id = ledger.record_duplicate_candidate({
                "documentId": subject_id,
                "candidateDocumentId": canonical_id,
                "matchType": "fuzzy_document_match",
                "confidenceScore": 0.95,
                "status": "pending",
            })
            remaining_candidate_id = ledger.record_duplicate_candidate({
                "documentId": subject_id,
                "candidateDocumentId": alternate_id,
                "matchType": "fuzzy_document_match",
                "confidenceScore": 0.91,
                "status": "pending",
            })
            review_id = ledger.create_review_item({
                "documentId": subject_id,
                "reason": "duplicate_candidate",
                "details": "Compare transaction identity.",
            })
            client = create_app({"fab_local_ledger_path": ledger_path}).test_client()

            initial = client.get("/api/review?status=open").get_json()
            candidates = {
                candidate["id"]: candidate
                for candidate in initial["workItems"][0]["duplicateCandidates"]
            }

            self.assertEqual(
                candidates[selected_candidate_id]["matchedIdentityFields"],
                ["vendor", "date", "amount", "transaction_reference"],
            )
            self.assertEqual(
                candidates[selected_candidate_id]["currentIdentity"]["transactionReference"],
                "10632674",
            )
            self.assertEqual(
                candidates[selected_candidate_id]["candidateIdentity"]["transactionReference"],
                "10632674",
            )
            self.assertEqual(
                candidates[remaining_candidate_id]["conflictingIdentityFields"],
                ["transaction_reference"],
            )

            rejected = client.post(
                f"/api/review/{review_id}/resolve",
                json={
                    "status": "rejected",
                    "resolution": "Not the same transaction.",
                    "corrections": {"duplicateCandidateId": selected_candidate_id},
                    "learnRule": False,
                },
            )

            self.assertEqual(rejected.status_code, 200)
            self.assertEqual(rejected.get_json()["status"], "candidate_rejected")
            self.assertEqual(
                rejected.get_json()["remainingDuplicateCandidateIds"],
                [remaining_candidate_id],
            )
            refreshed = client.get("/api/review?status=open").get_json()
            self.assertEqual(len(refreshed["workItems"][0]["duplicateCandidates"]), 1)
            self.assertEqual(
                refreshed["workItems"][0]["duplicateCandidates"][0]["id"],
                remaining_candidate_id,
            )
            self.assertEqual(
                refreshed["workItems"][0]["reviewItems"][0]["status"],
                "in_review",
            )

    def test_review_summary_separates_non_posting_evidence_from_posting_blocks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "bank-statement-evidence",
                "originalFilename": "statement.pdf",
                "documentType": "bank_statement",
                "processingStatus": "needs_review",
            })
            ledger.create_review_item({
                "documentId": document_id,
                "reason": "non_posting_document_type",
                "details": "Retain as supporting evidence.",
            })

            app = create_app({"fab_local_ledger_path": ledger_path})
            payload = app.test_client().get("/api/review?status=open").get_json()

            self.assertEqual(payload["summary"]["documents"], 1)
            self.assertEqual(payload["summary"]["postingBlockedDocuments"], 0)
            self.assertEqual(payload["summary"]["postingBlockedReviewItems"], 0)
            self.assertEqual(payload["summary"]["evidenceOnlyDocuments"], 1)
            self.assertEqual(payload["summary"]["evidenceOnlyReviewItems"], 1)
            self.assertFalse(payload["workItems"][0]["document"]["postingEligible"])

    def test_review_api_prefills_but_does_not_apply_exact_vendor_category_suggestion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "telecom-suggestion",
                "originalFilename": "invoice.pdf",
                "documentType": "vendor_invoice",
                "processingStatus": "needs_review",
                "vendorName": "T-Mobile",
                "category": "Manual Review",
            })
            ledger.create_review_item({
                "documentId": document_id,
                "reason": "manual_review_category",
                "details": "Confirm category.",
            })

            payload = create_app({"fab_local_ledger_path": ledger_path}).test_client().get(
                "/api/review?status=open"
            ).get_json()

            suggestion = payload["workItems"][0]["document"]["categorySuggestion"]
            self.assertEqual(suggestion["category"], "Telecommunications")
            self.assertTrue(suggestion["requiresApproval"])
            self.assertEqual(payload["summary"]["categorySuggestions"], 1)
            self.assertEqual(ledger.get_document(document_id)["category"], "Manual Review")

    def test_review_api_exposes_suppressed_financial_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "invalid-financial-fields",
                "originalFilename": "receipt.pdf",
                "documentType": "receipt",
                "processingStatus": "needs_review",
                "vendorName": "Vendor",
                "category": "Office Supplies",
                "transactionDate": "3038-06-10",
                "totalAmount": 59.6,
                "vatAmount": 8075.08,
                "confidenceScore": 0.9,
            })
            ledger.create_review_item({
                "documentId": document_id,
                "reason": "validation_failed",
                "details": "Confirm extracted financial fields.",
            })
            LocalBookkeepingRecordService(ledger, {}).upsert_from_document(document_id)

            payload = create_app({"fab_local_ledger_path": ledger_path}).test_client().get(
                "/api/review?status=open"
            ).get_json()
            document = payload["workItems"][0]["document"]

            self.assertEqual(document["bookkeepingExportStatus"], "blocked_invalid_financial_fields")
            self.assertIsNone(document["normalizedRecordDate"])
            self.assertIsNone(document["normalizedVatAmount"])
            self.assertEqual(
                {issue["field"] for issue in document["financialFieldIssues"]},
                {"recordDate", "vatAmount"},
            )

    def test_review_api_can_apply_an_exact_vendor_category_batch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            review_ids = []
            for source_id in ("telecom-1", "telecom-2"):
                document_id = ledger.register_document({
                    "source": "google_drive",
                    "sourceDocumentId": source_id,
                    "originalFilename": f"{source_id}.pdf",
                    "processingStatus": "needs_review",
                    "vendorName": "T-Mobile",
                    "category": "Manual Review",
                    "transactionDate": "2026-06-28",
                    "totalAmount": 25.0,
                    "metadata": {"targetSystem": "waveapps_business"},
                })
                review_ids.append(ledger.create_review_item({
                    "documentId": document_id,
                    "reason": "manual_review_category",
                    "details": "Choose a category.",
                }))

            app = create_app({"fab_local_ledger_path": ledger_path})
            response = app.test_client().post(
                f"/api/review/{review_ids[0]}/resolve",
                json={
                    "status": "approved",
                    "resolution": "Verified recurring telecom expense.",
                    "corrections": {
                        "vendorName": "T-Mobile",
                        "category": "Operations | Telecommunications",
                        "transactionDate": "2026-06-28",
                        "totalAmount": 25.0,
                        "targetSystem": "waveapps_business",
                    },
                    "learnRule": True,
                    "applyToMatchingVendor": True,
                },
            )

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload["success"])
            self.assertEqual(payload["batchPropagation"]["appliedDocuments"], 1)
            self.assertEqual(ledger.get_review_item(review_ids[1])["status"], "approved")

    def test_api_registers_and_lists_sources_without_secret_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({"fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3")})
            client = app.test_client()

            response = client.post("/api/sources", json={
                "sourceType": "google_drive",
                "sourceIdentifier": "drive-folder-sort-out",
                "label": "Drive sort out",
                "status": "ready",
                "metadata": {
                    "folderName": "sort out",
                    "token": "should-not-be-used-here",
                },
            })

            self.assertEqual(response.status_code, 200)
            sources = client.get("/api/sources?sourceType=google_drive").get_json()["sources"]
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0]["label"], "Drive sort out")
            self.assertEqual(sources[0]["status"], "ready")
            self.assertNotIn("should-not-be-used-here", client.get("/api/sources").data.decode("utf-8"))
            self.assertEqual(sources[0]["metadata"]["token"], "<redacted>")
            self.assertEqual(client.get("/api/audit").get_json()["auditEvents"][0]["action"], "local_api.source.upsert")

    def test_api_installs_and_launches_google_drive_authorization_without_exposing_secret(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            credentials_path = os.path.join(temp_dir, "credentials", "drive.json")
            token_path = os.path.join(temp_dir, "tokens", "drive.pickle")
            config = {
                "fab_local_ledger_path": ledger_path,
                "google_drive_credentials_file": credentials_path,
                "google_drive_token_file": token_path,
                "google_drive_folder_id": "approved-source-folder",
            }
            app = create_app(config)
            client = app.test_client()
            credentials = json.dumps({
                "installed": {
                    "client_id": "fab-api-test.apps.googleusercontent.com",
                    "client_secret": "api-test-client-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }).encode("utf-8")

            self.assertEqual(
                client.get("/api/connectors/google-drive/authorization").get_json()["status"],
                "credentials_required",
            )
            invalid = client.post(
                "/api/connectors/google-drive/credentials",
                json={"filename": "drive.json", "contentBase64": "not-base64"},
            )
            self.assertEqual(invalid.status_code, 400)
            invalid_replace = client.post(
                "/api/connectors/google-drive/credentials",
                json={
                    "filename": "drive.json",
                    "contentBase64": base64.b64encode(credentials).decode("ascii"),
                    "replace": "false",
                },
            )
            self.assertEqual(invalid_replace.status_code, 400)
            installed = client.post(
                "/api/connectors/google-drive/credentials",
                json={
                    "filename": "drive.json",
                    "contentBase64": base64.b64encode(credentials).decode("ascii"),
                    "actor": "api_test",
                },
            )
            self.assertEqual(installed.status_code, 201)
            self.assertEqual(installed.get_json()["status"], "ready_to_authorize")
            repeated = client.post(
                "/api/connectors/google-drive/credentials",
                json={
                    "filename": "drive.json",
                    "contentBase64": base64.b64encode(credentials).decode("ascii"),
                },
            )
            self.assertEqual(repeated.status_code, 409)

            ledger = LocalOperationsLedger(ledger_path)

            def authorize(settings):
                os.makedirs(os.path.dirname(settings["google_drive_token_file"]), exist_ok=True)
                with open(settings["google_drive_token_file"], "wb") as handle:
                    handle.write(b"test-token")
                return {"success": True, "status": "authorized", "folderVerified": True}

            app.config["FAB_GOOGLE_DRIVE_AUTH"] = LocalGoogleDriveAuthorizationCoordinator(
                ledger,
                config,
                authorize=authorize,
            )
            started = client.post(
                "/api/connectors/google-drive/authorization/start",
                json={"actor": "api_test"},
            )
            self.assertEqual(started.status_code, 202)
            for _ in range(100):
                status = client.get("/api/connectors/google-drive/authorization").get_json()
                if not status["authorizationInProgress"]:
                    break
                time.sleep(0.02)

            self.assertEqual(status["status"], "authorized")
            self.assertTrue(status["tokenPresent"])
            response_text = client.get("/api/audit").data.decode("utf-8")
            self.assertNotIn("api-test-client-secret", response_text)

    def test_api_installs_and_launches_read_only_gmail_scanner_authorization(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            credentials_path = os.path.join(temp_dir, "credentials", "gmail.json")
            token_path = os.path.join(temp_dir, "tokens", "gmail.pickle")
            config = {
                "fab_local_ledger_path": ledger_path,
                "gmail_credentials_file": credentials_path,
                "gmail_token_file": token_path,
                "gmail_scanner_mode": True,
                "gmail_trusted_senders": "eprintcenter@hp8.us",
                "gmail_query": "label:all from:eprintcenter@hp8.us has:attachment filename:pdf",
            }
            app = create_app(config)
            client = app.test_client()
            credentials = json.dumps({
                "installed": {
                    "client_id": "fab-api-test.apps.googleusercontent.com",
                    "client_secret": "gmail-api-test-client-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }).encode("utf-8")

            initial = client.get("/api/connectors/gmail/authorization").get_json()
            self.assertEqual(initial["status"], "credentials_required")
            self.assertTrue(initial["scannerPolicyReady"])
            installed = client.post(
                "/api/connectors/gmail/credentials",
                json={
                    "filename": "gmail.json",
                    "contentBase64": base64.b64encode(credentials).decode("ascii"),
                    "actor": "api_test",
                },
            )
            self.assertEqual(installed.status_code, 201)

            ledger = LocalOperationsLedger(ledger_path)

            def authorize(settings):
                os.makedirs(os.path.dirname(settings["gmail_token_file"]), exist_ok=True)
                with open(settings["gmail_token_file"], "wb") as handle:
                    handle.write(b"test-token")
                return {
                    "success": True,
                    "status": "authorized",
                    "mailboxVerified": True,
                    "emailAddress": "bookkeeping@example.com",
                }

            app.config["FAB_GMAIL_AUTH"] = LocalGmailAuthorizationCoordinator(
                ledger,
                config,
                authorize=authorize,
            )
            started = client.post(
                "/api/connectors/gmail/authorization/start",
                json={"actor": "api_test"},
            )
            self.assertEqual(started.status_code, 202)
            for _ in range(100):
                status = client.get("/api/connectors/gmail/authorization").get_json()
                if not status["authorizationInProgress"]:
                    break
                time.sleep(0.02)

            self.assertEqual(status["status"], "authorized")
            self.assertTrue(status["tokenPresent"])
            self.assertEqual(status["emailAddress"], "bookkeeping@example.com")
            response_text = client.get("/api/audit").data.decode("utf-8")
            self.assertNotIn("gmail-api-test-client-secret", response_text)

    def test_google_drive_desktop_oauth_mutations_are_loopback_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_api_host": "0.0.0.0",
                "fab_local_api_token": "remote-test-token",
                "google_drive_credentials_file": os.path.join(temp_dir, "drive.json"),
                "google_drive_token_file": os.path.join(temp_dir, "drive.pickle"),
                "google_drive_folder_id": "approved-source-folder",
            })
            client = app.test_client()
            headers = {"Authorization": "Bearer remote-test-token"}

            install = client.post(
                "/api/connectors/google-drive/credentials",
                json={"filename": "drive.json", "contentBase64": "e30="},
                headers=headers,
            )
            start = client.post(
                "/api/connectors/google-drive/authorization/start",
                json={},
                headers=headers,
            )

            self.assertEqual(install.status_code, 403)
            self.assertEqual(start.status_code, 403)

    def test_dashboard_renders_ledger_review_and_resolves_from_form(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "gmail",
                "sourceDocumentId": "msg-1",
                "originalFilename": "invoice.pdf",
                "processingStatus": "needs_review",
                "vendorName": "Vendor",
                "totalAmount": 42.5,
                "confidenceScore": 0.91,
            })
            review_id = ledger.create_review_item({
                "documentId": document_id,
                "reason": "confirm_wave_category",
                "details": "Approve suggested Wave category.",
            })

            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()

            page = client.get("/")
            self.assertEqual(page.status_code, 200)
            html = page.data.decode("utf-8")
            self.assertIn("FAB Operations", html)
            self.assertIn("Sources", html)
            self.assertIn("Document Ledger", html)
            self.assertIn("Bookkeeping Records", html)
            self.assertIn("Manual Review", html)
            self.assertIn("Close Readiness", html)
            self.assertIn("invoice.pdf", html)
            self.assertIn(f"/documents/{document_id}", html)
            self.assertIn("confirm_wave_category", html)
            self.assertIn("EUR 42.50", html)
            self.assertIn("91%", html)

            resolved = client.post(
                f"/review/{review_id}/resolve",
                data={"status": "approved", "resolution": "Approved in dashboard."},
                follow_redirects=True,
            )
            self.assertEqual(resolved.status_code, 200)
            self.assertNotIn("confirm_wave_category", resolved.data.decode("utf-8"))
            self.assertEqual(ledger.list_review_items()[0]["status"], "approved")
            self.assertEqual(ledger.list_audit_events()[0]["action"], "local_review.review_item.resolve")

    def test_document_detail_page_shows_review_context_and_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            receipt_path = os.path.join(temp_dir, "receipt.txt")
            with open(receipt_path, "w", encoding="utf-8") as handle:
                handle.write("Vendor: Office Shop\nDate: 2026-06-28\nTotal: EUR 42.50\n")
            ledger = LocalOperationsLedger(ledger_path)
            original_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "original",
                "originalFilename": "original.txt",
                "processingStatus": "processed",
            })
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "receipt-detail",
                "originalFilename": "receipt.txt",
                "mimeType": "text/plain",
                "storagePath": receipt_path,
                "documentType": "text",
                "processingStatus": "needs_review",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
                "confidenceScore": 0.91,
                "ocrText": "Vendor: Office Shop\nTotal: EUR 42.50",
                "extractedData": {"vendor_name": "Office Shop", "total_amount": 42.5},
                "duplicateOfDocumentId": original_id,
            })
            ledger.replace_extracted_fields(document_id, [{
                "fieldName": "vendor_name",
                "value": "Office Shop",
                "confidenceScore": 0.9,
                "source": "test",
                "provenance": {"stage": "test"},
            }])
            ledger.record_duplicate_candidate({
                "documentId": document_id,
                "candidateDocumentId": original_id,
                "matchType": "fuzzy_document_match",
                "confidenceScore": 0.93,
                "status": "pending",
            })
            group_id = ledger.upsert_document_group({
                "groupKey": "manual:test",
                "groupType": "manual_merge",
                "title": "Two page receipt",
                "status": "needs_review",
                "primaryDocumentId": document_id,
            })
            ledger.add_document_to_group(group_id, document_id, {"role": "primary"})
            review_id = ledger.create_review_item({
                "documentId": document_id,
                "reason": "duplicate_candidate",
                "details": "Possible duplicate.",
            })
            ledger.upsert_bookkeeping_record({
                "documentId": document_id,
                "status": "needs_review",
                "targetSystem": "waveapps_business",
                "vendorName": "Office Shop",
                "amount": 42.5,
                "reviewRequired": True,
            })
            ledger.record_audit_event({
                "action": "workflow.document.imported",
                "entityType": "bookkeeping_document",
                "entityId": str(document_id),
                "details": {"source": "test"},
            })
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()

            response = client.get(f"/documents/{document_id}")

            self.assertEqual(response.status_code, 200)
            html = response.data.decode("utf-8")
            self.assertIn("Review Summary", html)
            self.assertIn("Source Provenance", html)
            self.assertIn("Text preview from local source file", html)
            self.assertIn("Vendor: Office Shop", html)
            self.assertIn("Extracted Fields", html)
            self.assertIn("Duplicate And Group Evidence", html)
            self.assertIn("fuzzy_document_match", html)
            self.assertIn("Two page receipt", html)
            self.assertIn("Bookkeeping, Routing, Export, Reconciliation", html)
            self.assertIn("workflow.document.imported", html)
            self.assertIn(f"/review/{review_id}/resolve", html)

    def test_document_source_api_serves_only_checksum_verified_preview_bytes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            receipt_path = os.path.join(temp_dir, "receipt.txt")
            source_bytes = b"Vendor: Office Shop\nTotal: EUR 42.50\n"
            with open(receipt_path, "wb") as handle:
                handle.write(source_bytes)
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "verified-source",
                "originalFilename": "receipt.txt",
                "mimeType": "text/plain",
                "storagePath": receipt_path,
                "contentSha256": hashlib.sha256(source_bytes).hexdigest(),
                "processingStatus": "needs_review",
            })
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()

            response = client.get(f"/api/documents/{document_id}/source")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, source_bytes)
            self.assertEqual(response.mimetype, "text/plain")
            self.assertIn("inline", response.headers["Content-Disposition"])
            self.assertEqual(response.headers["X-FAB-Source-Integrity"], "verified")
            self.assertEqual(
                response.headers["X-FAB-Source-SHA256"],
                hashlib.sha256(source_bytes).hexdigest(),
            )
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            response.close()

            with open(receipt_path, "ab") as handle:
                handle.write(b"tampered")
            tampered = client.get(f"/api/documents/{document_id}/source")

            self.assertEqual(tampered.status_code, 409)
            self.assertNotIn(source_bytes, tampered.data)
            self.assertIn("failed its integrity check", tampered.get_json()["error"])
            tampered.close()

    def test_document_source_api_fails_closed_for_unhashed_unsafe_or_oversized_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            source_bytes = b"<html>active source</html>"
            source_path = os.path.join(temp_dir, "source.html")
            with open(source_path, "wb") as handle:
                handle.write(source_bytes)
            unhashed_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "unhashed-source",
                "originalFilename": "source.html",
                "mimeType": "text/html",
                "storagePath": source_path,
            })
            unsafe_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "unsafe-source",
                "originalFilename": "source.html",
                "mimeType": "text/html",
                "storagePath": source_path,
                "contentSha256": hashlib.sha256(source_bytes).hexdigest(),
            })
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()

            unhashed = client.get(f"/api/documents/{unhashed_id}/source")
            unsafe = client.get(f"/api/documents/{unsafe_id}/source")
            with patch("src.operations.local_api.LOCAL_SOURCE_PREVIEW_MAX_BYTES", 3):
                oversized = client.get(f"/api/documents/{unsafe_id}/source")

            self.assertEqual(unhashed.status_code, 409)
            self.assertIn("integrity hash", unhashed.get_json()["error"])
            self.assertEqual(unsafe.status_code, 415)
            self.assertEqual(oversized.status_code, 413)

    def test_api_review_resolution_applies_corrections_and_exposes_rules(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-2",
                "originalFilename": "receipt.txt",
                "processingStatus": "needs_review",
                "vendorName": "Old Vendor",
                "category": "Manual Review",
            })
            review_id = ledger.create_review_item({
                "documentId": document_id,
                "reason": "validation_failed",
                "details": "Missing corrected fields.",
            })
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()

            response = client.post(
                f"/api/review/{review_id}/resolve",
                json={
                    "status": "approved",
                    "resolution": "Corrected fields.",
                    "corrections": {
                        "vendorName": "Correct Vendor",
                        "category": "Office Supplies",
                        "transactionDate": "2026-06-28",
                        "totalAmount": 42.5,
                    },
                },
            )

            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["success"])
            detail = client.get(f"/api/documents/{document_id}").get_json()
            self.assertEqual(detail["vendor_name"], "Correct Vendor")
            self.assertEqual(detail["category"], "Office Supplies")
            self.assertEqual(detail["processing_status"], "reviewed")
            self.assertEqual(detail["review_corrections"][0]["corrected_data"]["category"], "Office Supplies")
            rules = client.get("/api/rules").get_json()["vendorCategoryRules"]
            self.assertEqual(rules[0]["vendor_name"], "Correct Vendor")
            self.assertEqual(rules[0]["status"], "approved")
            corrections = client.get(f"/api/corrections?documentId={document_id}").get_json()["reviewCorrections"]
            self.assertEqual(corrections[0]["corrected_data"]["vendorName"], "Correct Vendor")
            records = client.get("/api/bookkeeping-records?status=ready_to_route").get_json()["bookkeepingRecords"]
            self.assertEqual(records[0]["vendor_name"], "Correct Vendor")
            self.assertEqual(records[0]["amount"], 42.5)
            self.assertEqual(records[0]["line_item_count"], 1)
            record_detail = client.get(f"/api/bookkeeping-records/{records[0]['id']}").get_json()
            line_items = client.get(f"/api/bookkeeping-records/{records[0]['id']}/line-items").get_json()["lineItems"]
            self.assertEqual(record_detail["line_items"][0]["source"], "document_total")
            self.assertEqual(line_items[0]["amount"], 42.5)

    def test_api_created_vendor_rule_defaults_to_suggested(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({"fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3")})
            client = app.test_client()

            response = client.post("/api/rules", json={
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "targetSystem": "waveapps_business",
            })

            self.assertEqual(response.status_code, 200)
            rules = client.get("/api/rules").get_json()["vendorCategoryRules"]
            self.assertEqual(rules[0]["status"], "suggested")

    def test_api_rule_resolution_updates_status_dashboard_and_audit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            rule_id = ledger.upsert_vendor_category_rule({
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "targetSystem": "waveapps_business",
                "status": "suggested",
            })
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()

            response = client.post(
                f"/api/rules/{rule_id}/resolve",
                json={
                    "status": "approved",
                    "resolution": "Recurring vendor verified.",
                    "actor": "operator",
                },
            )

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload["success"])
            self.assertEqual(payload["rule"]["status"], "approved")
            self.assertEqual(payload["rule"]["metadata"]["statusHistory"][0]["to"], "approved")
            rules = client.get("/api/rules?status=approved").get_json()["vendorCategoryRules"]
            self.assertEqual(rules[0]["id"], rule_id)
            audit_actions = [
                event["action"]
                for event in client.get("/api/audit").get_json()["auditEvents"]
            ]
            self.assertIn("local_api.vendor_category_rule.status_changed", audit_actions)
            dashboard_html = client.get("/").data.decode("utf-8")
            self.assertIn("Rule Review", dashboard_html)
            self.assertIn("Disable", dashboard_html)

    def test_dashboard_rule_resolution_form_rejects_suggestion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            rule_id = ledger.upsert_vendor_category_rule({
                "vendorName": "Train Vendor",
                "category": "Travel",
                "targetSystem": "waveapps_business",
                "status": "suggested",
            })
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()

            response = client.post(
                f"/rules/{rule_id}/resolve",
                data={
                    "status": "rejected",
                    "resolution": "Wrong category for this vendor.",
                },
                follow_redirects=False,
            )

            self.assertEqual(response.status_code, 302)
            rule = ledger.get_vendor_category_rule(rule_id)
            self.assertEqual(rule["status"], "rejected")
            self.assertEqual(rule["metadata"]["lastActor"], "local_dashboard")
            self.assertEqual(ledger.dashboard_metrics()["suggested_vendor_rules"], 0)

    def test_api_exposes_vendor_and_category_operating_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "vendor-directory-api",
                "originalFilename": "office.pdf",
                "processingStatus": "processed",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
            })
            ledger.upsert_bookkeeping_record({
                "documentId": document_id,
                "status": "ready_to_route",
                "targetSystem": "waveapps_business",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "amount": 42.5,
                "currency": "EUR",
                "exportStatus": "ready",
            })
            ledger.upsert_vendor_category_rule({
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "targetSystem": "waveapps_business",
                "status": "approved",
            })
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()

            vendors = client.get("/api/vendors")
            categories = client.get("/api/categories")
            dashboard = client.get("/")

            self.assertEqual(vendors.status_code, 200)
            self.assertEqual(categories.status_code, 200)
            self.assertEqual(vendors.get_json()["externalSubmission"], "not_executed")
            self.assertEqual(vendors.get_json()["vendors"][0]["vendorName"], "Office Shop")
            self.assertEqual(vendors.get_json()["vendors"][0]["recordCount"], 1)
            self.assertEqual(vendors.get_json()["vendors"][0]["ruleCount"], 1)
            self.assertEqual(categories.get_json()["categories"][0]["category"], "Office Supplies")
            self.assertIn("Vendor Directory", dashboard.data.decode("utf-8"))
            self.assertIn("Category Directory", dashboard.data.decode("utf-8"))
            self.assertIn("Office Shop", dashboard.data.decode("utf-8"))

    def test_api_prepares_wave_route_without_external_submission(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-route-api",
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
                "metadata": {"targetAccount": "Office Supplies"},
            })
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()

            response = client.post(f"/api/documents/{document_id}/route", json={"targetSystem": "waveapps_business"})

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload["success"])
            self.assertEqual(payload["status"], "draft_prepared")
            self.assertEqual(payload["operation"]["action_id"], "transaction_add")
            self.assertEqual(payload["operation"]["payload"]["lineItems"][0]["account"], "Office Supplies")
            self.assertEqual(payload["operation"]["payload"]["lineItems"][0]["tax"], "BTW 21%")
            routing = client.get("/api/routing").get_json()["routingAttempts"]
            self.assertEqual(routing[0]["status"], "draft_prepared")
            self.assertEqual(routing[0]["metadata"]["externalSubmission"], "not_executed")
            records = client.get("/api/bookkeeping-records").get_json()["bookkeepingRecords"]
            self.assertEqual(records[0]["document_id"], document_id)
            self.assertEqual(records[0]["export_status"], "draft_prepared")
            self.assertEqual(records[0]["line_items"][0]["account_name"], "Office Supplies")
            detail = client.get(f"/api/documents/{document_id}").get_json()
            self.assertEqual(detail["processing_status"], "export_draft_prepared")
            self.assertEqual(detail["bookkeeping_record"]["export_status"], "draft_prepared")
            self.assertEqual(detail["bookkeeping_record"]["line_items"][0]["tax_code"], "BTW 21%")

    def test_dashboard_prepare_ready_routes_shows_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-route-form",
                "originalFilename": "receipt.txt",
                "processingStatus": "reviewed",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
            })
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()

            page = client.post("/routing/prepare-ready", follow_redirects=True)

            self.assertEqual(page.status_code, 200)
            html = page.data.decode("utf-8")
            self.assertIn("Routing & Export Drafts", html)
            self.assertIn("Last routing run", html)
            self.assertIn("draft_prepared", html)
            self.assertIn("transaction_add", html)

    def test_api_routes_bank_bookkeeping_record_to_wave_draft(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            ledger.upsert_vendor_category_rule({
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "targetSystem": "waveapps",
                "status": "approved",
            })
            ledger.upsert_bank_transaction({
                "accountIdentifier": "wave-checking",
                "transactionId": "tx-api-bank-route",
                "transactionDate": "2026-06-28",
                "amount": -42.5,
                "currency": "EUR",
                "description": "Printer paper",
                "counterparty": "Office Shop",
                "reconciliationStatus": "not_started",
            })
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()
            client.post("/api/bookkeeping-records/refresh", json={"sourceType": "bank_transaction"})
            record = client.get("/api/bookkeeping-records?sourceType=bank_transaction").get_json()["bookkeepingRecords"][0]

            response = client.post(f"/api/bookkeeping-records/{record['id']}/route")

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload["success"])
            self.assertEqual(payload["status"], "draft_prepared")
            self.assertEqual(payload["operation"]["action_id"], "transaction_add")
            self.assertEqual(payload["operation"]["payload"]["amount"], 42.5)
            self.assertEqual(payload["operation"]["payload"]["category"], "Office Supplies")
            routing = client.get("/api/routing").get_json()["routingAttempts"]
            self.assertIsNone(routing[0]["document_id"])
            self.assertEqual(routing[0]["bookkeeping_record_id"], record["id"])
            self.assertEqual(routing[0]["metadata"]["bookkeepingRecordId"], record["id"])
            filtered_routing = client.get(f"/api/routing?bookkeepingRecordId={record['id']}").get_json()["routingAttempts"]
            self.assertEqual(len(filtered_routing), 1)
            self.assertEqual(filtered_routing[0]["id"], routing[0]["id"])
            invalid_routing = client.get("/api/routing?bookkeepingRecordId=not-an-id")
            self.assertEqual(invalid_routing.status_code, 400)
            records = client.get("/api/bookkeeping-records?sourceType=bank_transaction").get_json()["bookkeepingRecords"]
            self.assertEqual(records[0]["status"], "export_draft_prepared")
            self.assertEqual(records[0]["export_status"], "draft_prepared")
            detail = client.get(f"/api/bookkeeping-records/{record['id']}").get_json()
            self.assertEqual(detail["routing_attempts"][0]["id"], routing[0]["id"])
            self.assertEqual(detail["routing_attempts"][0]["bookkeeping_record_id"], record["id"])
            self.assertEqual(detail["routing_attempts"][0]["metadata"]["bookkeepingRecordId"], record["id"])

    def test_api_prepare_ready_routes_defaults_to_documents_and_bank_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-route-all",
                "originalFilename": "receipt.txt",
                "processingStatus": "reviewed",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
            })
            ledger.upsert_vendor_category_rule({
                "vendorName": "Paper Store",
                "category": "Office Supplies",
                "targetSystem": "waveapps",
                "status": "approved",
            })
            ledger.upsert_bank_transaction({
                "accountIdentifier": "wave-checking",
                "transactionId": "tx-api-route-all",
                "transactionDate": "2026-06-29",
                "amount": -13.25,
                "currency": "EUR",
                "description": "Notebook",
                "counterparty": "Paper Store",
                "reconciliationStatus": "not_started",
            })
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()
            client.post("/api/bookkeeping-records/refresh", json={"sourceType": "bank_transaction"})

            response = client.post("/api/routing/prepare-ready", json={})

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["sourceType"], "all")
            self.assertEqual(payload["requested"], 2)
            self.assertEqual(payload["draftPrepared"], 2)
            self.assertEqual(payload["documentRouting"]["draftPrepared"], 1)
            self.assertEqual(payload["bankRecordRouting"]["draftPrepared"], 1)
            routing = client.get("/api/routing?status=draft_prepared").get_json()["routingAttempts"]
            self.assertEqual(len(routing), 2)
            self.assertEqual(
                {attempt["metadata"].get("sourceType", "document") for attempt in routing},
                {"document", "bank_transaction"},
            )
            record_routes = [attempt for attempt in routing if attempt.get("bookkeeping_record_id")]
            self.assertEqual(len(record_routes), 1)

    def test_api_refreshes_bank_bookkeeping_records_with_rules(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            ledger.upsert_vendor_category_rule({
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "targetSystem": "waveapps",
                "status": "approved",
            })
            transaction_id = ledger.upsert_bank_transaction({
                "accountIdentifier": "wave-checking",
                "transactionId": "tx-api-bank-refresh",
                "transactionDate": "2026-06-28",
                "amount": -42.5,
                "currency": "EUR",
                "description": "Printer paper",
                "counterparty": "Office Shop",
                "reconciliationStatus": "not_started",
            })
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()

            response = client.post(
                "/api/bookkeeping-records/refresh",
                json={"sourceType": "bank_transaction"},
            )

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["sourceType"], "bank_transaction")
            self.assertEqual(payload["externalSubmission"], "not_executed")
            self.assertEqual(payload["updated"], 1)
            records = client.get("/api/bookkeeping-records").get_json()["bookkeepingRecords"]
            self.assertEqual(records[0]["bank_transaction_id"], transaction_id)
            self.assertEqual(records[0]["category"], "Office Supplies")
            self.assertEqual(records[0]["line_items"][0]["account_name"], "Office Supplies")

    def test_dashboard_refreshes_all_bookkeeping_records_and_shows_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-refresh-all",
                "originalFilename": "receipt.txt",
                "processingStatus": "processed",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
            })
            ledger.upsert_bank_transaction({
                "accountIdentifier": "wave-checking",
                "transactionId": "tx-refresh-all",
                "transactionDate": "2026-06-29",
                "amount": -12.5,
                "currency": "EUR",
                "description": "Train ticket",
                "counterparty": "Rail Vendor",
                "reconciliationStatus": "not_started",
            })
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()

            page = client.post(
                "/bookkeeping-records/refresh",
                data={"sourceType": "all"},
                follow_redirects=True,
            )

            self.assertEqual(page.status_code, 200)
            html = page.data.decode("utf-8")
            self.assertIn("Bookkeeping Records", html)
            self.assertIn("Last record refresh", html)
            self.assertIn("sourceType", html)
            self.assertIn("all", html)
            records = client.get("/api/bookkeeping-records").get_json()["bookkeepingRecords"]
            self.assertEqual(len(records), 2)
            bank_records = client.get("/api/bookkeeping-records?sourceType=bank_transaction").get_json()["bookkeepingRecords"]
            document_records = client.get("/api/bookkeeping-records?sourceType=document").get_json()["bookkeepingRecords"]
            self.assertEqual(len(bank_records), 1)
            self.assertEqual(bank_records[0]["source_type"], "bank_transaction")
            self.assertEqual(len(document_records), 1)
            self.assertEqual(document_records[0]["source_type"], "document")

    def test_api_resolves_bookkeeping_record_with_corrections_and_dashboard_actions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            record_id = ledger.upsert_bookkeeping_record({
                "sourceType": "document",
                "status": "needs_review",
                "exportStatus": "blocked_by_review",
                "targetSystem": "waveapps",
                "vendorName": "Unknown",
                "category": "Manual Review",
                "amount": 10,
                "reviewRequired": True,
            })
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()

            dashboard = client.get("/")
            self.assertEqual(dashboard.status_code, 200)
            html = dashboard.data.decode("utf-8")
            self.assertIn(f"/bookkeeping-records/{record_id}/resolve", html)
            self.assertIn(f"/bookkeeping-records/{record_id}", html)
            self.assertIn("Approve", html)
            self.assertIn("Reject", html)

            detail_page = client.get(f"/bookkeeping-records/{record_id}")
            detail_html = detail_page.data.decode("utf-8")
            self.assertEqual(detail_page.status_code, 200)
            self.assertIn(f"Bookkeeping Record #{record_id}", detail_html)
            self.assertIn("Record Review", detail_html)
            self.assertIn("Source Proof", detail_html)
            self.assertIn("Unknown", detail_html)
            self.assertIn(f"/api/bookkeeping-records/{record_id}", detail_html)

            response = client.post(
                f"/api/bookkeeping-records/{record_id}/resolve",
                json={
                    "status": "approved",
                    "resolution": "Corrected from normalized record queue.",
                    "actor": "api-test",
                    "corrections": {
                        "vendorName": "Office Shop",
                        "category": "Office Supplies",
                        "amount": 42.5,
                        "targetAccount": "Office Supplies",
                    },
                },
            )
            payload = response.get_json()
            record = client.get(f"/api/bookkeeping-records/{record_id}").get_json()
            audit = client.get("/api/audit").get_json()["auditEvents"][0]

            self.assertEqual(response.status_code, 200)
            self.assertTrue(payload["success"])
            self.assertEqual(payload["externalSubmission"], "not_executed")
            self.assertEqual(record["status"], "ready_to_route")
            self.assertEqual(record["export_status"], "ready")
            self.assertEqual(record["review_required"], 0)
            self.assertEqual(record["vendor_name"], "Office Shop")
            self.assertEqual(record["metadata"]["lastResolution"]["actor"], "api-test")
            self.assertEqual(audit["action"], "local_bookkeeping_records.record.resolve")

    def test_api_prepares_and_approves_export_attempts_without_submission(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-export-api",
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
                    }]
                },
            })
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()

            route = client.post(f"/api/documents/{document_id}/route", json={}).get_json()
            prepared = client.post(
                f"/api/routing/{route['routingAttemptId']}/export-attempt",
                json={"actor": "test"},
            )
            blocked = client.post(
                f"/api/export-attempts/{prepared.get_json()['exportAttemptId']}/approve",
                json={"confirmation": "wrong"},
            )
            approved = client.post(
                f"/api/export-attempts/{prepared.get_json()['exportAttemptId']}/approve",
                json={"confirmation": EXPORT_APPROVAL_PHRASE, "actor": "test"},
            )

            self.assertEqual(prepared.status_code, 200)
            self.assertEqual(prepared.get_json()["status"], "approval_required")
            self.assertEqual(prepared.get_json()["exportAttempt"]["external_submission"], "not_executed")
            self.assertEqual(blocked.status_code, 400)
            self.assertEqual(blocked.get_json()["status"], "requires_confirmation")
            self.assertEqual(approved.status_code, 200)
            self.assertEqual(approved.get_json()["exportAttempt"]["external_submission"], "approved_not_executed")
            attempts = client.get("/api/export-attempts").get_json()["exportAttempts"]
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["status"], "approved")
            records = client.get("/api/bookkeeping-records").get_json()["bookkeepingRecords"]
            self.assertEqual(records[0]["export_status"], "approved_not_submitted")
            self.assertEqual(records[0]["status"], "export_approved")
            html = client.get("/").data.decode("utf-8")
            self.assertIn("Export Attempts", html)
            self.assertIn("approved_not_executed", html)

    def test_api_exposes_mijngeldzaken_export_master_ledger_draft(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-export-api-mgz",
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
            app = create_app({
                "fab_local_ledger_path": ledger_path,
                "mijngeldzaken_category_mapping": {"Personal": "Huishouden"},
            })
            client = app.test_client()

            route = client.post(
                f"/api/documents/{document_id}/route",
                json={"targetSystem": "mijngeldzaken"},
            ).get_json()
            prepared = client.post(
                f"/api/routing/{route['routingAttemptId']}/export-attempt",
                json={"actor": "test"},
            )

            self.assertEqual(prepared.status_code, 200)
            export_attempt = prepared.get_json()["exportAttempt"]
            draft = export_attempt["metadata"]["masterLedgerDraft"]
            json_artifact = client.get(f"/api/export-attempts/{export_attempt['id']}/artifact")
            csv_artifact = client.get(f"/api/export-attempts/{export_attempt['id']}/artifact?format=csv")
            self.assertEqual(export_attempt["target_system"], "mijngeldzaken")
            self.assertEqual(draft["draftType"], "transaction_import")
            self.assertEqual(draft["importRow"]["Categorie"], "Huishouden")
            self.assertEqual(draft["importRow"]["Omschrijving"], "Weekly groceries")
            self.assertEqual(draft["sourceProof"]["documentId"], document_id)
            self.assertEqual(export_attempt["metadata"]["masterLedgerChecksum"], draft["checksum"])
            self.assertEqual(json_artifact.status_code, 200)
            self.assertEqual(json_artifact.get_json()["artifact"]["checksum"], draft["checksum"])
            self.assertEqual(json_artifact.get_json()["content"]["externalSubmission"], "not_executed")
            self.assertEqual(csv_artifact.status_code, 200)
            self.assertIn("text/csv", csv_artifact.headers["Content-Type"])
            self.assertEqual(csv_artifact.headers["X-FAB-External-Submission"], "not_executed")
            self.assertEqual(csv_artifact.headers["X-FAB-Master-Ledger-Checksum"], draft["checksum"])
            self.assertIn("2026-06-28,Weekly groceries,Local Supermarket,42.5,Huishouden", csv_artifact.data.decode("utf-8"))
            artifact_events = [
                event for event in client.get("/api/audit").get_json()["auditEvents"]
                if event["action"] == "local_export_attempt.artifact_prepared"
            ]
            self.assertEqual({event["details"]["format"] for event in artifact_events}, {"json", "csv"})
            self.assertTrue(all(event["details"]["checksum"] == draft["checksum"] for event in artifact_events))
            approved = client.post(
                f"/api/export-attempts/{export_attempt['id']}/approve",
                json={"confirmation": EXPORT_APPROVAL_PHRASE, "actor": "test"},
            )
            recorded = client.post(
                f"/api/export-attempts/{export_attempt['id']}/result",
                json={
                    "status": "queued",
                    "externalId": "mgz-import-queued",
                    "confirmation": EXPORT_RESULT_CONFIRMATION_PHRASE,
                    "actor": "test",
                },
            )
            self.assertEqual(approved.status_code, 200)
            self.assertEqual(recorded.status_code, 200)
            self.assertEqual(
                recorded.get_json()["exportAttempt"]["metadata"]["lastResult"]["masterLedgerChecksum"],
                draft["checksum"],
            )
            self.assertEqual(
                recorded.get_json()["exportAttempt"]["result"]["masterLedgerChecksum"],
                draft["checksum"],
            )
            master_ledger = client.get("/api/master-ledger?audit=true&actor=test")
            master_summary = client.get("/api/master-ledger?summaryOnly=true")
            master_csv = client.get("/api/master-ledger?format=csv")
            self.assertEqual(master_ledger.status_code, 200)
            master_payload = master_ledger.get_json()
            self.assertEqual(master_payload["summary"]["totalRows"], 1)
            self.assertEqual(master_payload["summary"]["byTargetSystem"]["mijngeldzaken"]["statuses"]["queued"], 1)
            self.assertEqual(master_payload["rows"][0]["masterLedgerChecksum"], draft["checksum"])
            self.assertEqual(master_payload["rows"][0]["downstreamStatus"], "queued")
            self.assertEqual(master_payload["rows"][0]["externalSubmission"], "queued")
            self.assertEqual(len(master_payload["ledgerChecksum"]), 64)
            self.assertEqual(master_summary.status_code, 200)
            self.assertNotIn("rows", master_summary.get_json())
            self.assertEqual(master_summary.get_json()["rowsOmitted"], 1)
            self.assertEqual(master_summary.get_json()["dataThroughDate"], "2026-06-28")
            self.assertEqual(
                master_summary.get_json()["ledgerChecksum"],
                master_payload["ledgerChecksum"],
            )
            self.assertEqual(master_csv.status_code, 200)
            self.assertIn("text/csv", master_csv.headers["Content-Type"])
            self.assertEqual(master_csv.headers["X-FAB-External-Submission"], "not_executed")
            self.assertEqual(master_csv.headers["X-FAB-Master-Ledger-Checksum"], master_payload["ledgerChecksum"])
            self.assertIn("mijngeldzaken", master_csv.data.decode("utf-8"))
            audit_actions = [event["action"] for event in client.get("/api/audit").get_json()["auditEvents"]]
            self.assertIn("local_master_ledger.projection_prepared", audit_actions)
            html = client.get("/").data.decode("utf-8")
            self.assertIn("Master Ledger", html)
            self.assertIn("Master ledger transaction_import", html)
            self.assertIn(draft["checksum"][:12], html)
            self.assertIn(master_payload["ledgerChecksum"][:12], html)
            self.assertIn(f"/api/export-attempts/{export_attempt['id']}/artifact?format=csv", html)

    def test_api_regenerates_stale_mijngeldzaken_export_attempt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-export-api-mgz-regen",
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
            app = create_app({
                "fab_local_ledger_path": ledger_path,
                "mijngeldzaken_category_mapping": {"Personal": "Huishouden"},
            })
            client = app.test_client()
            route = client.post(
                f"/api/documents/{document_id}/route",
                json={"targetSystem": "mijngeldzaken"},
            ).get_json()
            prepared = client.post(
                f"/api/routing/{route['routingAttemptId']}/export-attempt",
                json={"actor": "test"},
            ).get_json()
            original_checksum = prepared["exportAttempt"]["metadata"]["masterLedgerChecksum"]
            ledger.update_document(document_id, {
                "source": "scanner",
                "sourceDocumentId": "scan-export-api-mgz-regen",
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

            stale_master = client.get("/api/master-ledger").get_json()
            html = client.get("/").data.decode("utf-8")
            regenerated = client.post(
                f"/api/export-attempts/{prepared['exportAttemptId']}/regenerate",
                json={"actor": "api-test"},
            )
            fresh_master = client.get("/api/master-ledger").get_json()

            self.assertEqual(stale_master["rows"][0]["downstreamStatus"], "stale_master_ledger_draft")
            self.assertIn(f"/export-attempts/{prepared['exportAttemptId']}/regenerate", html)
            self.assertEqual(regenerated.status_code, 200)
            payload = regenerated.get_json()
            self.assertEqual(payload["status"], "regenerated")
            self.assertEqual(payload["externalSubmission"], "not_executed")
            self.assertNotEqual(payload["masterLedgerChecksum"], original_checksum)
            self.assertEqual(payload["exportAttempt"]["metadata"]["masterLedgerDraft"]["importRow"]["Bedrag"], 99.99)
            self.assertEqual(fresh_master["rows"][0]["downstreamStatus"], "awaiting_approval")
            self.assertEqual(fresh_master["summary"]["blockedRows"], 0)
            audit_actions = [event["action"] for event in client.get("/api/audit").get_json()["auditEvents"]]
            self.assertIn("local_export_attempt.regenerated", audit_actions)

    def test_api_lists_bank_record_export_attempts_by_bookkeeping_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            ledger.upsert_vendor_category_rule({
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "targetSystem": "waveapps",
                "status": "approved",
            })
            ledger.upsert_bank_transaction({
                "accountIdentifier": "wave-checking",
                "transactionId": "tx-api-bank-export-filter",
                "transactionDate": "2026-06-28",
                "amount": -42.5,
                "currency": "EUR",
                "description": "Printer paper",
                "counterparty": "Office Shop",
                "reconciliationStatus": "not_started",
            })
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()
            client.post("/api/bookkeeping-records/refresh", json={"sourceType": "bank_transaction"})
            record = client.get("/api/bookkeeping-records?sourceType=bank_transaction").get_json()["bookkeepingRecords"][0]
            route = client.post(f"/api/bookkeeping-records/{record['id']}/route").get_json()

            prepared = client.post(
                f"/api/routing/{route['routingAttemptId']}/export-attempt",
                json={"actor": "test"},
            )
            filtered = client.get(f"/api/export-attempts?bookkeepingRecordId={record['id']}")
            document_filtered = client.get("/api/export-attempts?documentId=999999")
            invalid = client.get("/api/export-attempts?bookkeepingRecordId=not-an-id")
            page = client.get("/")

            self.assertEqual(prepared.status_code, 200)
            prepared_payload = prepared.get_json()
            self.assertEqual(prepared_payload["bookkeepingRecordId"], record["id"])
            self.assertIsNone(prepared_payload["documentId"])
            self.assertEqual(prepared_payload["exportAttempt"]["bookkeeping_record_id"], record["id"])
            self.assertIsNone(prepared_payload["exportAttempt"]["document_id"])
            self.assertEqual(filtered.status_code, 200)
            attempts = filtered.get_json()["exportAttempts"]
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["bookkeeping_record_id"], record["id"])
            self.assertIsNone(attempts[0]["document_id"])
            detail = client.get(f"/api/bookkeeping-records/{record['id']}").get_json()
            self.assertEqual(detail["export_attempts"][0]["id"], attempts[0]["id"])
            self.assertEqual(detail["routing_attempts"][0]["bookkeeping_record_id"], record["id"])
            self.assertEqual(detail["routing_attempts"][0]["metadata"]["bookkeepingRecordId"], record["id"])
            self.assertEqual(document_filtered.get_json()["exportAttempts"], [])
            self.assertEqual(invalid.status_code, 400)
            self.assertEqual(invalid.get_json()["error"], "Invalid bookkeepingRecordId")
            html = page.data.decode("utf-8")
            self.assertIn(f"record #{record['id']}", html)
            self.assertIn("approval_required", html)

    def test_api_executes_approved_export_attempt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            reset_all_limiters()
            self.addCleanup(reset_all_limiters)
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-export-api-exec",
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
                "metadata": {"targetSystem": "waveapps_business"},
            })
            app = create_app({
                "fab_local_ledger_path": ledger_path,
                "waveapps_business_access_token": "secret-store-token",
                "waveapps_business_id": "business-1",
                "waveapps_business_anchor_account_id": "bank-account-1",
                "waveapps_business_category_mapping": {
                    "Office Supplies": "Office Supplies",
                },
                "waveapps_business_category_account_ids": {
                    "Office Supplies": "expense-account-1",
                },
            })
            client = app.test_client()

            route = client.post(f"/api/documents/{document_id}/route", json={}).get_json()
            prepared = client.post(
                f"/api/routing/{route['routingAttemptId']}/export-attempt",
                json={"actor": "test"},
            )
            approved = client.post(
                f"/api/export-attempts/{prepared.get_json()['exportAttemptId']}/approve",
                json={"confirmation": EXPORT_APPROVAL_PHRASE, "actor": "test"},
            )
            wave_response = MagicMock()
            wave_response.raise_for_status.return_value = None
            wave_response.json.return_value = {
                "data": {
                    "moneyTransactionCreate": {
                        "didSucceed": True,
                        "inputErrors": [],
                        "transaction": {"id": "wave-api-transaction-1"},
                    }
                }
            }
            with patch(
                "src.data_entry.waveapps_business_handler.requests.post",
                return_value=wave_response,
            ) as wave_post:
                executed = client.post(
                    f"/api/export-attempts/{prepared.get_json()['exportAttemptId']}/execute",
                    json={},
                )

            self.assertEqual(prepared.status_code, 200)
            self.assertEqual(approved.status_code, 200)
            self.assertEqual(executed.status_code, 200)
            self.assertEqual(executed.get_json()["status"], "executed")
            self.assertEqual(executed.get_json()["externalSubmission"], "executed")
            wave_post.assert_called_once()
            request_input = wave_post.call_args.kwargs["json"]["variables"]["input"]
            self.assertEqual(request_input["businessId"], "business-1")
            self.assertEqual(request_input["externalId"], f"fab:{prepared.get_json()['operationId']}")
            self.assertEqual(request_input["anchor"]["accountId"], "bank-account-1")
            self.assertEqual(request_input["lineItems"][0]["accountId"], "expense-account-1")
            page = client.get("/").data.decode("utf-8")
            self.assertIn("wave-api-transaction-1", page)
            attempt = client.get(f"/api/export-attempts/{prepared.get_json()['exportAttemptId']}").get_json()
            self.assertEqual(attempt["external_submission"], "executed")
            self.assertEqual(attempt["external_id"], "wave-api-transaction-1")
            records = client.get("/api/bookkeeping-records").get_json()["bookkeepingRecords"]
            self.assertEqual(records[0]["status"], "routed")

    def test_dashboard_prepare_ready_export_attempts_shows_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-export-form",
                "originalFilename": "receipt.txt",
                "documentType": "vendor_invoice",
                "processingStatus": "reviewed",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
            })
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()
            client.post("/routing/prepare-ready")

            page = client.post("/exports/prepare-ready", follow_redirects=True)

            self.assertEqual(page.status_code, 200)
            html = page.data.decode("utf-8")
            self.assertIn("Export Attempts", html)
            self.assertIn("Last export preparation run", html)
            self.assertIn("approval_required", html)
            self.assertIn("APPROVE FAB EXPORT DRAFT", html)
            self.assertIn('class="export-table"', html)
            self.assertIn(f'href="/documents/{ledger.list_documents(limit=1)[0]["id"]}"', html)
            self.assertIn("Open bill", html)

    def test_api_runs_and_resolves_local_reconciliation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-reconcile-api",
                "originalFilename": "receipt.txt",
                "processingStatus": "processed",
                "vendorName": "Office Shop",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
            })
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()

            response = client.post("/api/reconciliation/run", json={
                "bankTransactions": [{
                    "id": "tx-api-1",
                    "date": "2026-06-28",
                    "amount": -42.5,
                    "description": "Office Shop",
                }]
            })

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["matchedCandidates"], 1)
            matches = client.get("/api/reconciliation").get_json()["reconciliationMatches"]
            self.assertEqual(matches[0]["status"], "candidate")
            self.assertEqual(matches[0]["document_id"], document_id)
            record = client.get("/api/bookkeeping-records").get_json()["bookkeepingRecords"][0]
            record_page = client.get(f"/bookkeeping-records/{record['id']}")
            record_html = record_page.data.decode("utf-8")
            self.assertEqual(record_page.status_code, 200)
            self.assertIn("Reconciliation Evidence", record_html)
            self.assertIn("tx-api-1", record_html)
            self.assertIn("Reconcile", record_html)
            self.assertIn("Reject match", record_html)
            self.assertIn(f"/reconciliation/{matches[0]['id']}/resolve", record_html)

            resolved = client.post(
                f"/api/reconciliation/{matches[0]['id']}/resolve",
                json={"status": "approved", "resolution": "Confirmed from API."},
            )

            self.assertEqual(resolved.status_code, 200)
            self.assertTrue(resolved.get_json()["success"])
            detail = client.get(f"/api/documents/{document_id}").get_json()
            self.assertEqual(detail["processing_status"], "processed")
            self.assertEqual(detail["reconciliation_status"], "reconciled")

    def test_dashboard_reconciliation_form_shows_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-reconcile-form",
                "originalFilename": "receipt.txt",
                "processingStatus": "processed",
                "vendorName": "Office Shop",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
            })
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()

            page = client.post(
                "/reconciliation/run",
                data={
                    "bankTransactionsJson": (
                        '[{"id":"tx-form-1","date":"2026-06-28","amount":-42.5,'
                        '"description":"Office Shop"}]'
                    )
                },
                follow_redirects=True,
            )

            self.assertEqual(page.status_code, 200)
            html = page.data.decode("utf-8")
            self.assertIn("Reconciliation", html)
            self.assertIn("Last reconciliation run", html)
            self.assertIn("tx-form-1", html)
            self.assertIn("candidate", html)

    def test_api_imports_lists_and_reconciles_persisted_bank_transactions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-bank-api",
                "originalFilename": "receipt.txt",
                "processingStatus": "processed",
                "vendorName": "Office Shop",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
            })
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()

            imported = client.post("/api/bank-transactions/import", json={
                "accountIdentifier": "wave-checking",
                "source": "wave_report",
                "filename": "account-transactions.json",
                "bankTransactions": [{
                    "id": "tx-bank-api-1",
                    "date": "2026-06-28",
                    "amount": -42.5,
                    "description": "Office Shop",
                }],
            })
            listed = client.get("/api/bank-transactions?accountIdentifier=wave-checking")
            reconciled = client.post("/api/reconciliation/run", json={})

            self.assertEqual(imported.status_code, 200)
            self.assertEqual(imported.get_json()["rowsImported"], 1)
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(listed.get_json()["bankTransactions"][0]["transaction_id"], "tx-bank-api-1")
            self.assertEqual(reconciled.status_code, 200)
            self.assertEqual(reconciled.get_json()["matchedCandidates"], 1)
            refreshed = client.get("/api/bank-transactions?accountIdentifier=wave-checking").get_json()["bankTransactions"][0]
            self.assertEqual(refreshed["reconciliation_status"], "candidate")

    def test_api_imports_bounded_bank_statement_file_with_actor_and_format_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()
            content = (
                "Datum;Omschrijving;Bedrag;Valuta\n"
                "28-06-2026;Transit fare;-12,50;EUR\n"
                "28-06-2026;Transit fare;-12,50;EUR\n"
            ).encode("utf-8")

            imported = client.post("/api/bank-transactions/import", json={
                "accountIdentifier": "nl-checking",
                "actor": "fab_dashboard:9",
                "contentBase64": base64.b64encode(content).decode("ascii"),
                "filename": "statement.csv",
                "format": "csv",
                "source": "dashboard_bank_statement",
            })
            mismatched = client.post("/api/bank-transactions/import", json={
                "contentBase64": base64.b64encode(content).decode("ascii"),
                "filename": "statement.json",
                "format": "csv",
            })
            malformed = client.post("/api/bank-transactions/import", json={
                "contentBase64": "not-base64",
                "filename": "statement.csv",
                "format": "csv",
            })
            non_object = client.post("/api/bank-transactions/import", json=[])

            self.assertEqual(imported.status_code, 200)
            self.assertEqual(imported.get_json()["rowsImported"], 2)
            self.assertEqual(imported.get_json()["externalSubmission"], "not_executed")
            self.assertEqual(mismatched.status_code, 400)
            self.assertEqual(malformed.status_code, 400)
            self.assertEqual(non_object.status_code, 400)
            self.assertEqual(non_object.get_json()["errorCode"], "invalid_request")
            self.assertFalse(non_object.get_json()["success"])
            ledger = LocalOperationsLedger(ledger_path)
            transactions = ledger.list_bank_transactions(account_identifier="nl-checking")
            self.assertEqual(len(transactions), 2)
            import_event = next(
                event
                for event in ledger.list_audit_events(limit=20)
                if event["action"] == "local_bank_transactions.import_completed"
            )
            self.assertEqual(import_event["details"]["actor"], "fab_dashboard:9")

    def test_api_review_ignore_closes_missing_receipt_bank_exception(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()

            client.post("/api/bank-transactions/import", json={
                "accountIdentifier": "wave-checking",
                "bankTransactions": [{
                    "id": "tx-api-no-receipt",
                    "date": "2026-06-28",
                    "amount": -8.5,
                    "description": "Bank service fee",
                }],
            })
            client.post("/api/reconciliation/run", json={})
            page = client.get("/")
            review_item = client.get("/api/review?status=pending").get_json()["reviewItems"][0]

            resolved = client.post(
                f"/api/review/{review_item['id']}/resolve",
                json={"status": "ignored", "resolution": "No receipt required for bank fee."},
            )

            self.assertEqual(page.status_code, 200)
            html = page.data.decode("utf-8")
            self.assertIn("missing_receipt", html)
            self.assertIn("No receipt needed", html)
            bank_record = client.get("/api/bookkeeping-records?sourceType=bank_transaction").get_json()["bookkeepingRecords"][0]
            record_page = client.get(f"/bookkeeping-records/{bank_record['id']}")
            record_html = record_page.data.decode("utf-8")
            self.assertEqual(record_page.status_code, 200)
            self.assertIn("Reconciliation Evidence", record_html)
            self.assertIn("tx-api-no-receipt", record_html)
            self.assertIn("missing_receipt", record_html)
            self.assertIn("Bank service fee", record_html)
            self.assertEqual(resolved.status_code, 200)
            self.assertTrue(resolved.get_json()["success"])
            self.assertEqual(
                resolved.get_json()["reconciliationResolution"]["appliedReconciliationStatus"],
                "ignored",
            )
            matches = client.get("/api/reconciliation").get_json()["reconciliationMatches"]
            self.assertEqual(matches[0]["status"], "ignored")
            bank_transaction = client.get("/api/bank-transactions?accountIdentifier=wave-checking").get_json()["bankTransactions"][0]
            self.assertEqual(bank_transaction["reconciliation_status"], "ignored")

    def test_dashboard_bank_import_form_shows_imported_transactions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({"fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3")})
            client = app.test_client()

            page = client.post(
                "/bank-transactions/import",
                data={
                    "format": "json",
                    "accountIdentifier": "wave-checking",
                    "source": "wave_report",
                    "filename": "account-transactions.json",
                    "statementText": (
                        '[{"id":"tx-dashboard-bank","date":"2026-06-28","amount":-42.5,'
                        '"description":"Office Shop"}]'
                    ),
                },
                follow_redirects=True,
            )

            self.assertEqual(page.status_code, 200)
            html = page.data.decode("utf-8")
            self.assertIn("Bank Transactions", html)
            self.assertIn("Last bank import", html)
            self.assertIn("tx-dashboard-bank", html)
            self.assertIn("Unreconciled tx", html)

    def test_api_creates_lists_and_restores_local_backup_with_confirmation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            backup_dir = os.path.join(temp_dir, "backups")
            original_path = os.path.join(temp_dir, "original.pdf")
            original_bytes = b"source-complete API backup evidence"
            with open(original_path, "wb") as handle:
                handle.write(original_bytes)
            ledger = LocalOperationsLedger(ledger_path)
            original_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-api-backup",
                "originalFilename": "original.pdf",
                "storagePath": original_path,
                "contentSha256": hashlib.sha256(original_bytes).hexdigest(),
            })
            app = create_app({
                "fab_local_ledger_path": ledger_path,
                "fab_local_backup_dir": backup_dir,
            })
            client = app.test_client()

            created = client.post("/api/backups", json={
                "note": "api test",
                "actor": "test-operator",
            })
            self.assertEqual(created.status_code, 200)
            self.assertEqual(
                created.get_json()["manifest"]["sourceEvidence"]["coverageStatus"],
                "complete",
            )
            backup_path = created.get_json()["backupPath"]
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-after-backup",
                "originalFilename": "after.pdf",
            })
            blocked = client.post("/api/backups/restore", json={
                "backupPath": backup_path,
                "confirmation": "wrong",
            })
            maintenance_required = client.post("/api/backups/restore", json={
                "backupPath": backup_path,
                "confirmation": RESTORE_CONFIRMATION_PHRASE,
            })

            self.assertEqual(blocked.status_code, 400)
            self.assertEqual(blocked.get_json()["status"], "requires_confirmation")
            self.assertEqual(maintenance_required.status_code, 423)
            self.assertEqual(maintenance_required.get_json()["status"], "blocked")
            self.assertEqual(
                maintenance_required.get_json()["blockers"][0]["code"],
                "maintenance_required",
            )

            maintenance_client = create_app({
                "fab_local_ledger_path": ledger_path,
                "fab_local_backup_dir": backup_dir,
                "fab_maintenance_mode": True,
            }).test_client()
            mutation_locked = maintenance_client.post("/api/backups", json={})
            plan = maintenance_client.get(
                "/api/backups/restore-plan",
                query_string={"backupPath": backup_path},
            )
            restored = maintenance_client.post("/api/backups/restore", json={
                "backupPath": backup_path,
                "confirmation": RESTORE_CONFIRMATION_PHRASE,
            })

            self.assertEqual(mutation_locked.status_code, 423)
            self.assertEqual(mutation_locked.get_json()["status"], "maintenance_locked")
            self.assertEqual(plan.status_code, 200)
            self.assertTrue(plan.get_json()["canRestore"])
            self.assertEqual(restored.status_code, 200)
            self.assertTrue(restored.get_json()["success"])
            documents = maintenance_client.get("/api/documents").get_json()["documents"]
            self.assertEqual([document["id"] for document in documents], [original_id])
            live = maintenance_client.get("/api/live").get_json()
            self.assertTrue(live["maintenanceMode"])
            self.assertEqual(live["runtimeMode"], "maintenance")
            backups = maintenance_client.get("/api/backups").get_json()
            self.assertGreaterEqual(len(backups["backups"]), 2)
            self.assertIn(backups["schedule"]["status"], {"current", "due"})
            self.assertEqual(backups["verificationMode"], "deep")
            manifest_only = maintenance_client.get("/api/backups?verify=false").get_json()
            self.assertEqual(manifest_only["verificationMode"], "manifest_only")
            self.assertEqual(
                manifest_only["schedule"]["integrityVerification"],
                "manifest_only",
            )
            self.assertTrue(all(
                backup["status"] == "manifest_valid"
                for backup in manifest_only["backups"]
            ))

    def test_dashboard_backup_form_shows_backup_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            backup_dir = os.path.join(temp_dir, "backups")
            source_path = os.path.join(temp_dir, "receipt.pdf")
            source_bytes = b"source-complete dashboard backup evidence"
            with open(source_path, "wb") as handle:
                handle.write(source_bytes)
            LocalOperationsLedger(ledger_path).register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-dashboard-backup",
                "originalFilename": "receipt.pdf",
                "storagePath": source_path,
                "contentSha256": hashlib.sha256(source_bytes).hexdigest(),
            })
            app = create_app({
                "fab_local_ledger_path": ledger_path,
                "fab_local_backup_dir": backup_dir,
            })
            client = app.test_client()

            page = client.post("/backups/create", follow_redirects=True)

            self.assertEqual(page.status_code, 200)
            html = page.data.decode("utf-8")
            self.assertIn("Backups", html)
            self.assertIn("Last backup action", html)
            self.assertIn("fab-recovery-package", html)
            self.assertIn(RESTORE_CONFIRMATION_PHRASE, html)

    def test_api_plans_and_executes_full_recovery_in_maintenance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            backup_dir = os.path.join(temp_dir, "backups")
            restore_root = os.path.join(temp_dir, "restored")
            original_path = os.path.join(temp_dir, "original.pdf")
            current_path = os.path.join(temp_dir, "current.pdf")
            original_bytes = b"API full recovery source"
            current_bytes = b"API current rollback source"
            with open(original_path, "wb") as handle:
                handle.write(original_bytes)
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "api-full-recovery",
                "originalFilename": "original.pdf",
                "storagePath": original_path,
                "contentSha256": hashlib.sha256(original_bytes).hexdigest(),
            })
            backup = LocalBackupService(ledger, {
                "fab_local_backup_dir": backup_dir,
            }).create_backup(require_complete_source_evidence=True)
            with open(current_path, "wb") as handle:
                handle.write(current_bytes)
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "api-full-recovery",
                "originalFilename": "current.pdf",
                "storagePath": current_path,
                "contentSha256": hashlib.sha256(current_bytes).hexdigest(),
            })
            client = create_app({
                "fab_local_ledger_path": ledger_path,
                "fab_local_backup_dir": backup_dir,
                "fab_backup_restore_source_root": restore_root,
                "fab_maintenance_mode": True,
            }).test_client()

            plan = client.get("/api/backups/restore-plan", query_string={
                "backupPath": backup["backupPath"],
                "restoreMode": RESTORE_MODE_FULL,
            })
            restored = client.post("/api/backups/restore", json={
                "backupPath": backup["backupPath"],
                "restoreMode": RESTORE_MODE_FULL,
                "confirmation": FULL_RESTORE_CONFIRMATION_PHRASE,
                "actor": "api-recovery-test",
            })

            self.assertEqual(plan.status_code, 200)
            self.assertTrue(plan.get_json()["canRestore"])
            self.assertEqual(plan.get_json()["sourceRestoreTargetStatus"], "ready")
            self.assertEqual(restored.status_code, 200)
            self.assertTrue(restored.get_json()["sourceTargetCreated"])
            document = ledger.get_document(document_id)
            self.assertTrue(str(document["storage_path"]).startswith(restore_root))
            with open(document["storage_path"], "rb") as handle:
                self.assertEqual(handle.read(), original_bytes)

    def test_maintenance_dashboard_exposes_full_recovery_only_for_complete_package(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            backup_dir = os.path.join(temp_dir, "backups")
            source_path = os.path.join(temp_dir, "receipt.pdf")
            source_bytes = b"maintenance dashboard recovery evidence"
            with open(source_path, "wb") as handle:
                handle.write(source_bytes)
            ledger = LocalOperationsLedger(ledger_path)
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "dashboard-full-recovery",
                "originalFilename": "receipt.pdf",
                "storagePath": source_path,
                "contentSha256": hashlib.sha256(source_bytes).hexdigest(),
            })
            LocalBackupService(ledger, {
                "fab_local_backup_dir": backup_dir,
            }).create_backup(require_complete_source_evidence=True)
            client = create_app({
                "fab_local_ledger_path": ledger_path,
                "fab_local_backup_dir": backup_dir,
                "fab_maintenance_mode": True,
            }).test_client()

            page = client.get("/")
            html = page.data.decode("utf-8")

            self.assertEqual(page.status_code, 200)
            self.assertIn("Maintenance mode is local-only", html)
            self.assertIn("Restore ledger", html)
            self.assertIn("Restore ledger and files", html)
            self.assertIn(FULL_RESTORE_CONFIRMATION_PHRASE, html)
            self.assertIn(f'value="{RESTORE_MODE_FULL}"', html)

    def test_api_blocks_strict_backup_when_source_evidence_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            backup_dir = os.path.join(temp_dir, "backups")
            LocalOperationsLedger(ledger_path).register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-missing-backup-source",
                "originalFilename": "missing.pdf",
            })
            client = create_app({
                "fab_local_ledger_path": ledger_path,
                "fab_local_backup_dir": backup_dir,
            }).test_client()

            response = client.post("/api/backups", json={
                "actor": "test-operator",
                "requireCompleteSourceEvidence": True,
            })

            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.get_json()["status"], "blocked")
            self.assertIn("Source-complete backup blocked", response.get_json()["error"])
            self.assertEqual(os.listdir(backup_dir), [])

    def test_api_rescans_configured_intake_folder(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            receipt_path = os.path.join(intake_dir, "receipt.pdf")
            with open(receipt_path, "wb") as handle:
                handle.write(b"receipt bytes")

            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_intake_paths": intake_dir,
                "fab_local_intake_extensions": "pdf,png",
            })
            client = app.test_client()

            response = client.post("/api/intake/rescan")

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["scanned"], 1)
            self.assertEqual(payload["registered"], 1)
            self.assertEqual(payload["duplicates"], 0)
            documents = client.get("/api/documents").get_json()["documents"]
            self.assertEqual(documents[0]["original_filename"], "receipt.pdf")
            self.assertEqual(documents[0]["source"], "local_folder")
            self.assertIsNotNone(documents[0]["source_account_id"])
            sources = client.get("/api/sources").get_json()["sources"]
            self.assertEqual(sources[0]["source_type"], "local_folder")
            self.assertEqual(sources[0]["documents_seen"], 1)
            self.assertEqual(sources[0]["documents_imported"], 1)

    def test_api_uploads_and_registers_a_local_intake_document(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            intake_dir = os.path.join(temp_dir, "sort-out")
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_intake_paths": intake_dir,
                "fab_local_intake_extensions": "pdf,png",
            })
            client = app.test_client()

            response = client.post("/api/intake/upload", json={
                "filename": "../receipt.pdf",
                "contentBase64": base64.b64encode(b"receipt bytes").decode("ascii"),
            })

            self.assertEqual(response.status_code, 201)
            payload = response.get_json()
            self.assertTrue(payload["success"])
            self.assertEqual(payload["filename"], "receipt.pdf")
            self.assertEqual(payload["externalSubmission"], "not_executed")
            self.assertTrue(os.path.isfile(os.path.join(intake_dir, "receipt.pdf")))
            documents = client.get("/api/documents").get_json()["documents"]
            self.assertEqual(len(documents), 1)
            self.assertEqual(documents[0]["processing_status"], "imported")

    def test_api_upload_retries_reuse_the_existing_file_and_ledger_document(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            intake_dir = os.path.join(temp_dir, "sort-out")
            client = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_intake_paths": intake_dir,
                "fab_local_intake_extensions": "pdf",
            }).test_client()
            payload = {
                "filename": "receipt.pdf",
                "contentBase64": base64.b64encode(b"same receipt bytes").decode("ascii"),
            }

            first = client.post("/api/intake/upload", json=payload)
            retry = client.post("/api/intake/upload", json=payload)

            self.assertEqual(first.status_code, 201)
            self.assertEqual(retry.status_code, 200)
            self.assertTrue(retry.get_json()["idempotentReplay"])
            self.assertEqual(
                retry.get_json()["document"]["id"],
                first.get_json()["document"]["id"],
            )
            self.assertEqual(os.listdir(intake_dir), ["receipt.pdf"])
            self.assertEqual(len(client.get("/api/documents").get_json()["documents"]), 1)

            changed = {
                **payload,
                "contentBase64": base64.b64encode(b"updated receipt bytes").decode("ascii"),
            }
            second_document = client.post("/api/intake/upload", json=changed)
            second_retry = client.post("/api/intake/upload", json=changed)
            source = client.get("/api/sources").get_json()["sources"][0]
            changed_filename = (
                "receipt-"
                + hashlib.sha256(b"updated receipt bytes").hexdigest()[:12]
                + ".pdf"
            )

            self.assertEqual(second_document.status_code, 201)
            self.assertEqual(second_retry.status_code, 200)
            self.assertTrue(second_retry.get_json()["idempotentReplay"])
            self.assertEqual(sorted(os.listdir(intake_dir)), sorted(["receipt.pdf", changed_filename]))
            self.assertEqual(len(client.get("/api/documents").get_json()["documents"]), 2)
            self.assertEqual(source["documents_seen"], 2)
            self.assertEqual(source["documents_imported"], 2)
            self.assertIsNone(source["last_scan_at"])

    def test_api_upload_registers_one_file_without_rescanning_intake_folder(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            intake_dir = os.path.join(temp_dir, "sort-out")
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_intake_paths": intake_dir,
                "fab_local_intake_extensions": "pdf",
            })
            client = app.test_client()
            with patch.object(LocalFolderIntake, "rescan", side_effect=AssertionError("full folder scan")):
                response = client.post("/api/intake/upload", json={
                    "filename": "receipt.pdf",
                    "contentBase64": base64.b64encode(b"receipt bytes").decode("ascii"),
                })

            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.get_json()["document"]["status"], "imported")

    def test_api_upload_does_not_claim_success_without_confirmed_ledger_document(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            intake_dir = os.path.join(temp_dir, "sort-out")
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_intake_paths": intake_dir,
                "fab_local_intake_extensions": "pdf",
            })
            with patch.object(LocalFolderIntake, "register_local_file", return_value={"status": "skipped"}):
                response = app.test_client().post("/api/intake/upload", json={
                    "filename": "receipt.pdf",
                    "contentBase64": base64.b64encode(b"receipt bytes").decode("ascii"),
                })

            self.assertEqual(response.status_code, 503)
            self.assertFalse(response.get_json()["success"])
            self.assertEqual(response.get_json()["status"], "stored_not_registered")
            self.assertTrue(os.path.isfile(os.path.join(intake_dir, "receipt.pdf")))

    def test_api_rejects_invalid_or_oversized_local_intake_uploads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_intake_paths": os.path.join(temp_dir, "sort-out"),
                "fab_local_intake_extensions": "pdf",
                "fab_local_upload_max_bytes": 4,
            })
            client = app.test_client()

            unsupported = client.post("/api/intake/upload", json={
                "filename": "receipt.exe",
                "contentBase64": base64.b64encode(b"x").decode("ascii"),
            })
            invalid = client.post("/api/intake/upload", json={
                "filename": "receipt.pdf",
                "contentBase64": "not-base64",
            })
            oversized = client.post("/api/intake/upload", json={
                "filename": "receipt.pdf",
                "contentBase64": base64.b64encode(b"12345").decode("ascii"),
            })

            self.assertEqual(unsupported.status_code, 400)
            self.assertEqual(invalid.status_code, 400)
            self.assertEqual(oversized.status_code, 413)

    def test_source_connector_readiness_sync_api_and_dashboard_form(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()
            sync_result = {
                "success": True,
                "status": "completed",
                "workflowRunId": 7,
                "summary": {
                    "sources": 1,
                    "seen": 1,
                    "registered": 1,
                    "duplicates": 0,
                    "revisions": 0,
                    "alreadyRegistered": 0,
                    "skipped": 0,
                    "failedSources": 0,
                },
                "results": [{
                    "source": "gmail",
                    "status": "ready",
                    "registered": 1,
                    "duplicates": 0,
                    "revisions": 0,
                }],
                "externalSubmission": "not_executed",
            }

            readiness = client.get("/api/sources/readiness")
            with patch(
                "src.operations.local_api.LocalConnectorIntakeService.sync",
                return_value=sync_result,
            ) as sync:
                api_response = client.post(
                    "/api/sources/sync",
                    json={"sources": ["gmail"], "actor": "tester"},
                )
                page = client.post("/sources/sync", follow_redirects=True)

            self.assertEqual(readiness.status_code, 200)
            photos = next(
                item for item in readiness.get_json()["sources"]
                if item["source"] == "google_photos"
            )
            self.assertEqual(photos["mode"], "picker_required")
            self.assertEqual(api_response.status_code, 200)
            self.assertEqual(api_response.get_json()["externalSubmission"], "not_executed")
            self.assertEqual(sync.call_args_list[0].kwargs["sources"], ["gmail"])
            html = page.data.decode("utf-8")
            self.assertIn("Sync configured sources", html)
            self.assertIn("Google Photos Picker", html)
            self.assertIn("&#34;workflowRunId&#34;: 7", html)

    def test_google_photos_picker_sessions_api_and_dashboard_controls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            workflow_run_id = ledger.create_workflow_run({
                "status": "awaiting_user_selection",
                "triggerSource": "google_photos_picker",
                "metadata": {
                    "sourceAccountId": 1,
                    "providerSessionId": "picker-session-1",
                    "pickerUri": "https://photos.google.com/picker/picker-session-1/autoclose",
                    "providerSessionDeleted": False,
                    "externalSubmission": "not_executed",
                },
            })
            app = create_app({"fab_local_ledger_path": ledger_path})
            client = app.test_client()
            collected_result = {
                "success": True,
                "status": "completed",
                "session": {
                    "id": workflow_run_id,
                    "status": "completed",
                    "selectedItemCount": 1,
                    "providerSessionDeleted": True,
                },
                "summary": {"registered": 1},
                "externalSubmission": "not_executed",
            }
            cancelled_result = {
                "success": True,
                "status": "cancelled",
                "session": {
                    "id": workflow_run_id,
                    "status": "cancelled",
                    "selectedItemCount": 0,
                    "providerSessionDeleted": True,
                },
                "externalSubmission": "not_executed",
            }

            sessions = client.get("/api/sources/google-photos/sessions")
            detail = client.get(f"/api/sources/google-photos/sessions/{workflow_run_id}")
            with patch(
                "src.operations.local_api.LocalGooglePhotosPickerService.collect_session",
                return_value=collected_result,
            ) as collect:
                collected = client.post(
                    f"/api/sources/google-photos/sessions/{workflow_run_id}/collect",
                    json={"actor": "tester"},
                )
            with patch(
                "src.operations.local_api.LocalGooglePhotosPickerService.cancel_session",
                return_value=cancelled_result,
            ) as cancel:
                cancelled = client.post(
                    f"/api/sources/google-photos/sessions/{workflow_run_id}/cancel",
                    json={"actor": "tester"},
                )
            page = client.get("/")

            self.assertEqual(sessions.status_code, 200)
            self.assertEqual(sessions.get_json()["sessions"][0]["id"], workflow_run_id)
            self.assertEqual(detail.get_json()["session"]["providerSessionId"], "picker-session-1")
            self.assertEqual(collected.status_code, 200)
            self.assertEqual(cancelled.status_code, 200)
            self.assertEqual(collect.call_args.kwargs["actor"], "tester")
            self.assertEqual(cancel.call_args.kwargs["actor"], "tester")
            html = page.data.decode("utf-8")
            self.assertIn("Google Photos selections", html)
            self.assertIn("Open selection", html)
            self.assertIn("Check &amp; import", html)
            self.assertIn("picker-session-1", html)

            missing = client.get("/api/sources/google-photos/sessions/999999")
            self.assertEqual(missing.status_code, 404)

    def test_dashboard_can_cancel_interrupted_picker_creation_without_provider_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            workflow_run_id = ledger.create_workflow_run({
                "status": "creating",
                "triggerSource": "google_photos_picker",
                "metadata": {"externalSubmission": "not_executed"},
            })
            client = create_app({"fab_local_ledger_path": ledger_path}).test_client()

            html = client.get("/").data.decode("utf-8")

            self.assertIn(
                f'/sources/google-photos/sessions/{workflow_run_id}/cancel',
                html,
            )
            self.assertNotIn(
                f'/sources/google-photos/sessions/{workflow_run_id}/collect',
                html,
            )

    def test_api_exposes_duplicate_candidates_from_intake(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            with open(os.path.join(intake_dir, "receipt-a.pdf"), "wb") as handle:
                handle.write(b"same receipt bytes")
            with open(os.path.join(intake_dir, "receipt-copy.pdf"), "wb") as handle:
                handle.write(b"same receipt bytes")
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_intake_paths": intake_dir,
                "fab_local_intake_extensions": "pdf",
            })
            client = app.test_client()

            response = client.post("/api/intake/rescan")
            duplicates = client.get("/api/duplicates?status=pending").get_json()["duplicateCandidates"]
            dashboard = client.get("/api/dashboard").get_json()
            page = client.get("/")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["duplicates"], 1)
            self.assertEqual(len(duplicates), 1)
            self.assertEqual(duplicates[0]["match_type"], "exact_content_hash")
            self.assertEqual(dashboard["duplicate_candidates"], 1)
            self.assertEqual(dashboard["open_duplicate_candidates"], 1)
            self.assertIn("Duplicate Candidates", page.data.decode("utf-8"))

    def test_api_detects_and_exposes_document_groups(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            intake_dir = os.path.join(temp_dir, "scanner")
            os.makedirs(intake_dir)
            with open(os.path.join(intake_dir, "health_invoice_page_001.pdf"), "wb") as handle:
                handle.write(b"page one")
            with open(os.path.join(intake_dir, "health_invoice_page_002.pdf"), "wb") as handle:
                handle.write(b"page two")
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_intake_paths": intake_dir,
                "fab_local_intake_extensions": "pdf",
            })
            client = app.test_client()
            client.post("/api/intake/rescan")

            detected = client.post("/api/document-groups/detect", json={"limit": 25})
            groups = client.get("/api/document-groups?status=candidate").get_json()["documentGroups"]
            dashboard = client.get("/api/dashboard").get_json()
            page = client.get("/")

            self.assertEqual(detected.status_code, 200)
            self.assertEqual(detected.get_json()["groupsCreated"], 1)
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0]["member_count"], 2)
            self.assertEqual(dashboard["document_groups"], 1)
            self.assertEqual(dashboard["open_document_groups"], 1)
            self.assertIn("Document Groups", page.data.decode("utf-8"))

            split = client.post(
                f"/api/document-groups/{groups[0]['id']}/split",
                json={"documentId": groups[0]["members"][1]["document_id"], "reason": "Wrong page."},
            )

            self.assertEqual(split.status_code, 200)
            self.assertEqual(split.get_json()["status"], "split")

    def test_dashboard_rescan_form_imports_documents_and_shows_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            with open(os.path.join(intake_dir, "receipt.pdf"), "wb") as handle:
                handle.write(b"receipt bytes")

            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_intake_paths": intake_dir,
            })
            client = app.test_client()

            page = client.post("/intake/rescan", follow_redirects=True)

            self.assertEqual(page.status_code, 200)
            html = page.data.decode("utf-8")
            self.assertIn("Folder Intake", html)
            self.assertIn("Sources", html)
            self.assertIn("receipt.pdf", html)
            self.assertIn("Already in ledger", html)
            self.assertIn("local_intake.document_imported", html)

    def test_api_processes_imported_text_document(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = os.path.join(temp_dir, "receipt.txt")
            with open(receipt_path, "w", encoding="utf-8") as handle:
                handle.write("Vendor: Test Vendor\nDate: 2026-06-28\nTotal: EUR 42.50\nOffice supplies\n")
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "local_folder",
                "sourceDocumentId": "receipt-text-1",
                "originalFilename": "receipt.txt",
                "mimeType": "text/plain",
                "storagePath": receipt_path,
                "documentType": "text",
                "processingStatus": "imported",
            })
            app = create_app({
                "fab_local_ledger_path": ledger_path,
                "categorization_rules": {
                    "Office Supplies": {
                        "keywords": ["office supplies"],
                        "vendors": ["test vendor"],
                    }
                },
            })
            client = app.test_client()

            response = client.post(f"/api/documents/{document_id}/process")

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["status"], "processed")
            detail = client.get(f"/api/documents/{document_id}").get_json()
            self.assertEqual(detail["processing_status"], "processed")
            self.assertEqual(detail["vendor_name"], "Test Vendor")
            self.assertEqual(detail["category"], "Office Supplies")
            self.assertEqual(detail["total_amount"], 42.5)
            self.assertEqual(detail["extracted_fields"][0]["document_id"], document_id)
            fields = client.get(f"/api/extracted-fields?documentId={document_id}").get_json()["extractedFields"]
            field_names = {field["field_name"] for field in fields}
            self.assertIn("vendor_name", field_names)
            self.assertIn("total_amount", field_names)
            self.assertEqual(detail["audit_events"][0]["action"], "local_processing.document_processed")

    def test_api_retries_failed_text_document_processing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = os.path.join(temp_dir, "receipt.txt")
            with open(receipt_path, "w", encoding="utf-8") as handle:
                handle.write("Vendor: Retry Vendor\nDate: 2026-06-28\nTotal: EUR 42.50\nOffice supplies\n")
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "local_folder",
                "sourceDocumentId": "receipt-retry-1",
                "originalFilename": "receipt.txt",
                "mimeType": "text/plain",
                "storagePath": receipt_path,
                "documentType": "text",
                "processingStatus": "failed",
                "metadata": {"processingError": "Previous OCR failure"},
            })
            review_id = ledger.create_review_item({
                "documentId": document_id,
                "reason": "processing_failed",
                "details": "Previous OCR failure",
            })
            app = create_app({
                "fab_local_ledger_path": ledger_path,
                "categorization_rules": {
                    "Office Supplies": {
                        "keywords": ["office supplies"],
                        "vendors": ["retry vendor"],
                    }
                },
            })
            client = app.test_client()

            dashboard_before = client.get("/").data.decode("utf-8")
            response = client.post(f"/api/documents/{document_id}/retry-processing", json={"actor": "tester"})

            self.assertIn("Retry", dashboard_before)
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["status"], "processed")
            self.assertTrue(payload["retry"])
            self.assertEqual(payload["retryCount"], 1)
            detail = client.get(f"/api/documents/{document_id}").get_json()
            self.assertEqual(detail["processing_status"], "processed")
            self.assertEqual(detail["vendor_name"], "Retry Vendor")
            self.assertEqual(detail["metadata"]["processing"]["retryCount"], 1)
            review = ledger.get_review_item(review_id)
            self.assertEqual(review["status"], "resolved")
            audit_actions = [event["action"] for event in client.get("/api/audit").get_json()["auditEvents"]]
            self.assertIn("local_processing.retry_started", audit_actions)
            self.assertIn("local_processing.processing_failed_review_resolved", audit_actions)

    def test_dashboard_process_imported_form_shows_processing_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            with open(os.path.join(intake_dir, "receipt.txt"), "w", encoding="utf-8") as handle:
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
            client.post("/intake/rescan")

            page = client.post("/documents/process-imported", follow_redirects=True)

            self.assertEqual(page.status_code, 200)
            html = page.data.decode("utf-8")
            self.assertIn("Last processing run", html)
            self.assertIn("Extracted Fields", html)
            self.assertIn("receipt.txt", html)
            self.assertIn("Office Supplies", html)
            self.assertIn("vendor_name", html)
            self.assertIn("processed", html)

    def test_api_reprocess_incomplete_is_bounded_and_actor_audited(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({"fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3")})
            client = app.test_client()
            expected = {
                "requested": 2,
                "reprocessed": 2,
                "ocrRecovered": 1,
                "externalSubmission": "not_executed",
            }
            with patch(
                "src.operations.local_api.LocalDocumentProcessor.reprocess_incomplete",
                return_value=expected,
            ) as reprocess:
                response = client.post("/api/documents/reprocess-incomplete", json={
                    "limit": 2,
                    "actor": "dashboard-operator",
                })

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), expected)
            reprocess.assert_called_once_with(limit=2, actor="dashboard-operator")

    def test_api_reprocess_review_queue_is_bounded_and_actor_audited(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({"fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3")})
            client = app.test_client()
            expected = {
                "requested": 3,
                "reprocessed": 3,
                "resolvedReviewItems": 2,
                "externalSubmission": "not_executed",
            }
            with patch(
                "src.operations.local_api.LocalDocumentProcessor.reprocess_review_queue",
                return_value=expected,
            ) as reprocess:
                response = client.post("/api/documents/reprocess-review-queue", json={
                    "limit": 3,
                    "actor": "dashboard-operator",
                })

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), expected)
            reprocess.assert_called_once_with(limit=3, actor="dashboard-operator")

    def test_api_exposes_wave_control_center_without_external_submission(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "waveapps_business_access_token": "secret-token",
                "waveapps_business_id": "business-id",
            })
            client = app.test_client()

            overview = client.get("/api/wave")
            reports = client.get("/api/wave/reports?section=detailed_reporting")
            actions = client.get("/api/wave/actions?surface=reports&safety=read_only")
            report_plan = client.post("/api/wave/reports/plan", json={
                "reportType": "account-transactions",
                "fromDate": "2026-06-28",
                "toDate": "2026-06-28",
                "accountOption": "-1",
                "contactOption": "0",
                "cashMode": "1",
            })
            action_plan = client.post("/api/wave/plan", json={
                "surface": "reports",
                "actionId": "report_table_read",
                "payload": {"reportType": "account-transactions"},
                "capabilityId": "ledger_report_reconciliation",
                "availableSignals": ["ledger_period", "account_scope", "reconciliation_status"],
                "confidence": 0.95,
            })
            workflow_plan = client.post("/api/wave/workflows/plan", json={
                "workflowId": "daily_reconciliation_run",
                "fromDate": "2026-06-28",
                "toDate": "2026-06-28",
            })
            snapshots = client.get("/api/wave/report-snapshots?reportType=account-transactions")
            report_controls = client.get("/api/wave/report-controls?workflowId=daily_reconciliation_run")
            table_snapshot = next(
                snapshot
                for snapshot in snapshots.get_json()["waveReportSnapshots"]
                if snapshot["action_id"] == "report_table_read"
            )
            result_capture = client.post("/api/wave/report-results", json={
                "snapshotId": table_snapshot["id"],
                "format": "csv",
                "accountIdentifier": "wave-checking",
                "importTransactions": True,
                "refreshBookkeepingRecords": True,
                "runReconciliation": True,
                "resultText": (
                    "Date;Description;Debit;Credit;Reference\n"
                    "2026-06-28;Office Shop;42,50;;wave-1\n"
                    "2026-06-29;Client payment;;100,00;wave-2\n"
                ),
            })
            imported_bank_transactions = client.get("/api/bank-transactions?accountIdentifier=wave-checking")
            imported_records = client.get("/api/bookkeeping-records?sourceType=bank_transaction")
            operation_snapshots = client.get("/api/wave/operations?safety=read_only&surface=reports")

            overview_payload = overview.get_json()
            self.assertEqual(overview.status_code, 200)
            self.assertEqual(overview_payload["summary"]["reports"], 12)
            self.assertTrue(overview_payload["credentials"]["waveappsBusiness"]["accessTokenConfigured"])
            self.assertNotIn("secret-token", overview.data.decode("utf-8"))
            self.assertEqual(reports.get_json()["count"], 3)
            self.assertIn("account-transactions", {report["type"] for report in reports.get_json()["reports"]})
            self.assertGreater(actions.get_json()["count"], 0)
            self.assertEqual(report_plan.status_code, 200)
            self.assertEqual(report_plan.get_json()["status"], "planned")
            self.assertIsNotNone(report_plan.get_json()["waveReportSnapshotId"])
            self.assertIsNotNone(report_plan.get_json()["waveOperationSnapshotId"])
            self.assertEqual(action_plan.status_code, 200)
            self.assertEqual(action_plan.get_json()["status"], "planned")
            self.assertEqual(action_plan.get_json()["externalSubmission"], "not_executed")
            self.assertIsNotNone(action_plan.get_json()["waveOperationSnapshotId"])
            self.assertEqual(workflow_plan.status_code, 200)
            self.assertEqual(workflow_plan.get_json()["status"], "ready")
            self.assertEqual(workflow_plan.get_json()["externalSubmission"], "not_executed")
            self.assertGreater(workflow_plan.get_json()["waveReportSnapshots"]["snapshotCount"], 0)
            self.assertGreater(workflow_plan.get_json()["waveOperationSnapshots"]["snapshotCount"], 0)
            self.assertEqual(workflow_plan.get_json()["waveReportControls"]["status"], "ready_for_wave_read")
            self.assertGreaterEqual(len(snapshots.get_json()["waveReportSnapshots"]), 1)
            self.assertEqual(report_controls.status_code, 200)
            self.assertEqual(report_controls.get_json()["status"], "ready_for_wave_read")
            self.assertEqual(report_controls.get_json()["blockingCount"], 0)
            self.assertEqual(result_capture.status_code, 200)
            self.assertTrue(result_capture.get_json()["success"])
            self.assertEqual(result_capture.get_json()["waveReportControls"]["status"], "ready")
            self.assertEqual(result_capture.get_json()["waveReportSnapshot"]["row_count"], 2)
            self.assertEqual(result_capture.get_json()["waveReportSnapshot"]["total_debits"], 42.5)
            self.assertEqual(result_capture.get_json()["waveReportSnapshot"]["total_credits"], 100.0)
            self.assertEqual(result_capture.get_json()["bankTransactionImport"]["rowsImported"], 2)
            self.assertEqual(result_capture.get_json()["bookkeepingRecordRefresh"]["updated"], 2)
            self.assertEqual(result_capture.get_json()["reconciliation"]["missingReceipts"], 2)
            self.assertEqual(result_capture.get_json()["externalSubmission"], "not_executed")
            imported_payload = imported_bank_transactions.get_json()["bankTransactions"]
            self.assertEqual({transaction["transaction_id"] for transaction in imported_payload}, {"wave-1", "wave-2"})
            self.assertEqual(
                {transaction["reconciliation_status"] for transaction in imported_payload},
                {"missing_receipt"},
            )
            record_payload = imported_records.get_json()["bookkeepingRecords"]
            self.assertEqual({record["reconciliation_status"] for record in record_payload}, {"missing_receipt"})
            self.assertGreaterEqual(len(operation_snapshots.get_json()["waveOperationSnapshots"]), 1)
            audit_actions = [event["action"] for event in client.get("/api/audit").get_json()["auditEvents"]]
            self.assertIn("local_wave.report_plan_prepared", audit_actions)
            self.assertIn("local_wave.action_plan_prepared", audit_actions)
            self.assertIn("local_wave.workflow_plan_prepared", audit_actions)
            self.assertIn("local_wave.report_result_captured", audit_actions)

    @patch("src.data_entry.waveapps_account_discovery.requests.post")
    def test_api_discovers_and_persists_verified_wave_accounts(self, mock_post):
        reset_all_limiters()
        self.addCleanup(reset_all_limiters)
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": {
                "business": {
                    "id": "business-1",
                    "name": "FAB Test Business",
                    "accounts": {
                        "edges": [
                            {"node": {"id": "anchor-1", "name": "Checking", "subtype": {"name": "Cash and Bank", "value": "CASH_AND_BANK"}}},
                            {"node": {"id": "expense-1", "name": "Office Supplies", "subtype": {"name": "Expense", "value": "EXPENSE"}}},
                        ],
                    },
                },
            },
        }
        mock_post.return_value = response
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "waveapps_business_access_token": "business-secret-token",
                "waveapps_business_id": "business-1",
                "waveapps_business_anchor_account_id": "anchor-1",
                "waveapps_business_category_account_ids": {"Office Supplies": "expense-1"},
            })
            client = app.test_client()

            mappings = client.get("/api/wave/account-mappings")
            discovery = client.post("/api/wave/accounts/discover", json={"targetSystem": "waveapps_business"})
            snapshots = client.get("/api/wave/operations?actionId=chart_account_list_read")
            audit = client.get("/api/audit")
            dashboard = client.get("/")

            self.assertEqual(mappings.status_code, 200)
            self.assertTrue(mappings.get_json()["targets"][0]["configured"])
            self.assertEqual(discovery.status_code, 200)
            result = discovery.get_json()
            self.assertTrue(result["mapping"]["verified"])
            self.assertEqual(result["accounts"][0]["id"], "anchor-1")
            self.assertNotIn("business-secret-token", discovery.data.decode("utf-8"))
            self.assertEqual(len(snapshots.get_json()["waveOperationSnapshots"]), 1)
            self.assertEqual(
                snapshots.get_json()["waveOperationSnapshots"][0]["metadata"]["accountDiscovery"]["accounts"][0]["id"],
                "anchor-1",
            )
            self.assertIn(
                "local_wave.account_discovery_read",
                [event["action"] for event in audit.get_json()["auditEvents"]],
            )
            self.assertIn("Verified account mappings", dashboard.data.decode("utf-8"))
            self.assertIn("anchor-1", dashboard.data.decode("utf-8"))
            self.assertIn("/wave/accounts/discover", dashboard.data.decode("utf-8"))

    @patch("src.data_entry.waveapps_entity_sync.requests.post")
    def test_api_syncs_and_exposes_wave_entity_mirror(self, mock_post):
        reset_all_limiters()
        set_rate_limiter(
            "waveapps",
            limiter=RateLimiter(calls_per_second=100, calls_per_day=1000, name="WaveApps"),
        )
        self.addCleanup(reset_all_limiters)

        def response_for_request(*args, **kwargs):
            query = kwargs["json"]["query"]
            if "customers(" in query:
                collection = "customers"
                nodes = [{"id": "customer-1", "name": "Acme", "email": "billing@acme.test", "currency": {"code": "EUR"}}]
            elif "products(" in query:
                collection = "products"
                nodes = [{"id": "product-1", "name": "Consulting", "unitPrice": "125.00", "isArchived": False}]
            else:
                collection = "invoices"
                nodes = [{
                    "id": "invoice-1",
                    "invoiceNumber": "INV-1",
                    "status": "DRAFT",
                    "invoiceDate": "2026-07-10",
                    "dueDate": "2026-08-09",
                    "currency": {"code": "EUR"},
                    "total": {"value": "250.00"},
                }]
            response = MagicMock()
            response.raise_for_status.return_value = None
            response.json.return_value = {
                "data": {
                    "business": {
                        "id": "business-1",
                        collection: {
                            "pageInfo": {"currentPage": 1, "totalPages": 1, "totalCount": 1},
                            "edges": [{"node": node} for node in nodes],
                        },
                    }
                }
            }
            return response

        mock_post.side_effect = response_for_request
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "waveapps_business_access_token": "business-secret-token",
                "waveapps_business_id": "business-1",
                "wave_entity_sync_max_wait_seconds": 0,
            })
            client = app.test_client()

            synced = client.post("/api/wave/entities/sync", json={
                "targetSystem": "waveapps_business",
                "entityTypes": ["customer", "product", "invoice"],
            })
            customers = client.get("/api/wave/entities?entityType=customer")
            runs = client.get("/api/wave/entity-sync-runs")
            overview = client.get("/api/wave")
            dashboard = client.get("/")

            self.assertEqual(synced.status_code, 200)
            self.assertTrue(synced.get_json()["success"])
            self.assertEqual(synced.get_json()["entitiesSeen"], 3)
            self.assertEqual(len(customers.get_json()["waveEntities"]), 1)
            self.assertEqual(customers.get_json()["waveEntities"][0]["external_id"], "customer-1")
            self.assertEqual(runs.get_json()["waveSyncRuns"][0]["status"], "completed")
            self.assertEqual(overview.get_json()["entityMirror"]["entityCount"], 3)
            self.assertEqual(overview.get_json()["entityMirror"]["status"], "ready")
            self.assertNotIn("business-secret-token", synced.data.decode("utf-8"))
            html = dashboard.data.decode("utf-8")
            self.assertIn("Wave entity mirror", html)
            self.assertIn("Sync Wave records", html)
            self.assertIn("INV-1", html)
            self.assertIn("customer-1", html)
            audit_actions = [event["action"] for event in client.get("/api/audit").get_json()["auditEvents"]]
            self.assertIn("local_wave.entity_sync_completed", audit_actions)

    def test_api_prepares_lists_and_inspects_close_pack_after_wave_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_close_pack_dir": os.path.join(temp_dir, "close-packs"),
            })
            client = app.test_client()

            client.post("/api/wave/workflows/plan", json={
                "workflowId": "daily_reconciliation_run",
                "fromDate": "2026-06-28",
                "toDate": "2026-06-28",
            })
            snapshots = client.get("/api/wave/report-snapshots?reportType=account-transactions")
            table_snapshot = next(
                snapshot
                for snapshot in snapshots.get_json()["waveReportSnapshots"]
                if snapshot["action_id"] == "report_table_read"
            )
            client.post("/api/wave/report-results", json={
                "snapshotId": table_snapshot["id"],
                "result": {
                    "rowCount": 0,
                    "totalDebits": 0,
                    "totalCredits": 0,
                },
            })

            prepared = client.post("/api/close-packs", json={
                "workflowId": "daily_reconciliation_run",
                "fromDate": "2026-06-28",
                "toDate": "2026-06-28",
            })
            listed = client.get("/api/close-packs")
            filename = prepared.get_json()["closePackFilename"]
            inspected = client.get(f"/api/close-packs/inspect?closePackFilename={filename}")

            self.assertEqual(prepared.status_code, 200)
            self.assertTrue(os.path.exists(prepared.get_json()["closePackPath"]))
            self.assertEqual(prepared.get_json()["status"], "prepared")
            self.assertEqual(prepared.get_json()["manifest"]["externalSubmission"], "not_executed")
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(len(listed.get_json()["packs"]), 1)
            self.assertEqual(inspected.status_code, 200)
            self.assertEqual(inspected.get_json()["payload"]["closeReadiness"]["status"], "ready")
            self.assertEqual(inspected.get_json()["payload"]["externalSubmission"], "not_executed")
            page = client.get("/").data.decode("utf-8")
            self.assertIn("Prepare pack", page)
            self.assertIn(filename, page)
            audit_actions = [event["action"] for event in client.get("/api/audit").get_json()["auditEvents"]]
            self.assertIn("local_close_pack.prepared", audit_actions)

    def test_api_rejects_export_attempt_with_confirmation_and_dashboard_action(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-export-reject-api",
                "originalFilename": "receipt.txt",
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
            app = create_app({
                "fab_local_ledger_path": ledger_path,
                "mijngeldzaken_category_mapping": {"Personal": "Huishouden"},
            })
            client = app.test_client()
            route = client.post(f"/api/documents/{document_id}/route", json={"targetSystem": "mijngeldzaken"}).get_json()
            prepared = client.post(f"/api/routing/{route['routingAttemptId']}/export-attempt").get_json()
            export_id = prepared["exportAttemptId"]
            pending_dashboard = client.get("/").data.decode("utf-8")

            blocked = client.post(f"/api/export-attempts/{export_id}/reject", json={"confirmation": "wrong"})
            rejected = client.post(
                f"/api/export-attempts/{export_id}/reject",
                json={"confirmation": EXPORT_REJECTION_PHRASE, "actor": "tester"},
            )
            listed = client.get("/api/export-attempts").get_json()
            dashboard = client.get("/")

            self.assertEqual(blocked.status_code, 400)
            self.assertEqual(blocked.get_json()["status"], "requires_confirmation")
            self.assertEqual(rejected.status_code, 200)
            self.assertEqual(rejected.get_json()["externalSubmission"], "rejected_not_executed")
            self.assertEqual(listed["rejectionPhrase"], EXPORT_REJECTION_PHRASE)
            self.assertEqual(listed["exportAttempts"][0]["status"], "rejected")
            self.assertIn(EXPORT_REJECTION_PHRASE, pending_dashboard)
            html = dashboard.data.decode("utf-8")
            self.assertIn("rejected_not_executed", html)

    def test_api_runs_mijngeldzaken_export_into_supervised_result_workflow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            export_dir = os.path.join(temp_dir, "mijngeldzaken-exports")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-supervised-api",
                "originalFilename": "receipt.txt",
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
            app = create_app({
                "fab_local_ledger_path": ledger_path,
                "mijngeldzaken_export_dir": export_dir,
                "mijngeldzaken_category_mapping": {"Personal": "Huishouden"},
            })
            client = app.test_client()
            route = client.post(
                f"/api/documents/{document_id}/route",
                json={"targetSystem": "mijngeldzaken"},
            ).get_json()
            prepared = client.post(
                f"/api/routing/{route['routingAttemptId']}/export-attempt"
            ).get_json()
            export_id = prepared["exportAttemptId"]
            approved = client.post(
                f"/api/export-attempts/{export_id}/approve",
                json={"confirmation": EXPORT_APPROVAL_PHRASE, "actor": "tester"},
            )
            executed = client.post(f"/api/export-attempts/{export_id}/execute")
            dashboard_metrics = client.get("/api/dashboard").get_json()
            dashboard = client.get("/").get_data(as_text=True)

            self.assertEqual(approved.status_code, 200)
            self.assertEqual(executed.status_code, 200)
            self.assertEqual(executed.get_json()["status"], "supervision_required")
            self.assertTrue(os.path.isfile(executed.get_json()["artifact"]["path"]))
            self.assertEqual(dashboard_metrics["supervised_export_attempts"], 1)
            self.assertIn("Record supervised result", dashboard)
            review = ledger.list_review_items(document_id=document_id)[0]
            self.assertEqual(review["reason"], "mijngeldzaken_supervision_required")

            blocked_queued = client.post(
                f"/api/export-attempts/{export_id}/result",
                json={
                    "status": "queued",
                    "confirmation": EXPORT_RESULT_CONFIRMATION_PHRASE,
                },
            )

            recorded = client.post(
                f"/api/export-attempts/{export_id}/result",
                json={
                    "status": "executed",
                    "externalId": "mgz-api-confirmation",
                    "confirmation": EXPORT_RESULT_CONFIRMATION_PHRASE,
                    "result": {"source": "supervised-session"},
                },
            )

            self.assertEqual(blocked_queued.status_code, 400)
            self.assertEqual(blocked_queued.get_json()["status"], "invalid_supervised_result")
            self.assertEqual(recorded.status_code, 200)
            self.assertEqual(recorded.get_json()["status"], "executed")
            self.assertEqual(recorded.get_json()["resolvedReviewIds"], [review["id"]])
            self.assertEqual(ledger.get_review_item(review["id"])["status"], "resolved")
            self.assertEqual(client.get("/api/dashboard").get_json()["supervised_export_attempts"], 0)
            self.assertEqual(
                ledger.get_export_attempt(export_id)["external_id"],
                "mgz-api-confirmation",
            )

    def test_api_exposes_mijngeldzaken_master_ledger_control_center(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "mijngeldzaken_username": "user@example.test",
                "mijngeldzaken_password": "secret-password",
            })
            client = app.test_client()

            overview = client.get("/api/mijngeldzaken")
            actions = client.get("/api/mijngeldzaken/actions?surface=transactions")
            plan = client.post("/api/mijngeldzaken/plan", json={
                "surface": "transactions",
                "actionId": "transaction_import_prepare",
                "payload": {
                    "date": "2026-06-28",
                    "amount": 42.5,
                    "description": "Weekly groceries",
                    "category": "Huishouden",
                },
            })

            self.assertEqual(overview.status_code, 200)
            self.assertEqual(overview.get_json()["status"], "modeled")
            self.assertTrue(overview.get_json()["credentials"]["usernameConfigured"])
            self.assertTrue(overview.get_json()["credentials"]["passwordConfigured"])
            self.assertNotIn("secret-password", overview.data.decode("utf-8"))
            self.assertIn("fab_master_ledger_to_mijngeldzaken", overview.get_json()["syncContracts"])
            self.assertEqual(actions.status_code, 200)
            self.assertIn("transaction_import_prepare", {action["id"] for action in actions.get_json()["actions"]})
            self.assertEqual(plan.status_code, 200)
            self.assertEqual(plan.get_json()["status"], "planned")
            self.assertEqual(plan.get_json()["operation"]["safety"], "safe_draft")
            self.assertEqual(plan.get_json()["externalSubmission"], "not_executed")
            self.assertEqual(
                client.get("/api/audit").get_json()["auditEvents"][0]["action"],
                "local_mijngeldzaken.action_plan_prepared",
            )

    def test_dashboard_renders_wave_control_center_and_workflow_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({"fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3")})
            client = app.test_client()

            page = client.get("/")
            planned = client.post(
                "/wave/workflows/plan",
                data={
                    "workflowId": "daily_reconciliation_run",
                    "fromDate": "2026-06-28",
                    "toDate": "2026-06-28",
                },
                follow_redirects=True,
            )

            self.assertEqual(page.status_code, 200)
            html = page.data.decode("utf-8")
            self.assertIn("Wave Control Center", html)
            self.assertIn("Detailed reporting", html)
            self.assertIn("Account Transactions (General Ledger)", html)
            self.assertEqual(planned.status_code, 200)
            planned_html = planned.data.decode("utf-8")
            self.assertIn("Last Wave workflow plan", planned_html)
            self.assertIn("report_table_read", planned_html)
            self.assertIn("Report evidence", planned_html)
            self.assertIn("Report control gate", planned_html)
            self.assertIn("ready_for_wave_read", planned_html)
            self.assertIn("Operation evidence", planned_html)
            self.assertIn("waveReportSnapshots", planned_html)
            self.assertIn("not_executed", planned_html)

    def test_financial_reports_api_dashboard_csv_and_explicit_generation_audit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            ledger.upsert_bookkeeping_record({
                "documentId": 81,
                "sourceType": "document",
                "recordType": "expense",
                "status": "ready_to_route",
                "targetSystem": "waveapps",
                "targetAccount": "Office expenses",
                "vendorName": "Office Shop",
                "category": "Office",
                "recordDate": "2026-07-01",
                "amount": 121,
                "vatAmount": 21,
                "currency": "EUR",
                "reviewRequired": False,
                "reconciliationStatus": "reconciled",
            })
            app = create_app({
                "fab_local_ledger_path": ledger_path,
                "fab_local_report_dir": os.path.join(temp_dir, "reports"),
                "report_schedule_frequency": "monthly",
                "report_schedule_period_mode": "current_year_to_date",
            })
            client = app.test_client()

            response = client.get(
                "/api/reports?reportType=profit_and_loss&fromDate=2026-07-01&toDate=2026-07-31&includeRows=true"
            )
            report = response.get_json()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(report["reportType"], "profit_and_loss")
            self.assertEqual(report["report"]["byCurrency"][0]["expensesNet"], 100.0)
            self.assertEqual(report["externalSubmission"], "not_executed")
            self.assertFalse(any(
                event["action"] == "local_reporting.report_generated"
                for event in ledger.list_audit_events(limit=20)
            ))

            csv_response = client.get(
                "/api/reports?format=csv&fromDate=2026-07-01&toDate=2026-07-31"
            )
            self.assertEqual(csv_response.status_code, 200)
            self.assertEqual(csv_response.headers["X-FAB-External-Submission"], "not_executed")
            self.assertIn("recordId,recordDate,sourceType", csv_response.data.decode("utf-8"))

            generated = client.post("/api/reports", json={
                "reportType": "vat",
                "fromDate": "2026-07-01",
                "toDate": "2026-07-31",
                "actor": "test_api",
            })
            self.assertEqual(generated.status_code, 200)
            self.assertIsInstance(generated.get_json()["auditEventId"], int)
            self.assertEqual(
                ledger.list_audit_events(limit=1)[0]["action"],
                "local_reporting.report_generated",
            )

            schedule_before = client.get("/api/report-runs")
            self.assertEqual(schedule_before.status_code, 200)
            self.assertEqual(schedule_before.get_json()["scheduleStatus"]["status"], "due")
            scheduled = client.post("/api/report-runs/run-due", json={"actor": "test_api"})
            self.assertEqual(scheduled.status_code, 200)
            self.assertTrue(scheduled.get_json()["status"].startswith("prepared"))
            report_run_id = scheduled.get_json()["reportRun"]["id"]
            report_run = client.get(f"/api/report-runs/{report_run_id}")
            self.assertEqual(report_run.status_code, 200)
            self.assertEqual(report_run.get_json()["status"], "valid")
            artifact = client.get(f"/api/report-runs/{report_run_id}/artifact?format=json")
            self.assertEqual(artifact.status_code, 200)
            self.assertEqual(artifact.headers["X-FAB-External-Submission"], "not_executed")
            self.assertEqual(len(artifact.headers["X-FAB-Report-SHA256"]), 64)
            self.assertIn("fab-scheduled-financial-report-v1", artifact.data.decode("utf-8"))
            duplicate_schedule = client.post("/api/report-runs/run-due")
            self.assertEqual(duplicate_schedule.get_json()["status"], "already_generated")

            dashboard = client.get("/")
            self.assertEqual(dashboard.status_code, 200)
            html = dashboard.data.decode("utf-8")
            self.assertIn("Financial Reports", html)
            self.assertIn("Report completeness gates", html)
            self.assertIn("Scheduled report generation", html)
            self.assertIn("Run due schedule", html)
            self.assertIn("monthly:", html)
            self.assertIn("Revenue net", html)
            self.assertEqual(client.get("/api/reports?basis=guess").status_code, 400)

    def test_token_is_required_when_configured(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_api_token": "secret",
            })
            client = app.test_client()

            self.assertEqual(client.get("/api/health").status_code, 401)
            self.assertEqual(
                client.get("/api/health", headers={"Authorization": "Bearer secret"}).status_code,
                200,
            )

    def test_dashboard_token_login_sets_browser_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_api_token": "secret",
            })
            client = app.test_client()

            self.assertEqual(client.get("/").status_code, 302)
            bad_login = client.post("/login", data={"token": "wrong"})
            self.assertEqual(bad_login.status_code, 401)

            good_login = client.post("/login", data={"token": "secret"}, follow_redirects=True)
            self.assertEqual(good_login.status_code, 200)
            self.assertIn("FAB Operations", good_login.data.decode("utf-8"))
            self.assertEqual(client.get("/").status_code, 200)

    def test_dashboard_exposes_and_runs_due_governed_workflow_recovery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            workflow_run_id = ledger.create_workflow_run({
                "status": "failed",
                "triggerSource": "local_autonomous_cycle",
            })
            ledger.create_workflow_step({
                "workflowRunId": workflow_run_id,
                "stepKey": "rescan_intake",
                "stage": "intake",
                "status": "failed",
                "stepOrder": 1,
                "metadata": {"risk": "low", "mode": "safe_auto"},
            })
            app = create_app({
                "fab_local_ledger_path": ledger_path,
                "fab_local_intake_paths": [intake_dir],
                "fab_autonomy_ignore_health_blocks": True,
                "fab_workflow_recovery_base_delay_seconds": 0,
            })
            client = app.test_client()

            queue = client.get("/api/workflows/recovery")
            recovered = client.post(
                "/api/workflows/recovery/run-due",
                json={"actor": "test_api"},
            )
            dashboard = client.get("/")

            self.assertEqual(queue.status_code, 200)
            self.assertEqual(queue.get_json()["dueCount"], 1)
            self.assertEqual(recovered.status_code, 200)
            self.assertTrue(recovered.get_json()["success"])
            self.assertEqual(recovered.get_json()["attempted"], 1)
            self.assertIn("Recovery policy", dashboard.data.decode("utf-8"))
            self.assertIn("Run due safe recovery", dashboard.data.decode("utf-8"))

    def test_authenticated_dashboard_rejects_cross_origin_mutations_and_disables_caching(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_api_token": "secret",
                "fab_local_api_base_url": "https://fab.example.test",
            })
            client = app.test_client()
            login = client.post(
                "/login",
                base_url="https://fab.example.test",
                data={"token": "secret"},
            )

            rejected = client.post(
                "/api/workflows/recovery/run-due",
                base_url="https://fab.example.test",
                headers={
                    "Origin": "https://attacker.example",
                    "Sec-Fetch-Site": "cross-site",
                },
            )
            allowed = client.post(
                "/api/workflows/recovery/run-due",
                base_url="https://fab.example.test",
                headers={
                    "Origin": "https://fab.example.test",
                    "Sec-Fetch-Site": "cross-site",
                },
            )
            configured_proxy_origin = client.post(
                "/api/workflows/recovery/run-due",
                headers={
                    "Origin": "https://fab.example.test",
                    "Sec-Fetch-Site": "same-origin",
                    "Authorization": "Bearer secret",
                },
            )
            dashboard = client.get("/", base_url="https://fab.example.test")

            self.assertEqual(rejected.status_code, 403)
            self.assertEqual(allowed.status_code, 200)
            self.assertEqual(configured_proxy_origin.status_code, 200)
            self.assertIn("Secure", login.headers["Set-Cookie"])
            self.assertEqual(dashboard.headers["Cache-Control"], "no-store, max-age=0")
            self.assertEqual(dashboard.headers["X-Frame-Options"], "DENY")
            self.assertIn("frame-ancestors 'none'", dashboard.headers["Content-Security-Policy"])

    def test_api_error_envelopes_redact_provider_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
            })
            client = app.test_client()
            provider_error = RuntimeError(
                "Wave failed at https://operator:url-secret@example.test/graphql?access_token=query-secret "
                "Authorization: Bearer bearer-secret Cookie: session=cookie-secret"
            )

            with patch(
                "src.operations.local_api.LocalBackupService.create_backup",
                side_effect=provider_error,
            ):
                response = client.post("/api/backups", json={})

            serialized = json.dumps(response.get_json())
            self.assertEqual(response.status_code, 500)
            self.assertIn("[REDACTED]", serialized)
            for secret in ("url-secret", "query-secret", "bearer-secret", "cookie-secret"):
                self.assertNotIn(secret, serialized)

    def test_loopback_mode_without_token_still_rejects_cross_origin_mutations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
            })
            client = app.test_client()

            response = client.post(
                "/api/workflows/recovery/run-due",
                headers={
                    "Origin": "https://attacker.example",
                    "Sec-Fetch-Site": "cross-site",
                },
            )

            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.get_json()["error"], "Cross-origin mutation rejected")

    def test_loopback_mode_rejects_untrusted_host_headers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
            })
            client = app.test_client()

            response = client.get(
                "/api/health",
                headers={"Host": "attacker.example"},
            )

            self.assertEqual(response.status_code, 421)
            self.assertEqual(
                response.get_json()["error"],
                "Untrusted host for loopback-only service",
            )

    def test_dashboard_session_allows_opaque_origin_forms_but_not_api_mutations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
            })
            client = app.test_client()

            unprimed = client.post(
                "/workflows/recovery/run-due",
                headers={"Origin": "null"},
            )
            client.get("/")
            form_response = client.post(
                "/workflows/recovery/run-due",
                data={"source": "dashboard"},
                headers={"Origin": "null"},
            )
            api_response = client.post(
                "/api/workflows/recovery/run-due",
                headers={"Origin": "null"},
            )

            self.assertEqual(unprimed.status_code, 403)
            self.assertEqual(form_response.status_code, 302)
            self.assertEqual(api_response.status_code, 403)

    def test_tokenless_apps_use_unpredictable_session_signing_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = {
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
            }

            first = create_app(config)
            second = create_app(config)

            self.assertNotEqual(first.secret_key, second.secret_key)

    def test_remote_host_requires_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                create_app({
                    "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                    "fab_local_api_host": "0.0.0.0",
                })


if __name__ == "__main__":
    unittest.main()
