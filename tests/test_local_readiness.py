import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.operations.local_api import create_app
from src.operations.local_readiness import LocalReadinessService
from src.utils.runtime_identity import local_instance_id


class TestLocalReadinessService(unittest.TestCase):
    def test_summary_reuses_one_dependency_snapshot_for_source_readiness(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = LocalReadinessService(
                {},
                ledger_path=os.path.join(temp_dir, "fab.sqlite3"),
                api_host="127.0.0.1",
                api_port=5001,
                api_token_configured=False,
                intake_paths=[],
                intake_extensions=[],
                instance_root=temp_dir,
            )

            with patch.object(service, "_dependencies", wraps=service._dependencies) as dependency_scan:
                summary = service.summarize()

            self.assertEqual(dependency_scan.call_count, 1)
            self.assertTrue(any(item["id"] == "tesseract_ocr" for item in summary["sources"]))

    def test_dependency_discovery_cache_is_bounded_and_returns_independent_snapshots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = LocalReadinessService(
                {},
                ledger_path=os.path.join(temp_dir, "fab.sqlite3"),
                api_host="127.0.0.1",
                api_port=5001,
                api_token_configured=False,
                intake_paths=[],
                intake_extensions=[],
                instance_root=temp_dir,
            )

            with (
                patch("src.operations.local_readiness.time.monotonic", side_effect=[100.0, 101.0, 106.0]),
                patch.object(service, "_discover_dependencies", wraps=service._discover_dependencies) as discovery,
            ):
                first = service.summarize()
                first["dependencies"][0]["status"] = "mutated-by-caller"
                cached = service.summarize()
                refreshed = service.summarize()

            self.assertEqual(discovery.call_count, 2)
            self.assertEqual(cached["dependencies"][0]["status"], "ok")
            self.assertEqual(refreshed["dependencies"][0]["status"], "ok")

    def test_api_routes_share_the_app_owned_readiness_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_intake_paths": temp_dir,
            })
            client = app.test_client()
            readiness = app.extensions["fab_readiness_service"]
            support = app.extensions["fab_support_bundle_service"]

            with patch.object(
                readiness,
                "_discover_dependencies",
                wraps=readiness._discover_dependencies,
            ) as discovery:
                first = client.get("/api/settings")
                second = client.get("/api/settings")
                doctor = client.get("/api/doctor")

            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            self.assertEqual(doctor.status_code, 200)
            self.assertEqual(discovery.call_count, 1)
            self.assertIs(support.readiness, readiness)

    def test_readiness_reports_sources_and_redacts_secret_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            gmail_credentials = os.path.join(temp_dir, "gmail_credentials.json")
            gmail_token = os.path.join(temp_dir, "gmail_token.json")
            for path in (gmail_credentials, gmail_token):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("{}")

            summary = LocalReadinessService(
                {
                    "gmail": {
                        "credentials_file": gmail_credentials,
                        "token_file": gmail_token,
                    },
                    "waveapps_business": {
                        "access_token": "wave-business-secret",
                        "business_id": "business-123",
                    },
                    "mijngeldzaken_password": "mgz-secret",
                    "freshdesk_api_key": "freshdesk-secret",
                    "freshdesk_domain": "example",
                },
                ledger_path=os.path.join(temp_dir, "fab.sqlite3"),
                api_host="127.0.0.1",
                api_port=5055,
                api_token_configured=True,
                intake_paths=[intake_dir],
                intake_extensions=["pdf", "txt"],
            ).summarize()
            rendered = json.dumps(summary, sort_keys=True)
            sources = {source["id"]: source for source in summary["sources"]}
            credentials = {credential["id"]: credential for credential in summary["credentials"]}

            self.assertEqual(sources["local_folder"]["status"], "ready")
            self.assertEqual(sources["gmail"]["status"], "ready")
            self.assertEqual(sources["waveapps_business"]["status"], "ready")
            self.assertEqual(sources["mijngeldzaken"]["status"], "supervision_required")
            self.assertTrue(sources["mijngeldzaken"]["localArtifactReady"])
            self.assertFalse(sources["mijngeldzaken"]["ready"])
            self.assertIn("stored passwords are ignored", sources["mijngeldzaken"]["details"])
            self.assertTrue(credentials["wave_business_token"]["configured"])
            self.assertIn("ignored", credentials["mijngeldzaken_password"]["label"])
            self.assertTrue(summary["security"]["secretValuesRedacted"])
            self.assertEqual(summary["localAccess"]["dashboardUrl"], "http://127.0.0.1:5055/")
            self.assertEqual(summary["localAccess"]["apiBaseUrl"], "http://127.0.0.1:5055/api")
            self.assertEqual(summary["localAccess"]["authMode"], "bearer_token_or_dashboard_login")
            self.assertIn("FAB_LOCAL_API_TOKEN", summary["localAccess"]["windows"]["recommendedEnvironment"])
            self.assertNotIn("wave-business-secret", rendered)
            self.assertNotIn("mgz-secret", rendered)
            self.assertNotIn("freshdesk-secret", rendered)
            self.assertNotIn("api-secret-token", rendered)

    def test_remote_host_without_api_token_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = LocalReadinessService(
                {},
                ledger_path=os.path.join(temp_dir, "fab.sqlite3"),
                api_host="0.0.0.0",
                api_token_configured=False,
                intake_paths=[],
                intake_extensions=[],
            ).summarize()

            self.assertEqual(summary["status"], "blocked")
            self.assertFalse(summary["security"]["remoteExposureSafe"])
            self.assertIn("remote_api_without_token", {issue["type"] for issue in summary["issues"]})

    def test_freshdesk_readiness_accepts_repository_025_api_url_form(self):
        summary = LocalReadinessService({
            "freshdesk_api_key": "freshdesk-secret",
            "freshdesk_api_url": "https://example.freshdesk.com/api",
        }).summarize()
        sources = {item["id"]: item for item in summary["sources"]}
        credentials = {item["id"]: item for item in summary["credentials"]}

        self.assertTrue(sources["freshdesk"]["ready"])
        self.assertTrue(credentials["freshdesk_api_url"]["configured"])
        self.assertFalse(credentials["freshdesk_api_url"]["secret"])

    def test_drive_readiness_blocks_sync_during_oauth_client_rotation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials_path = os.path.join(temp_dir, "drive.json")
            token_path = os.path.join(temp_dir, "drive.json")
            for path in (credentials_path, token_path, f"{token_path}.reauthorize"):
                with open(path, "wb") as handle:
                    handle.write(b"configured")

            summary = LocalReadinessService({
                "google_drive_credentials_file": credentials_path,
                "google_drive_token_file": token_path,
                "google_drive_folder_id": "approved-source-folder",
            }).summarize()
            drive = next(item for item in summary["sources"] if item["id"] == "google_drive")

            self.assertFalse(drive["ready"])
            self.assertEqual(drive["status"], "needs_authorization")
            self.assertIn("fresh Google consent", drive["details"])

    def test_readiness_rejects_legacy_pickle_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials_path = os.path.join(temp_dir, "drive-credentials.json")
            legacy_path = os.path.join(temp_dir, "drive-token.pickle")
            for path in (credentials_path, legacy_path):
                with open(path, "wb") as handle:
                    handle.write(b"configured")

            summary = LocalReadinessService({
                "google_drive_enabled": True,
                "google_drive_credentials_file": credentials_path,
                "google_drive_token_file": legacy_path,
                "google_drive_folder_id": "approved-source-folder",
            }).summarize()
            drive = next(item for item in summary["sources"] if item["id"] == "google_drive")
            token = next(item for item in summary["credentials"] if item["id"] == "drive_token")

            self.assertFalse(drive["ready"])
            self.assertEqual(drive["status"], "needs_authorization")
            self.assertIn("never loaded", drive["details"])
            self.assertFalse(token["exists"])
            self.assertTrue(token["legacyTokenPresent"])
            self.assertTrue(token["path"].endswith("drive-token.json"))
            self.assertTrue(os.path.isfile(legacy_path))

    def test_base_url_overrides_displayed_local_access_without_exposing_token(self):
        summary = LocalReadinessService(
            {"fab_local_api_base_url": "https://fab-local.example.test", "fab_local_api_token": "secret-token"},
            ledger_path="data/fab.sqlite3",
            api_host="127.0.0.1",
            api_port=5001,
            api_token_configured=True,
            intake_paths=[],
            intake_extensions=[],
        ).summarize()
        rendered = json.dumps(summary, sort_keys=True)

        self.assertEqual(summary["localAccess"]["dashboardUrl"], "https://fab-local.example.test/")
        self.assertEqual(summary["localAccess"]["apiBaseUrl"], "https://fab-local.example.test/api")
        self.assertEqual(summary["localAccess"]["authHeaderExample"], "Authorization: Bearer <FAB_LOCAL_API_TOKEN>")
        self.assertNotIn("secret-token", rendered)

    def test_verified_launcher_runtime_exposes_operator_dashboard_without_changing_api_origin(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            runtime_path = os.path.join(temp_dir, "fab-runtime.json")
            runtime = {
                "root": temp_dir,
                "apiBaseUrl": "http://127.0.0.1:5055",
                "dashboardUrl": "http://127.0.0.1:3005/admin/operations",
                "webIdentityUrl": "http://127.0.0.1:3005/api/fab/runtime",
            }
            with open(runtime_path, "w", encoding="utf-8") as handle:
                json.dump(runtime, handle)
            identity = {
                "service": "fab-operator-dashboard",
                "localApiEndpoint": "http://127.0.0.1:5055",
                "instanceId": local_instance_id(Path(temp_dir)),
            }
            service = LocalReadinessService(
                {},
                ledger_path=ledger_path,
                api_host="127.0.0.1",
                api_port=5055,
                api_token_configured=True,
                intake_paths=[],
                intake_extensions=[],
                instance_root=temp_dir,
            )

            with patch("src.operations.local_readiness._fetch_runtime_identity", return_value=identity) as probe:
                summary = service.summarize()
                cached_summary = service.summarize()

            self.assertEqual(summary["localAccess"]["dashboardUrl"], "http://127.0.0.1:3005/admin/operations")
            self.assertEqual(cached_summary["localAccess"]["dashboardUrl"], summary["localAccess"]["dashboardUrl"])
            self.assertEqual(summary["localAccess"]["dashboardSource"], "verified_launcher_runtime")
            self.assertEqual(summary["localAccess"]["ledgerDashboardUrl"], "http://127.0.0.1:5055/")
            self.assertEqual(summary["localAccess"]["apiBaseUrl"], "http://127.0.0.1:5055/api")
            probe.assert_called_once_with("http://127.0.0.1:3005/api/fab/runtime")

    def test_untrusted_launcher_runtime_cannot_redirect_operator_or_probe_remote_urls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            runtime_path = os.path.join(temp_dir, "fab-runtime.json")
            with open(runtime_path, "w", encoding="utf-8") as handle:
                json.dump({
                    "root": temp_dir,
                    "apiBaseUrl": "http://127.0.0.1:5055",
                    "dashboardUrl": "http://attacker.invalid/admin/operations",
                    "webIdentityUrl": "http://attacker.invalid/api/fab/runtime",
                }, handle)

            with patch("src.operations.local_readiness._fetch_runtime_identity") as probe:
                summary = LocalReadinessService(
                    {"fab_operator_dashboard_url": "http://attacker.invalid/admin/operations"},
                    ledger_path=ledger_path,
                    api_host="127.0.0.1",
                    api_port=5055,
                    api_token_configured=True,
                    intake_paths=[],
                    intake_extensions=[],
                    instance_root=temp_dir,
                ).summarize()

            self.assertEqual(summary["localAccess"]["dashboardUrl"], "http://127.0.0.1:5055/")
            self.assertEqual(summary["localAccess"]["dashboardSource"], "ledger_api")
            probe.assert_not_called()

    def test_configured_operator_dashboard_is_used_for_compose_projection(self):
        summary = LocalReadinessService(
            {"fab_operator_dashboard_url": "http://localhost:3010/admin/operations/"},
            ledger_path="data/fab.sqlite3",
            api_host="127.0.0.1",
            api_port=5001,
            api_token_configured=True,
            intake_paths=[],
            intake_extensions=[],
        ).summarize()

        self.assertEqual(summary["localAccess"]["dashboardUrl"], "http://localhost:3010/admin/operations")
        self.assertEqual(summary["localAccess"]["dashboardSource"], "configured_operator_dashboard")
        self.assertEqual(summary["localAccess"]["apiBaseUrl"], "http://127.0.0.1:5001/api")

    def test_connector_api_url_cannot_replace_local_fab_access_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = LocalReadinessService(
                {
                    "api_url": "https://gql.waveapps.com/graphql/public",
                    "waveapps_api_url": "https://gql.waveapps.com/graphql/public",
                },
                ledger_path=os.path.join(temp_dir, "fab.sqlite3"),
                api_host="127.0.0.1",
                api_port=5001,
                api_token_configured=False,
                intake_paths=[],
                intake_extensions=[],
                instance_root=temp_dir,
            ).summarize()

        self.assertEqual(summary["localAccess"]["dashboardUrl"], "http://127.0.0.1:5001/")
        self.assertEqual(summary["localAccess"]["apiBaseUrl"], "http://127.0.0.1:5001/api")

    def test_local_ocr_readiness_requires_pdf_tools_and_configured_languages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = os.path.join(temp_dir, "tesseract.exe")
            poppler_dir = os.path.join(temp_dir, "poppler")
            tessdata_dir = os.path.join(temp_dir, "tessdata")
            os.makedirs(poppler_dir)
            os.makedirs(tessdata_dir)
            for path in (executable, os.path.join(poppler_dir, "pdftoppm.exe")):
                with open(path, "wb") as handle:
                    handle.write(b"test")
            for language in ("eng", "nld"):
                with open(os.path.join(tessdata_dir, f"{language}.traineddata"), "wb") as handle:
                    handle.write(b"test")

            summary = LocalReadinessService({
                "tesseract_cmd": executable,
                "tesseract_data_dir": tessdata_dir,
                "tesseract_lang": "nld+eng",
                "poppler_path": poppler_dir,
            }).summarize()
            dependencies = {item["id"]: item for item in summary["dependencies"]}
            sources = {item["id"]: item for item in summary["sources"]}

            self.assertEqual(dependencies["tesseract_languages"]["status"], "ok")
            self.assertEqual(dependencies["poppler"]["status"], "ok")
            self.assertEqual(sources["tesseract_ocr"]["status"], "ready")

    def test_category_model_readiness_does_not_claim_an_untrained_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_path = os.path.join(temp_dir, "model.joblib")
            vectorizer_path = os.path.join(temp_dir, "vectorizer.joblib")
            missing_summary = LocalReadinessService({
                "ml_model_path": model_path,
                "ml_vectorizer_path": vectorizer_path,
            }).summarize()
            missing = next(
                item for item in missing_summary["dependencies"]
                if item["id"] == "category_model"
            )

            self.assertEqual(missing["status"], "attention")
            self.assertFalse(missing["configured"])
            self.assertFalse(missing["required"])
            self.assertFalse(any(
                issue.get("entity") == "category_model"
                for issue in missing_summary["issues"]
            ))

            for path in (model_path, vectorizer_path):
                with open(path, "wb") as handle:
                    handle.write(b"approved-test-artifact")
            with patch("src.operations.local_readiness._ml_dependencies_available", return_value=True):
                ready_summary = LocalReadinessService({
                    "ml_model_path": model_path,
                    "ml_vectorizer_path": vectorizer_path,
                }).summarize()
            ready = next(
                item for item in ready_summary["dependencies"]
                if item["id"] == "category_model"
            )

            self.assertEqual(ready["status"], "ok")
            self.assertTrue(ready["configured"])

            with patch("src.operations.local_readiness._ml_dependencies_available", return_value=False):
                unavailable_summary = LocalReadinessService({
                    "ml_model_path": model_path,
                    "ml_vectorizer_path": vectorizer_path,
                }).summarize()
            unavailable = next(
                item for item in unavailable_summary["dependencies"]
                if item["id"] == "category_model"
            )
            self.assertEqual(unavailable["status"], "attention")
            self.assertFalse(unavailable["configured"])
            self.assertIn("requirements-ml.lock", unavailable["details"])
            self.assertFalse(any(
                issue.get("entity") == "category_model"
                for issue in unavailable_summary["issues"]
            ))

    def test_disabled_optional_sources_do_not_create_readiness_issues(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = LocalReadinessService(
                {
                    "google_photos": {
                        "enabled": False,
                        "credentials_file": os.path.join(temp_dir, "missing-credentials.json"),
                        "picker_token_file": os.path.join(temp_dir, "missing-token.json"),
                    },
                },
                ledger_path=os.path.join(temp_dir, "fab.sqlite3"),
                intake_paths=[temp_dir],
            ).summarize()
            photos = next(
                source for source in summary["sources"]
                if source["id"] == "google_photos"
            )
            issue_entities = {issue.get("entity") for issue in summary["issues"]}

            self.assertEqual(photos["status"], "disabled")
            self.assertFalse(photos["enabled"])
            self.assertNotIn("photos_credentials", issue_entities)
            self.assertNotIn("photos_token", issue_entities)
            self.assertNotIn("category_model", issue_entities)
            self.assertNotIn("playwright", issue_entities)

    def test_enabled_photos_reports_missing_required_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = LocalReadinessService(
                {
                    "google_photos": {
                        "enabled": True,
                        "credentials_file": os.path.join(temp_dir, "missing-credentials.json"),
                        "picker_token_file": os.path.join(temp_dir, "missing-token.json"),
                    },
                },
                ledger_path=os.path.join(temp_dir, "fab.sqlite3"),
                intake_paths=[temp_dir],
            ).summarize()
            photos = next(
                source for source in summary["sources"]
                if source["id"] == "google_photos"
            )
            issue_entities = {issue.get("entity") for issue in summary["issues"]}

            self.assertEqual(photos["status"], "needs_attention")
            self.assertTrue(photos["enabled"])
            self.assertIn("photos_credentials", issue_entities)
            self.assertIn("photos_token", issue_entities)

    def test_api_settings_and_dashboard_render_readiness_without_secrets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            app = create_app({
                "fab_local_ledger_path": os.path.join(temp_dir, "fab.sqlite3"),
                "fab_local_intake_paths": intake_dir,
                "fab_local_api_token": "api-secret-token",
                "waveapps_business_access_token": "wave-secret-token",
                "waveapps_business_id": "business-123",
            })
            client = app.test_client()
            headers = {"Authorization": "Bearer api-secret-token"}

            settings = client.get("/api/settings", headers=headers)
            health = client.get("/api/health", headers=headers)
            dashboard = client.get("/", headers=headers)
            settings_text = settings.data.decode("utf-8")
            dashboard_html = dashboard.data.decode("utf-8")

            self.assertEqual(settings.status_code, 200)
            self.assertEqual(health.status_code, 200)
            self.assertIn("readiness", health.get_json())
            self.assertEqual(health.get_json()["readiness"]["authMode"], "bearer_token_or_dashboard_login")
            self.assertEqual(settings.get_json()["sources"][0]["id"], "local_folder")
            self.assertIn("localAccess", settings.get_json())
            self.assertEqual(settings.get_json()["localAccess"]["authHeaderExample"], "Authorization: Bearer <FAB_LOCAL_API_TOKEN>")
            self.assertIn("Source Status", dashboard_html)
            self.assertIn("Dependency Status", dashboard_html)
            self.assertIn("Credential Status", dashboard_html)
            self.assertIn("Windows Local Runbook", dashboard_html)
            self.assertIn("python -m src.operations.local_api", dashboard_html)
            self.assertIn("Authorization: Bearer &lt;FAB_LOCAL_API_TOKEN&gt;", dashboard_html)
            self.assertNotIn("api-secret-token", settings_text)
            self.assertNotIn("wave-secret-token", settings_text)
            self.assertNotIn("api-secret-token", dashboard_html)
            self.assertNotIn("wave-secret-token", dashboard_html)


if __name__ == "__main__":
    unittest.main()
