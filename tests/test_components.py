import unittest
from unittest.mock import MagicMock, patch
import logging
import os
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import closing

from src.config_loader import ConfigLoader
from src.workflow.logger import AppLogger
from src.security.security_manager import SecurityManager
from src.compliance.regulatory_compliance import RegulatoryCompliance
from src.reconciliation.automated_reconciliation import AutomatedReconciliation
from src.budget.budget_manager import BudgetManager
from src.banking.banking_api import BankingAPI
from src.financial_analysis.financial_analyzer import FinancialAnalyzer
from src.backup.backup_manager import BackupManager

class TestComponents(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config = {
            "log_file": os.path.join(self.temp_dir.name, "test_app.log"),
            "security_key": "a_very_secret_key_for_testing_1234567890",
            "compliance_rules_file": os.path.join(self.temp_dir.name, "compliance_rules.json"),
            "reconciliation_threshold": 0.05,
            "budget_file": os.path.join(self.temp_dir.name, "budgets.json"),
            "banking_api_endpoint": "http://banking.api/",
            "banking_api_credentials": {"client_id": "test", "client_secret": "test"},
            "backup_base_dir": os.path.join(self.temp_dir.name, "backups"),
            "backup_paths": [],
            "backup_config": {"type": "zip"},
        }

    def tearDown(self):
        logger = logging.getLogger("automated_bookkeeping")
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

    def test_config_loader(self):
        # Create a dummy config file
        with open("config/test_config.ini", "w") as f:
            f.write("[app]\nkey=value\n[section2]\nkey2=value2")
        
        loader = ConfigLoader(config_file="config/test_config.ini")
        config = loader.get_all_config()
        self.assertEqual(config["app"]["key"], "value")
        self.assertEqual(loader.get("section2", "key2"), "value2")
        os.remove("config/test_config.ini")

    def test_app_logger(self):
        logger = AppLogger(log_file=self.config["log_file"]).get_logger()
        logger.info("Test log message")
        with open(self.config["log_file"], "r") as f:
            content = f.read()
            self.assertIn("Test log message", content)

    def test_security_manager(self):
        manager = SecurityManager(self.config)
        encrypted_data = manager.encrypt_data("sensitive_info")
        decrypted_data = manager.decrypt_data(encrypted_data)
        self.assertEqual(decrypted_data, "sensitive_info")

    def test_regulatory_compliance(self):
        # Create a dummy compliance rules file
        with open(self.config["compliance_rules_file"], "w") as f:
            f.write("{\"rules\": [{\"id\": \"rule1\", \"description\": \"Test Rule\", \"criteria\": {\"category\": \"Business\"}}]}")
        
        compliance = RegulatoryCompliance(self.config)
        document = {"category": "Business", "amount": 100}
        result = compliance.check_compliance(document)
        self.assertTrue(result["is_compliant"])
        self.assertIn("rule1", result["compliant_rules"])
        os.remove(self.config["compliance_rules_file"])

    def test_automated_reconciliation(self):
        reconciliation = AutomatedReconciliation(self.config)
        # Dummy data for reconciliation
        transaction1 = {"id": "t1", "amount": 100.0, "date": "2025-01-01"}
        transaction2 = {"id": "t2", "amount": 100.01, "date": "2025-01-01"}
        transactions = [transaction1, transaction2]
        receipt = {"id": "r1", "total_amount": 100.0, "transaction_date": "2025-01-01"}
        
        matches = [
            result
            for result in reconciliation.reconcile(transactions, [receipt])
            if result["type"] == "match"
        ]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["receipt_id"], "r1")

    def test_budget_manager(self):
        # Create a dummy budget file
        with open(self.config["budget_file"], "w") as f:
            f.write("{\"categories\": {\"Food\": {\"limit\": 200, \"spent\": 50}}}")
        
        manager = BudgetManager(self.config)
        status = manager.check_budget("Food", 100)
        self.assertTrue(status["is_within_budget"])
        self.assertEqual(status["remaining"], 50)
        os.remove(self.config["budget_file"])

    @patch("src.banking.banking_api.requests.get")
    def test_banking_api(self, mock_get):
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"transactions": [{"id": "tx1", "amount": 50.0}]}
        mock_get.return_value = mock_response

        api = BankingAPI(self.config)
        transactions = api.fetch_transactions({}, "2025-01-01", "2025-01-31")
        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0]["amount"], 50.0)

    def test_financial_analyzer(self):
        analyzer = FinancialAnalyzer(self.config)
        # Dummy data for analysis
        transactions = [
            {"date": "2025-01-01", "amount": 100, "category": "Income"},
            {"date": "2025-01-05", "amount": -50, "category": "Food"}
        ]
        report = analyzer.generate_report(transactions)
        self.assertIn("total_income", report)
        self.assertEqual(report["total_income"], 100)

    def test_backup_manager(self):
        manager = BackupManager(self.config)
        # Create a dummy file to backup
        test_file = os.path.join(self.temp_dir.name, "test_file.txt")
        with open(test_file, "w") as f:
            f.write("This is a test file.")
        self.config["backup_paths"] = [test_file]
        
        backup_result = manager.perform_backup([test_file], {"type": "zip"})
        self.assertEqual(backup_result["status"], "success")
        self.assertTrue(os.path.exists(backup_result["path"]))

        restore_dir = os.path.join(self.temp_dir.name, "restore-root", "restore test")
        self.config["backup_restore_dir"] = os.path.dirname(restore_dir)
        manager = BackupManager(self.config)
        restore_result = manager.restore_backup(backup_result["path"], restore_dir)
        self.assertEqual(restore_result["status"], "success")
        self.assertTrue(os.path.exists(os.path.join(restore_dir, "test_file.txt")))

        os.remove(test_file)
        os.remove(backup_result["path"])
        shutil.rmtree(os.path.dirname(restore_dir))
        shutil.rmtree(self.config["backup_base_dir"])

    def test_backup_manager_fails_closed_on_unreadable_source_tree(self):
        source_dir = os.path.join(self.temp_dir.name, "unreadable-data")
        os.makedirs(source_dir)
        self.config["backup_paths"] = [source_dir]
        manager = BackupManager(self.config)

        def unreadable_walk(_path, *, followlinks, onerror):
            self.assertFalse(followlinks)
            onerror(PermissionError("synthetic read denied"))
            return iter(())

        with patch("src.backup.backup_manager.os.walk", side_effect=unreadable_walk):
            result = manager.perform_backup([source_dir])

        self.assertEqual(result["status"], "failure")
        self.assertEqual(manager.list_backups(), [])
        self.assertFalse(any(name.endswith(".partial") for name in os.listdir(manager.backup_base_dir)))

    def test_backup_manager_fails_closed_on_nested_symbolic_link(self):
        source_dir = os.path.join(self.temp_dir.name, "linked-data")
        outside_dir = os.path.join(self.temp_dir.name, "outside-data")
        os.makedirs(source_dir)
        os.makedirs(outside_dir)
        try:
            os.symlink(outside_dir, os.path.join(source_dir, "linked"), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Directory symlinks are unavailable in this Windows environment")
        self.config["backup_paths"] = [source_dir]
        manager = BackupManager(self.config)

        result = manager.perform_backup([source_dir])

        self.assertEqual(result["status"], "failure")
        self.assertEqual(manager.list_backups(), [])

    def test_backup_manager_rejects_sources_larger_than_restore_limit(self):
        source_file = os.path.join(self.temp_dir.name, "oversized.txt")
        with open(source_file, "wb") as handle:
            handle.write(b"too large")
        self.config["backup_paths"] = [source_file]
        manager = BackupManager(self.config)

        with patch("src.backup.backup_manager.MAX_RESTORE_BYTES", 4):
            result = manager.perform_backup([source_file])

        self.assertEqual(result["status"], "failure")
        self.assertEqual(manager.list_backups(), [])

    def test_backup_manager_snapshots_live_sqlite_wal_database(self):
        database_path = os.path.join(self.temp_dir.name, "live.sqlite3")
        writer = sqlite3.connect(database_path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE ledger_entries (id INTEGER PRIMARY KEY, amount TEXT NOT NULL)")
        writer.execute("INSERT INTO ledger_entries (amount) VALUES ('42.50')")
        writer.commit()

        manager = BackupManager({
            "backup_base_dir": os.path.join(self.temp_dir.name, "database-backups"),
            "backup_paths": [database_path],
            "database_path": database_path,
        })
        result = manager.perform_backup([database_path])

        self.assertEqual(result["status"], "success", result)
        with zipfile.ZipFile(result["path"]) as archive:
            snapshot_path = os.path.join(self.temp_dir.name, "restored.sqlite3")
            with archive.open("live.sqlite3") as source, open(snapshot_path, "wb") as target:
                shutil.copyfileobj(source, target)
        with closing(sqlite3.connect(snapshot_path)) as snapshot:
            rows = snapshot.execute("SELECT amount FROM ledger_entries").fetchall()
            integrity = snapshot.execute("PRAGMA integrity_check").fetchone()

        self.assertEqual(rows, [("42.50",)])
        self.assertEqual(integrity, ("ok",))
        leftovers = [name for name in os.listdir(manager.backup_base_dir) if name.startswith(".fab-sqlite-")]
        self.assertEqual(leftovers, [])

    def test_backup_manager_rejects_sibling_prefix_zip_traversal(self):
        manager = BackupManager(self.config)
        archive_path = os.path.join(manager.backup_base_dir, "malicious.zip")
        restore_dir = "traversal"
        escaped_sibling = os.path.join(restore_dir + "-escape", "payload.txt")
        escaped_sibling = os.path.join(manager.restore_base_dir, escaped_sibling)
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("../restore-escape/payload.txt", "must not be extracted")

        result = manager.restore_backup(archive_path, restore_dir)

        self.assertEqual(result["status"], "failure")
        self.assertIn("Unsafe path", result["message"])
        self.assertFalse(os.path.exists(escaped_sibling))

    def test_backup_manager_rejects_backslash_zip_traversal(self):
        manager = BackupManager(self.config)
        archive_path = os.path.join(manager.backup_base_dir, "backslash-traversal.zip")
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("..\\escape\\payload.txt", "must not be extracted")

        result = manager.restore_backup(archive_path, "backslash-target")

        self.assertEqual(result["status"], "failure")
        self.assertIn("Unsafe path", result["message"])

    def test_backup_manager_rejects_nonportable_archive_paths(self):
        manager = BackupManager(self.config)
        unsafe_members = ("C:/escape.txt", "folder/CON.txt", "trailing./file.txt")

        for index, member in enumerate(unsafe_members):
            with self.subTest(member=member):
                archive_path = os.path.join(manager.backup_base_dir, f"unsafe-{index}.zip")
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr(member, "must not be extracted")

                result = manager.restore_backup(archive_path, f"unsafe-target-{index}")

                self.assertEqual(result["status"], "failure")
                self.assertIn("Unsafe path", result["message"])

    def test_backup_manager_rejects_case_colliding_archive_paths(self):
        manager = BackupManager(self.config)
        archive_path = os.path.join(manager.backup_base_dir, "case-collision.zip")
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("Receipt.txt", "first")
            archive.writestr("receipt.txt", "second")

        result = manager.restore_backup(archive_path, "case-collision")

        self.assertEqual(result["status"], "failure")
        self.assertIn("duplicate paths", result["message"])

    def test_backup_manager_restores_from_configured_source_subdirectory(self):
        source_dir = os.path.join(self.temp_dir.name, "configured-data")
        os.makedirs(source_dir)
        source_file = os.path.join(source_dir, "nested.txt")
        with open(source_file, "w", encoding="utf-8") as handle:
            handle.write("configured content")
        manager = BackupManager({
            "backup_base_dir": os.path.join(self.temp_dir.name, "backup-output"),
            "backup_restore_dir": os.path.join(self.temp_dir.name, "restore-output"),
            "backup_paths": [source_dir],
        })

        result = manager.perform_backup([source_file])

        self.assertEqual(result["status"], "success")
        self.assertTrue(os.path.isfile(result["path"]))
        with zipfile.ZipFile(result["path"]) as archive:
            self.assertIn("nested.txt", archive.namelist())

    def test_backup_manager_rejects_source_outside_configured_root(self):
        source_dir = os.path.join(self.temp_dir.name, "configured-data")
        outside_dir = os.path.join(self.temp_dir.name, "outside-data")
        os.makedirs(source_dir)
        os.makedirs(outside_dir)
        source_file = os.path.join(source_dir, "nested.txt")
        with open(source_file, "w", encoding="utf-8") as handle:
            handle.write("configured content")
        outside_file = os.path.join(outside_dir, "other.txt")
        with open(outside_file, "w", encoding="utf-8") as handle:
            handle.write("unconfigured content")
        manager = BackupManager({
            "backup_base_dir": os.path.join(self.temp_dir.name, "backup-output"),
            "backup_paths": [source_dir],
        })

        result = manager.perform_backup([outside_file])

        self.assertEqual(result["status"], "failure")
        self.assertEqual(manager.list_backups(), [])

    def test_failed_restore_preserves_existing_destination(self):
        manager = BackupManager({
            "backup_base_dir": os.path.join(self.temp_dir.name, "backup-output"),
            "backup_restore_dir": os.path.join(self.temp_dir.name, "restore-output"),
        })
        archive_path = os.path.join(manager.backup_base_dir, "corrupt.zip")
        with open(archive_path, "wb") as handle:
            handle.write(b"not a zip archive")
        restore_root = os.path.join(manager.restore_base_dir, "existing")
        os.makedirs(restore_root)
        sentinel = os.path.join(restore_root, "keep.txt")
        with open(sentinel, "w", encoding="utf-8") as handle:
            handle.write("preserve")

        result = manager.restore_backup(archive_path, restore_root, allow_overwrite=True)

        self.assertEqual(result["status"], "failure")
        with open(sentinel, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "preserve")

    def test_backup_manager_rejects_restore_through_existing_symlink(self):
        manager = BackupManager(self.config)
        archive_path = os.path.join(manager.backup_base_dir, "symlink.zip")
        restore_dir = "restore-symlink"
        restore_root = os.path.join(manager.restore_base_dir, restore_dir)
        outside_dir = os.path.join(self.temp_dir.name, "outside")
        outside_file = os.path.join(outside_dir, "payload.txt")
        os.makedirs(restore_root)
        os.makedirs(outside_dir)
        try:
            os.symlink(outside_dir, os.path.join(restore_root, "linked"), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Directory symlinks are unavailable in this Windows environment")
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("linked/payload.txt", "must not be extracted")

        result = manager.restore_backup(archive_path, restore_dir, allow_overwrite=True)

        self.assertEqual(result["status"], "failure")
        self.assertIn("Unsafe path", result["message"])
        self.assertFalse(os.path.exists(outside_file))

if __name__ == "__main__":
    unittest.main()


