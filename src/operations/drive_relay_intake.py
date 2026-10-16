from __future__ import annotations

import hashlib
import os
import re
import tempfile
from typing import Any, Dict, Optional

from werkzeug.utils import secure_filename

from src.operations.local_intake import LocalFolderIntake
from src.operations.local_ledger import LocalOperationsLedger


DEFAULT_DRIVE_RELAY_MAX_BYTES = 25 * 1024 * 1024
MAX_DRIVE_RELAY_MAX_BYTES = 100 * 1024 * 1024
GOOGLE_DRIVE_FILE_ID = re.compile(r"^[A-Za-z0-9_-]{10,200}$")


class DriveRelayIntakeService:
    """Accept exact Drive bytes from an authenticated external connector."""

    def __init__(
        self,
        ledger: LocalOperationsLedger,
        config: Optional[Dict[str, Any]] = None,
    ):
        self.ledger = ledger
        self.config = config or {}

    def status(self) -> Dict[str, Any]:
        source_folder_id = self._source_folder_id()
        return {
            "status": "ready" if source_folder_id else "needs_configuration",
            "mode": "authenticated_binary_relay",
            "sourceFolderConfigured": bool(source_folder_id),
            "sourceFolderId": source_folder_id or None,
            "maxBytes": self._max_bytes(),
            "intakePath": "/api/connectors/google-drive/relay",
            "identityPolicy": "provider_file_id_plus_sha256",
            "overwritePolicy": "content_addressed_never_overwrite",
            "externalSubmission": "not_executed",
        }

    def ingest(
        self,
        content: bytes,
        *,
        provider_file_id: Any,
        source_folder_id: Any,
        filename: Any,
        mime_type: Any = None,
        provider_size: Any = None,
        expected_sha256: Any = None,
        created_time: Any = None,
        modified_time: Any = None,
        md5_checksum: Any = None,
        web_view_link: Any = None,
        actor: Any = "hai-drive-relay",
    ) -> Dict[str, Any]:
        configured_folder_id = self._source_folder_id()
        received_folder_id = str(source_folder_id or "").strip()
        file_id = str(provider_file_id or "").strip()
        original_filename = os.path.basename(str(filename or "").strip())
        reasons = []
        if not configured_folder_id:
            reasons.append("drive_source_folder_not_configured")
        elif received_folder_id != configured_folder_id:
            reasons.append("drive_source_folder_mismatch")
        if not GOOGLE_DRIVE_FILE_ID.fullmatch(file_id):
            reasons.append("drive_provider_file_id_invalid")
        if not original_filename:
            reasons.append("drive_filename_missing")
        if not isinstance(content, bytes) or not content:
            reasons.append("drive_file_empty")
        elif len(content) > self._max_bytes():
            reasons.append("drive_file_exceeds_relay_limit")

        source_hash = hashlib.sha256(content).hexdigest() if isinstance(content, bytes) else ""
        expected_hash = str(expected_sha256 or "").strip().lower()
        if expected_hash and expected_hash != source_hash:
            reasons.append("drive_source_sha256_mismatch")
        parsed_provider_size = _optional_non_negative_int(provider_size)
        if provider_size not in (None, "") and parsed_provider_size is None:
            reasons.append("drive_provider_size_invalid")
        elif parsed_provider_size is not None and parsed_provider_size != len(content):
            reasons.append("drive_provider_size_mismatch")
        if reasons:
            return self._reject(file_id, reasons, actor)

        download_root = self._download_root()
        os.makedirs(download_root, exist_ok=True)
        destination = os.path.join(
            download_root,
            f"{source_hash[:12]}-{_safe_filename(original_filename)}",
        )
        if not os.path.isfile(destination):
            _atomic_write(destination, content)
        elif _sha256_file(destination) != source_hash:
            return self._reject(file_id, ["drive_relay_destination_hash_mismatch"], actor)

        source_account_id = self.ledger.upsert_source_account({
            "sourceType": "google_drive",
            "sourceIdentifier": configured_folder_id,
            "label": "Google Drive relay",
            "status": "relay_ready",
            "lastSeenAt": modified_time or created_time,
            "lastScanAt": _now(),
            "metadata": {
                "mode": "authenticated_binary_relay",
                "intakePath": "/api/connectors/google-drive/relay",
                "externalSubmission": "not_executed",
            },
        })
        registrar = LocalFolderIntake(
            self.ledger,
            allowed_extensions={"*"},
            source="google_drive",
        )
        registration = registrar.register_fetched_document(
            {
                "id": file_id,
                "source": "google_drive",
                "original_filename": original_filename,
                "mime_type": str(mime_type or "application/octet-stream").strip(),
                "local_path": destination,
                "timestamp": created_time,
                "metadata": {
                    "folder_id": configured_folder_id,
                    "mime_type": str(mime_type or "application/octet-stream").strip(),
                    "provider_mime_type": str(mime_type or "application/octet-stream").strip(),
                    "modified_time": modified_time,
                    "size": len(content),
                    "md5_checksum": str(md5_checksum or "").strip() or None,
                    "web_view_link": str(web_view_link or "").strip() or None,
                    "relay_sha256": source_hash,
                    "relay_actor": str(actor or "hai-drive-relay")[:200],
                },
            },
            source_account_id=source_account_id,
            root=download_root,
        )
        if registration.get("skipped"):
            return self._reject(
                file_id,
                [str((registration.get("skipped") or {}).get("reason") or "drive_registration_failed")],
                actor,
            )

        document = registration.get("document") or {}
        self.ledger.record_audit_event({
            "action": "drive_relay.document_received",
            "entityType": "bookkeeping_document",
            "entityId": str(document.get("id") or ""),
            "details": {
                "actor": str(actor or "hai-drive-relay")[:200],
                "providerFileId": file_id,
                "sourceFolderId": configured_folder_id,
                "sourceSha256": source_hash,
                "sizeBytes": len(content),
                "registrationStatus": registration.get("status"),
                "externalSubmission": "not_executed",
            },
        })
        return {
            "success": True,
            "status": str(registration.get("status") or "registered"),
            "providerFileId": file_id,
            "sourceFolderId": configured_folder_id,
            "sourceSha256": source_hash,
            "sizeBytes": len(content),
            "document": document,
            "externalSubmission": "not_executed",
        }

    def _reject(self, file_id: str, reasons: list[str], actor: Any) -> Dict[str, Any]:
        self.ledger.record_audit_event({
            "action": "drive_relay.document_rejected",
            "entityType": "google_drive_file",
            "entityId": file_id,
            "details": {
                "actor": str(actor or "hai-drive-relay")[:200],
                "reasons": sorted(set(reasons)),
                "externalSubmission": "not_executed",
            },
        })
        return {
            "success": False,
            "status": "rejected",
            "providerFileId": file_id or None,
            "reasons": sorted(set(reasons)),
            "externalSubmission": "not_executed",
        }

    def _source_folder_id(self) -> str:
        return str(
            self.config.get("google_drive_wave_source_folder_id")
            or self.config.get("google_drive_folder_id")
            or self.config.get("drive_folder_id")
            or ""
        ).strip()

    def _download_root(self) -> str:
        value = (
            self.config.get("google_drive_download_dir")
            or self.config.get("drive_download_dir")
            or "data/source_downloads/google-drive"
        )
        return os.path.abspath(os.path.expanduser(str(value)))

    def _max_bytes(self) -> int:
        value = self.config.get("google_drive_relay_max_bytes")
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = DEFAULT_DRIVE_RELAY_MAX_BYTES
        return max(1, min(parsed, MAX_DRIVE_RELAY_MAX_BYTES))


def _safe_filename(filename: str) -> str:
    portable_basename = os.path.basename(str(filename).replace("\\", "/"))
    safe = secure_filename(portable_basename).strip(" .")
    safe = safe or "drive-document"
    if len(safe) <= 180:
        return safe
    stem, extension = os.path.splitext(safe)
    extension = extension[:20]
    return f"{stem[:max(1, 180 - len(extension))]}{extension}"


def _atomic_write(destination: str, content: bytes) -> None:
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=".fab-drive-relay-",
        suffix=".tmp",
        dir=os.path.dirname(destination),
        delete=False,
    )
    temporary_path = handle.name
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _optional_non_negative_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
