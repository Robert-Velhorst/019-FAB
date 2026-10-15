import os
import tempfile
import unittest
from pathlib import Path

from src.config_loader import ConfigLoader
from src.security.local_secret_store import LocalSecretStore


class TestConfigLoader(unittest.TestCase):
    def setUp(self):
        self._original_env = {
            key: os.environ.get(key)
            for key in (
                "FAB_LOCAL_LEDGER_PATH",
                "FAB_LOCAL_API_PORT",
                "FAB_WAVEAPPS_BUSINESS_ACCESS_TOKEN",
                "FAB_WAVEAPPS_BUSINESS_ID",
                "FAB_WAVEAPPS_DEFAULT_TARGET",
                "FAB_HAI_API_TOKEN",
                "FAB_HAI_API_TOKEN_FILE",
            )
        }

    def tearDown(self):
        for key, value in self._original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_sectioned_config_values_are_available_as_flat_aliases(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.ini")
            with open(config_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "[gmail]\nquery=has:attachment\n"
                    "[google_drive]\nfolder_id=sort-out\n"
                    "[reconciliation]\n"
                    "reconciliation_threshold=0.05\n"
                    "reconciliation_date_tolerance_days=2\n"
                    "[waveapps_business]\n"
                    "category_account_ids={\"Office Supplies\": \"account-1\"}\n"
                )

            config = ConfigLoader(config_file=config_path).get_all_config()

        self.assertEqual(config["gmail"]["query"], "has:attachment")
        self.assertEqual(config["gmail_query"], "has:attachment")
        self.assertEqual(config["google_drive_folder_id"], "sort-out")
        self.assertEqual(config["reconciliation_threshold"], "0.05")
        self.assertEqual(config["reconciliation_date_tolerance_days"], "2")
        self.assertEqual(config["waveapps_business_category_account_ids"], {"Office Supplies": "account-1"})

    def test_direct_fab_local_environment_values_are_flat_aliases(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "env-ledger.sqlite3")
            os.environ["FAB_LOCAL_LEDGER_PATH"] = ledger_path
            os.environ["FAB_LOCAL_API_PORT"] = "5052"
            config_path = os.path.join(temp_dir, "missing.ini")

            config = ConfigLoader(config_file=config_path).get_all_config()

        self.assertEqual(config["fab_local_ledger_path"], ledger_path)
        self.assertEqual(config["fab_local_api_port"], "5052")

    def test_wave_credentials_can_be_loaded_from_uncommitted_environment_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["FAB_WAVEAPPS_BUSINESS_ACCESS_TOKEN"] = "environment-token"
            os.environ["FAB_WAVEAPPS_BUSINESS_ID"] = "business-123"
            os.environ["FAB_WAVEAPPS_DEFAULT_TARGET"] = "waveapps_business"

            config = ConfigLoader(
                config_file=os.path.join(temp_dir, "missing.ini")
            ).get_all_config()

        self.assertEqual(config["waveapps_business_access_token"], "environment-token")
        self.assertEqual(config["waveapps_business_id"], "business-123")
        self.assertEqual(config["waveapps_business"]["access_token"], "environment-token")
        self.assertEqual(config["waveapps_default_target"], "waveapps_business")

    def test_dedicated_hai_token_file_is_resolved_as_a_secret(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = Path(temp_dir) / "hai-token"
            token_path.write_text("dedicated-hai-secret-value-1234567890", encoding="utf-8")
            os.environ.pop("FAB_HAI_API_TOKEN", None)
            os.environ["FAB_HAI_API_TOKEN_FILE"] = str(token_path)

            config = ConfigLoader(
                config_file=os.path.join(temp_dir, "missing.ini")
            ).get_all_config()

        self.assertEqual(config["fab_hai_api_token"], "dedicated-hai-secret-value-1234567890")

    def test_wave_settings_can_be_loaded_from_encrypted_local_store(self):
        os.environ.pop("FAB_WAVEAPPS_BUSINESS_ACCESS_TOKEN", None)
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = os.path.join(temp_dir, "config")
            os.makedirs(config_dir)
            config_path = os.path.join(config_dir, "config.ini")
            with open(config_path, "w", encoding="utf-8") as handle:
                handle.write("[waveapps_business]\n")
            store_config = {
                "fab_local_secret_store_path": os.path.join(temp_dir, "credentials", "fab-local-secrets.enc"),
                "fab_local_secret_key_path": os.path.join(temp_dir, "credentials", "fab-local-secrets.key"),
            }
            LocalSecretStore(store_config).update_wave_target("waveapps_business", {
                "access_token": "encrypted-local-token",
                "business_id": "business-local",
                "anchor_account_id": "anchor-local",
                "default_category_account_id": "expense-local",
            })

            config = ConfigLoader(config_file=config_path).get_all_config()

        self.assertEqual(config["waveapps_business_access_token"], "encrypted-local-token")
        self.assertEqual(config["waveapps_business_id"], "business-local")
        self.assertEqual(config["waveapps_business_anchor_account_id"], "anchor-local")
        self.assertEqual(config["waveapps_business_default_category_account_id"], "expense-local")

    def test_template_keeps_web_api_and_connector_settings_in_their_own_sections(self):
        template_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "config", "config_template.ini")
        )

        config = ConfigLoader(config_file=template_path).get_all_config()

        self.assertEqual(config["operations_api_url"], "")
        self.assertEqual(config["operations_timeout_seconds"], "5")
        self.assertNotIn("api_url", config["google_photos"])
        self.assertFalse(config["gmail_enabled"])
        self.assertFalse(config["google_drive_enabled"])
        self.assertEqual(config["google_photos_mode"], "picker")
        self.assertTrue(config["freshdesk_financial_filter_enabled"])
        self.assertEqual(config["freshdesk_ticket_statuses"], "2,3")
        self.assertTrue(config["freshdesk_pdf_only"])
        self.assertTrue(config["enable_enhanced_preprocessing"])
        self.assertEqual(config["denoising_strength"], "10")
        self.assertEqual(config["deskew_threshold"], "0.5")
        self.assertEqual(config["deskew_max_angle"], "15")
        self.assertEqual(config["fab_preprocessing_temp_dir"], "")
        self.assertTrue(config["enable_template_matching"])
        self.assertEqual(config["template_matching_templates_dir"], "")
        self.assertEqual(config["vendor_templates_file"], "")
        self.assertEqual(config["vendor_templates"], {})
        self.assertTrue(config["enable_line_item_extraction"])

    def test_python_container_installs_fail_closed_runtime_defaults(self):
        project_root = Path(__file__).resolve().parents[1]
        dockerfile = (project_root / "Dockerfile").read_text(encoding="utf-8")
        template = ConfigLoader(
            config_file=str(project_root / "config" / "config_template.ini")
        ).get_all_config()

        self.assertIn(
            "COPY config/config_template.ini ./config/config.ini",
            dockerfile,
        )
        self.assertNotIn("worker_run_legacy_workflow", template)
        self.assertFalse(template["freshdesk_enabled"])

    def test_duplicate_option_names_do_not_overwrite_earlier_legacy_aliases(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.ini")
            with open(config_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "[operations]\napi_url=\n"
                    "[waveapps]\napi_url=https://gql.waveapps.com/graphql/public\n"
                )

            config = ConfigLoader(config_file=config_path).get_all_config()

        self.assertEqual(config["api_url"], "")
        self.assertEqual(config["operations_api_url"], "")
        self.assertEqual(
            config["waveapps_api_url"],
            "https://gql.waveapps.com/graphql/public",
        )


if __name__ == "__main__":
    unittest.main()
