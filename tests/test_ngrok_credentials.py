import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.security.local_secret_store import LocalSecretStore


ROOT = Path(__file__).resolve().parents[1]
API_TOKEN = "synthetic-operator-0123456789-ABCDEFGHIJK"
HAI_TOKEN = "synthetic-hai-9876543210-LMNOPQRSTUVWXYZ"
SCRIPTS = ("Start-FAB-Ngrok.ps1", "Test-FAB-Ngrok.ps1")


class TestNgrokCredentialResolution(unittest.TestCase):
    def setUp(self):
        self.shell = shutil.which("pwsh") or shutil.which("powershell")
        if not self.shell:
            self.skipTest("PowerShell is unavailable")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "config").mkdir()
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.upper().startswith(("FAB_", "APP_", "JWT_SECRET"))
        }
        self.environment["PYTHONPATH"] = str(ROOT)
        self.store = LocalSecretStore({
            "fab_local_secret_store_path": str(self.root / "credentials/fab-local-secrets.enc"),
            "fab_local_secret_key_path": str(self.root / "credentials/fab-local-secrets.key"),
        })

    def configure(self, profile="windows", values=""):
        (self.root / "config/config.ini").write_text(
            f"[operations]\ndeployment_profile = {profile}\n{values}", encoding="utf-8"
        )

    def seed_store(self):
        return {
            "apiToken": self.store.get_or_create_runtime_secret("operator_api_token"),
            "haiToken": self.store.get_or_create_runtime_secret("hai_api_token"),
        }

    def run_credentials(self, filename):
        script = (ROOT / filename).read_text(encoding="utf-8")
        marker = "$credentialsJson = &" if "$credentialsJson = &" in script else "$apiToken = &"
        start = script.index(marker)
        end = script.index("$headers =" if filename.startswith("Start") else "$localLive =", start)
        block = script[start:end]
        # Only run credential resolution, never the tunnel or an HTTP request.
        command = (
            "$ErrorActionPreference = 'Stop'; Set-StrictMode -Version Latest; "
            + "$venvPython = '" + sys.executable.replace("'", "''") + "';\n"
            + block
            + "\n@{apiToken=$apiToken; haiToken=$haiApiToken} | ConvertTo-Json -Compress"
        )
        return subprocess.run(
            [self.shell, "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=self.root, env=self.environment, capture_output=True, text=True, timeout=30,
        )

    def assert_credentials(self, expected):
        for filename in SCRIPTS:
            with self.subTest(script=filename):
                result = self.run_credentials(filename)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)

    def assert_rejected(self):
        for filename in SCRIPTS:
            with self.subTest(script=filename):
                result = self.run_credentials(filename)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertNotIn(API_TOKEN, result.stderr)
                self.assertNotIn(HAI_TOKEN, result.stderr)

    def test_configured_tokens_override_other_stored_tokens_without_writing(self):
        self.seed_store()
        before = Path(self.store.store_path).read_bytes()
        self.configure(values=f"api_token = {API_TOKEN}\nhai_api_token = {HAI_TOKEN}\n")
        self.assert_credentials({"apiToken": API_TOKEN, "haiToken": HAI_TOKEN})
        self.assertEqual(Path(self.store.store_path).read_bytes(), before)

    def test_secret_files_override_ini_without_creating_store(self):
        self.configure(values="api_token = obsolete\nhai_api_token = obsolete\n")
        for name, value in (("FAB_LOCAL_API_TOKEN", API_TOKEN), ("FAB_HAI_API_TOKEN", HAI_TOKEN)):
            path = self.root / (name + ".txt")
            path.write_text(value + "\n", encoding="utf-8")
            self.environment[name + "_FILE"] = str(path)
        self.assert_credentials({"apiToken": API_TOKEN, "haiToken": HAI_TOKEN})
        self.assertFalse((self.root / "credentials").exists())

    def test_missing_credentials_fail_without_provisioning(self):
        self.configure()
        self.assert_rejected()
        self.assertFalse((self.root / "credentials").exists())

    def test_existing_local_credentials_are_reused(self):
        expected = self.seed_store()
        self.configure(profile="local")
        self.assert_credentials(expected)

    def test_local_short_configuration_uses_launcher_fallback(self):
        expected = self.seed_store()
        self.configure(profile="local", values="api_token = old\nhai_api_token = old\n")
        self.assert_credentials(expected)

    def test_windows_weak_explicit_token_does_not_fall_back_to_store(self):
        self.seed_store()
        self.configure(values=f"api_token = short\nhai_api_token = {HAI_TOKEN}\n")
        self.assert_rejected()

    def test_configured_operator_can_use_existing_hai_credential(self):
        expected = self.seed_store()
        expected["apiToken"] = API_TOKEN
        self.configure(values=f"api_token = {API_TOKEN}\n")
        self.assert_credentials(expected)

    def test_equal_effective_credentials_fail(self):
        self.seed_store()
        self.configure(values=f"api_token = {API_TOKEN}\nhai_api_token = {API_TOKEN}\n")
        self.assert_rejected()

    def test_conflicting_secret_file_and_value_fail_without_writes(self):
        self.configure()
        path = self.root / "operator.txt"
        path.write_text(API_TOKEN, encoding="utf-8")
        self.environment.update(FAB_LOCAL_API_TOKEN=API_TOKEN, FAB_LOCAL_API_TOKEN_FILE=str(path))
        self.assert_rejected()
        self.assertFalse((self.root / "credentials").exists())


if __name__ == "__main__":
    unittest.main()
