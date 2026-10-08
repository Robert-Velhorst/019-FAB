import os
import tempfile
import unittest
from pathlib import Path

from src.runtime_fingerprint import runtime_fingerprint


class TestRuntimeFingerprint(unittest.TestCase):
    def test_web_startup_and_build_scripts_invalidate_the_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in ("production-env.mjs", "verify-production-build.mjs"):
                with self.subTest(name=name):
                    helper = root / "web" / "scripts" / name
                    helper.parent.mkdir(parents=True, exist_ok=True)
                    helper.write_text("before", encoding="utf-8")
                    initial = runtime_fingerprint(root)
                    helper.write_text("after", encoding="utf-8")
                    self.assertNotEqual(runtime_fingerprint(root), initial)

    def test_windows_helpers_invalidate_the_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in ("Windows-Profile.ps1", "Windows-Job.ps1", "Windows-Job.cs", "Windows-Process.ps1"):
                with self.subTest(name=name):
                    helper = root / "scripts" / name
                    helper.parent.mkdir(exist_ok=True)
                    helper.write_text("before", encoding="utf-8")
                    initial = runtime_fingerprint(root)
                    helper.write_text("after", encoding="utf-8")
                    self.assertNotEqual(runtime_fingerprint(root), initial)

    def test_is_deterministic_and_changes_with_runtime_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "src" / "service.py"
            source.parent.mkdir(parents=True)
            source.write_text("STATE = 1\n", encoding="utf-8")

            initial = runtime_fingerprint(root)
            self.assertEqual(runtime_fingerprint(root), initial)

            source.write_text("STATE = 2\n", encoding="utf-8")
            self.assertNotEqual(runtime_fingerprint(root), initial)

    def test_ignores_runtime_data_and_secret_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "src" / "service.py"
            source.parent.mkdir(parents=True)
            source.write_text("STATE = 1\n", encoding="utf-8")
            initial = runtime_fingerprint(root)

            data_file = root / "data" / "ledger.sqlite3"
            data_file.parent.mkdir(parents=True)
            data_file.write_bytes(b"private ledger contents")
            credential = root / "credentials" / "client.json"
            credential.parent.mkdir(parents=True)
            credential.write_text('{"client_secret":"private"}', encoding="utf-8")

            self.assertEqual(runtime_fingerprint(root), initial)

    def test_configuration_metadata_changes_the_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config" / "config.ini"
            config.parent.mkdir(parents=True)
            config.write_text("[fab]\nmode=one\n", encoding="utf-8")
            initial = runtime_fingerprint(root)

            config.write_text("[fab]\nmode=two\n", encoding="utf-8")
            stat = config.stat()
            os.utime(config, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

            self.assertNotEqual(runtime_fingerprint(root), initial)


if __name__ == "__main__":
    unittest.main()
