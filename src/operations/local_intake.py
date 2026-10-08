import hashlib
import mimetypes
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Sequence, Set

from src.document_handling.source_identity import source_document_id
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_targets import resolve_document_target_system


DEFAULT_ALLOWED_EXTENSIONS = {
    ".csv",
    ".heic",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".tif",
    ".tiff",
    ".txt",
}


class LocalFolderIntake:
    """Import document metadata from local or synced folders into the ledger."""

    def __init__(
        self,
        ledger: LocalOperationsLedger,
        allowed_extensions: Optional[Iterable[str]] = None,
        source: str = "local_folder",
    ):
        self.ledger = ledger
        self.allowed_extensions = _normalize_extensions(allowed_extensions)
        self.source = source

    def rescan(self, folders: Sequence[str]) -> Dict[str, Any]:
        roots = [_normalize_path(folder) for folder in folders if str(folder or "").strip()]
        summary: Dict[str, Any] = {
            "folders": roots,
            "allowedExtensions": sorted(self.allowed_extensions),
            "scanned": 0,
            "registered": 0,
            "duplicates": 0,
            "alreadyRegistered": 0,
            "skipped": [],
            "documents": [],
        }

        for root in roots:
            scan_started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            if not os.path.isdir(root):
                summary["skipped"].append({"path": root, "reason": "folder_missing"})
                self.ledger.upsert_source_account({
                    "sourceType": self.source,
                    "sourceIdentifier": root,
                    "label": os.path.basename(root) or root,
                    "status": "missing",
                    "lastScanAt": scan_started_at,
                    "metadata": {
                        "path": root,
                        "reason": "folder_missing",
                        "allowedExtensions": sorted(self.allowed_extensions),
                    },
                })
                continue
            source_account_id = self.ledger.upsert_source_account({
                "sourceType": self.source,
                "sourceIdentifier": root,
                "label": os.path.basename(root) or root,
                "status": "ready",
                "lastScanAt": scan_started_at,
                "metadata": {
                    "path": root,
                    "allowedExtensions": sorted(self.allowed_extensions),
                },
            })
            root_summary = {
                "scanned": 0,
                "registered": 0,
                "duplicates": 0,
                "alreadyRegistered": 0,
            }
            for path in self._iter_document_paths(root):
                result = self._register_file(root, path, source_account_id)
                if result.get("skipped"):
                    summary["skipped"].append(result["skipped"])
                    continue
                summary["scanned"] += 1
                root_summary["scanned"] += 1
                status = result["status"]
                if status == "already_registered":
                    summary["alreadyRegistered"] += 1
                    root_summary["alreadyRegistered"] += 1
                else:
                    summary["registered"] += 1
                    root_summary["registered"] += 1
                    if status == "duplicate":
                        summary["duplicates"] += 1
                        root_summary["duplicates"] += 1
                summary["documents"].append(result["document"])
            self.ledger.upsert_source_account({
                "sourceType": self.source,
                "sourceIdentifier": root,
                "label": os.path.basename(root) or root,
                "status": "ready",
                "lastSeenAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "lastScanAt": scan_started_at,
                "documentsSeen": root_summary["scanned"],
                "documentsImported": root_summary["registered"],
                "duplicatesDetected": root_summary["duplicates"],
                "metadata": {
                    "path": root,
                    "allowedExtensions": sorted(self.allowed_extensions),
                    **root_summary,
                },
            })

        self.ledger.record_audit_event({
            "action": "local_intake.rescan",
            "entityType": "folder_intake",
            "details": {
                "folders": roots,
                "scanned": summary["scanned"],
                "registered": summary["registered"],
                "duplicates": summary["duplicates"],
                "alreadyRegistered": summary["alreadyRegistered"],
                "skipped": len(summary["skipped"]),
            },
        })
        return summary

    def register_local_file(self, root: str, path: str) -> Dict[str, Any]:
        """Register one newly uploaded file without rescanning its whole folder."""
        normalized_root = _normalize_path(root)
        normalized_path = _normalize_path(path)
        try:
            if os.path.commonpath((normalized_root, normalized_path)) != normalized_root:
                return {"skipped": {"reason": "path_outside_source_root"}}
        except ValueError:
            return {"skipped": {"reason": "path_outside_source_root"}}
        if (
            not os.path.isdir(normalized_root)
            or os.path.islink(normalized_path)
            or not os.path.isfile(normalized_path)
        ):
            return {"skipped": {"reason": "local_file_unavailable"}}
        extension = os.path.splitext(normalized_path)[1].lower()
        if "*" not in self.allowed_extensions and extension not in self.allowed_extensions:
            return {"skipped": {"reason": "unsupported_extension"}}

        timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        source_account_id = self.ledger.upsert_source_account({
            "sourceType": self.source,
            "sourceIdentifier": normalized_root,
            "label": os.path.basename(normalized_root) or normalized_root,
            "status": "ready",
            "metadata": {
                "path": normalized_root,
                "allowedExtensions": sorted(self.allowed_extensions),
            },
        })
        result = self._register_file(normalized_root, normalized_path, source_account_id)
        if result.get("skipped"):
            return result

        status = str(result.get("status") or "")
        self.ledger.upsert_source_account({
            "sourceType": self.source,
            "sourceIdentifier": normalized_root,
            "label": os.path.basename(normalized_root) or normalized_root,
            "status": "ready",
            "lastSeenAt": timestamp,
            "documentsSeen": int(status != "already_registered"),
            "documentsImported": int(status != "already_registered"),
            "duplicatesDetected": int(status == "duplicate"),
            "metadata": {
                "path": normalized_root,
                "allowedExtensions": sorted(self.allowed_extensions),
                "lastFileRegistrationStatus": status,
                "lastFileRegistrationAt": timestamp,
            },
        })
        self.ledger.record_audit_event({
            "action": "local_intake.single_file_registration",
            "entityType": "bookkeeping_document",
            "entityId": str(result["document"].get("id") or ""),
            "details": {
                "sourceAccountId": source_account_id,
                "path": normalized_path,
                "status": status,
            },
        })
        return result

    def has_pending_changes(self, folders: Sequence[str]) -> bool:
        """Return true when a scheduled rescan would change durable state."""
        roots = [_normalize_path(folder) for folder in folders if str(folder or "").strip()]
        source_accounts = {
            str(account.get("source_identifier") or account.get("sourceIdentifier") or ""): account
            for account in self.ledger.list_source_accounts(
                source_type=self.source,
                limit=max(100, len(roots) * 2),
            )
        }
        for root in roots:
            account = source_accounts.get(root)
            if not os.path.isdir(root):
                if not account or str(account.get("status") or "").lower() != "missing":
                    return True
                continue
            if not account or str(account.get("status") or "").lower() != "ready":
                return True
            for path in self._iter_document_paths(root):
                try:
                    stat = os.stat(path)
                    modified_at = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
                    source_id = source_document_id({
                        "local_path": path,
                        "original_filename": os.path.basename(path),
                        "mime_type": mimetypes.guess_type(path)[0] or "application/octet-stream",
                        "modified_time": modified_at,
                        "size": stat.st_size,
                    })
                    existing = self.ledger.get_document_by_source(self.source, source_id)
                    if not existing:
                        return True
                    existing_hash = _document_content_hash(existing)
                    if not existing_hash:
                        return True
                    if existing_hash != _sha256_file(path):
                        return True
                except OSError:
                    return True
        return False

    def _iter_document_paths(self, root: str):
        for current_root, dir_names, file_names in os.walk(root, followlinks=False):
            dir_names[:] = [
                name
                for name in dir_names
                if not os.path.islink(os.path.join(current_root, name))
            ]
            for file_name in sorted(file_names):
                path = os.path.join(current_root, file_name)
                if os.path.islink(path):
                    continue
                extension = os.path.splitext(file_name)[1].lower()
                if "*" not in self.allowed_extensions and extension not in self.allowed_extensions:
                    continue
                yield path

    def register_fetched_document(
        self,
        document: Dict[str, Any],
        source_account_id: Optional[int] = None,
        root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Register a downloaded connector document through the local intake ledger."""
        path_value = document.get("local_path") or document.get("storage_path")
        if not str(path_value or "").strip():
            return {"skipped": {"path": None, "reason": "local_path_missing"}}
        path = _normalize_path(path_value)
        source_root = _normalize_path(root or os.path.dirname(path))
        try:
            if os.path.commonpath((source_root, path)) != source_root:
                return {"skipped": {"path": path, "reason": "path_outside_source_root"}}
        except ValueError:
            return {"skipped": {"path": path, "reason": "path_outside_source_root"}}
        return self._register_file(
            source_root,
            path,
            source_account_id,
            source_document=document,
        )

    def _register_file(
        self,
        root: str,
        path: str,
        source_account_id: Optional[int] = None,
        source_document: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        try:
            stat = os.stat(path)
            content_hash = _sha256_file(path)
        except OSError as exc:
            return {"skipped": {"path": path, "reason": "read_error", "message": str(exc)}}

        source_document = dict(source_document or {})
        source_metadata = source_document.get("metadata") if isinstance(source_document.get("metadata"), dict) else {}
        target_system = _source_document_target_system(source_document)
        mime_type = (
            source_document.get("mime_type")
            or source_metadata.get("mime_type")
            or mimetypes.guess_type(path)[0]
            or "application/octet-stream"
        )
        original_filename = os.path.basename(
            str(source_document.get("original_filename") or source_document.get("filename") or path)
        )
        modified_at = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
        identity_document = {
            **source_document,
            "local_path": path,
            "original_filename": original_filename,
            "mime_type": mime_type,
            "modified_time": modified_at,
            "size": stat.st_size,
        }
        source_id = source_document_id(identity_document)
        existing = self.ledger.get_document_by_source(self.source, source_id) if source_id else None
        if existing:
            existing_hash = _document_content_hash(existing)
            if existing_hash and existing_hash != content_hash:
                revision_source_id = source_document_id({
                    "id": f"{source_id}:revision:{content_hash[:16]}",
                })
                existing_revision = self.ledger.get_document_by_source(self.source, revision_source_id)
                if existing_revision:
                    return {
                        "status": "already_registered",
                        "document": {
                            "id": existing_revision["id"],
                            "path": path,
                            "sourceAccountId": source_account_id or existing_revision.get("source_account_id"),
                            "sourceDocumentId": revision_source_id,
                            "status": existing_revision["processing_status"],
                        },
                    }
                source_id = revision_source_id
                revision_of_document_id = int(existing["id"])
            else:
                revision_of_document_id = None
        else:
            revision_of_document_id = None

        if existing and revision_of_document_id is None:
            update_payload: Dict[str, Any] = {}
            target_backfilled = False
            if source_account_id and not existing.get("source_account_id"):
                update_payload["sourceAccountId"] = source_account_id
            if target_system and not _stored_document_target_system(existing):
                existing_metadata = dict(existing.get("metadata") or {})
                existing_metadata["targetSystem"] = target_system
                update_payload["metadata"] = existing_metadata
                target_backfilled = True
            if update_payload:
                self.ledger.update_document(int(existing["id"]), update_payload)
            if target_backfilled:
                self.ledger.record_audit_event({
                    "action": "local_intake.target_system_backfilled",
                    "entityType": "bookkeeping_document",
                    "entityId": str(existing["id"]),
                    "details": {
                        "source": self.source,
                        "sourceAccountId": source_account_id or existing.get("source_account_id"),
                        "targetSystem": target_system,
                        "externalSubmission": "not_executed",
                    },
                })
            return {
                "status": "already_registered",
                "document": {
                    "id": existing["id"],
                    "path": path,
                    "sourceAccountId": source_account_id or existing.get("source_account_id"),
                    "sourceDocumentId": source_id,
                    "status": existing["processing_status"],
                    "targetSystem": target_system or _stored_document_target_system(existing),
                    "targetBackfilled": target_backfilled,
                },
            }

        duplicate = self.ledger.find_document_by_content_hash(
            content_hash,
            exclude_source_document_id=source_id,
        )
        duplicate_of_document_id = duplicate["id"] if duplicate else None
        processing_status = "needs_review" if duplicate_of_document_id or revision_of_document_id else "imported"
        try:
            relative_path = os.path.relpath(path, root)
        except ValueError:
            relative_path = original_filename
        document_metadata = {
            "contentSha256": content_hash,
            "folder": root,
            "relativePath": relative_path,
            "sizeBytes": stat.st_size,
            "modifiedAt": modified_at,
            "intakeSource": self.source,
            "sourceAccountId": source_account_id,
            "sourceIdentifier": root,
            "providerMetadata": source_metadata,
            "providerTimestamp": source_document.get("timestamp"),
            "sourceRevision": (
                {
                    "revisionOfDocumentId": revision_of_document_id,
                    "baseSourceDocumentId": source_document_id(identity_document),
                }
                if revision_of_document_id
                else None
            ),
        }
        if target_system:
            document_metadata["targetSystem"] = target_system
        payload = {
            "sourceAccountId": source_account_id,
            "source": self.source,
            "sourceDocumentId": source_id,
            "originalFilename": original_filename,
            "mimeType": mime_type,
            "storagePath": path,
            "documentType": _document_type(path, mime_type),
            "processingStatus": processing_status,
            "duplicateFingerprint": content_hash,
            "contentSha256": content_hash,
            "duplicateOfDocumentId": duplicate_of_document_id,
            "metadata": document_metadata,
        }
        document_id = self.ledger.register_document(payload)

        if duplicate_of_document_id:
            duplicate_candidate_id = self.ledger.record_duplicate_candidate({
                "documentId": document_id,
                "candidateDocumentId": duplicate_of_document_id,
                "matchType": "exact_content_hash",
                "confidenceScore": 1.0,
                "status": "pending",
                "reason": "Exact content hash match during folder intake.",
                "evidence": {
                    "contentSha256": content_hash,
                    "sourceDocumentId": source_id,
                    "path": path,
                    "duplicateOfDocumentId": duplicate_of_document_id,
                    "source": self.source,
                },
            })
            self.ledger.create_review_item({
                "documentId": document_id,
                "reason": "duplicate_candidate",
                "details": f"Exact content match with document #{duplicate_of_document_id}.",
                "correctedData": {
                    "duplicateCandidateId": duplicate_candidate_id,
                    "duplicateOfDocumentId": duplicate_of_document_id,
                    "contentSha256": content_hash,
                    "sourceDocumentId": source_id,
                },
            })
            self.ledger.record_audit_event({
                "action": "local_intake.duplicate_candidate",
                "entityType": "bookkeeping_document",
                "entityId": str(document_id),
                "details": {
                    "duplicateCandidateId": duplicate_candidate_id,
                    "duplicateOfDocumentId": duplicate_of_document_id,
                    "contentSha256": content_hash,
                    "path": path,
                },
            })
            status = "duplicate"
        elif revision_of_document_id:
            self.ledger.create_review_item({
                "documentId": document_id,
                "reason": "source_revision_detected",
                "details": f"The provider document changed after document #{revision_of_document_id} was imported.",
                "correctedData": {
                    "revisionOfDocumentId": revision_of_document_id,
                    "contentSha256": content_hash,
                    "sourceDocumentId": source_id,
                },
            })
            self.ledger.record_audit_event({
                "action": "local_intake.source_revision_detected",
                "entityType": "bookkeeping_document",
                "entityId": str(document_id),
                "details": {
                    "revisionOfDocumentId": revision_of_document_id,
                    "contentSha256": content_hash,
                    "sourceDocumentId": source_id,
                    "path": path,
                },
            })
            status = "revision"
        else:
            self.ledger.record_audit_event({
                "action": "local_intake.document_imported",
                "entityType": "bookkeeping_document",
                "entityId": str(document_id),
                "details": {
                    "contentSha256": content_hash,
                    "path": path,
                    "sourceDocumentId": source_id,
                },
            })
            status = "registered"

        return {
            "status": status,
            "document": {
                "id": document_id,
                "path": path,
                "sourceAccountId": source_account_id,
                "sourceDocumentId": source_id,
                "status": processing_status,
                "duplicateOfDocumentId": duplicate_of_document_id,
                "targetSystem": target_system,
                "targetBackfilled": False,
            },
        }


def _normalize_extensions(extensions: Optional[Iterable[str]]) -> Set[str]:
    if extensions is None:
        return set(DEFAULT_ALLOWED_EXTENSIONS)
    normalized = set()
    for extension in extensions:
        value = str(extension).strip().lower()
        if not value:
            continue
        if value == "*":
            normalized.add("*")
            continue
        normalized.add(value if value.startswith(".") else f".{value}")
    return normalized or set(DEFAULT_ALLOWED_EXTENSIONS)


def _normalize_path(path: str) -> str:
    return os.path.abspath(os.path.expandvars(os.path.expanduser(str(path).strip())))


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _document_content_hash(document: Dict[str, Any]) -> str:
    direct = str(document.get("content_sha256") or "").strip().lower()
    if direct:
        return direct
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    recorded = str(metadata.get("contentSha256") or "").strip().lower()
    if recorded:
        return recorded
    storage_path = str(document.get("storage_path") or "").strip()
    if storage_path and os.path.isfile(storage_path):
        try:
            return _sha256_file(storage_path)
        except OSError:
            return ""
    return ""


def _source_document_target_system(source_document: Dict[str, Any]) -> str:
    return resolve_document_target_system(source_document)


def _stored_document_target_system(document: Dict[str, Any]) -> str:
    return resolve_document_target_system(document)


def _document_type(path: str, mime_type: str) -> str:
    extension = os.path.splitext(path)[1].lower()
    if extension == ".pdf" or mime_type == "application/pdf":
        return "pdf"
    if mime_type.startswith("image/"):
        return "image"
    if extension == ".csv" or mime_type in {"text/csv", "application/csv"}:
        return "csv"
    if mime_type.startswith("text/"):
        return "text"
    return "unknown"
