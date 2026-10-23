import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from package import PackageBuilder, ReleasePackageError


class ReleasePackageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name) / "source"
        self.output = Path(self.temp_dir.name) / "release"
        self.root.mkdir()
        self._write_required_sources()
        self._git("init", "--quiet")
        self._git("config", "user.name", "FAB Tests")
        self._git("config", "user.email", "fab-tests@example.invalid")
        self._commit("initial")
        self.builder = PackageBuilder(self.root, self.output)

    def test_builds_and_verifies_tracked_only_archives(self):
        generated = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
        windows_path = Path(self.builder.build_local_package(generated_at=generated))
        compose_path = Path(self.builder.build_cloud_package(generated_at=generated))

        windows = self.builder.verify_package(windows_path)
        compose = self.builder.verify_package(compose_path)
        self.assertEqual(windows["status"], "verified")
        self.assertEqual(windows["target"], "windows")
        self.assertEqual(compose["target"], "compose")
        self.assertTrue(windows_path.with_suffix(".zip.sha256").is_file())

        with zipfile.ZipFile(windows_path, "r") as release:
            names = release.namelist()
            manifest = json.loads(release.read("FAB/RELEASE-MANIFEST.json"))
        self.assertIn("FAB/Start-FAB.ps1", names)
        self.assertIn("FAB/Start-FAB-Maintenance.cmd", names)
        self.assertIn("FAB/config/config_template.ini", names)
        for path in ("sqlite-runtime.json", "fab_sqlite_runtime.py",
                     "scripts/setup_sqlite_runtime.py", "runtime_bootstrap/sitecustomize.py"):
            self.assertIn(f"FAB/{path}", names)
        self.assertNotIn("FAB/tests/development_only.py", names)
        self.assertNotIn("FAB/config/config.ini", names)
        self.assertEqual(manifest["fileCount"], len(manifest["files"]))
        self.assertEqual(manifest["sourceCommit"], self._git_output("rev-parse", "HEAD"))

    def test_refuses_modified_tracked_sources(self):
        (self.root / "README.md").write_text("modified\n", encoding="utf-8")
        with self.assertRaisesRegex(ReleasePackageError, "modified tracked files"):
            self.builder.build_local_package()

    def test_refuses_tracked_runtime_or_secret_paths(self):
        secret = self.root / "credentials" / "token.json"
        secret.parent.mkdir()
        secret.write_text('{"token":"not-a-real-token"}\n', encoding="utf-8")
        self._commit("tracked secret fixture")
        with self.assertRaisesRegex(ReleasePackageError, "Forbidden runtime or secret path"):
            self.builder.build_cloud_package()

    def test_refuses_retired_production_looking_entrypoint(self):
        retired = self.root / "cloud_functions.py"
        retired.write_text("def trigger_workflow_http(request): return 'success'\n", encoding="utf-8")
        self._commit("retired entrypoint fixture")
        with self.assertRaisesRegex(ReleasePackageError, "Forbidden runtime or secret path"):
            self.builder.build_cloud_package()

    def test_refuses_tracked_native_runtime_artifacts(self):
        native = self.root / ".fab-runtime" / "sqlite" / "untrusted.dll"
        native.parent.mkdir(parents=True)
        native.write_bytes(b"synthetic native artifact")
        self._commit("tracked native fixture")
        with self.assertRaisesRegex(ReleasePackageError, "Forbidden runtime or secret path"):
            self.builder.build_local_package()

    def test_detects_archive_tampering(self):
        archive = Path(self.builder.build_local_package())
        with archive.open("ab") as release:
            release.write(b"tampered")
        with self.assertRaisesRegex(ReleasePackageError, "SHA-256 sidecar"):
            self.builder.verify_package(archive)

    def test_rejects_oversized_manifest_member_before_extraction(self):
        archive = self.output / "untrusted.zip"
        archive.parent.mkdir()
        manifest = {
            "schemaVersion": 1,
            "target": "windows",
            "sourceCommit": "a" * 40,
            "fileCount": 1,
            "totalBytes": PackageBuilder.MAX_FILE_BYTES + 1,
            "files": [{
                "path": "README.md",
                "size": PackageBuilder.MAX_FILE_BYTES + 1,
                "sha256": "b" * 64,
            }],
        }
        with zipfile.ZipFile(archive, "w") as release:
            release.writestr("FAB/README.md", b"")
            release.writestr("FAB/RELEASE-MANIFEST.json", json.dumps(manifest))
        with self.assertRaisesRegex(ReleasePackageError, "exceeds the size limit"):
            self.builder.verify_package(archive)

    def test_failed_final_verification_leaves_no_release_artifacts(self):
        with patch.object(self.builder, "verify_package", side_effect=ReleasePackageError("verification failed")):
            with self.assertRaisesRegex(ReleasePackageError, "verification failed"):
                self.builder.build_local_package()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_repository_has_no_retired_unsafe_entrypoints_or_dependencies(self):
        repository = Path(__file__).resolve().parents[1]
        retired_paths = (
            "cloud_functions.py",
            "main.py",
            "src/cloud_functions.py",
            "src/mobile_capture/mobile_document_capture.py",
            "src/workflow/controller.py",
            "src/workflow/checkpoint_store.py",
            "src/learning/enhanced_learning_system.py",
            "src/learning/learning_manager.py",
            "src/learning/feedback_learner.py",
            "src/learning/waveapps_analyzer.py",
            "src/learning/mijngeldzaken_analyzer.py",
            "src/performance/batch_processor.py",
            "src/performance/cache_manager.py",
            "src/performance/performance_optimizer.py",
            "src/migration/data_migration.py",
            "src/migration/migration_wizard.py",
            "src/document_processors/vendor_template_processor.py",
            "src/manual_review/manual_review_interface.py",
            "src/error_handling/enhanced_error_recovery.py",
            "src/error_handling/manual_review.py",
        )
        self.assertFalse([path for path in retired_paths if (repository / path).exists()])
        requirements = (repository / "requirements.txt").read_text(encoding="utf-8").lower()
        self.assertNotIn("functions-framework", requirements)
        self.assertNotIn("google-cloud-storage", requirements)
        compose = (repository / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("FAB_INSTANCE_ROOT: /app", compose)

    def _write_required_sources(self):
        files = {
            "Start-FAB.ps1": "Write-Output 'start'\n",
            "Start-FAB-Maintenance.cmd": "@echo off\n",
            "Stop-FAB.ps1": "Write-Output 'stop'\n",
            "sqlite-runtime.json": '{"version":"3.54.1"}\n',
            "fab_sqlite_runtime.py": "def require_stable_sqlite(): pass\n",
            "runtime_bootstrap/sitecustomize.py": "# synthetic bootstrap fixture\n",
            "scripts/setup_sqlite_runtime.py": "# synthetic installer fixture\n",
            "config/config_template.ini": "[operations]\nenabled=false\n",
            "requirements.txt": "Flask\n",
            "requirements-local.lock": "Flask==3.1.0\n",
            "requirements.lock": "Flask==3.1.0\n",
            "requirements-ml.lock": "joblib==1.0.0\n",
            "src/main.py": "def main(): return 0\n",
            "web/package.json": '{"name":"fab-test"}\n',
            "web/pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
            "web/Dockerfile": "FROM node:22-slim\n",
            "Dockerfile": "FROM python:3.13-slim\n",
            "docker-compose.yml": "services: {}\n",
            "docker-compose.maintenance.yml": "services: {}\n",
            "README.md": "# FAB\n",
            "tests/development_only.py": "assert True\n",
        }
        for relative_path, content in files.items():
            path = self.root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def _commit(self, message: str):
        self._git("add", "--all")
        self._git("commit", "--quiet", "-m", message)

    def _git(self, *args: str):
        subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def _git_output(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
