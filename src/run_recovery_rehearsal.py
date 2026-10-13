"""Exercise real FAB recovery using only a newly allocated synthetic workspace.

No configuration loader, caller-supplied paths, credentials, or provider clients
are used. Output is bounded evidence, never a ledger dump or temporary path.
"""

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import zipfile

from src.operations.local_backup import (
    BACKUP_LEDGER_NAME,
    FULL_RESTORE_CONFIRMATION_PHRASE,
    LocalBackupService,
    RESTORE_MODE_FULL,
)
from src.operations.local_ledger import LocalOperationsLedger


SYNTHETIC_BYTES = b"FAB synthetic recovery document\nNot a financial record.\n"


def run_rehearsal():
    checks = {}

    def verify(name, condition):
        checks[name] = bool(condition)
        if not condition:
            raise ValueError("Synthetic recovery verification failed: " + name)

    with tempfile.TemporaryDirectory(prefix="fab-recovery-rehearsal-") as directory:
        root = Path(directory).resolve()
        ledger_path = root / "synthetic.sqlite3"
        source = root / "synthetic-document.txt"
        source.write_bytes(SYNTHETIC_BYTES)
        digest = hashlib.sha256(SYNTHETIC_BYTES).hexdigest()
        ledger = LocalOperationsLedger(str(ledger_path))
        document_id = ledger.register_document({
            "source": "scanner",
            "sourceDocumentId": "synthetic-recovery-only",
            "originalFilename": source.name,
            "storagePath": str(source),
            "contentSha256": digest,
        })
        service = LocalBackupService(ledger, {
            "fab_instance_root": str(root),
            "fab_local_backup_dir": str(root / "backups"),
            "fab_backup_restore_source_root": str(root / "restored-evidence"),
            "fab_maintenance_mode": True,
        })
        backup = service.create_backup(
            note="Synthetic disposable rehearsal", require_complete_source_evidence=True,
            actor="synthetic_recovery_rehearsal",
        )
        inspected = service.inspect_backup(backup["backupPath"])
        manifest = inspected["manifest"]
        evidence = manifest["sourceEvidence"]
        verify("sourceComplete", evidence["coverageStatus"] == "complete"
               and evidence["includedDocuments"] == 1 and evidence["gapCount"] == 0)
        with zipfile.ZipFile(backup["backupPath"]) as archive:
            ledger_bytes = archive.read(BACKUP_LEDGER_NAME)
            verify("backupLedgerChecksum", hashlib.sha256(ledger_bytes).hexdigest() == manifest["ledgerSha256"])
            verify("backupLedgerBytes", len(ledger_bytes) == manifest["ledgerBytes"])

        # Change the disposable record and remove its original source so a no-op
        # restore cannot pass. Keep current evidence valid for the safety backup.
        current_source = root / "changed-synthetic-document.txt"
        current_bytes = b"Changed synthetic document, after backup.\n"
        current_source.write_bytes(current_bytes)
        ledger.register_document({
            "source": "scanner", "sourceDocumentId": "synthetic-recovery-only",
            "originalFilename": current_source.name, "storagePath": str(current_source),
            "contentSha256": hashlib.sha256(current_bytes).hexdigest(),
        })
        source.unlink()
        verify("maintenancePlanReady", service.plan_restore(
            backup["backupPath"], restore_mode=RESTORE_MODE_FULL)["canRestore"])
        restored = service.restore_backup(
            backup["backupPath"], FULL_RESTORE_CONFIRMATION_PHRASE,
            restore_mode=RESTORE_MODE_FULL, actor="synthetic_recovery_rehearsal",
        )
        verify("maintenanceRestore", restored.get("success") is True)
        safety = service.inspect_backup(restored["preRestoreBackupPath"])
        verify("preRestoreBackupComplete", safety["manifest"]["sourceEvidence"]["coverageStatus"] == "complete")
        document = ledger.get_document(document_id)
        verify("ledgerRowsRestored", len(ledger.list_documents(limit=10)) == 1
               and document["original_filename"] == source.name
               and document["content_sha256"] == digest)
        restored_source = Path(document["storage_path"]).resolve()
        verify("restoredPathConfined", restored_source.is_relative_to(root / "restored-evidence"))
        restored_bytes = restored_source.read_bytes()
        verify("sourceBytesRestored", restored_bytes == SYNTHETIC_BYTES)
        verify("sourceChecksumRestored", hashlib.sha256(restored_bytes).hexdigest() == digest)
        with closing(sqlite3.connect(ledger_path.as_uri() + "?mode=ro", uri=True)) as connection:
            verify("sqliteIntegrity", connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)])
        verify("restoreAuditRecorded", ledger.list_audit_events()[0]["action"] == "local_backup.restored")
        result = {
            "schemaVersion": 1, "status": "passed", "syntheticOnly": True,
            "externalSubmission": "not_executed", "restoreMode": RESTORE_MODE_FULL,
            "backupFormat": manifest["format"], "sourceSha256": digest,
            "backupLedgerSha256": manifest["ledgerSha256"],
            "sourceEvidence": {key: evidence[key] for key in (
                "coverageStatus", "includedDocuments", "includedFiles", "includedBytes", "gapCount")},
            "checks": checks,
        }
    verify("temporaryWorkspaceRemoved", not root.exists())
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a disposable synthetic FAB backup/restore rehearsal.")
    parser.parse_args(argv)
    try:
        result = run_rehearsal()
    except Exception:
        result = {
            "schemaVersion": 1, "status": "failed", "syntheticOnly": True,
            "externalSubmission": "not_executed",
            "error": "Synthetic recovery rehearsal failed; run the focused recovery tests for diagnosis.",
        }
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
