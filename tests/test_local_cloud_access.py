import json
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request
from unittest.mock import MagicMock, patch

from src.operations.local_api import create_app
from src.operations.local_cloud_access import (
    LocalCloudAccessService,
    _fetch_inspector_json,
)
from src.utils.runtime_identity import local_instance_id


class TestLocalCloudAccessService(unittest.TestCase):
    def test_missing_runtime_is_truthfully_not_running(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = LocalCloudAccessService(
                project_root=root,
                api_token_configured=True,
                inspector_fetch=lambda _url, _timeout: {"tunnels": []},
            ).summarize()

        self.assertEqual(result["status"], "not_running")
        self.assertFalse(result["active"])
        self.assertTrue(result["configured"])
        self.assertIsNone(result["publicUrl"])
        self.assertEqual(result["externalSubmission"], "not_executed")

    def test_existing_shared_agent_reports_separate_endpoint_requirement(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = LocalCloudAccessService(
                project_root=root,
                api_token_configured=True,
                inspector_fetch=lambda _url, _timeout: {
                    "tunnels": [{
                        "name": "another-project",
                        "public_url": "https://other-project.example.test",
                        "config": {"addr": "http://127.0.0.1:3000"},
                    }],
                },
            ).summarize()

        self.assertEqual(result["status"], "endpoint_required")
        self.assertEqual(result["blockerCode"], "ngrok_endpoint_already_active")
        self.assertEqual(result["detectedEndpointCount"], 1)
        self.assertFalse(result["active"])
        self.assertIsNone(result["publicUrl"])
        self.assertNotIn("other-project", json.dumps(result))

    def test_non_loopback_shared_inspector_is_never_contacted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = LocalCloudAccessService(
                project_root=root,
                api_token_configured=True,
                shared_inspector_url="https://attacker.example/api/tunnels",
                inspector_fetch=lambda _url, _timeout: self.fail(
                    "non-loopback inspector must not be contacted"
                ),
            ).summarize()

        self.assertEqual(result["status"], "not_running")
        self.assertFalse(result["active"])
        self.assertIsNone(result["publicUrl"])

    def test_inspector_fetch_disables_environment_proxies_and_redirects(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"tunnels": []}'

        with patch("src.operations.local_cloud_access.build_opener", return_value=opener) as build_opener:
            result = _fetch_inspector_json("http://127.0.0.1:4040/api/tunnels", 0.75)

        handlers = build_opener.call_args.args
        self.assertIsInstance(handlers[0], ProxyHandler)
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], HTTPRedirectHandler)
        self.assertIsNone(handlers[1].redirect_request(
            Request("http://127.0.0.1:4040/api/tunnels"),
            BytesIO(),
            302,
            "Found",
            {},
            "https://external.example.test/collect",
        ))
        opener.open.assert_called_once()
        self.assertEqual(result, {"tunnels": []})

    def test_valid_owned_tunnel_is_active_and_sanitized(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_path = self._write_runtime(root)
            result = LocalCloudAccessService(
                project_root=root,
                runtime_path=runtime_path,
                api_token_configured=True,
                inspector_fetch=lambda _url, _timeout: {
                    "tunnels": [{
                        "name": "fab-managed",
                        "public_url": "https://fab.example.test",
                        "config": {"addr": "http://127.0.0.1:5001"},
                    }],
                },
            ).summarize()

        self.assertEqual(result["status"], "active")
        self.assertTrue(result["active"])
        self.assertEqual(result["publicUrl"], "https://fab.example.test")
        self.assertEqual(result["haiManifestUrl"], "https://fab.example.test/api/hai/manifest")
        rendered = json.dumps(result, sort_keys=True)
        self.assertNotIn(temp_dir, rendered)
        self.assertNotIn("processId", rendered)
        self.assertNotIn("inspectorPort", rendered)
        self.assertNotIn("overlay", rendered)
        self.assertNotIn("private-token-value", rendered)

    def test_remote_dashboard_requires_verified_basic_auth_endpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_path = self._write_runtime(root, dashboard_url="https://dashboard.example.test")
            result = LocalCloudAccessService(
                project_root=root,
                runtime_path=runtime_path,
                api_token_configured=True,
                inspector_fetch=lambda _url, _timeout: {
                    "tunnels": [
                        {
                            "name": "fab-managed",
                            "public_url": "https://fab.example.test",
                            "config": {"addr": "http://127.0.0.1:5001"},
                        },
                        {
                            "name": "fab-managed-dashboard",
                            "public_url": "https://dashboard.example.test",
                            "config": {"addr": "http://127.0.0.1:3000"},
                        },
                    ],
                },
            ).summarize()

        self.assertEqual(result["status"], "active")
        self.assertEqual(result["dashboardUrl"], "https://dashboard.example.test")
        self.assertEqual(result["dashboardAuthMode"], "ngrok_basic_auth")

    def test_remote_dashboard_missing_inspector_endpoint_is_stale(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_path = self._write_runtime(root, dashboard_url="https://dashboard.example.test")
            result = LocalCloudAccessService(
                project_root=root,
                runtime_path=runtime_path,
                api_token_configured=True,
                inspector_fetch=lambda _url, _timeout: {
                    "tunnels": [{
                        "name": "fab-managed",
                        "public_url": "https://fab.example.test",
                        "config": {"addr": "http://127.0.0.1:5001"},
                    }],
                },
            ).summarize()

        self.assertEqual(result["status"], "stale")
        self.assertFalse(result["active"])
        self.assertIsNone(result["dashboardUrl"])

    def test_remote_dashboard_runtime_requires_clean_separate_origin_and_auth_gate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_path = self._write_runtime(root, dashboard_url="https://fab.example.test")
            result = LocalCloudAccessService(
                project_root=root,
                runtime_path=runtime_path,
                api_token_configured=True,
                inspector_fetch=lambda _url, _timeout: self.fail("invalid runtime must not query inspector"),
            ).summarize()

        self.assertEqual(result["status"], "invalid_runtime")
        self.assertIsNone(result["dashboardUrl"])

    def test_remote_dashboard_runtime_without_upstream_port_is_invalid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_path = self._write_runtime(root, dashboard_url="https://dashboard.example.test")
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            runtime.pop("dashboardPort")
            runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
            result = LocalCloudAccessService(
                project_root=root,
                runtime_path=runtime_path,
                api_token_configured=True,
                inspector_fetch=lambda _url, _timeout: self.fail("invalid runtime must not query inspector"),
            ).summarize()

        self.assertEqual(result["status"], "invalid_runtime")
        self.assertIsNone(result["dashboardUrl"])

    def test_missing_or_mismatched_inspector_tunnel_is_stale(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_path = self._write_runtime(root)
            service = LocalCloudAccessService(
                project_root=root,
                runtime_path=runtime_path,
                api_token_configured=True,
                inspector_fetch=lambda _url, _timeout: {
                    "tunnels": [{
                        "name": "another-service",
                        "public_url": "https://fab.example.test",
                        "config": {"addr": "http://127.0.0.1:5001"},
                    }],
                },
            )

            result = service.summarize()

        self.assertEqual(result["status"], "stale")
        self.assertFalse(result["active"])
        self.assertIsNone(result["publicUrl"])
        self.assertIsNone(result["haiManifestUrl"])

    def test_cleanup_required_runtime_is_never_reported_as_active(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_path = self._write_runtime(root)
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            runtime["status"] = "cleanup_required"
            runtime_path.write_text(json.dumps(runtime), encoding="utf-8")

            result = LocalCloudAccessService(
                project_root=root,
                runtime_path=runtime_path,
                api_token_configured=True,
                inspector_fetch=lambda _url, _timeout: self.fail(
                    "unverified cleanup state must not query the inspector"
                ),
            ).summarize()

        self.assertEqual(result["status"], "invalid_runtime")
        self.assertFalse(result["active"])
        self.assertIsNone(result["publicUrl"])
        self.assertIsNone(result["haiManifestUrl"])

    def test_runtime_for_another_checkout_or_without_auth_is_invalid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_path = self._write_runtime(root)
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            runtime["instanceRoot"] = str(root / "other-checkout")
            runtime["authRequired"] = False
            runtime_path.write_text(json.dumps(runtime), encoding="utf-8")

            result = LocalCloudAccessService(
                project_root=root,
                runtime_path=runtime_path,
                api_token_configured=True,
                inspector_fetch=lambda _url, _timeout: self.fail("inspector must not be called"),
            ).summarize()

        self.assertEqual(result["status"], "invalid_runtime")
        self.assertFalse(result["active"])
        self.assertIsNone(result["publicUrl"])

    def test_public_url_must_be_a_clean_https_origin(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_path = self._write_runtime(root)
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            runtime["publicUrl"] = "https://user:secret@fab.example.test/private?token=secret"
            runtime_path.write_text(json.dumps(runtime), encoding="utf-8")

            result = LocalCloudAccessService(
                project_root=root,
                runtime_path=runtime_path,
                api_token_configured=True,
            ).summarize()

        self.assertEqual(result["status"], "invalid_runtime")
        self.assertNotIn("secret", json.dumps(result))

    def test_api_exposes_authenticated_sanitized_cloud_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_path = root / "missing-runtime.json"
            client = create_app({
                "fab_local_ledger_path": str(root / "fab.sqlite3"),
                "fab_local_api_token": "cloud-status-test-token",
                "fab_ngrok_runtime_path": str(runtime_path),
                "fab_ngrok_shared_inspector_url": "http://127.0.0.1:1/api/tunnels",
            }).test_client()

            unauthorized = client.get("/api/cloud/status")
            response = client.get(
                "/api/cloud/status",
                headers={"Authorization": "Bearer cloud-status-test-token"},
            )

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "not_running")
        self.assertEqual(response.get_json()["authMode"], "bearer_token")

    def test_api_disables_cloud_access_during_local_maintenance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client = create_app({
                "fab_local_ledger_path": str(root / "fab.sqlite3"),
                "fab_maintenance_mode": True,
                "fab_ngrok_runtime_path": str(root / "missing-runtime.json"),
                "fab_ngrok_shared_inspector_url": "http://127.0.0.1:1/api/tunnels",
            }).test_client()

            response = client.get("/api/cloud/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "disabled_for_maintenance")
        self.assertTrue(response.get_json()["maintenanceMode"])
        self.assertFalse(response.get_json()["active"])
        self.assertIsNone(response.get_json()["publicUrl"])

    def test_maintenance_api_refuses_non_loopback_binding(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "local-only"):
                create_app({
                    "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                    "fab_local_api_host": "0.0.0.0",
                    "fab_local_api_token": "Remote-Token-2026-ABcd-7890-Valid-XY",
                    "fab_maintenance_mode": True,
                })

    @staticmethod
    def _write_runtime(root: Path, dashboard_url=None) -> Path:
        runtime_path = root / "data" / "fab-ngrok-runtime.json"
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_path.write_text(json.dumps({
            "version": 1,
            "service": "fab-ngrok-tunnel",
            "instanceRoot": str(root),
            "instanceId": local_instance_id(root),
            "processId": 1234,
            "inspectorPort": 4041,
            "publicUrl": "https://fab.example.test",
            "dashboardUrl": dashboard_url,
            "dashboardPort": 3000 if dashboard_url else None,
            "dashboardAuthRequired": bool(dashboard_url),
            "localApiBaseUrl": "http://127.0.0.1:5001",
            "status": "active",
            "startedAt": "2026-08-09T10:00:00Z",
            "verifiedAt": "2026-08-09T10:00:02Z",
            "authRequired": True,
            "haiManifestUrl": "https://fab.example.test/api/hai/manifest",
            "overlayPath": str(root / "private-overlay.yml"),
            "stdoutPath": str(root / "private.out.log"),
            "stderrPath": str(root / "private.err.log"),
        }), encoding="utf-8")
        return runtime_path


if __name__ == "__main__":
    unittest.main()
