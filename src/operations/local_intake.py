import hashlib
from itertools import chain
import mimetypes
import os
import stat as stat_module
from uuid import uuid4
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Sequence, Set

from src.document_handling.source_identity import source_document_id
from src.document_processors.document_resource_limits import (
    MAX_SOURCE_DOCUMENT_BYTES,
    MAX_TEXT_DOCUMENT_BYTES,
)
from src.operations.local_ledger import LocalOperationsLedger, SourceScanManifestLimitError
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
LOCAL_FOLDER_INTAKE_LEASE_NAME = "local_folder_intake"


class LocalFolderIntake:
    """Import document metadata from local or synced folders into the ledger."""

    MAX_FILES_PER_SCAN = 100
    MAX_BYTES_PER_SCAN = 256 * 1024 * 1024
    MAX_MANIFEST_ENTRIES = 100_000
    MAX_MANIFEST_PATH_BYTES = 16 * 1024 * 1024
    MAX_DIRECTORIES_PER_SCAN = 20_000

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
        owner_token = uuid4().hex
        lease = self.ledger.acquire_runtime_lease(
            LOCAL_FOLDER_INTAKE_LEASE_NAME,
            owner_token,
            ttl_seconds=21600,
            metadata={"source": self.source, "folderCount": len(roots)},
        )
        if not lease.get("acquired"):
            return {
                "folders": roots,
                "allowedExtensions": sorted(self.allowed_extensions),
                "scanned": 0,
                "registered": 0,
                "duplicates": 0,
                "alreadyRegistered": 0,
                "skipped": [{"reason": "intake_already_running"}],
                "documents": [],
                "status": "already_running",
                "runtimeLease": lease.get("lease"),
            }
        try:
            return self._rescan_with_lease(roots)
        finally:
            self.ledger.release_runtime_lease(
                LOCAL_FOLDER_INTAKE_LEASE_NAME,
                owner_token,
            )

    def _rescan_with_lease(self, roots: Sequence[str]) -> Dict[str, Any]:
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
            cursor = self.ledger.get_source_scan_cursor(source_account_id)
            try:
                paths, next_cursor, scan_incomplete, scan_bytes = self._scan_batch(
                    root,
                    source_account_id,
                    cursor,
                )
            except (SourceScanManifestLimitError, OSError) as exc:
                reason = getattr(exc, "reason", "source_tree_unreadable")
                summary["skipped"].append({
                    "path": root,
                    "reason": reason,
                    "maxManifestEntries": self.MAX_MANIFEST_ENTRIES,
                    "maxManifestPathBytes": self.MAX_MANIFEST_PATH_BYTES,
                    "maxDirectories": self.MAX_DIRECTORIES_PER_SCAN,
                })
                summary.setdefault("scanIncomplete", {})[root] = True
                summary["status"] = "attention_required"
                self.ledger.upsert_source_account({
                    "sourceType": self.source,
                    "sourceIdentifier": root,
                    "label": os.path.basename(root) or root,
                    "status": "attention_required",
                    "lastScanAt": scan_started_at,
                    "metadata": {
                        "path": root,
                        "reason": reason,
                        "maxManifestEntries": self.MAX_MANIFEST_ENTRIES,
                        "maxManifestPathBytes": self.MAX_MANIFEST_PATH_BYTES,
                        "maxDirectories": self.MAX_DIRECTORIES_PER_SCAN,
                    },
                })
                continue
            root_summary = {
                "scanned": 0,
                "registered": 0,
                "duplicates": 0,
                "alreadyRegistered": 0,
            }
            read_budget = _SourceReadBudget(self.MAX_BYTES_PER_SCAN)
            for index, path in enumerate(paths):
                result = self._register_file(
                    root,
                    path,
                    source_account_id,
                    read_budget=read_budget,
                )
                if result.get("skipped"):
                    summary["skipped"].append(result["skipped"])
                    if result["skipped"].get("reason") == "scan_byte_limit_exceeded":
                        scan_incomplete = True
                        next_cursor = paths[index - 1] if index else cursor
                        break
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
            self.ledger.set_source_scan_cursor(source_account_id, next_cursor)
            summary.setdefault("scanIncomplete", {})[root] = scan_incomplete
            summary.setdefault("scanBytes", {})[root] = read_budget.bytes_read
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
                    "scanIncomplete": scan_incomplete,
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
        """Return whether a scheduled rescan could change durable state."""
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
            if account and str(account.get("status") or "").lower() == "attention_required":
                continue

            # Empty watch folders need no recurring autonomy workflow or hashes.
            document_paths = iter(self._iter_document_paths(root))
            first_path = next(document_paths, None)
            if first_path is None:
                continue
            if not account or str(account.get("status") or "").lower() != "ready":
                return True

            for path in chain((first_path,), document_paths):
                try:
                    stat = os.stat(path, follow_symlinks=False)
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
                    if not existing_hash or existing_hash != _sha256_file(path):
                        return True
                except (OSError, DocumentSizeLimitError):
                    return True
        return False

    def _scan_batch(self, root: str, source_account_id: int, cursor: Optional[str]):
        limit = max(1, int(self.MAX_FILES_PER_SCAN))
        byte_limit = max(1, int(self.MAX_BYTES_PER_SCAN))
        if not cursor:
            self.ledger.rebuild_source_scan_manifest(
                source_account_id,
                self._iter_manifest_entries(root),
                max_entries=self.MAX_MANIFEST_ENTRIES,
                max_path_bytes=self.MAX_MANIFEST_PATH_BYTES,
            )
        candidates = self.ledger.get_source_scan_batch(
            source_account_id,
            cursor,
            limit + 1,
        )
        paths = []
        scan_bytes = 0
        incomplete = False
        for candidate in candidates:
            path = str(candidate["path"])
            try:
                size = os.stat(path, follow_symlinks=False).st_size
            except OSError:
                size = 0
            is_text = os.path.splitext(path)[1].lower() in {".csv", ".txt"}
            max_file_bytes = MAX_TEXT_DOCUMENT_BYTES if is_text else MAX_SOURCE_DOCUMENT_BYTES
            admitted_bytes = size if size <= max_file_bytes else 0
            if len(paths) >= limit or (paths and scan_bytes + admitted_bytes > byte_limit):
                incomplete = True
                break
            paths.append(path)
            scan_bytes += admitted_bytes
            if scan_bytes >= byte_limit and len(paths) < len(candidates):
                incomplete = True
                break
        return paths, paths[-1] if incomplete and paths else None, incomplete, scan_bytes

    def _iter_manifest_entries(self, root: str):
        yield from self._iter_document_paths(root)

    def _iter_document_paths(self, root: str):
        visited_directories = 0

        def fail_walk(error):
            raise error

        for current_root, dir_names, file_names in os.walk(
            root,
            followlinks=False,
            onerror=fail_walk,
        ):
            visited_directories += 1
            if visited_directories > self.MAX_DIRECTORIES_PER_SCAN:
                raise SourceScanManifestLimitError(
                    "directory_limit_exceeded",
                    max_entries=self.MAX_MANIFEST_ENTRIES,
                    max_path_bytes=self.MAX_MANIFEST_PATH_BYTES,
                )
            dir_names.sort()
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
        if not str(root or "").strip():
            return {
                "skipped": {
                    "path": _normalize_path(path_value),
                    "reason": "source_root_missing",
                }
            }
        source_root = os.path.realpath(_normalize_path(root))
        path = os.path.realpath(_normalize_path(path_value))
        if not os.path.isdir(source_root):
            return {"skipped": {"path": path, "reason": "source_root_missing"}}
        try:
            common_root = os.path.commonpath((source_root, path))
            if os.path.normcase(common_root) != os.path.normcase(source_root):
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
        read_budget: Optional["_SourceReadBudget"] = None,
    ) -> Dict[str, Any]:
        try:
            stat = os.stat(path, follow_symlinks=False)
            if not stat_module.S_ISREG(stat.st_mode):
                return {"skipped": {"path": path, "reason": "not_regular_file"}}
            is_text = os.path.splitext(path)[1].lower() in {".csv", ".txt"}
            max_bytes = MAX_TEXT_DOCUMENT_BYTES if is_text else MAX_SOURCE_DOCUMENT_BYTES
            if stat.st_size > max_bytes:
                return {
                    "skipped": {
                        "path": path,
                        "reason": "text_document_exceeds_limit" if is_text else "document_exceeds_limit",
                        "maxBytes": max_bytes,
                    }
                }
            if read_budget is not None and stat.st_size > read_budget.remaining:
                return {
                    "skipped": {
                        "path": path,
                        "reason": "scan_byte_limit_exceeded",
                        "maxBytes": self.MAX_BYTES_PER_SCAN,
                    }
                }
            try:
                content_hash = _sha256_file(path, max_bytes=max_bytes, read_budget=read_budget)
            except DocumentSizeLimitError as exc:
                if str(exc) == "scan_byte_limit_exceeded":
                    return {
                        "skipped": {
                            "path": path,
                            "reason": "scan_byte_limit_exceeded",
                            "maxBytes": self.MAX_BYTES_PER_SCAN,
                        }
                    }
                return {
                    "skipped": {
                        "path": path,
                        "reason": "text_document_exceeds_limit" if is_text else "document_exceeds_limit",
                        "maxBytes": max_bytes,
                    }
                }
            current_stat = os.stat(path, follow_symlinks=False)
            if not _same_file_identity(stat, current_stat):
                return {"skipped": {"path": path, "reason": "source_changed_during_intake"}}
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
            try:
                existing_hash = _document_content_hash(existing, read_budget=read_budget)
            except DocumentSizeLimitError as exc:
                if str(exc) == "scan_byte_limit_exceeded":
                    return {
                        "skipped": {
                            "path": path,
                            "reason": "scan_byte_limit_exceeded",
                            "maxBytes": self.MAX_BYTES_PER_SCAN,
                        }
                    }
                existing_hash = ""
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
            "bytesRead": read_budget.bytes_read if read_budget is not None else 0,
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


