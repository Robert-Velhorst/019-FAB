import os
import sqlite3
import tempfile
import unittest

from src.operations.local_intake import LocalFolderIntake
from src.operations.local_ledger import LocalOperationsLedger


class TestLocalFolderIntake(unittest.TestCase):
    def test_fetched_document_requires_an_explicit_source_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_path = os.path.join(temp_dir, "receipt.pdf")
            with open(document_path, "wb") as handle:
                handle.write(b"receipt bytes")

            result = LocalFolderIntake(ledger).register_fetched_document({
                "local_path": document_path,
                "original_filename": "receipt.pdf",
            })

            self.assertEqual(result["skipped"]["reason"], "source_root_missing")
            self.assertEqual(ledger.list_documents(), [])

    def test_fetched_document_rejects_symlink_outside_source_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            source_root = os.path.join(temp_dir, "downloads")
            outside_root = os.path.join(temp_dir, "outside")
            os.makedirs(source_root)
            os.makedirs(outside_root)
            outside_path = os.path.join(outside_root, "receipt.pdf")
            with open(outside_path, "wb") as handle:
                handle.write(b"must not be read")
            linked_path = os.path.join(source_root, "receipt.pdf")
            try:
                os.symlink(outside_path, linked_path)
            except (OSError, NotImplementedError):
                self.skipTest("File symlinks are unavailable in this environment")

            result = LocalFolderIntake(ledger).register_fetched_document(
                {"local_path": linked_path, "original_filename": "receipt.pdf"},
                root=source_root,
            )

            self.assertEqual(result["skipped"]["reason"], "path_outside_source_root")
            self.assertEqual(ledger.list_documents(), [])

    def test_rescan_imports_documents_flags_duplicates_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            first_path = os.path.join(intake_dir, "receipt-a.pdf")
            duplicate_path = os.path.join(intake_dir, "receipt-copy.pdf")
            ignored_path = os.path.join(intake_dir, "desktop.ini")
            with open(first_path, "wb") as handle:
                handle.write(b"same receipt bytes")
            with open(duplicate_path, "wb") as handle:
                handle.write(b"same receipt bytes")
            with open(ignored_path, "wb") as handle:
                handle.write(b"ignored")

            intake = LocalFolderIntake(ledger)
            first_summary = intake.rescan([intake_dir])

            self.assertEqual(first_summary["scanned"], 2)
            self.assertEqual(first_summary["registered"], 2)
            self.assertEqual(first_summary["duplicates"], 1)
            self.assertEqual(first_summary["alreadyRegistered"], 0)
            self.assertEqual(first_summary["skipped"], [])

            metrics = ledger.dashboard_metrics()
            self.assertEqual(metrics["documents"], 2)
            self.assertEqual(metrics["duplicates"], 1)
            self.assertEqual(metrics["duplicate_candidates"], 1)
            self.assertEqual(metrics["open_duplicate_candidates"], 1)
            self.assertEqual(metrics["pending_review"], 1)

            review_items = ledger.list_review_items(status="pending")
            self.assertEqual(review_items[0]["reason"], "duplicate_candidate")
            self.assertIsNotNone(review_items[0]["corrected_data"]["duplicateCandidateId"])
            duplicate_candidates = ledger.list_duplicate_candidates(status="pending")
            self.assertEqual(len(duplicate_candidates), 1)
            self.assertEqual(duplicate_candidates[0]["match_type"], "exact_content_hash")
            self.assertEqual(duplicate_candidates[0]["confidence_score"], 1.0)
            duplicate_documents = [
                document
                for document in ledger.list_documents()
                if document["duplicate_of_document_id"] is not None
            ]
            sources = ledger.list_source_accounts(source_type="local_folder")
            self.assertEqual(len(duplicate_documents), 1)
            self.assertEqual(duplicate_documents[0]["processing_status"], "needs_review")
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0]["status"], "ready")
            self.assertEqual(sources[0]["documents_seen"], 2)
            self.assertEqual(sources[0]["documents_imported"], 2)
            self.assertEqual(sources[0]["duplicates_detected"], 1)
            self.assertEqual(duplicate_documents[0]["source_account_id"], sources[0]["id"])

            second_summary = intake.rescan([intake_dir])

            self.assertEqual(second_summary["scanned"], 2)
            self.assertEqual(second_summary["registered"], 0)
            self.assertEqual(second_summary["duplicates"], 0)
            self.assertEqual(second_summary["alreadyRegistered"], 2)
            self.assertEqual(ledger.dashboard_metrics()["documents"], 2)
            self.assertEqual(ledger.list_source_accounts()[0]["documents_seen"], 4)

    def test_rescan_reports_missing_folders_without_failing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            missing_dir = os.path.join(temp_dir, "missing")

            summary = LocalFolderIntake(ledger).rescan([missing_dir])

            self.assertEqual(summary["scanned"], 0)
            self.assertEqual(summary["registered"], 0)
            self.assertEqual(summary["skipped"][0]["reason"], "folder_missing")
            self.assertEqual(ledger.list_source_accounts()[0]["status"], "missing")

    def test_content_identity_survives_semantic_fingerprint_updates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            original_path = os.path.join(intake_dir, "receipt.pdf")
            with open(original_path, "wb") as handle:
                handle.write(b"stable receipt bytes")

            intake = LocalFolderIntake(ledger)
            intake.rescan([intake_dir])
            original = ledger.list_documents()[0]
            ledger.update_document(original["id"], {"duplicateFingerprint": "semantic-fingerprint"})

            unchanged = intake.rescan([intake_dir])
            copy_path = os.path.join(intake_dir, "receipt-copy.pdf")
            with open(copy_path, "wb") as handle:
                handle.write(b"stable receipt bytes")
            with_copy = intake.rescan([intake_dir])

            self.assertEqual(unchanged["alreadyRegistered"], 1)
            self.assertEqual(unchanged["registered"], 0)
            self.assertEqual(with_copy["duplicates"], 1)
            documents = sorted(ledger.list_documents(), key=lambda item: item["id"])
            self.assertEqual(len(documents), 2)
            self.assertEqual(documents[0]["duplicate_fingerprint"], "semantic-fingerprint")
            self.assertEqual(documents[0]["content_sha256"], documents[1]["content_sha256"])
            self.assertEqual(documents[1]["duplicate_of_document_id"], documents[0]["id"])

    def test_pending_change_check_fails_closed_without_content_hash_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            source_path = os.path.join(intake_dir, "receipt.pdf")
            with open(source_path, "wb") as handle:
                handle.write(b"receipt evidence")

            intake = LocalFolderIntake(ledger)
            intake.rescan([intake_dir])
            document = ledger.list_documents(limit=1)[0]
            connection = sqlite3.connect(ledger.path)
            try:
                connection.execute(
                    """
                    UPDATE bookkeeping_documents
                    SET content_sha256 = NULL, storage_path = ?, metadata_json = '{}'
                    WHERE id = ?
                    """,
                    (os.path.join(temp_dir, "missing-evidence.pdf"), document["id"]),
                )
                connection.commit()
            finally:
                connection.close()

            self.assertTrue(intake.has_pending_changes([intake_dir]))


if __name__ == "__main__":
    unittest.main()
