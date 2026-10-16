import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from src.operations import local_intake as local_intake_module
from src.operations.local_intake import LOCAL_FOLDER_INTAKE_LEASE_NAME, LocalFolderIntake
from src.document_processors.document_resource_limits import (
    MAX_SOURCE_DOCUMENT_BYTES,
    MAX_TEXT_DOCUMENT_BYTES,
)
from src.operations.local_ledger import LocalOperationsLedger


class TestLocalFolderIntake(unittest.TestCase):
    def test_hash_rejects_unexpected_eof_before_recorded_file_size(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "receipt.pdf")
            with open(path, "wb") as handle:
                handle.write(b"complete receipt bytes")

            real_fdopen = os.fdopen

            class PrematureEOF:
                def __init__(self, handle):
                    self.handle = handle
                    self.has_returned_data = False

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc_value, traceback):
                    self.handle.close()

                def fileno(self):
                    return self.handle.fileno()

                def read(self, size=-1):
                    if self.has_returned_data:
                        return b""
                    self.has_returned_data = True
                    return self.handle.read(min(3, size))

            def open_short_reader(descriptor, mode):
                return PrematureEOF(real_fdopen(descriptor, mode))

            with mock.patch.object(
                local_intake_module.os,
                "fdopen",
                side_effect=open_short_reader,
            ):
                with self.assertRaisesRegex(OSError, "source_short_read"):
                    local_intake_module._sha256_file(path)

    def test_fetched_document_requires_an_explicit_source_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(db_path)
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

    def test_rescan_skips_manifest_mutation_when_another_scan_holds_lease(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            with open(os.path.join(intake_dir, "receipt.pdf"), "wb") as handle:
                handle.write(b"receipt")
            ledger.acquire_runtime_lease(LOCAL_FOLDER_INTAKE_LEASE_NAME, "other-scan")

            result = LocalFolderIntake(ledger).rescan([intake_dir])

            self.assertEqual(result["status"], "already_running")
            self.assertEqual(result["skipped"][0]["reason"], "intake_already_running")
            self.assertEqual(ledger.list_documents(), [])
            self.assertTrue(ledger.release_runtime_lease(LOCAL_FOLDER_INTAKE_LEASE_NAME, "other-scan"))

    def test_rescan_processes_large_folder_in_resumable_bounded_batches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(db_path)
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            for index in range(5):
                with open(os.path.join(intake_dir, f"receipt-{index}.pdf"), "wb") as handle:
                    handle.write(f"receipt {index}".encode())

            intake = LocalFolderIntake(ledger)
            intake.MAX_FILES_PER_SCAN = 2
            batches = [intake.rescan([intake_dir])]
            ledger = LocalOperationsLedger(db_path)
            intake = LocalFolderIntake(ledger)
            intake.MAX_FILES_PER_SCAN = 2
            batches.extend(intake.rescan([intake_dir]) for _ in range(2))

            self.assertEqual([batch["scanned"] for batch in batches], [2, 2, 1])
            self.assertEqual(
                [batch["scanIncomplete"][intake_dir] for batch in batches],
                [True, True, False],
            )
            self.assertEqual(len(ledger.list_documents(limit=20)), 5)

    def test_rescan_enforces_actual_aggregate_bytes_when_files_grow_after_admission(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            path = os.path.join(intake_dir, "receipt.pdf")
            with open(path, "wb") as handle:
                handle.write(b"abc")

            intake = LocalFolderIntake(ledger)
            intake.MAX_BYTES_PER_SCAN = 5
            original_hash = local_intake_module._sha256_file

            def grow_before_hash(file_path, **kwargs):
                with open(file_path, "ab") as handle:
                    handle.write(b"def")
                return original_hash(file_path, **kwargs)

            with mock.patch.object(local_intake_module, "_sha256_file", side_effect=grow_before_hash):
                first = intake.rescan([intake_dir])

            source = ledger.list_source_accounts(source_type="local_folder")[0]
            self.assertEqual(first["skipped"][0]["reason"], "scan_byte_limit_exceeded")
            self.assertTrue(first["scanIncomplete"][intake_dir])
            self.assertEqual(first["scanBytes"][intake_dir], 0)
            self.assertIsNone(ledger.get_source_scan_cursor(source["id"]))
            self.assertEqual(ledger.list_documents(), [])

            intake.MAX_BYTES_PER_SCAN = 10
            retried = intake.rescan([intake_dir])

            self.assertEqual(retried["registered"], 1)
            self.assertEqual(retried["scanBytes"][intake_dir], 6)
            self.assertEqual(len(ledger.list_documents()), 1)

    def test_rescan_wraps_to_discover_new_files_before_saved_cursor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            for name in ("b.pdf", "c.pdf", "d.pdf"):
                with open(os.path.join(intake_dir, name), "wb") as handle:
                    handle.write(name.encode())

            intake = LocalFolderIntake(ledger)
            intake.MAX_FILES_PER_SCAN = 2
            first = intake.rescan([intake_dir])
            self.assertEqual(first["scanned"], 2)
            with open(os.path.join(intake_dir, "a.pdf"), "wb") as handle:
                handle.write(b"new earlier-sorted file")

            second = intake.rescan([intake_dir])
            third = intake.rescan([intake_dir])

            self.assertEqual(second["scanned"], 1)
            self.assertEqual(third["registered"], 1)
            self.assertIn(
                "a.pdf",
                {document["original_filename"] for document in ledger.list_documents(limit=20)},
            )

    def test_resumed_batch_uses_manifest_without_rewalking_source_tree(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            for index in range(3):
                with open(os.path.join(intake_dir, f"receipt-{index}.pdf"), "wb") as handle:
                    handle.write(f"receipt {index}".encode())

            intake = LocalFolderIntake(ledger)
            intake.MAX_FILES_PER_SCAN = 1
            with mock.patch.object(
                intake,
                "_iter_document_paths",
                wraps=intake._iter_document_paths,
            ) as walk:
                intake.rescan([intake_dir])
                intake.rescan([intake_dir])

            self.assertEqual(walk.call_count, 1)

    def test_manifest_entry_limit_fails_closed_and_requires_manual_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            for index in range(3):
                with open(os.path.join(intake_dir, f"receipt-{index}.pdf"), "wb") as handle:
                    handle.write(str(index).encode())

            intake = LocalFolderIntake(ledger)
            intake.MAX_MANIFEST_ENTRIES = 2
            result = intake.rescan([intake_dir])

            self.assertEqual(result["status"], "attention_required")
            self.assertEqual(result["skipped"][0]["reason"], "manifest_limit_exceeded")
            self.assertEqual(ledger.list_documents(), [])
            account = ledger.list_source_accounts(source_type="local_folder")[0]
            self.assertEqual(account["status"], "attention_required")
            self.assertEqual(
                ledger.get_source_scan_batch(account["id"], None, 10),
                [],
            )
            self.assertFalse(intake.has_pending_changes([intake_dir]))
            retried = intake.rescan([intake_dir])
            self.assertEqual(retried["status"], "attention_required")

    def test_directory_limit_fails_closed_without_persisting_a_partial_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(os.path.join(intake_dir, "nested"))
            with open(os.path.join(intake_dir, "nested", "receipt.pdf"), "wb") as handle:
                handle.write(b"receipt")
            intake = LocalFolderIntake(ledger)
            intake.MAX_DIRECTORIES_PER_SCAN = 1

            result = intake.rescan([intake_dir])

            self.assertEqual(result["skipped"][0]["reason"], "directory_limit_exceeded")
            self.assertEqual(ledger.list_documents(), [])
            account = ledger.list_source_accounts(source_type="local_folder")[0]
            self.assertEqual(ledger.get_source_scan_batch(account["id"], None, 10), [])
            with mock.patch.object(
                intake,
                "_iter_document_paths",
                side_effect=AssertionError("attention-required roots must not be rescanned automatically"),
            ):
                self.assertFalse(intake.has_pending_changes([intake_dir]))

    def test_sha256_rejects_symlink_and_applies_default_size_cap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            outside = os.path.join(temp_dir, "outside.pdf")
            with open(outside, "wb") as handle:
                handle.write(b"external")
            linked = os.path.join(temp_dir, "linked.pdf")
            try:
                os.symlink(outside, linked)
            except (OSError, NotImplementedError):
                self.skipTest("File symlinks are unavailable in this environment")
            with self.assertRaises(OSError):
                local_intake_module._sha256_file(linked)

            with mock.patch("src.operations.local_intake.MAX_SOURCE_DOCUMENT_BYTES", 4):
                with self.assertRaises(local_intake_module.DocumentSizeLimitError):
                    local_intake_module._sha256_file(outside)

    def test_rescan_skips_text_documents_over_memory_bound_before_hashing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            source = os.path.join(intake_dir, "oversized.csv")
            with open(source, "wb") as handle:
                handle.truncate(MAX_TEXT_DOCUMENT_BYTES + 1)

            result = LocalFolderIntake(ledger).rescan([intake_dir])

            self.assertEqual(result["registered"], 0)
            self.assertEqual(result["skipped"][0]["reason"], "text_document_exceeds_limit")
            self.assertEqual(result["skipped"][0]["maxBytes"], MAX_TEXT_DOCUMENT_BYTES)
            self.assertEqual(ledger.list_documents(), [])

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

    def test_rescan_skips_oversized_binary_before_hashing_or_registration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            source = os.path.join(intake_dir, "oversized.pdf")
            with open(source, "wb") as handle:
                handle.truncate(MAX_SOURCE_DOCUMENT_BYTES + 1)

            result = LocalFolderIntake(ledger).rescan([intake_dir])

            self.assertEqual(result["registered"], 0)
            self.assertEqual(result["skipped"][0]["reason"], "document_exceeds_limit")
            self.assertEqual(result["skipped"][0]["maxBytes"], MAX_SOURCE_DOCUMENT_BYTES)
            self.assertEqual(ledger.list_documents(), [])

    def test_rescan_enforces_size_limit_again_while_hashing_file_that_grows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            intake_dir = os.path.join(temp_dir, "sort-out")
            os.makedirs(intake_dir)
            source = os.path.join(intake_dir, "growing.pdf")
            with open(source, "wb") as handle:
                handle.write(b"1234")

            with mock.patch("src.operations.local_intake.MAX_SOURCE_DOCUMENT_BYTES", 5):
                original_hash = local_intake_module._sha256_file

                def grow_before_hash(path, **kwargs):
                    with open(path, "ab") as handle:
                        handle.write(b"678")
                    return original_hash(path, **kwargs)

                with mock.patch("src.operations.local_intake._sha256_file", side_effect=grow_before_hash):
                    result = LocalFolderIntake(ledger).rescan([intake_dir])

            self.assertEqual(result["registered"], 0)
            self.assertEqual(result["skipped"][0]["reason"], "document_exceeds_limit")
            self.assertEqual(result["skipped"][0]["maxBytes"], 5)
            self.assertEqual(ledger.list_documents(), [])


if __name__ == "__main__":
    unittest.main()
