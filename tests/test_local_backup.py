import json
import hashlib
import os
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from src.operations.local_backup import (
    BACKUP_FORMAT_V1,
    BACKUP_FORMAT_V2,
    BACKUP_FORMAT_V3,
    FULL_RESTORE_CONFIRMATION_PHRASE,
    LEGACY_RESTORE_CONFIRMATION_PHRASE,
    LocalBackupService,
    RESTORE_CONFIRMATION_PHRASE,
    RESTORE_MODE_FULL,
)
from src.operations.local_ledger import LocalOperationsLedger
from src.worker.runtime import WorkerAlreadyRunningError


class TestLocalBackupService(unittest.TestCase):
    def test_create_backup_writes_manifest_and_audit_event(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            backup_dir = os.path.join(temp_dir, "backups")
            ledger = LocalOperationsLedger(ledger_path)
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-backup-1",
                "originalFilename": "receipt.pdf",
            })

            result = LocalBackupService(ledger, {"fab_local_backup_dir": backup_dir}).create_backup(
                note="test",
                actor="test-operator",
            )

            self.assertTrue(result["success"])
            self.assertEqual(result["manifest"]["format"], BACKUP_FORMAT_V3)
            self.assertEqual(
                LocalBackupService(ledger, {"fab_local_backup_dir": backup_dir})
                .inspect_backup(result["backupPath"])["integrityStatus"],
                "authenticated",
            )
            self.assertTrue(os.path.exists(result["backupPath"]))
            self.assertEqual(
                result["manifest"]["sourceEvidence"]["coverageStatus"],
                "incomplete",
            )
            with zipfile.ZipFile(result["backupPath"], "r") as archive:
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                self.assertEqual(manifest["ledgerFilename"], "fab_operations.sqlite3")
                self.assertIn("fab_operations.sqlite3", archive.namelist())
            audit_event = ledger.list_audit_events()[0]
            self.assertEqual(audit_event["action"], "local_backup.created")
            self.assertEqual(audit_event["details"]["actor"], "test-operator")

    def test_restore_requires_confirmation_and_restores_ledger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            backup_dir = os.path.join(temp_dir, "backups")
            ledger = LocalOperationsLedger(ledger_path)
            original_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-original",
                "originalFilename": "original.pdf",
            })
            service = LocalBackupService(ledger, {
                "fab_local_backup_dir": backup_dir,
                "fab_maintenance_mode": True,
            })
            backup = service.create_backup(note="restore test")
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-new",
                "originalFilename": "new.pdf",
            })

            blocked = service.restore_backup(backup["backupPath"], "wrong")
            restored = service.restore_backup(backup["backupPath"], RESTORE_CONFIRMATION_PHRASE)
            documents = ledger.list_documents(limit=10)

            self.assertFalse(blocked["success"])
            self.assertEqual(blocked["status"], "requires_confirmation")
            self.assertTrue(restored["success"])
            self.assertEqual([document["id"] for document in documents], [original_id])
            self.assertEqual(ledger.list_audit_events()[0]["action"], "local_backup.restored")
            self.assertTrue(os.path.exists(restored["preRestoreBackupPath"]))

    def test_full_restore_recovers_source_bytes_and_rewrites_ledger_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            backup_dir = os.path.join(temp_dir, "backups")
            restore_root = os.path.join(temp_dir, "restored-evidence")
            original_path = os.path.join(temp_dir, "original.pdf")
            current_path = os.path.join(temp_dir, "current.pdf")
            original_bytes = b"original recovery package evidence"
            current_bytes = b"current rollback evidence"
            with open(original_path, "wb") as handle:
                handle.write(original_bytes)
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "full-restore-1",
                "originalFilename": "original.pdf",
                "storagePath": original_path,
                "contentSha256": hashlib.sha256(original_bytes).hexdigest(),
            })
            service = LocalBackupService(ledger, {
                "fab_local_backup_dir": backup_dir,
                "fab_backup_restore_source_root": restore_root,
                "fab_maintenance_mode": True,
            })
            backup = service.create_backup(require_complete_source_evidence=True)
            with open(current_path, "wb") as handle:
                handle.write(current_bytes)
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "full-restore-1",
                "originalFilename": "current.pdf",
                "storagePath": current_path,
                "contentSha256": hashlib.sha256(current_bytes).hexdigest(),
            })
            os.remove(original_path)

            restored = service.restore_backup(
                backup["backupPath"],
                FULL_RESTORE_CONFIRMATION_PHRASE,
                restore_mode=RESTORE_MODE_FULL,
                actor="recovery-test",
            )

            self.assertTrue(restored["success"])
            self.assertTrue(restored["sourceTargetCreated"])
            document = ledger.get_document(document_id)
            self.assertTrue(str(document["storage_path"]).startswith(restore_root))
            self.assertEqual(document["content_sha256"], hashlib.sha256(original_bytes).hexdigest())
            with open(document["storage_path"], "rb") as handle:
                self.assertEqual(handle.read(), original_bytes)
            self.assertTrue(os.path.exists(restored["preRestoreBackupPath"]))

            repeated = service.restore_backup(
                backup["backupPath"],
                FULL_RESTORE_CONFIRMATION_PHRASE,
                restore_mode=RESTORE_MODE_FULL,
            )
            self.assertTrue(repeated["success"])
            self.assertFalse(repeated["sourceTargetCreated"])
            self.assertEqual(repeated["sourceRestoreTarget"], restored["sourceRestoreTarget"])

    def test_full_restore_blocks_tampered_existing_immutable_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, "receipt.pdf")
            source_bytes = b"immutable source evidence"
            with open(source_path, "wb") as handle:
                handle.write(source_bytes)
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "immutable-restore-1",
                "originalFilename": "receipt.pdf",
                "storagePath": source_path,
                "contentSha256": hashlib.sha256(source_bytes).hexdigest(),
            })
            service = LocalBackupService(ledger, {
                "fab_local_backup_dir": os.path.join(temp_dir, "backups"),
                "fab_backup_restore_source_root": os.path.join(temp_dir, "restored"),
                "fab_maintenance_mode": True,
            })
            backup = service.create_backup(require_complete_source_evidence=True)
            ready = service.plan_restore(backup["backupPath"], restore_mode=RESTORE_MODE_FULL)
            entry = ready["manifest"]["sourceEvidence"]
            inspected = service.inspect_backup(backup["backupPath"])
            archive_path = inspected["manifest"]["sourceEvidence"]["entries"][0]["archivePath"]
            target_path = os.path.join(ready["sourceRestoreTarget"], *archive_path.split("/"))
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            with open(target_path, "wb") as handle:
                handle.write(b"tampered")

            blocked = service.restore_backup(
                backup["backupPath"],
                FULL_RESTORE_CONFIRMATION_PHRASE,
                restore_mode=RESTORE_MODE_FULL,
            )

            self.assertEqual(entry["coverageStatus"], "complete")
            self.assertFalse(blocked["success"])
            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(blocked["blockers"][0]["code"], "source_restore_target_blocked")

    def test_full_restore_rejects_manifest_to_ledger_document_mismatch_before_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, "receipt.pdf")
            source_bytes = b"covered restore evidence"
            with open(source_path, "wb") as handle:
                handle.write(source_bytes)
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "coverage-mismatch-1",
                "originalFilename": "receipt.pdf",
                "storagePath": source_path,
                "contentSha256": hashlib.sha256(source_bytes).hexdigest(),
            })
            service = LocalBackupService(ledger, {
                "fab_local_backup_dir": os.path.join(temp_dir, "backups"),
                "fab_backup_restore_source_root": os.path.join(temp_dir, "restored"),
                "fab_maintenance_mode": True,
            })
            backup = service.create_backup(require_complete_source_evidence=True)
            with zipfile.ZipFile(backup["backupPath"], "r") as archive:
                members = {name: archive.read(name) for name in archive.namelist()}
            altered_ledger_path = os.path.join(temp_dir, "altered.sqlite3")
            with open(altered_ledger_path, "wb") as handle:
                handle.write(members["fab_operations.sqlite3"])
            LocalOperationsLedger(altered_ledger_path).register_document({
                "source": "scanner",
                "sourceDocumentId": "uncovered-restore-row",
                "originalFilename": "uncovered.pdf",
            })
            with open(altered_ledger_path, "rb") as handle:
                members["fab_operations.sqlite3"] = handle.read()
            manifest = json.loads(members["manifest.json"].decode("utf-8"))
            manifest["ledgerBytes"] = len(members["fab_operations.sqlite3"])
            manifest["ledgerSha256"] = hashlib.sha256(
                members["fab_operations.sqlite3"]
            ).hexdigest()
            manifest.pop("integrity", None)
            manifest["integrity"] = service._sign_manifest(manifest)
            members["manifest.json"] = json.dumps(manifest).encode("utf-8")
            with zipfile.ZipFile(backup["backupPath"], "w", zipfile.ZIP_DEFLATED) as archive:
                for name, content in members.items():
                    archive.writestr(name, content)

            with self.assertRaisesRegex(ValueError, "does not cover every restored ledger document"):
                service.restore_backup(
                    backup["backupPath"],
                    FULL_RESTORE_CONFIRMATION_PHRASE,
                    restore_mode=RESTORE_MODE_FULL,
                )

            self.assertEqual([item["id"] for item in ledger.list_documents(limit=10)], [document_id])

    def test_full_restore_rejects_dangling_link_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, "receipt.pdf")
            source_bytes = b"link-safe evidence"
            with open(source_path, "wb") as handle:
                handle.write(source_bytes)
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "link-restore-1",
                "originalFilename": "receipt.pdf",
                "storagePath": source_path,
                "contentSha256": hashlib.sha256(source_bytes).hexdigest(),
            })
            service = LocalBackupService(ledger, {
                "fab_local_backup_dir": os.path.join(temp_dir, "backups"),
                "fab_backup_restore_source_root": os.path.join(temp_dir, "restored"),
                "fab_maintenance_mode": True,
            })
            backup = service.create_backup(require_complete_source_evidence=True)
            ready = service.plan_restore(backup["backupPath"], restore_mode=RESTORE_MODE_FULL)
            os.makedirs(os.path.dirname(ready["sourceRestoreTarget"]), exist_ok=True)
            try:
                os.symlink(
                    os.path.join(temp_dir, "missing-link-target"),
                    ready["sourceRestoreTarget"],
                    target_is_directory=True,
                )
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"Directory symlinks are unavailable: {exc}")

            blocked = service.plan_restore(
                backup["backupPath"],
                restore_mode=RESTORE_MODE_FULL,
            )

            self.assertFalse(blocked["canRestore"])
            self.assertEqual(blocked["blockers"][0]["code"], "source_restore_target_blocked")

    def test_full_restore_rolls_back_ledger_when_post_restore_verification_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original_path = os.path.join(temp_dir, "original.pdf")
            current_path = os.path.join(temp_dir, "current.pdf")
            original_bytes = b"older evidence"
            current_bytes = b"newer evidence"
            with open(original_path, "wb") as handle:
                handle.write(original_bytes)
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "rollback-restore-1",
                "originalFilename": "original.pdf",
                "storagePath": original_path,
                "contentSha256": hashlib.sha256(original_bytes).hexdigest(),
            })
            service = LocalBackupService(ledger, {
                "fab_local_backup_dir": os.path.join(temp_dir, "backups"),
                "fab_backup_restore_source_root": os.path.join(temp_dir, "restored"),
                "fab_maintenance_mode": True,
            })
            backup = service.create_backup(require_complete_source_evidence=True)
            with open(current_path, "wb") as handle:
                handle.write(current_bytes)
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "rollback-restore-1",
                "originalFilename": "current.pdf",
                "storagePath": current_path,
                "contentSha256": hashlib.sha256(current_bytes).hexdigest(),
            })

            with patch.object(
                service,
                "_verify_live_restore",
                side_effect=ValueError("forced verification failure"),
            ):
                with self.assertRaisesRegex(ValueError, "forced verification failure"):
                    service.restore_backup(
                        backup["backupPath"],
                        FULL_RESTORE_CONFIRMATION_PHRASE,
                        restore_mode=RESTORE_MODE_FULL,
                    )

            document = ledger.get_document(document_id)
            self.assertEqual(document["storage_path"], current_path)
            self.assertEqual(document["content_sha256"], hashlib.sha256(current_bytes).hexdigest())

    def test_restore_reports_worker_ownership_conflict(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalBackupService(ledger, {
                "fab_local_backup_dir": os.path.join(temp_dir, "backups"),
                "fab_maintenance_mode": True,
            })
            backup = service.create_backup()
            with patch(
                "src.operations.local_backup.managed_worker_maintenance",
                side_effect=WorkerAlreadyRunningError("worker active"),
            ):
                blocked = service.restore_backup(
                    backup["backupPath"],
                    RESTORE_CONFIRMATION_PHRASE,
                )

            self.assertFalse(blocked["success"])
            self.assertEqual(blocked["status"], "worker_active")

    def test_inspect_rejects_unsafe_archive_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            backup_dir = os.path.join(temp_dir, "backups")
            os.makedirs(backup_dir)
            unsafe_path = os.path.join(backup_dir, "unsafe.zip")
            with zipfile.ZipFile(unsafe_path, "w") as archive:
                archive.writestr("../manifest.json", "{}")
                archive.writestr("fab_operations.sqlite3", b"not sqlite")

            with self.assertRaises(ValueError):
                LocalBackupService(ledger, {"fab_local_backup_dir": backup_dir}).inspect_backup(unsafe_path)

    def test_backup_path_symlink_cannot_escape_configured_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            outside_dir = os.path.join(temp_dir, "outside")
            backup_dir = os.path.join(temp_dir, "backups")
            outside = LocalBackupService(
                ledger,
                {"fab_local_backup_dir": outside_dir},
            ).create_backup()
            service = LocalBackupService(ledger, {"fab_local_backup_dir": backup_dir})
            linked_path = os.path.join(backup_dir, "linked.zip")
            try:
                os.symlink(outside["backupPath"], linked_path)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"File symlinks are unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "links or reparse points"):
                service.inspect_backup(linked_path)
            self.assertEqual(service._backup_paths(10), [])

    def test_list_backups_returns_valid_backup_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            backup_dir = os.path.join(temp_dir, "backups")
            service = LocalBackupService(ledger, {"fab_local_backup_dir": backup_dir})
            backup = service.create_backup()

            listed = service.list_backups()

            self.assertEqual(listed["backupDir"], backup_dir)
            self.assertEqual(listed["backups"][0]["backupFilename"], backup["backupFilename"])
            self.assertEqual(listed["backups"][0]["status"], "valid")

            manifest_only = service.list_backups(deep_verify=False)
            self.assertEqual(manifest_only["verificationMode"], "manifest_only")
            self.assertEqual(manifest_only["backups"][0]["status"], "manifest_valid")
            self.assertEqual(
                manifest_only["schedule"]["integrityVerification"],
                "manifest_only",
            )

    def test_manifest_cache_rechecks_authenticated_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalBackupService(
                ledger,
                {"fab_local_backup_dir": os.path.join(temp_dir, "backups")},
            )
            backup = service.create_backup()
            first = service.inspect_backup_manifest(backup["backupPath"])

            with patch.object(
                service,
                "_verify_manifest_integrity",
                wraps=service._verify_manifest_integrity,
            ) as verify_manifest:
                cached = service.inspect_backup_manifest(backup["backupPath"])

            self.assertEqual(first, cached)
            verify_manifest.assert_called_once()

    def test_rewritten_manifest_is_rejected_even_when_archive_structure_is_valid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            backup_dir = os.path.join(temp_dir, "backups")
            service = LocalBackupService(ledger, {"fab_local_backup_dir": backup_dir})
            backup = service.create_backup()
            with zipfile.ZipFile(backup["backupPath"], "r") as archive:
                members = {name: archive.read(name) for name in archive.namelist()}
            manifest = json.loads(members["manifest.json"])
            manifest["note"] = "rewritten without the integrity key"
            members["manifest.json"] = json.dumps(manifest).encode("utf-8")
            with zipfile.ZipFile(backup["backupPath"], "w", zipfile.ZIP_DEFLATED) as archive:
                for name, content in members.items():
                    archive.writestr(name, content)

            with self.assertRaisesRegex(ValueError, "authenticity"):
                service.inspect_backup(backup["backupPath"])

    def test_external_integrity_key_enables_portable_restore_and_rejects_wrong_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            key = "FAB_external_backup_integrity_key_2026_test_A9x"
            with patch.dict(os.environ, {"FAB_BACKUP_INTEGRITY_KEY": key}):
                ledger = LocalOperationsLedger(os.path.join(temp_dir, "source.sqlite3"))
                backup_dir = os.path.join(temp_dir, "backups")
                backup = LocalBackupService(
                    ledger,
                    {"fab_local_backup_dir": backup_dir},
                ).create_backup()
                portable_ledger = LocalOperationsLedger(
                    os.path.join(temp_dir, "portable.sqlite3")
                )
                portable = LocalBackupService(
                    portable_ledger,
                    {"fab_local_backup_dir": backup_dir},
                )
                self.assertEqual(
                    portable.inspect_backup(backup["backupPath"])["integrityStatus"],
                    "authenticated",
                )
            with patch.dict(
                os.environ,
                {"FAB_BACKUP_INTEGRITY_KEY": "FAB_wrong_integrity_key_2026_test_B8y"},
            ):
                with self.assertRaisesRegex(ValueError, "does not match"):
                    portable.inspect_backup(backup["backupPath"])

    def test_version_three_backup_cannot_be_downgraded_by_removing_signature(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            backup_dir = os.path.join(temp_dir, "backups")
            service = LocalBackupService(ledger, {"fab_local_backup_dir": backup_dir})
            backup = service.create_backup()
            with zipfile.ZipFile(backup["backupPath"], "r") as archive:
                members = {name: archive.read(name) for name in archive.namelist()}
            manifest = json.loads(members["manifest.json"])
            self.assertEqual(manifest["format"], BACKUP_FORMAT_V3)
            manifest.pop("integrity")
            members["manifest.json"] = json.dumps(manifest).encode("utf-8")
            with zipfile.ZipFile(backup["backupPath"], "w", zipfile.ZIP_DEFLATED) as archive:
                for name, content in members.items():
                    archive.writestr(name, content)

            with self.assertRaisesRegex(ValueError, "require an authenticated"):
                service.inspect_backup(backup["backupPath"])

    def test_inspect_rejects_checksum_mismatched_ledger_bytes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            backup_dir = os.path.join(temp_dir, "backups")
            service = LocalBackupService(
                ledger,
                {"fab_local_backup_dir": backup_dir},
            )
            backup = service.create_backup()
            with zipfile.ZipFile(backup["backupPath"], "r") as archive:
                manifest = archive.read("manifest.json")
                ledger_bytes = bytearray(archive.read("fab_operations.sqlite3"))
            ledger_bytes[-1] ^= 1
            with zipfile.ZipFile(
                backup["backupPath"],
                "w",
                zipfile.ZIP_DEFLATED,
            ) as archive:
                archive.writestr("manifest.json", manifest)
                archive.writestr("fab_operations.sqlite3", ledger_bytes)

            with self.assertRaisesRegex(ValueError, "checksum"):
                service.inspect_backup(backup["backupPath"])

    def test_source_complete_backup_includes_and_verifies_document_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            backup_dir = os.path.join(temp_dir, "backups")
            source_path = os.path.join(temp_dir, "receipt.pdf")
            source_bytes = b"%PDF-1.4 source-complete evidence"
            with open(source_path, "wb") as handle:
                handle.write(source_bytes)
            source_sha256 = hashlib.sha256(source_bytes).hexdigest()
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "source-complete-1",
                "originalFilename": "receipt.pdf",
                "storagePath": source_path,
                "contentSha256": source_sha256,
            })
            service = LocalBackupService(
                ledger,
                {"fab_local_backup_dir": backup_dir},
            )

            backup = service.create_backup(
                note="source complete",
                require_complete_source_evidence=True,
            )
            inspected = service.inspect_backup(backup["backupPath"])
            source_evidence = inspected["manifest"]["sourceEvidence"]

            self.assertEqual(source_evidence["coverageStatus"], "complete")
            self.assertEqual(source_evidence["totalDocuments"], 1)
            self.assertEqual(source_evidence["includedDocuments"], 1)
            self.assertEqual(source_evidence["includedFiles"], 1)
            self.assertEqual(source_evidence["includedBytes"], len(source_bytes))
            self.assertEqual(source_evidence["entries"][0]["documentId"], document_id)
            with zipfile.ZipFile(backup["backupPath"], "r") as archive:
                evidence_name = source_evidence["entries"][0]["archivePath"]
                self.assertEqual(archive.read(evidence_name), source_bytes)

    def test_source_snapshot_canonicalizes_trusted_roots_once_for_many_documents(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            for index in range(3):
                source_path = os.path.join(temp_dir, f"receipt-{index}.pdf")
                source_bytes = f"receipt {index}".encode("utf-8")
                with open(source_path, "wb") as handle:
                    handle.write(source_bytes)
                ledger.register_document({
                    "source": "scanner",
                    "sourceDocumentId": f"snapshot-{index}",
                    "originalFilename": os.path.basename(source_path),
                    "storagePath": source_path,
                    "contentSha256": hashlib.sha256(source_bytes).hexdigest(),
                })
            service = LocalBackupService(ledger, {
                "fab_local_backup_dir": os.path.join(temp_dir, "backups"),
            })
            original_realpath = os.path.realpath

            with patch(
                "src.operations.local_backup.os.path.realpath",
                wraps=original_realpath,
            ) as realpath:
                result, _ = service._snapshot_source_evidence(ledger_path, temp_dir, False)

            canonical_root_calls = [
                call for call in realpath.call_args_list
                if call.args and os.fspath(call.args[0]) == temp_dir
            ]
            self.assertEqual(result["includedDocuments"], 3)
            self.assertEqual(len(canonical_root_calls), 1)

    def test_source_complete_backup_blocks_missing_evidence_without_partial_archive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            backup_dir = os.path.join(temp_dir, "backups")
            ledger = LocalOperationsLedger(ledger_path)
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "missing-source-1",
                "originalFilename": "missing.pdf",
                "storagePath": os.path.join(temp_dir, "missing.pdf"),
                "contentSha256": "a" * 64,
            })
            service = LocalBackupService(
                ledger,
                {"fab_local_backup_dir": backup_dir},
            )

            with self.assertRaisesRegex(ValueError, "Source-complete backup blocked"):
                service.create_backup(require_complete_source_evidence=True)

            self.assertEqual(os.listdir(backup_dir), [])

    def test_source_complete_backup_rejects_symlink_outside_trusted_roots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fab_root = os.path.join(temp_dir, "fab")
            intake_root = os.path.join(fab_root, "intake")
            outside_root = os.path.join(temp_dir, "outside")
            os.makedirs(intake_root)
            os.makedirs(outside_root)
            ledger_path = os.path.join(fab_root, "fab.sqlite3")
            backup_dir = os.path.join(fab_root, "backups")
            outside_path = os.path.join(outside_root, "private.pdf")
            source_path = os.path.join(intake_root, "receipt.pdf")
            outside_bytes = b"private source outside trusted roots"
            with open(outside_path, "wb") as handle:
                handle.write(outside_bytes)
            try:
                os.symlink(outside_path, source_path)
            except OSError as exc:
                self.skipTest(f"File symlinks are unavailable: {exc}")

            ledger = LocalOperationsLedger(ledger_path)
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "symlink-backup",
                "originalFilename": "receipt.pdf",
                "storagePath": source_path,
                "contentSha256": hashlib.sha256(outside_bytes).hexdigest(),
            })
            service = LocalBackupService(ledger, {
                "fab_local_backup_dir": backup_dir,
                "fab_local_intake_paths": [intake_root],
            })

            with self.assertRaisesRegex(ValueError, "Source-complete backup blocked"):
                service.create_backup(require_complete_source_evidence=True)

            self.assertEqual(os.listdir(backup_dir), [])

    def test_source_complete_backup_rejects_source_outside_trusted_roots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fab_root = os.path.join(temp_dir, "fab")
            outside_root = os.path.join(temp_dir, "outside")
            os.makedirs(fab_root)
            os.makedirs(outside_root)
            ledger_path = os.path.join(fab_root, "fab.sqlite3")
            backup_dir = os.path.join(fab_root, "backups")
            outside_path = os.path.join(outside_root, "private.pdf")
            with open(outside_path, "wb") as handle:
                handle.write(b"private source outside trusted roots")
            ledger = LocalOperationsLedger(ledger_path)
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "outside-root-backup",
                "originalFilename": "private.pdf",
                "storagePath": outside_path,
            })
            service = LocalBackupService(ledger, {"fab_local_backup_dir": backup_dir})

            with self.assertRaisesRegex(ValueError, "Source-complete backup blocked"):
                service.create_backup(require_complete_source_evidence=True)

            self.assertEqual(os.listdir(backup_dir), [])

    def test_source_checksum_drift_blocks_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            source_path = os.path.join(temp_dir, "receipt.pdf")
            with open(source_path, "wb") as handle:
                handle.write(b"changed evidence")
            ledger = LocalOperationsLedger(ledger_path)
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "drifted-source-1",
                "originalFilename": "receipt.pdf",
                "storagePath": source_path,
                "contentSha256": hashlib.sha256(b"original evidence").hexdigest(),
            })
            service = LocalBackupService(
                ledger,
                {"fab_local_backup_dir": os.path.join(temp_dir, "backups")},
            )

            with self.assertRaisesRegex(ValueError, "no longer matches"):
                service.create_backup(require_complete_source_evidence=True)

    def test_inspect_rejects_tampered_source_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            backup_dir = os.path.join(temp_dir, "backups")
            source_path = os.path.join(temp_dir, "receipt.pdf")
            source_bytes = b"verified evidence"
            with open(source_path, "wb") as handle:
                handle.write(source_bytes)
            ledger = LocalOperationsLedger(ledger_path)
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "tamper-source-1",
                "originalFilename": "receipt.pdf",
                "storagePath": source_path,
                "contentSha256": hashlib.sha256(source_bytes).hexdigest(),
            })
            service = LocalBackupService(
                ledger,
                {"fab_local_backup_dir": backup_dir},
            )
            backup = service.create_backup(require_complete_source_evidence=True)
            with zipfile.ZipFile(backup["backupPath"], "r") as archive:
                members = {
                    name: archive.read(name)
                    for name in archive.namelist()
                }
            evidence_name = next(
                name for name in members
                if name.startswith("source_evidence/")
            )
            members[evidence_name] = b"tampered evidence"
            with zipfile.ZipFile(
                backup["backupPath"],
                "w",
                zipfile.ZIP_DEFLATED,
            ) as archive:
                for name, content in members.items():
                    archive.writestr(name, content)

            with self.assertRaisesRegex(ValueError, "size|checksum"):
                service.inspect_backup(backup["backupPath"])

    def test_inspect_remains_compatible_with_legacy_ledger_only_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            backup_dir = os.path.join(temp_dir, "backups")
            os.makedirs(backup_dir)
            ledger = LocalOperationsLedger(ledger_path)
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "legacy-backup-1",
                "originalFilename": "legacy.pdf",
            })
            legacy_path = os.path.join(backup_dir, "legacy.zip")
            with open(ledger_path, "rb") as handle:
                ledger_sha256 = hashlib.sha256(handle.read()).hexdigest()
            manifest = {
                "format": BACKUP_FORMAT_V1,
                "createdAt": "2026-07-25T00:00:00Z",
                "ledgerFilename": "fab_operations.sqlite3",
                "ledgerSha256": ledger_sha256,
                "ledgerBytes": os.path.getsize(ledger_path),
            }
            with zipfile.ZipFile(legacy_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest))
                archive.write(ledger_path, "fab_operations.sqlite3")

            inspected = LocalBackupService(
                ledger,
                {"fab_local_backup_dir": backup_dir},
            ).inspect_backup(legacy_path)

            self.assertEqual(inspected["manifest"]["format"], BACKUP_FORMAT_V1)
            self.assertEqual(inspected["integrityStatus"], "legacy_checksum_only")
            plan = LocalBackupService(
                ledger,
                {"fab_local_backup_dir": backup_dir, "fab_maintenance_mode": True},
            ).plan_restore(legacy_path)
            self.assertEqual(plan["confirmationPhrase"], LEGACY_RESTORE_CONFIRMATION_PHRASE)

    def test_run_due_creates_one_source_complete_package_then_reports_current(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            source_path = os.path.join(temp_dir, "receipt.pdf")
            source_bytes = b"scheduled backup evidence"
            with open(source_path, "wb") as handle:
                handle.write(source_bytes)
            ledger = LocalOperationsLedger(ledger_path)
            ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scheduled-source-1",
                "originalFilename": "receipt.pdf",
                "storagePath": source_path,
                "contentSha256": hashlib.sha256(source_bytes).hexdigest(),
            })
            service = LocalBackupService(ledger, {
                "fab_local_backup_dir": os.path.join(temp_dir, "backups"),
                "backup_schedule_interval_hours": 24,
                "backup_require_complete_source_evidence": True,
            })

            first = service.run_due(actor="test-worker")
            second = service.run_due(actor="test-worker")

            self.assertEqual(first["status"], "created")
            self.assertEqual(
                first["backup"]["manifest"]["sourceEvidence"]["coverageStatus"],
                "complete",
            )
            self.assertEqual(second["status"], "not_due")
            self.assertFalse(second["schedule"]["due"])


if __name__ == "__main__":
    unittest.main()
