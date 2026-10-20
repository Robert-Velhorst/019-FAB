from contextlib import contextmanager
import copy
import hmac
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from src.operations.local_ledger import LocalOperationsLedger
from src.operations.runtime_lease import RuntimeLeaseHeartbeat
from src.security.deployment_secrets import read_secret, strong_secret
from src.security.local_secret_store import LocalSecretStore
from src.worker.runtime import WorkerAlreadyRunningError, managed_worker_maintenance


BACKUP_MANIFEST_NAME = "manifest.json"
BACKUP_LEDGER_NAME = "fab_operations.sqlite3"
RESTORE_CONFIRMATION_PHRASE = "RESTORE FAB LOCAL LEDGER"
FULL_RESTORE_CONFIRMATION_PHRASE = "RESTORE FAB LEDGER AND SOURCE EVIDENCE"
LEGACY_RESTORE_CONFIRMATION_PHRASE = "RESTORE FAB LEGACY CHECKSUM ONLY BACKUP"
LEGACY_FULL_RESTORE_CONFIRMATION_PHRASE = (
    "RESTORE FAB LEGACY CHECKSUM ONLY BACKUP AND SOURCE EVIDENCE"
)
RESTORE_MODE_LEDGER_ONLY = "ledger_only"
RESTORE_MODE_FULL = "ledger_and_source_evidence"
BACKUP_FORMAT_V1 = "fab-local-ledger-backup-v1"
BACKUP_FORMAT_V2 = "fab-recovery-package-v2"
BACKUP_FORMAT_V3 = "fab-recovery-package-v3"
SOURCE_EVIDENCE_FORMATS = {BACKUP_FORMAT_V2, BACKUP_FORMAT_V3}
SOURCE_EVIDENCE_PREFIX = "source_evidence/"
RESTORED_SOURCE_DIR_PREFIX = "fab-source-evidence-"
SCHEDULED_BACKUP_LEASE_NAME = "local_scheduled_backup"
SCHEDULED_BACKUP_LEASE_TTL_SECONDS = 1800
MAX_BACKUP_ARCHIVE_FILES = 10_000
MAX_BACKUP_CENTRAL_DIRECTORY_BYTES = 8 * 1024 * 1024
MAX_BACKUP_EVIDENCE_FILE_BYTES = 250 * 1024 * 1024
MAX_BACKUP_UNCOMPRESSED_BYTES = 20 * 1024 * 1024 * 1024
MAX_BACKUP_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_BACKUP_MANIFEST_DEPTH = 32
MAX_CACHED_MANIFEST_BYTES = 256 * 1024
_INSPECTION_CACHE: Dict[str, Dict[str, Any]] = {}
_MANIFEST_INSPECTION_CACHE: Dict[str, Dict[str, Any]] = {}
MAX_INSPECTION_CACHE_ENTRIES = 128


