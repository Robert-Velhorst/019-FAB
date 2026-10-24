import os
import tempfile
import unittest
import uuid
import zipfile

from src.backup.backup_manager import BackupManager
from src.learning.correction_learning import CorrectionLearningService
from src.storage.database import Database


class TestCorrectionLearningAndBackup(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.config = {
            "database_path": os.path.join(self.tempdir.name, "fab.sqlite3"),
            "backup_base_dir": os.path.join(self.tempdir.name, "backups"),
        }
        self.database = Database(self.config)
        self.database.upsert_document({"id": "doc-1", "source": "test", "original_filename": "receipt.pdf"})

    def tearDown(self):
        self.tempdir.cleanup()

    def test_manual_correction_learns_vendor_and_category(self):
        service = CorrectionLearningService(self.config)
        result = service.apply_document_correction(
            "doc-1",
            {
                "extracted_data": {"vendor_name": "Albert Heijn", "total_amount": 4.28},
                "vendor_name": "Albert Heijn",
                "vendor_alias": "AH",
                "category": "Groceries",
                "category_path": ["Household", "Groceries"],
            },
            "Corrected from manual review",
        )

        self.assertEqual(result["status"], "correction_applied")
        vendor = self.database.fetch_one("SELECT * FROM vendors WHERE normalized_name = ?", ("albert heijn",))
        self.assertIsNotNone(vendor)
        decision = self.database.fetch_one("SELECT * FROM category_decisions WHERE document_id = ?", ("doc-1",))
        self.assertEqual(decision["category"], "Groceries")
        correction = self.database.fetch_one("SELECT * FROM document_corrections WHERE document_id = ?", ("doc-1",))
        self.assertIsNotNone(correction)

    def test_category_rule_can_be_created(self):
        service = CorrectionLearningService(self.config)
        result = service.create_category_rule("groceries_ah", "Groceries", "Albert Heijn")
        self.assertEqual(result["status"], "rule_saved")
        rule = self.database.fetch_one("SELECT * FROM category_rules WHERE rule_name = ?", ("groceries_ah",))
        self.assertEqual(rule["category"], "Groceries")

    def test_backup_manager_creates_and_lists_backup(self):
        data_file = os.path.join(self.tempdir.name, "sample.txt")
        with open(data_file, "w", encoding="utf-8") as handle:
            handle.write("important")

        self.config["backup_paths"] = [data_file]
        manager = BackupManager(self.config)
        result = manager.perform_backup([data_file], {"type": "zip"})
        self.assertEqual(result["status"], "success")
        self.assertTrue(os.path.exists(result["path"]))
        backups = manager.list_backups()
        self.assertEqual(len(backups), 1)

    def test_backup_manager_restore_rejects_archive_path_traversal(self):
        backup_dir = os.path.join(self.tempdir.name, "backups")
        restore_dir = os.path.join(backup_dir, "restore")
        manager = BackupManager({
            **self.config,
            "backup_base_dir": backup_dir,
            "backup_restore_dir": restore_dir,
        })
        archive_path = os.path.join(backup_dir, "hostile.zip")
        escaped_name = f"fab-restore-path-probe-{uuid.uuid4().hex}.txt"
        escaped_path = os.path.join(backup_dir, escaped_name)
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(f"../../{escaped_name}", b"must not be extracted")

        result = manager.restore_backup(archive_path, "restored")

        self.assertEqual(result["status"], "failure")
        self.assertFalse(os.path.exists(escaped_path))
        self.assertFalse(os.path.exists(os.path.join(restore_dir, "restored", escaped_name)))

    def test_default_backup_includes_mijngeldzaken_supervised_artifacts(self):
        export_dir = os.path.join(self.tempdir.name, "mijngeldzaken-exports")
        os.makedirs(export_dir)
        artifact_path = os.path.join(export_dir, "mijngeldzaken_import_doc-1.csv")
        with open(artifact_path, "w", encoding="utf-8") as handle:
            handle.write("Datum;Bedrag\n2026-07-10;10.00\n")
        config = {**self.config, "mijngeldzaken_export_dir": export_dir}

        manager = BackupManager(config)
        result = manager.perform_backup()

        self.assertEqual(result["status"], "success")
        canonical_export_dir = os.path.normcase(os.path.realpath(export_dir))
        included_paths = {os.path.normcase(path) for path in result["included_paths"]}
        self.assertIn(canonical_export_dir, included_paths)
        with zipfile.ZipFile(result["path"], "r") as archive:
            self.assertIn(
                "mijngeldzaken-exports/mijngeldzaken_import_doc-1.csv",
                {name.replace("\\", "/") for name in archive.namelist()},
            )


if __name__ == "__main__":
    unittest.main()