class DocumentSizeLimitError(ValueError):
    pass


class _SourceReadBudget:
    def __init__(self, limit_bytes: int):
        self.limit_bytes = max(0, int(limit_bytes))
        self.bytes_read = 0

    @property
    def remaining(self) -> int:
        return self.limit_bytes - self.bytes_read

    def record(self, size: int) -> None:
        self.bytes_read += size


def _sha256_file(
    path: str,
    *,
    max_bytes: Optional[int] = None,
    read_budget: Optional[_SourceReadBudget] = None,
) -> str:
    if max_bytes is None:
        is_text = os.path.splitext(path)[1].lower() in {".csv", ".txt"}
        max_bytes = MAX_TEXT_DOCUMENT_BYTES if is_text else MAX_SOURCE_DOCUMENT_BYTES
    max_bytes = max(1, int(max_bytes))
    before = os.stat(path, follow_symlinks=False)
    if not stat_module.S_ISREG(before.st_mode):
        raise OSError("source_not_regular_file")
    if read_budget is not None and before.st_size > read_budget.remaining:
        raise DocumentSizeLimitError("scan_byte_limit_exceeded")
    effective_limit = min(max_bytes, read_budget.remaining) if read_budget is not None else max_bytes
    if before.st_size > effective_limit:
        raise DocumentSizeLimitError("document_exceeds_limit")
    digest = hashlib.sha256()
    bytes_read = 0
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat_module.S_ISREG(opened.st_mode) or not _same_file_identity(before, opened):
            raise OSError("source_changed_before_hash")
        if opened.st_size > effective_limit:
            reason = "scan_byte_limit_exceeded" if read_budget is not None else "document_exceeds_limit"
            raise DocumentSizeLimitError(reason)
        while bytes_read < opened.st_size:
            read_size = min(1024 * 1024, opened.st_size - bytes_read)
            chunk = handle.read(read_size)
            if not chunk:
                break
            bytes_read += len(chunk)
            if read_budget is not None:
                read_budget.record(len(chunk))
            digest.update(chunk)
        if bytes_read != opened.st_size:
            raise OSError("source_short_read")
        after = os.fstat(handle.fileno())
    current = os.stat(path, follow_symlinks=False)
    if not _same_file_identity(opened, after) or not _same_file_identity(after, current):
        raise OSError("source_changed_during_hash")
    return digest.hexdigest()


def _same_file_identity(left, right) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and getattr(left, "st_mtime_ns", int(left.st_mtime * 1_000_000_000))
        == getattr(right, "st_mtime_ns", int(right.st_mtime * 1_000_000_000))
    )


def _document_content_hash(
    document: Dict[str, Any],
    *,
    read_budget: Optional[_SourceReadBudget] = None,
) -> str:
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
            return _sha256_file(storage_path, read_budget=read_budget)
        except DocumentSizeLimitError as exc:
            if str(exc) == "scan_byte_limit_exceeded":
                raise
            return ""
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