class LocalBackupService:
    """Create and restore local FAB ledger backups with explicit restore gates."""

    def __init__(self, ledger: LocalOperationsLedger, config: Optional[Dict[str, Any]] = None):
        self.ledger = ledger
        self.config = config or {}
        self.ledger_path = os.path.abspath(ledger.path)
        self.project_root = _project_root(self.ledger_path, self.config)
        self.backup_dir = self._backup_dir()
        os.makedirs(self.backup_dir, exist_ok=True)
        credentials_dir = os.path.join(self.project_root, "credentials")
        self.config.setdefault(
            "fab_local_secret_store_path",
            os.path.join(credentials_dir, "fab-local-secrets.enc"),
        )
        self.config.setdefault(
            "fab_local_secret_key_path",
            os.path.join(credentials_dir, "fab-local-secrets.key"),
        )

    def create_backup(
        self,
        note: Optional[str] = None,
        require_complete_source_evidence: bool = False,
        actor: str = "local_backup",
    ) -> Dict[str, Any]:
        timestamp = _timestamp()
        backup_filename = f"fab-recovery-package_{timestamp}.zip"
        backup_path = os.path.join(self.backup_dir, backup_filename)
        temporary_backup_path = f"{backup_path}.{os.getpid()}.tmp"

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                ledger_snapshot_path = os.path.join(temp_dir, BACKUP_LEDGER_NAME)
                self._snapshot_ledger(ledger_snapshot_path)
                ledger_sha256 = _sha256_file(ledger_snapshot_path)
                ledger_bytes = os.path.getsize(ledger_snapshot_path)
                source_evidence, evidence_files = self._snapshot_source_evidence(
                    ledger_snapshot_path,
                    temp_dir,
                    require_complete=require_complete_source_evidence,
                )
                manifest = {
                    "format": BACKUP_FORMAT_V3,
                    "createdAt": _now(),
                    "ledgerFilename": BACKUP_LEDGER_NAME,
                    "ledgerSha256": ledger_sha256,
                    "ledgerBytes": ledger_bytes,
                    "sourceLedgerBasename": os.path.basename(self.ledger_path),
                    "note": note,
                    "configSummary": self._safe_config_summary(),
                    "sourceEvidence": source_evidence,
                    "safety": {
                        "containsSecrets": False,
                        "containsRawDocumentBytes": bool(evidence_files),
                        "restoreRequiresConfirmation": RESTORE_CONFIRMATION_PHRASE,
                    },
                }
                manifest["integrity"] = self._sign_manifest(manifest)
                manifest_path = os.path.join(temp_dir, BACKUP_MANIFEST_NAME)
                with open(manifest_path, "w", encoding="utf-8") as handle:
                    json.dump(manifest, handle, sort_keys=True, indent=2, default=str)

                with zipfile.ZipFile(
                    temporary_backup_path,
                    "w",
                    zipfile.ZIP_DEFLATED,
                    allowZip64=True,
                ) as archive:
                    archive.write(manifest_path, BACKUP_MANIFEST_NAME)
                    archive.write(ledger_snapshot_path, BACKUP_LEDGER_NAME)
                    for archive_path, evidence_path in sorted(evidence_files.items()):
                        archive.write(evidence_path, archive_path)
                os.replace(temporary_backup_path, backup_path)
                inspected = self.inspect_backup(backup_path)
                manifest = inspected["manifest"]
        except Exception:
            if os.path.exists(backup_path):
                os.remove(backup_path)
            raise
        finally:
            if os.path.exists(temporary_backup_path):
                os.remove(temporary_backup_path)

        self.ledger.record_audit_event({
            "action": "local_backup.created",
            "entityType": "backup",
            "entityId": os.path.basename(backup_path),
            "details": {
                "actor": str(actor or "local_backup")[:200],
                "backupPath": backup_path,
                "ledgerSha256": ledger_sha256,
                "ledgerBytes": ledger_bytes,
                "note": note,
                "sourceEvidenceStatus": source_evidence["coverageStatus"],
                "sourceEvidenceDocuments": source_evidence["includedDocuments"],
                "sourceEvidenceFiles": source_evidence["includedFiles"],
                "sourceEvidenceBytes": source_evidence["includedBytes"],
            },
        })
        return {
            "success": True,
            "status": "created",
            "backupPath": backup_path,
            "backupFilename": os.path.basename(backup_path),
            "manifest": manifest,
        }

    def list_backups(self, limit: int = 25, deep_verify: bool = True) -> Dict[str, Any]:
        backups = []
        for path in self._backup_paths(limit):
            name = os.path.basename(path)
            try:
                inspected = (
                    self.inspect_backup(path)
                    if deep_verify
                    else self.inspect_backup_manifest(path)
                )
                source_evidence = inspected.get("manifest", {}).get("sourceEvidence") or {}
                backups.append({
                    "backupFilename": name,
                    "backupPath": path,
                    "status": inspected["status"],
                    "createdAt": inspected.get("manifest", {}).get("createdAt"),
                    "ledgerBytes": inspected.get("manifest", {}).get("ledgerBytes"),
                    "ledgerSha256": inspected.get("manifest", {}).get("ledgerSha256"),
                    "sizeBytes": os.path.getsize(path),
                    "format": inspected.get("manifest", {}).get("format"),
                    "integrityStatus": inspected.get("integrityStatus"),
                    "restoreConfirmationPhrase": (
                        RESTORE_CONFIRMATION_PHRASE
                        if inspected.get("integrityStatus") == "authenticated"
                        else LEGACY_RESTORE_CONFIRMATION_PHRASE
                    ),
                    "fullRestoreConfirmationPhrase": (
                        FULL_RESTORE_CONFIRMATION_PHRASE
                        if inspected.get("integrityStatus") == "authenticated"
                        else LEGACY_FULL_RESTORE_CONFIRMATION_PHRASE
                    ),
                    "sourceEvidenceStatus": (
                        source_evidence.get("coverageStatus")
                        if source_evidence
                        else "legacy_ledger_only"
                    ),
                    "sourceEvidenceDocuments": source_evidence.get("includedDocuments", 0),
                    "sourceEvidenceFiles": source_evidence.get("includedFiles", 0),
                    "sourceEvidenceBytes": source_evidence.get("includedBytes", 0),
                    "sourceEvidenceGaps": source_evidence.get("gapCount", 0),
                })
            except (
                OSError,
                ValueError,
                KeyError,
                json.JSONDecodeError,
                zipfile.BadZipFile,
            ) as exc:
                backups.append({
                    "backupFilename": name,
                    "backupPath": path,
                    "status": "invalid",
                    "error": str(exc),
                    "sizeBytes": os.path.getsize(path) if os.path.exists(path) else 0,
                })
        return {
            "backupDir": self.backup_dir,
            "restoreConfirmationPhrase": RESTORE_CONFIRMATION_PHRASE,
            "fullRestoreConfirmationPhrase": FULL_RESTORE_CONFIRMATION_PHRASE,
            "legacyRestoreConfirmationPhrase": LEGACY_RESTORE_CONFIRMATION_PHRASE,
            "legacyFullRestoreConfirmationPhrase": LEGACY_FULL_RESTORE_CONFIRMATION_PHRASE,
            "restorePolicy": self.restore_policy(),
            "backups": backups[: _bounded_limit(limit)],
            "schedule": self.schedule_status(deep_verify=deep_verify),
            "verificationMode": "deep" if deep_verify else "manifest_only",
        }

    def inspect_backup_manifest(self, backup_path: str) -> Dict[str, Any]:
        """Validate archive structure and manifest without reading financial bytes."""
        resolved_path = self._resolve_backup_path(backup_path)
        if not os.path.exists(resolved_path):
            raise ValueError(f"Backup not found: {resolved_path}")
        if not resolved_path.lower().endswith(".zip"):
            raise ValueError("Only .zip local FAB backups are supported")
        signature = _file_signature(resolved_path)
        cached = _MANIFEST_INSPECTION_CACHE.get(resolved_path)
        cached_result = _verify_cached_manifest(cached, signature, self._verify_manifest_integrity)
        if cached_result is not None:
            return cached_result
        with _open_preflighted_archive(resolved_path) as archive:
            names = archive.namelist()
            self._validate_member_names(names)
            manifest = self._read_backup_manifest(archive)
            manifest_bytes = archive.getinfo(BACKUP_MANIFEST_NAME).file_size
            integrity_status = self._verify_manifest_integrity(manifest)
            backup_format = manifest.get("format")
            if backup_format not in {BACKUP_FORMAT_V1, BACKUP_FORMAT_V2, BACKUP_FORMAT_V3}:
                raise ValueError("Unsupported FAB backup format")
            if backup_format == BACKUP_FORMAT_V3 and integrity_status != "authenticated":
                raise ValueError("Version 3 FAB backups require an authenticated manifest")
            expected_names = {BACKUP_MANIFEST_NAME, BACKUP_LEDGER_NAME}
            if backup_format in SOURCE_EVIDENCE_FORMATS:
                expected_names.update(
                    self._validate_source_evidence_manifest(manifest, archive)
                )
            self._validate_expected_archive_names(names, expected_names)
            ledger_info = archive.getinfo(BACKUP_LEDGER_NAME)
            expected_bytes = manifest.get("ledgerBytes")
            if expected_bytes is not None and int(expected_bytes) != int(ledger_info.file_size):
                raise ValueError("Backup ledger size does not match manifest")
            expected_sha256 = str(manifest.get("ledgerSha256") or "").strip().lower()
            if not _valid_sha256(expected_sha256):
                raise ValueError("Backup manifest has no valid ledger SHA-256")
        cached_result = _authenticated_cached_result(
            cached,
            signature,
            manifest,
            integrity_status,
        )
        if cached_result is not None:
            return cached_result
        result = {
            "success": True,
            "status": "manifest_valid",
            "backupPath": resolved_path,
            "backupFilename": os.path.basename(resolved_path),
            "manifest": manifest,
            "deepVerification": "not_executed",
            "integrityStatus": integrity_status,
            "restoreConfirmationPhrase": (
                RESTORE_CONFIRMATION_PHRASE
                if integrity_status == "authenticated"
                else LEGACY_RESTORE_CONFIRMATION_PHRASE
            ),
            "fullRestoreConfirmationPhrase": (
                FULL_RESTORE_CONFIRMATION_PHRASE
                if integrity_status == "authenticated"
                else LEGACY_FULL_RESTORE_CONFIRMATION_PHRASE
            ),
        }
        _store_inspection_cache(
            _MANIFEST_INSPECTION_CACHE,
            resolved_path,
            signature,
            result,
            manifest_bytes=manifest_bytes,
        )
        return result

    def _backup_integrity_key(self) -> bytes:
        try:
            configured = read_secret("FAB_BACKUP_INTEGRITY_KEY")
            if configured:
                if not strong_secret(configured):
                    raise ValueError("FAB_BACKUP_INTEGRITY_KEY must be a strong secret of at least 32 characters.")
                return configured.encode("utf-8")
            value = LocalSecretStore(self.config).get_or_create_runtime_secret(
                "backup_integrity_key"
            )
        except (OSError, ValueError, RuntimeError) as exc:
            raise ValueError(
                "FAB backup integrity key is unavailable; configure FAB_BACKUP_INTEGRITY_KEY or repair the encrypted local secret store."
            ) from exc
        if not strong_secret(value):
            raise ValueError("The stored FAB backup integrity key is invalid.")
        return value.encode("utf-8")

    def _sign_manifest(self, manifest: Dict[str, Any]) -> Dict[str, str]:
        key = self._backup_integrity_key()
        payload = _canonical_manifest(manifest)
        return {
            "algorithm": "hmac-sha256",
            "keyId": hashlib.sha256(key).hexdigest()[:16],
            "signature": hmac.new(key, payload, hashlib.sha256).hexdigest(),
        }

    def _verify_manifest_integrity(self, manifest: Dict[str, Any]) -> str:
        integrity = manifest.get("integrity")
        if integrity is None:
            return "legacy_checksum_only"
        if not isinstance(integrity, dict):
            raise ValueError("Backup manifest integrity metadata is invalid")
        if integrity.get("algorithm") != "hmac-sha256":
            raise ValueError("Backup manifest uses an unsupported integrity algorithm")
        key_id = integrity.get("keyId")
        signature = integrity.get("signature")
        if not isinstance(key_id, str) or not re.fullmatch(r"[a-f0-9]{16}", key_id):
            raise ValueError("Backup manifest has an invalid integrity key identifier")
        if not isinstance(signature, str) or not re.fullmatch(r"[a-f0-9]{64}", signature):
            raise ValueError("Backup manifest has an invalid integrity signature")
        key = self._backup_integrity_key()
        expected_key_id = hashlib.sha256(key).hexdigest()[:16]
        if not hmac.compare_digest(key_id, expected_key_id):
            raise ValueError("Backup integrity key does not match this FAB installation")
        unsigned_manifest = dict(manifest)
        unsigned_manifest.pop("integrity", None)
        expected_signature = hmac.new(
            key,
            _canonical_manifest(unsigned_manifest),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            raise ValueError("Backup manifest authenticity check failed")
        return "authenticated"

    def inspect_backup(self, backup_path: str) -> Dict[str, Any]:
        resolved_path = self._resolve_backup_path(backup_path)
        if not os.path.exists(resolved_path):
            raise ValueError(f"Backup not found: {resolved_path}")
        if not resolved_path.lower().endswith(".zip"):
            raise ValueError("Only .zip local FAB backups are supported")
        signature = _file_signature(resolved_path)
        cached = _INSPECTION_CACHE.get(resolved_path)
        cached_result = _verify_cached_manifest(cached, signature, self._verify_manifest_integrity)
        if cached_result is not None:
            return cached_result
        with _open_preflighted_archive(resolved_path) as archive:
            names = archive.namelist()
            self._validate_member_names(names)
            manifest = self._read_backup_manifest(archive)
            manifest_bytes = archive.getinfo(BACKUP_MANIFEST_NAME).file_size
            integrity_status = self._verify_manifest_integrity(manifest)
            backup_format = manifest.get("format")
            if backup_format not in {BACKUP_FORMAT_V1, BACKUP_FORMAT_V2, BACKUP_FORMAT_V3}:
                raise ValueError("Unsupported FAB backup format")
            if backup_format == BACKUP_FORMAT_V3 and integrity_status != "authenticated":
                raise ValueError("Version 3 FAB backups require an authenticated manifest")
            expected_names = {BACKUP_MANIFEST_NAME, BACKUP_LEDGER_NAME}
            if backup_format in SOURCE_EVIDENCE_FORMATS:
                expected_names.update(
                    self._validate_source_evidence_manifest(manifest, archive)
                )
            self._validate_expected_archive_names(names, expected_names)
            ledger_info = archive.getinfo(BACKUP_LEDGER_NAME)
            expected_bytes = manifest.get("ledgerBytes")
            if expected_bytes is not None and int(expected_bytes) != int(ledger_info.file_size):
                raise ValueError("Backup ledger size does not match manifest")
            expected_sha256 = str(manifest.get("ledgerSha256") or "").strip().lower()
            if not _valid_sha256(expected_sha256):
                raise ValueError("Backup manifest has no valid ledger SHA-256")
            if backup_format in SOURCE_EVIDENCE_FORMATS:
                self._verify_source_evidence_bytes(manifest, archive)
            cached_result = _authenticated_cached_result(
                cached,
                signature,
                manifest,
                integrity_status,
            )
            if cached_result is not None:
                return cached_result
            with tempfile.TemporaryDirectory() as temp_dir:
                inspected_ledger_path = os.path.join(temp_dir, BACKUP_LEDGER_NAME)
                digest = hashlib.sha256()
                with archive.open(BACKUP_LEDGER_NAME) as source, open(
                    inspected_ledger_path,
                    "wb",
                ) as target:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        target.write(chunk)
                if digest.hexdigest() != expected_sha256:
                    raise ValueError("Backup ledger checksum does not match manifest")
                connection = sqlite3.connect(
                    f"file:{inspected_ledger_path}?mode=ro",
                    uri=True,
                )
                try:
                    integrity = connection.execute("PRAGMA quick_check").fetchone()
                finally:
                    connection.close()
                if not integrity or str(integrity[0]).lower() != "ok":
                    raise ValueError("Backup ledger failed SQLite integrity validation")

        result = {
            "success": True,
            "status": "valid",
            "backupPath": resolved_path,
            "backupFilename": os.path.basename(resolved_path),
            "manifest": manifest,
            "restoreConfirmationPhrase": (
                RESTORE_CONFIRMATION_PHRASE
                if integrity_status == "authenticated"
                else LEGACY_RESTORE_CONFIRMATION_PHRASE
            ),
            "integrityStatus": integrity_status,
        }
        _store_inspection_cache(_INSPECTION_CACHE, resolved_path, signature, result, manifest_bytes=manifest_bytes)
        return result

    def schedule_status(self, deep_verify: bool = True) -> Dict[str, Any]:
        interval_hours = _positive_float_config(
            self.config,
            "backup_schedule_interval_hours",
            "fab_backup_schedule_interval_hours",
            default=24.0,
        )
        require_complete = _bool_config(
            self.config,
            "backup_require_complete_source_evidence",
            "fab_backup_require_complete_source_evidence",
            default=True,
        )
        latest = None
        invalid_count = 0
        for path in self._backup_paths(100):
            try:
                latest = (
                    self.inspect_backup(path)
                    if deep_verify
                    else self.inspect_backup_manifest(path)
                )
                break
            except (
                OSError,
                ValueError,
                KeyError,
                json.JSONDecodeError,
                zipfile.BadZipFile,
            ):
                invalid_count += 1
        now = datetime.now(timezone.utc)
        if not latest:
            return {
                "status": "due",
                "due": True,
                "intervalHours": interval_hours,
                "requireCompleteSourceEvidence": require_complete,
                "lastSuccessfulAt": None,
                "nextDueAt": None,
                "invalidBackupCount": invalid_count,
                "reason": "no_valid_backup",
            }

        manifest = latest.get("manifest") or {}
        created_at = _parse_timestamp(manifest.get("createdAt"))
        source_evidence = manifest.get("sourceEvidence") or {}
        source_complete = (
            manifest.get("format") in SOURCE_EVIDENCE_FORMATS
            and source_evidence.get("coverageStatus") == "complete"
        )
        next_due = created_at + timedelta(hours=interval_hours) if created_at else now
        reason = "interval_elapsed" if now >= next_due else "current"
        if require_complete and not source_complete:
            reason = "source_evidence_backup_required"
        due = now >= next_due or (require_complete and not source_complete)
        return {
            "status": "due" if due else "current",
            "due": due,
            "intervalHours": interval_hours,
            "requireCompleteSourceEvidence": require_complete,
            "lastSuccessfulAt": manifest.get("createdAt"),
            "nextDueAt": _iso(next_due),
            "invalidBackupCount": invalid_count,
            "reason": reason,
            "latestBackupFilename": latest.get("backupFilename"),
            "latestLedgerSha256": manifest.get("ledgerSha256"),
            "sourceEvidenceStatus": (
                source_evidence.get("coverageStatus")
                if source_evidence
                else "legacy_ledger_only"
            ),
            "sourceEvidenceDocuments": source_evidence.get("includedDocuments", 0),
            "sourceEvidenceFiles": source_evidence.get("includedFiles", 0),
            "sourceEvidenceBytes": source_evidence.get("includedBytes", 0),
            "sourceEvidenceGaps": source_evidence.get("gapCount", 0),
            "integrityVerification": "deep" if deep_verify else "manifest_only",
        }

    def run_due(self, actor: str = "local_worker") -> Dict[str, Any]:
        schedule = self.schedule_status()
        if not schedule.get("due"):
            return {
                "success": True,
                "status": "not_due",
                "schedule": schedule,
                "externalSubmission": "not_executed",
            }
        owner_token = f"{str(actor or 'local_worker')[:80]}:{os.getpid()}:{uuid.uuid4().hex}"
        lease = self.ledger.acquire_runtime_lease(
            SCHEDULED_BACKUP_LEASE_NAME,
            owner_token,
            ttl_seconds=SCHEDULED_BACKUP_LEASE_TTL_SECONDS,
            metadata={"actor": str(actor or "local_worker")[:200]},
        )
        if not lease.get("acquired"):
            return {
                "success": True,
                "status": "already_running",
                "schedule": schedule,
                "runtimeLease": lease.get("lease"),
                "externalSubmission": "not_executed",
            }
        try:
            with RuntimeLeaseHeartbeat(
                self.ledger,
                SCHEDULED_BACKUP_LEASE_NAME,
                owner_token,
                SCHEDULED_BACKUP_LEASE_TTL_SECONDS,
                thread_name="fab-scheduled-backup-lease-heartbeat",
            ) as heartbeat:
                backup = self.create_backup(
                    note="Automatic scheduled source-complete recovery package.",
                    require_complete_source_evidence=_bool_config(
                        self.config,
                        "backup_require_complete_source_evidence",
                        "fab_backup_require_complete_source_evidence",
                        default=True,
                    ),
                    actor=actor,
                )
                heartbeat.assert_owned()
            return {
                "success": True,
                "status": "created",
                "backup": backup,
                "schedule": self.schedule_status(),
                "runtimeLease": lease.get("lease"),
                "externalSubmission": "not_executed",
            }
        finally:
            self.ledger.release_runtime_lease(
                SCHEDULED_BACKUP_LEASE_NAME,
                owner_token,
            )

    def restore_policy(self) -> Dict[str, Any]:
        maintenance_mode = _bool_config(
            self.config,
            "fab_maintenance_mode",
            "operations_maintenance_mode",
            default=False,
        )
        source_root = self._source_restore_base()
        return {
            "status": "maintenance_ready" if maintenance_mode else "maintenance_required",
            "maintenanceMode": maintenance_mode,
            "ledgerRestoreSupported": True,
            "sourceEvidenceRestoreSupported": True,
            "sourceRestoreRoot": source_root,
            "workerMustBeStopped": True,
            "externalSubmission": "not_executed",
            "nextAction": (
                "Inspect a verified package and enter the exact restore confirmation."
                if maintenance_mode
                else "Stop FAB and restart it with Start-FAB-Maintenance.cmd before restoring."
            ),
        }

    def plan_restore(
        self,
        backup_path: str,
        *,
        restore_mode: str = RESTORE_MODE_LEDGER_ONLY,
    ) -> Dict[str, Any]:
        mode = _restore_mode(restore_mode)
        inspected = self.inspect_backup(backup_path)
        manifest = inspected["manifest"]
        source_evidence = manifest.get("sourceEvidence") or {}
        policy = self.restore_policy()
        legacy_integrity = inspected.get("integrityStatus") != "authenticated"
        blockers = []
        source_target = None
        target_status = "not_applicable"
        if not policy["maintenanceMode"]:
            blockers.append({
                "code": "maintenance_required",
                "message": "Restore is locked while the normal FAB runtime is active.",
            })
        if mode == RESTORE_MODE_FULL:
            if manifest.get("format") not in SOURCE_EVIDENCE_FORMATS:
                blockers.append({
                    "code": "source_evidence_unavailable",
                    "message": "Legacy ledger-only packages cannot restore source evidence.",
                })
            elif source_evidence.get("coverageStatus") != "complete":
                blockers.append({
                    "code": "source_evidence_incomplete",
                    "message": "Full recovery requires a source-complete package with zero evidence gaps.",
                })
            else:
                try:
                    source_target = self._source_restore_target(manifest)
                    if os.path.lexists(source_target):
                        self._verify_restored_source_root(source_target, source_evidence)
                        target_status = "existing_verified"
                    else:
                        self._validate_source_restore_base(create=False)
                        target_status = "ready"
                except (OSError, ValueError) as exc:
                    blockers.append({
                        "code": "source_restore_target_blocked",
                        "message": str(exc),
                    })
                    target_status = "blocked"
        return {
            "success": not blockers,
            "status": "ready" if not blockers else "blocked",
            "canRestore": not blockers,
            "restoreMode": mode,
            "confirmationPhrase": (
                (
                    LEGACY_FULL_RESTORE_CONFIRMATION_PHRASE
                    if mode == RESTORE_MODE_FULL
                    else LEGACY_RESTORE_CONFIRMATION_PHRASE
                )
                if legacy_integrity
                else (
                    FULL_RESTORE_CONFIRMATION_PHRASE
                    if mode == RESTORE_MODE_FULL
                    else RESTORE_CONFIRMATION_PHRASE
                )
            ),
            "integrityStatus": inspected.get("integrityStatus"),
            "backupFilename": inspected["backupFilename"],
            "backupPath": inspected["backupPath"],
            "manifest": _restore_manifest_summary(manifest),
            "sourceRestoreTarget": source_target,
            "sourceRestoreTargetStatus": target_status,
            "blockers": blockers,
            "externalSubmission": "not_executed",
        }

    def restore_backup(
        self,
        backup_path: str,
        confirmation: str,
        *,
        restore_mode: str = RESTORE_MODE_LEDGER_ONLY,
        actor: str = "local_backup_restore",
    ) -> Dict[str, Any]:
        mode = _restore_mode(restore_mode)
        plan = self.plan_restore(backup_path, restore_mode=mode)
        required_confirmation = str(plan.get("confirmationPhrase") or "")
        if confirmation != required_confirmation:
            return {
                "success": False,
                "status": "requires_confirmation",
                "error": f"Restore requires exact confirmation: {required_confirmation}",
                "confirmationPhrase": required_confirmation,
                "integrityStatus": plan.get("integrityStatus"),
                "externalSubmission": "not_executed",
            }
        if not plan.get("canRestore"):
            return {
                **plan,
                "success": False,
                "error": (plan.get("blockers") or [{}])[0].get("message")
                or "Restore is blocked.",
            }
        try:
            with managed_worker_maintenance(Path(self.project_root)):
                return self._restore_backup_locked(plan, actor=actor)
        except WorkerAlreadyRunningError as exc:
            return {
                "success": False,
                "status": "worker_active",
                "error": str(exc),
                "externalSubmission": "not_executed",
            }

    def _restore_backup_locked(
        self,
        plan: Dict[str, Any],
        *,
        actor: str,
    ) -> Dict[str, Any]:
        resolved_path = str(plan["backupPath"])
        mode = str(plan["restoreMode"])
        inspected = self.inspect_backup(resolved_path)
        manifest = inspected["manifest"]
        source_evidence = manifest.get("sourceEvidence") or {}
        pre_restore = self.create_backup(
            note=f"Automatic pre-restore backup before {os.path.basename(resolved_path)}",
            require_complete_source_evidence=mode == RESTORE_MODE_FULL,
            actor=actor,
        )
        source_target = None
        source_target_created = False
        with tempfile.TemporaryDirectory() as temp_dir:
            rollback_ledger_path = os.path.join(temp_dir, "pre-restore-live.sqlite3")
            prepared_ledger_path = os.path.join(temp_dir, "prepared-restore.sqlite3")
            self._snapshot_ledger(rollback_ledger_path)
            with _open_preflighted_archive(resolved_path) as archive:
                names = archive.namelist()
                self._validate_member_names(names)
                if self._read_backup_manifest(archive) != manifest:
                    raise ValueError("Backup manifest changed after inspection")
                expected_names = {BACKUP_MANIFEST_NAME, BACKUP_LEDGER_NAME}
                if manifest.get("format") in SOURCE_EVIDENCE_FORMATS:
                    expected_names.update(self._validate_source_evidence_manifest(manifest, archive))
                self._validate_expected_archive_names(names, expected_names)
                self._prepare_restored_ledger(
                    archive,
                    manifest,
                    prepared_ledger_path,
                    source_target=(
                        self._source_restore_target(manifest)
                        if mode == RESTORE_MODE_FULL
                        else None
                    ),
                )
                if mode == RESTORE_MODE_FULL:
                    source_target = self._source_restore_target(manifest)
                    source_target_created = self._restore_source_evidence(
                        archive,
                        source_evidence,
                        source_target,
                    )
            try:
                _sqlite_backup(prepared_ledger_path, self.ledger_path)
                self._verify_live_restore(
                    manifest,
                    source_target=source_target,
                )
            except Exception:
                _sqlite_backup(rollback_ledger_path, self.ledger_path)
                raise

        verified_ledger_sha256 = _sha256_file(self.ledger_path)
        restored_ledger = LocalOperationsLedger(self.ledger_path)
        restored_ledger.record_audit_event({
            "action": "local_backup.restored",
            "entityType": "backup",
            "entityId": os.path.basename(resolved_path),
            "details": {
                "actor": str(actor or "local_backup_restore")[:200],
                "backupFilename": os.path.basename(resolved_path),
                "preRestoreBackupFilename": pre_restore["backupFilename"],
                "restoreMode": mode,
                "sourceLedgerSha256": manifest.get("ledgerSha256"),
                "verifiedLedgerSha256BeforeAudit": verified_ledger_sha256,
                "sourceEvidenceDocuments": source_evidence.get("includedDocuments", 0),
                "sourceEvidenceFiles": source_evidence.get("includedFiles", 0),
                "sourceEvidenceBytes": source_evidence.get("includedBytes", 0),
                "sourceTargetCreated": source_target_created,
                "externalSubmission": "not_executed",
            },
        })
        live_ledger_sha256 = _sha256_file(self.ledger_path)
        return {
            "success": True,
            "status": "restored",
            "restoreMode": mode,
            "backupPath": resolved_path,
            "backupFilename": os.path.basename(resolved_path),
            "preRestoreBackupPath": pre_restore["backupPath"],
            "preRestoreBackupFilename": pre_restore["backupFilename"],
            "sourceRestoreTarget": source_target,
            "sourceTargetCreated": source_target_created,
            "sourceLedgerSha256": manifest.get("ledgerSha256"),
            "verifiedLedgerSha256BeforeAudit": verified_ledger_sha256,
            "liveLedgerSha256": live_ledger_sha256,
            "manifest": _restore_manifest_summary(manifest),
            "externalSubmission": "not_executed",
        }

    def _prepare_restored_ledger(
        self,
        archive: zipfile.ZipFile,
        manifest: Dict[str, Any],
        destination_path: str,
        *,
        source_target: Optional[str],
    ) -> None:
        digest = hashlib.sha256()
        with archive.open(BACKUP_LEDGER_NAME) as source, open(destination_path, "xb") as target:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                target.write(chunk)
        if digest.hexdigest() != str(manifest.get("ledgerSha256") or ""):
            raise ValueError("Backup ledger checksum does not match manifest")
        connection = sqlite3.connect(destination_path)
        connection.row_factory = sqlite3.Row
        try:
            integrity = connection.execute("PRAGMA quick_check").fetchone()
            if not integrity or str(integrity[0]).lower() != "ok":
                raise ValueError("Prepared restore ledger failed SQLite integrity validation")
            if source_target is not None:
                source_evidence = manifest.get("sourceEvidence") or {}
                entries = source_evidence.get("entries") or []
                rows = connection.execute(
                    "SELECT id, content_sha256 FROM bookkeeping_documents ORDER BY id ASC"
                ).fetchall()
                entries_by_id = {int(entry["documentId"]): entry for entry in entries}
                row_ids = {int(row["id"]) for row in rows}
                if row_ids != set(entries_by_id):
                    raise ValueError(
                        "Source-evidence manifest does not cover every restored ledger document"
                    )
                for row in rows:
                    document_id = int(row["id"])
                    entry = entries_by_id[document_id]
                    expected_sha256 = str(entry["sha256"]).lower()
                    ledger_sha256 = str(row["content_sha256"] or "").strip().lower()
                    if ledger_sha256 and ledger_sha256 != expected_sha256:
                        raise ValueError(
                            f"Restored document #{document_id} checksum conflicts with source evidence"
                        )
                    storage_path = _safe_restore_member_path(
                        source_target,
                        str(entry["archivePath"]),
                    )
                    connection.execute(
                        """
                        UPDATE bookkeeping_documents
                        SET storage_path = ?, content_sha256 = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (storage_path, expected_sha256, _now(), document_id),
                    )
                connection.commit()
                integrity = connection.execute("PRAGMA quick_check").fetchone()
                if not integrity or str(integrity[0]).lower() != "ok":
                    raise ValueError("Rewritten restore ledger failed SQLite integrity validation")
        finally:
            connection.close()

    def _restore_source_evidence(
        self,
        archive: zipfile.ZipFile,
        source_evidence: Dict[str, Any],
        target_root: str,
    ) -> bool:
        if os.path.lexists(target_root):
            self._verify_restored_source_root(target_root, source_evidence)
            return False
        base_root = self._validate_source_restore_base(create=True)
        staging_root = tempfile.mkdtemp(prefix=".fab-source-restore-", dir=base_root)
        try:
            unique_entries = {
                str(entry["archivePath"]): entry
                for entry in source_evidence.get("entries") or []
            }
            for archive_path, entry in sorted(unique_entries.items()):
                destination = _safe_restore_member_path(staging_root, archive_path)
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                digest = hashlib.sha256()
                byte_count = 0
                with archive.open(archive_path) as source, open(destination, "xb") as target:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                        target.write(chunk)
                        byte_count += len(chunk)
                if (
                    digest.hexdigest() != str(entry["sha256"]).lower()
                    or byte_count != int(entry["bytes"])
                ):
                    raise ValueError("Extracted source evidence failed checksum verification")
                try:
                    os.chmod(destination, 0o600)
                except OSError:
                    pass
            self._verify_restored_source_root(staging_root, source_evidence)
            if os.path.lexists(target_root):
                raise ValueError("Source restore target appeared during extraction")
            os.replace(staging_root, target_root)
            staging_root = ""
            self._verify_restored_source_root(target_root, source_evidence)
            return True
        finally:
            if staging_root and os.path.isdir(staging_root):
                shutil.rmtree(staging_root)

    def _verify_restored_source_root(
        self,
        target_root: str,
        source_evidence: Dict[str, Any],
    ) -> None:
        _assert_no_reparse_points(target_root)
        if not os.path.isdir(target_root):
            raise ValueError("Source restore target is not a directory")
        expected = {
            str(entry["archivePath"]): {
                "sha256": str(entry["sha256"]).lower(),
                "bytes": int(entry["bytes"]),
            }
            for entry in source_evidence.get("entries") or []
        }
        actual = set()
        for root, directories, files in os.walk(target_root, followlinks=False):
            for name in [*directories, *files]:
                _assert_not_reparse_point(os.path.join(root, name))
            for filename in files:
                path = os.path.join(root, filename)
                relative = os.path.relpath(path, target_root).replace("\\", "/")
                actual.add(relative)
        if actual != set(expected):
            raise ValueError("Source restore target has missing or unexpected files")
        for relative, declaration in expected.items():
            path = _safe_restore_member_path(target_root, relative)
            if os.path.getsize(path) != declaration["bytes"]:
                raise ValueError("Restored source evidence size does not match manifest")
            if _sha256_file(path) != declaration["sha256"]:
                raise ValueError("Restored source evidence checksum does not match manifest")

    def _verify_live_restore(
        self,
        manifest: Dict[str, Any],
        *,
        source_target: Optional[str],
    ) -> None:
        connection = sqlite3.connect(self.ledger_path)
        connection.row_factory = sqlite3.Row
        try:
            integrity = connection.execute("PRAGMA quick_check").fetchone()
            if not integrity or str(integrity[0]).lower() != "ok":
                raise ValueError("Restored FAB ledger failed SQLite integrity validation")
            if source_target is not None:
                source_evidence = manifest.get("sourceEvidence") or {}
                self._verify_restored_source_root(source_target, source_evidence)
                rows = connection.execute(
                    "SELECT id, storage_path, content_sha256 FROM bookkeeping_documents"
                ).fetchall()
                entries = {
                    int(entry["documentId"]): entry
                    for entry in source_evidence.get("entries") or []
                }
                if {int(row["id"]) for row in rows} != set(entries):
                    raise ValueError("Restored ledger document coverage changed during restore")
                for row in rows:
                    entry = entries[int(row["id"])]
                    expected_path = _safe_restore_member_path(
                        source_target,
                        str(entry["archivePath"]),
                    )
                    if os.path.abspath(str(row["storage_path"] or "")) != expected_path:
                        raise ValueError("Restored ledger source path does not match verified evidence")
                    if str(row["content_sha256"] or "").lower() != str(entry["sha256"]).lower():
                        raise ValueError("Restored ledger source checksum does not match evidence")
        finally:
            connection.close()

    def _snapshot_ledger(self, destination_path: str) -> None:
        source = sqlite3.connect(self.ledger_path)
        destination = sqlite3.connect(destination_path)
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
            source.close()

    def _backup_dir(self) -> str:
        value = _config_value(
            self.config,
            "fab_local_backup_dir",
            "operations_backup_dir",
            "backup_base_dir",
        )
        if not value:
            value = os.path.join(os.path.dirname(self.ledger_path), "backups")
        return os.path.abspath(os.path.expanduser(str(value)))

    def _source_restore_base(self) -> str:
        value = _config_value(
            self.config,
            "fab_backup_restore_source_root",
            "operations_backup_restore_source_root",
            "backup_restore_source_root",
        )
        if not value:
            value = os.path.join(
                os.path.dirname(self.ledger_path),
                "restored-source-evidence",
            )
        expanded = os.path.expanduser(str(value))
        if not os.path.isabs(expanded):
            expanded = os.path.join(self.project_root, expanded)
        return os.path.abspath(expanded)

    def _validate_source_restore_base(self, *, create: bool) -> str:
        base_root = self._source_restore_base()
        try:
            if os.path.commonpath([base_root, self.backup_dir]) == self.backup_dir:
                raise ValueError("Source restore root cannot be inside the backup directory")
        except ValueError:
            raise ValueError("Source restore root is on an incompatible filesystem") from None
        _assert_no_reparse_points(base_root)
        if os.path.lexists(base_root) and not os.path.isdir(base_root):
            raise ValueError("Source restore root is not a directory")
        if create:
            os.makedirs(base_root, exist_ok=True)
            _assert_no_reparse_points(base_root)
        return base_root

    def _source_restore_target(self, manifest: Dict[str, Any]) -> str:
        ledger_sha256 = str(manifest.get("ledgerSha256") or "").strip().lower()
        if not _valid_sha256(ledger_sha256):
            raise ValueError("Recovery package has no valid ledger checksum")
        base_root = self._source_restore_base()
        target = os.path.abspath(
            os.path.join(base_root, f"{RESTORED_SOURCE_DIR_PREFIX}{ledger_sha256[:16]}")
        )
        if os.path.commonpath([target, base_root]) != base_root:
            raise ValueError("Source restore target escaped the configured root")
        return target

    def _resolve_backup_path(self, backup_path: str) -> str:
        if not backup_path:
            raise ValueError("backupPath is required")
        candidate = os.path.expanduser(str(backup_path))
        if not os.path.isabs(candidate):
            candidate = os.path.join(self.backup_dir, candidate)
        candidate = os.path.abspath(candidate)
        if os.path.commonpath([candidate, self.backup_dir]) != self.backup_dir:
            raise ValueError("Backup path must be inside the configured FAB backup directory")
        _assert_no_reparse_points(self.backup_dir)
        _assert_no_reparse_points(candidate)
        return candidate

    def _snapshot_source_evidence(
        self,
        ledger_snapshot_path: str,
        temp_dir: str,
        require_complete: bool,
    ) -> Tuple[Dict[str, Any], Dict[str, str]]:
        connection = sqlite3.connect(ledger_snapshot_path)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT id, storage_path, content_sha256, original_filename
                FROM bookkeeping_documents
                ORDER BY id ASC
                """
            ).fetchall()
        finally:
            connection.close()

        trusted_roots = self._source_evidence_roots()
        entries = []
        gaps = []
        evidence_files: Dict[str, str] = {}
        archive_by_sha: Dict[str, str] = {}
        bytes_by_archive: Dict[str, int] = {}

        for row in rows:
            document_id = int(row["id"])
            source_path = str(row["storage_path"] or "").strip()
            if not source_path:
                gaps.append({"documentId": document_id, "reason": "storage_path_missing"})
                continue
            resolved_path = os.path.abspath(os.path.expanduser(source_path))
            if not _path_within_roots(resolved_path, trusted_roots):
                gaps.append({"documentId": document_id, "reason": "outside_trusted_source_roots"})
                continue
            if not os.path.isfile(resolved_path):
                gaps.append({"documentId": document_id, "reason": "source_file_missing"})
                continue
            file_size = os.path.getsize(resolved_path)
            if file_size > MAX_BACKUP_EVIDENCE_FILE_BYTES:
                gaps.append({"documentId": document_id, "reason": "source_file_too_large"})
                continue

            captured_path = os.path.join(temp_dir, f"source-evidence-{document_id}.bin")
            actual_sha256, captured_bytes = _copy_with_sha256(
                resolved_path,
                captured_path,
            )
            expected_sha256 = str(row["content_sha256"] or "").strip().lower()
            if expected_sha256 and not _valid_sha256(expected_sha256):
                os.remove(captured_path)
                raise ValueError(
                    f"Document #{document_id} has an invalid source SHA-256."
                )
            if expected_sha256 and expected_sha256 != actual_sha256:
                os.remove(captured_path)
                raise ValueError(
                    f"Document #{document_id} source evidence no longer matches its ledger SHA-256."
                )

            archive_path = archive_by_sha.get(actual_sha256)
            if archive_path:
                os.remove(captured_path)
            else:
                extension = _safe_extension(
                    row["original_filename"] or resolved_path
                )
                archive_path = (
                    f"{SOURCE_EVIDENCE_PREFIX}{actual_sha256[:2]}/"
                    f"{actual_sha256}{extension}"
                )
                archive_by_sha[actual_sha256] = archive_path
                evidence_files[archive_path] = captured_path
                bytes_by_archive[archive_path] = captured_bytes
            entries.append({
                "documentId": document_id,
                "archivePath": archive_path,
                "sha256": actual_sha256,
                "bytes": captured_bytes,
                "originalFilename": os.path.basename(
                    str(row["original_filename"] or resolved_path)
                ),
            })

        if require_complete and gaps:
            raise ValueError(
                "Source-complete backup blocked because "
                f"{len(gaps)} document(s) have missing or unsafe source evidence."
            )
        coverage_status = "complete" if len(entries) == len(rows) else "incomplete"
        return {
            "coverageStatus": coverage_status,
            "totalDocuments": len(rows),
            "includedDocuments": len(entries),
            "includedFiles": len(evidence_files),
            "includedBytes": sum(bytes_by_archive.values()),
            "gapCount": len(gaps),
            "gaps": gaps,
            "entries": entries,
        }, evidence_files

    def _source_evidence_roots(self) -> list:
        roots = [os.path.dirname(self.ledger_path)]
        for key in (
            "fab_local_intake_paths",
            "operations_local_intake_paths",
            "local_intake_paths",
            "operations_scanner_folder",
            "operations_scanner_watch_folder",
            "scanner_folder",
            "scanner_watch_folder",
        ):
            value = _config_value(self.config, key)
            roots.extend(_path_values(value))
        return sorted({
            os.path.abspath(os.path.expanduser(root))
            for root in roots
            if str(root or "").strip()
        })

    def _backup_paths(self, limit: int) -> list:
        if not os.path.isdir(self.backup_dir):
            return []
        paths = []
        for name in os.listdir(self.backup_dir):
            if not name.lower().endswith(".zip"):
                continue
            path = os.path.join(self.backup_dir, name)
            try:
                _assert_no_reparse_points(path)
            except ValueError:
                continue
            if os.path.isfile(path):
                paths.append(path)
        paths.sort(
            key=lambda path: os.path.getmtime(path)
            if os.path.exists(path)
            else 0,
            reverse=True,
        )
        return paths[: _bounded_limit(limit)]

    @staticmethod
    def _validate_member_names(names: Any) -> None:
        if len(names) > MAX_BACKUP_ARCHIVE_FILES:
            raise ValueError("Backup archive contains too many files")
        if len(names) != len(set(names)):
            raise ValueError("Backup archive contains duplicate file names")
        for name in names:
            normalized = os.path.normpath(str(name)).replace("\\", "/")
            if normalized.startswith("../") or normalized == ".." or os.path.isabs(str(name)):
                raise ValueError("Backup archive contains an unsafe path")
            if normalized != str(name).replace("\\", "/"):
                raise ValueError("Backup archive contains a non-canonical path")

    @staticmethod
    def _validate_expected_archive_names(names: Any, allowed: set) -> None:
        normalized_names = {str(name).replace("\\", "/").rstrip("/") for name in names}
        unexpected = normalized_names - allowed
        if unexpected:
            raise ValueError(f"Backup archive contains unexpected files: {sorted(unexpected)}")
        missing = allowed - normalized_names
        if missing:
            raise ValueError(f"Backup archive is missing required files: {sorted(missing)}")

    @staticmethod
    def _read_backup_manifest(archive: zipfile.ZipFile) -> Dict[str, Any]:
        # Reject declared expansion before opening even the manifest member.
        total_bytes = 0
        for info in archive.infolist():
            if info.file_size < 0:
                raise ValueError("Backup archive has an invalid member size")
            total_bytes += info.file_size
            if total_bytes > MAX_BACKUP_UNCOMPRESSED_BYTES:
                raise ValueError("Backup exceeds the maximum uncompressed size")
            if info.filename.startswith(SOURCE_EVIDENCE_PREFIX) and info.file_size > MAX_BACKUP_EVIDENCE_FILE_BYTES:
                raise ValueError("Recovery package contains an oversized source-evidence file")
        try:
            info = archive.getinfo(BACKUP_MANIFEST_NAME)
        except KeyError:
            raise ValueError("Backup archive is missing manifest.json") from None
        if info.file_size > MAX_BACKUP_MANIFEST_BYTES:
            raise ValueError("Backup manifest exceeds the maximum size")
        with archive.open(info) as handle:
            content = handle.read(MAX_BACKUP_MANIFEST_BYTES + 1)
        if len(content) > MAX_BACKUP_MANIFEST_BYTES:
            raise ValueError("Backup manifest exceeds the maximum size")

        def reject_constant(_value):
            raise ValueError("Non-finite JSON number")

        try:
            manifest = json.loads(content.decode("utf-8"), parse_constant=reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError("Backup manifest contains invalid JSON") from None
        if not isinstance(manifest, dict):
            raise ValueError("Backup manifest must be a JSON object")
        # Iterator stack bounds traversal memory and prevents deep-copy recursion.
        stack = [iter([manifest])]
        while stack:
            try:
                value = next(stack[-1])
            except StopIteration:
                stack.pop()
                continue
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("Backup manifest contains a non-finite number")
            if isinstance(value, (dict, list)):
                if len(stack) > MAX_BACKUP_MANIFEST_DEPTH:
                    raise ValueError("Backup manifest exceeds the maximum nesting depth")
                stack.append(iter(value.values() if isinstance(value, dict) else value))
        return manifest

    @staticmethod
    def _validate_source_evidence_manifest(
        manifest: Dict[str, Any],
        archive: zipfile.ZipFile,
    ) -> set:
        source_evidence = manifest.get("sourceEvidence")
        if not isinstance(source_evidence, dict):
            raise ValueError("Recovery package has no source-evidence manifest")
        entries = source_evidence.get("entries")
        gaps = source_evidence.get("gaps")
        if not isinstance(entries, list) or not isinstance(gaps, list):
            raise ValueError("Recovery package source-evidence manifest is invalid")
        coverage_status = str(source_evidence.get("coverageStatus") or "")
        if coverage_status not in {"complete", "incomplete"}:
            raise ValueError("Recovery package source-evidence coverage status is invalid")

        declared_files: Dict[str, Dict[str, Any]] = {}
        document_ids = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Recovery package has an invalid source-evidence entry")
            try:
                document_id = int(entry.get("documentId"))
                declared_bytes = int(entry.get("bytes"))
            except (TypeError, ValueError):
                raise ValueError("Recovery package source-evidence entry has invalid numeric fields") from None
            archive_path = str(entry.get("archivePath") or "").replace("\\", "/")
            sha256 = str(entry.get("sha256") or "").strip().lower()
            if document_id <= 0 or document_id in document_ids:
                raise ValueError("Recovery package has duplicate or invalid document evidence")
            if (
                not archive_path.startswith(SOURCE_EVIDENCE_PREFIX)
                or os.path.normpath(archive_path).replace("\\", "/") != archive_path
            ):
                raise ValueError("Recovery package has an unsafe source-evidence path")
            if not _valid_sha256(sha256) or declared_bytes < 0:
                raise ValueError("Recovery package source-evidence checksum is invalid")
            expected_path_prefix = (
                f"{SOURCE_EVIDENCE_PREFIX}{sha256[:2]}/{sha256}"
            )
            if (
                not archive_path.startswith(f"{expected_path_prefix}.")
                or not re.fullmatch(
                    rf"{re.escape(expected_path_prefix)}\.[a-z0-9]{{1,16}}",
                    archive_path,
                )
            ):
                raise ValueError(
                    "Recovery package source-evidence path is not content addressed"
                )
            existing = declared_files.get(archive_path)
            if existing and (
                existing["sha256"] != sha256
                or existing["bytes"] != declared_bytes
            ):
                raise ValueError("Recovery package has conflicting source-evidence declarations")
            document_ids.add(document_id)
            declared_files[archive_path] = {
                "sha256": sha256,
                "bytes": declared_bytes,
            }

        gap_document_ids = set()
        for gap in gaps:
            if not isinstance(gap, dict):
                raise ValueError("Recovery package has an invalid source-evidence gap")
            try:
                gap_document_id = int(gap.get("documentId"))
            except (TypeError, ValueError):
                raise ValueError(
                    "Recovery package source-evidence gap has an invalid document id"
                ) from None
            reason = str(gap.get("reason") or "").strip()
            if (
                gap_document_id <= 0
                or gap_document_id in document_ids
                or gap_document_id in gap_document_ids
                or not reason
            ):
                raise ValueError(
                    "Recovery package has duplicate or invalid source-evidence gaps"
                )
            gap_document_ids.add(gap_document_id)

        included_documents = int(source_evidence.get("includedDocuments") or 0)
        included_files = int(source_evidence.get("includedFiles") or 0)
        included_bytes = int(source_evidence.get("includedBytes") or 0)
        total_documents = int(source_evidence.get("totalDocuments") or 0)
        gap_count = int(source_evidence.get("gapCount") or 0)
        if included_documents != len(entries):
            raise ValueError("Recovery package source-evidence document count is invalid")
        if included_files != len(declared_files):
            raise ValueError("Recovery package source-evidence file count is invalid")
        if gap_count != len(gaps):
            raise ValueError("Recovery package source-evidence gap count is invalid")
        if included_documents + gap_count != total_documents:
            raise ValueError("Recovery package source-evidence coverage count is invalid")
        if coverage_status == "complete" and gap_count:
            raise ValueError("Recovery package claims complete evidence with documented gaps")
        if coverage_status == "incomplete" and not gap_count:
            raise ValueError("Recovery package claims incomplete evidence without documented gaps")

        actual_total_bytes = 0
        for archive_path, declaration in declared_files.items():
            try:
                info = archive.getinfo(archive_path)
            except KeyError:
                raise ValueError(
                    f"Recovery package is missing source evidence: {archive_path}"
                ) from None
            if info.file_size != declaration["bytes"]:
                raise ValueError("Recovery package source-evidence size does not match manifest")
            if info.file_size > MAX_BACKUP_EVIDENCE_FILE_BYTES:
                raise ValueError("Recovery package contains an oversized source-evidence file")
            actual_total_bytes += info.file_size
        if actual_total_bytes != included_bytes:
            raise ValueError("Recovery package source-evidence byte count is invalid")
        return set(declared_files)

    @staticmethod
    def _verify_source_evidence_bytes(manifest: Dict[str, Any], archive: zipfile.ZipFile) -> None:
        entries = (manifest.get("sourceEvidence") or {}).get("entries") or []
        unique_entries = {entry["archivePath"].replace("\\", "/"): entry for entry in entries}
        for archive_path, entry in unique_entries.items():
            digest = hashlib.sha256()
            with archive.open(archive_path) as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != str(entry["sha256"]).strip().lower():
                raise ValueError("Recovery package source-evidence checksum does not match manifest")

    def _safe_config_summary(self) -> Dict[str, Any]:
        keys = (
            "fab_local_api_host",
            "fab_local_api_port",
            "fab_local_intake_paths",
            "fab_local_intake_extensions",
            "operations_local_intake_paths",
            "operations_intake_paths",
            "operations_scanner_folder",
            "operations_scanner_watch_folder",
            "scanner_folder",
            "scanner_watch_folder",
            "review_stale_hours",
            "document_stale_hours",
            "routing_stale_hours",
            "workflow_stale_hours",
        )
        summary = {"ledgerBasename": os.path.basename(self.ledger_path), "backupDir": self.backup_dir}
        for key in keys:
            value = _config_value(self.config, key)
            if value not in (None, ""):
                summary[key] = value
        return summary


def _restore_mode(value: Any) -> str:
    normalized = str(value or RESTORE_MODE_LEDGER_ONLY).strip().lower()
    aliases = {
        "ledger": RESTORE_MODE_LEDGER_ONLY,
        "ledger_only": RESTORE_MODE_LEDGER_ONLY,
        "full": RESTORE_MODE_FULL,
        "ledger_and_source_evidence": RESTORE_MODE_FULL,
    }
    if normalized not in aliases:
        raise ValueError(
            f"restoreMode must be {RESTORE_MODE_LEDGER_ONLY} or {RESTORE_MODE_FULL}"
        )
    return aliases[normalized]


def _restore_manifest_summary(manifest: Dict[str, Any]) -> Dict[str, Any]:
    source_evidence = manifest.get("sourceEvidence") or {}
    return {
        "format": manifest.get("format"),
        "createdAt": manifest.get("createdAt"),
        "ledgerBytes": manifest.get("ledgerBytes"),
        "ledgerSha256": manifest.get("ledgerSha256"),
        "sourceEvidence": {
            "coverageStatus": source_evidence.get("coverageStatus", "legacy_ledger_only"),
            "totalDocuments": source_evidence.get("totalDocuments", 0),
            "includedDocuments": source_evidence.get("includedDocuments", 0),
            "includedFiles": source_evidence.get("includedFiles", 0),
            "includedBytes": source_evidence.get("includedBytes", 0),
            "gapCount": source_evidence.get("gapCount", 0),
        },
    }


def _safe_restore_member_path(root: str, archive_path: str) -> str:
    normalized = str(archive_path or "").replace("\\", "/")
    if (
        not normalized.startswith(SOURCE_EVIDENCE_PREFIX)
        or os.path.normpath(normalized).replace("\\", "/") != normalized
        or os.path.isabs(normalized)
    ):
        raise ValueError("Source restore member has an unsafe path")
    root_path = os.path.abspath(root)
    destination = os.path.abspath(os.path.join(root_path, *normalized.split("/")))
    if os.path.commonpath([destination, root_path]) != root_path:
        raise ValueError("Source restore member escaped the target root")
    return destination


def _assert_not_reparse_point(path: str) -> None:
    try:
        stat = os.lstat(path)
    except FileNotFoundError:
        return
    file_attributes = int(getattr(stat, "st_file_attributes", 0) or 0)
    if os.path.islink(path) or file_attributes & 0x400:
        raise ValueError("Paths cannot contain links or reparse points")


def _assert_no_reparse_points(path: str) -> None:
    candidate = Path(os.path.abspath(path))
    existing = candidate
    while not os.path.lexists(str(existing)) and existing.parent != existing:
        existing = existing.parent
    chain = [existing]
    while chain[-1].parent != chain[-1]:
        chain.append(chain[-1].parent)
    for item in reversed(chain):
        if os.path.lexists(str(item)):
            _assert_not_reparse_point(str(item))
    if os.path.lexists(str(candidate)):
        _assert_not_reparse_point(str(candidate))


def _project_root(ledger_path: str, config: Dict[str, Any]) -> str:
    configured = _config_value(
        config,
        "fab_instance_root",
        "operations_instance_root",
        "instance_root",
    )
    if configured:
        return os.path.abspath(os.path.expanduser(str(configured)))
    ledger_parent = Path(ledger_path).resolve().parent
    return str(ledger_parent.parent if ledger_parent.name.lower() == "data" else ledger_parent)


def _sqlite_backup(source_path: str, destination_path: str) -> None:
    source = sqlite3.connect(f"file:{Path(source_path).as_posix()}?mode=ro", uri=True)
    destination = sqlite3.connect(destination_path, timeout=30.0)
    try:
        destination.execute("PRAGMA busy_timeout = 30000")
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()


def _config_value(config: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = config.get(key)
        if value not in (None, ""):
            return value
    for key in keys:
        if "_" not in key:
            continue
        section, option = key.split("_", 1)
        section_values = config.get(section)
        if isinstance(section_values, dict):
            value = section_values.get(option)
            if value not in (None, ""):
                return value
    return None


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_with_sha256(source_path: str, destination_path: str) -> Tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    with open(source_path, "rb") as source, open(destination_path, "wb") as destination:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            destination.write(chunk)
            byte_count += len(chunk)
    return digest.hexdigest(), byte_count


def _canonical_manifest(manifest: Dict[str, Any]) -> bytes:
    return json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _authenticated_cached_result(
    cached: Optional[Dict[str, Any]],
    signature: Tuple[int, ...],
    manifest: Dict[str, Any],
    integrity_status: str,
) -> Optional[Dict[str, Any]]:
    if not cached or cached.get("signature") != signature:
        return None
    result = cached.get("result")
    if not isinstance(result, dict):
        return None
    if result.get("manifest") != manifest:
        return None
    if result.get("integrityStatus") != integrity_status:
        return None
    return copy.deepcopy(result)


def _verify_cached_manifest(
    cached: Optional[Dict[str, Any]],
    signature: Tuple[int, ...],
    verify_integrity: Any,
) -> Optional[Dict[str, Any]]:
    if not cached or cached.get("signature") != signature:
        return None
    result = cached.get("result")
    manifest = result.get("manifest") if isinstance(result, dict) else None
    if not isinstance(manifest, dict):
        return None
    integrity_status = verify_integrity(manifest)
    if result.get("integrityStatus") != integrity_status:
        return None
    return copy.deepcopy(result)


def _file_signature(path: str) -> Tuple[int, ...]:
    stat = os.stat(path)
    return (
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(stat.st_ctime_ns),
        int(getattr(stat, "st_dev", 0)),
        int(getattr(stat, "st_ino", 0)),
    )


@contextmanager
def _open_preflighted_archive(path: str):
    with open(path, "rb") as source:
        _preflight_zip_directory(source)
        with zipfile.ZipFile(source, "r") as archive:
            yield archive


def _preflight_zip_directory(source: Any) -> None:
    file_size = os.fstat(source.fileno()).st_size
    minimum_end_record_bytes = 22
    if file_size < minimum_end_record_bytes:
        raise ValueError("Backup archive has no valid end-of-directory record")

    tail_size = min(file_size, minimum_end_record_bytes + 0xFFFF)
    source.seek(file_size - tail_size)
    tail = source.read(tail_size)
    end_record_signature = b"PK\x05\x06"
    end_record_offset = tail.rfind(end_record_signature)
    while end_record_offset >= 0:
        if end_record_offset + minimum_end_record_bytes <= len(tail):
            comment_size = struct.unpack_from("<H", tail, end_record_offset + 20)[0]
            if end_record_offset + minimum_end_record_bytes + comment_size == len(tail):
                break
        end_record_offset = tail.rfind(end_record_signature, 0, end_record_offset)
    if end_record_offset < 0:
        raise ValueError("Backup archive has no valid end-of-directory record")

    (
        _,
        disk_number,
        central_directory_disk,
        entries_on_disk,
        entry_count,
        central_directory_size,
        central_directory_offset,
        _,
    ) = struct.unpack_from("<4s4H2LH", tail, end_record_offset)
    end_record_position = file_size - tail_size + end_record_offset
    zip64_required = (
        entries_on_disk == 0xFFFF
        or entry_count == 0xFFFF
        or central_directory_size == 0xFFFFFFFF
        or central_directory_offset == 0xFFFFFFFF
    )

    if zip64_required:
        locator_position = end_record_position - 20
        if locator_position < 0:
            raise ValueError("Backup archive has an invalid ZIP64 locator")
        source.seek(locator_position)
        locator = source.read(20)
        if len(locator) != 20:
            raise ValueError("Backup archive has an invalid ZIP64 locator")
        locator_signature, locator_disk, zip64_record_position, disk_count = struct.unpack(
            "<4sIQI",
            locator,
        )
        if (
            locator_signature != b"PK\x06\x07"
            or locator_disk != 0
            or disk_count != 1
        ):
            raise ValueError("Multi-volume backup archives are not supported")
        source.seek(zip64_record_position)
        zip64_record = source.read(56)
        if len(zip64_record) != 56:
            raise ValueError("Backup archive has an invalid ZIP64 end record")
        (
            zip64_signature,
            zip64_record_size,
            _,
            _,
            disk_number,
            central_directory_disk,
            entries_on_disk,
            entry_count,
            central_directory_size,
            central_directory_offset,
        ) = struct.unpack("<4sQ2H2I4Q", zip64_record)
        if (
            zip64_signature != b"PK\x06\x06"
            or zip64_record_size < 44
            or zip64_record_position + 12 + zip64_record_size > locator_position
        ):
            raise ValueError("Backup archive has an invalid ZIP64 end record")

    if disk_number != 0 or central_directory_disk != 0 or entries_on_disk != entry_count:
        raise ValueError("Multi-volume backup archives are not supported")
    if entry_count > MAX_BACKUP_ARCHIVE_FILES:
        raise ValueError("Backup archive contains too many files")
    if central_directory_size > MAX_BACKUP_CENTRAL_DIRECTORY_BYTES:
        raise ValueError("Backup archive directory exceeds the maximum size")
    if central_directory_offset + central_directory_size > file_size:
        raise ValueError("Backup archive has an invalid central-directory range")


def _store_inspection_cache(
    cache: Dict[str, Dict[str, Any]],
    path: str,
    signature: Tuple[int, ...],
    result: Dict[str, Any],
    *,
    manifest_bytes: int,
) -> None:
    if manifest_bytes > MAX_CACHED_MANIFEST_BYTES:
        cache.pop(path, None)
        return
    cache[path] = {
        "signature": signature,
        "result": copy.deepcopy(result),
    }
    while len(cache) > MAX_INSPECTION_CACHE_ENTRIES:
        cache.pop(next(iter(cache)))


def _valid_sha256(value: Any) -> bool:
    normalized = str(value or "").strip().lower()
    return bool(re.fullmatch(r"[0-9a-f]{64}", normalized))


def _safe_extension(value: Any) -> str:
    extension = os.path.splitext(str(value or ""))[1].lower()
    if re.fullmatch(r"\.[a-z0-9]{1,16}", extension):
        return extension
    return ".bin"


def _path_values(value: Any) -> list:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set)):
        return [
            str(item).strip()
            for item in value
            if str(item or "").strip()
        ]
    return [
        item.strip()
        for item in re.split(r"[,;\n]+", str(value))
        if item.strip()
    ]


def _path_within_roots(path: str, roots: Any) -> bool:
    for root in roots:
        try:
            if os.path.commonpath([path, root]) == root:
                return True
        except ValueError:
            continue
    return False


def _bool_config(
    config: Dict[str, Any],
    *keys: str,
    default: bool = False,
) -> bool:
    value = _config_value(config, *keys)
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _positive_float_config(
    config: Dict[str, Any],
    *keys: str,
    default: float,
) -> float:
    value = _config_value(config, *keys)
    try:
        parsed = float(value) if value not in (None, "") else float(default)
    except (TypeError, ValueError):
        parsed = float(default)
    return max(1.0, min(parsed, 24.0 * 365.0))


def _parse_timestamp(value: Any) -> Optional[datetime]:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _bounded_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 25
    return max(1, min(parsed, 100))


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
