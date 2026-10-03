from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Dict, Optional
from urllib.parse import urlsplit

from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_wave_receipt_executor import (
    LocalWaveReceiptExecutorService,
    wave_business_uuid,
)
from src.operations.local_wave_setup import LocalWaveSetupService
from src.security.google_oauth_store import GoogleOAuthTokenStore

if TYPE_CHECKING:
    from src.document_fetchers.drive_archiver import DriveArchiveClient


EVIDENCE_ACTION = "drive_wave.attachment_verified"
ARCHIVE_ACTION = "drive_wave.source_archived"
REQUIRED_FIELD_MATCHES = ("vendor", "date", "amount", "currency", "category", "description")
TERMINAL_EXPORT_STATUSES = {"executed", "submitted"}
OPEN_REVIEW_STATUSES = {"pending", "open", "needs_review", "in_progress", "in_review"}
WORK_ORDER_VERSION = "fab-source-wave-work-order-v3"
FINISHED_WAVE_TRANSACTION_STATUSES = {"completed", "posted", "reviewed"}
ARCHIVABLE_BOOKKEEPING_RECORD_STATUSES = {
    "approved", "export_draft_prepared", "ready_to_route", "reconciled",
    "reviewed", "routed", "validated",
}
WAVE_RECEIPT_MAX_BYTES = 6 * 1024 * 1024
WAVE_RECEIPT_ALLOWED_EXTENSIONS = {
    ".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".pdf", ".png", ".tif", ".tiff",
}
WAVE_RECEIPT_ALLOWED_MIME_TYPES = {
    "application/pdf",
    "image/bmp",
    "image/gif",
    "image/heic",
    "image/heif",
    "image/jpeg",
    "image/png",
    "image/tiff",
}


class DriveWaveDeliveryService:
    """Close source-to-Wave loops without treating transaction presence as file proof."""

    def __init__(
        self,
        ledger: LocalOperationsLedger,
        config: Optional[Dict[str, Any]] = None,
        drive_archiver: Optional["DriveArchiveClient"] = None,
    ):
        self.ledger = ledger
        self.config = config or {}
        self.drive_archiver = drive_archiver

    def status(
        self,
        *,
        wave_setup: Optional[Dict[str, Any]] = None,
        receipt_executor: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        source_folder_id = self._source_folder_id()
        archive_folder_id = self._archive_folder_id()
        business_id = self._business_id()
        enabled = self._archive_enabled()
        configured_token_path = str(
            _first(self.config, "google_drive_token_file", "drive_token_path")
            or "tokens/drive_token.json"
        )
        credentials_path = str(
            _first(self.config, "google_drive_credentials_file", "drive_credentials_path")
            or "credentials/drive_credentials.json"
        )
        token_status = GoogleOAuthTokenStore(configured_token_path, []).status()
        token_present = token_status["tokenPresent"]
        reauthorization_required = token_status["reauthorizationRequired"]
        credentials_present = os.path.isfile(os.path.abspath(os.path.expanduser(credentials_path)))
        folders_distinct = bool(source_folder_id) and bool(archive_folder_id) and source_folder_id != archive_folder_id
        archive_configured = enabled and folders_distinct and bool(business_id)
        if wave_setup is None:
            wave_setup = LocalWaveSetupService(self.config).status(
                self.ledger,
                "waveapps_business",
            )
        wave_activation = (
            wave_setup.get("activation")
            if isinstance(wave_setup.get("activation"), dict)
            else {}
        )
        if receipt_executor is None:
            receipt_executor = LocalWaveReceiptExecutorService(
                self.ledger,
                self.config,
            ).status()
        if not archive_configured:
            status = "needs_configuration"
        elif not token_present or reauthorization_required:
            status = "needs_authorization"
        elif wave_setup.get("ready") is not True:
            status = "needs_wave_setup"
        elif receipt_executor.get("ready") is not True:
            status = "needs_receipt_executor"
        else:
            status = "ready"
        gmail_scanner_ready = self._gmail_scanner_configured()
        return {
            "status": status,
            "archiveEnabled": enabled,
            "sourceFolderConfigured": bool(source_folder_id),
            "archiveFolderConfigured": bool(archive_folder_id),
            "foldersDistinct": folders_distinct,
            "waveBusinessSelected": bool(business_id),
            "waveBusinessConfigured": wave_setup.get("ready") is True,
            "waveAccessTokenConfigured": wave_setup.get("accessTokenConfigured") is True,
            "waveSetupStatus": wave_setup.get("status"),
            "waveSetupCurrentStep": wave_activation.get("currentStep"),
            "waveSetupNextAction": wave_activation.get("nextAction"),
            "waveReceiptExecutorReady": receipt_executor.get("ready") is True,
            "waveReceiptExecutorStatus": receipt_executor.get("status"),
            "waveReceiptExecutorNextAction": receipt_executor.get("nextAction"),
            "waveReceiptExecutorLastSeenAt": receipt_executor.get("lastSeenAt"),
            "waveReceiptExecutorMissingCapabilities": receipt_executor.get("missingCapabilities") or [],
            "driveTokenPresent": token_present,
            "driveTokenPath": token_status["tokenPath"],
            "driveLegacyTokenPresent": token_status["legacyTokenPresent"],
            "driveReauthorizationRequired": reauthorization_required,
            "driveReauthorizationReason": token_status["reauthorizationReason"],
            "driveCredentialsPresent": credentials_present,
            "relayIntakeReady": bool(source_folder_id),
            "relayIntakePath": "/api/connectors/google-drive/relay",
            "gmailScannerReady": gmail_scanner_ready,
            "gmailRetentionPolicy": "email_unchanged_local_evidence_retained",
            "driveScopeVerification": "checked_on_archive",
            "verificationPolicy": "unique_reviewed_wave_transaction_and_attachment_hash_round_trip_v3",
            "deletionPolicy": "never_delete_move_only",
            "externalSubmission": "policy_gated",
        }

    def list_candidates(self, limit: int = 100) -> Dict[str, Any]:
        with self.ledger.read_snapshot():
            return self._list_candidates(limit)

    def _list_candidates(self, limit: int) -> Dict[str, Any]:
        documents = self._candidate_documents(limit)
        contexts = self._delivery_contexts(documents)
        candidates = []
        for document in documents:
            context = contexts[int(document["id"])]
            plan = self._plan_archive_for_document(
                context["document"],
                context["record"],
                context["exports"],
                context["evidence_event"],
                configured_source_verified=context["configured_source_verified"],
            )
            candidates.append({
                "documentId": document["id"],
                "sourceDocumentId": document.get("source_document_id"),
                "sourceProvider": _source_provider(document),
                "filename": document.get("original_filename"),
                "storagePath": document.get("storage_path"),
                "sourceSha256": _source_sha256(document),
                "processingStatus": document.get("processing_status"),
                "archivePlan": plan,
            })
        return {
            "status": "ready",
            "count": len(candidates),
            "candidates": candidates,
            "externalSubmission": "not_executed",
        }

    def list_work_orders(
        self,
        limit: int = 100,
        *,
        compact: bool = False,
        item_limit: Optional[int] = None,
        connector_status: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self.ledger.read_snapshot():
            return self._list_work_orders(
                limit,
                compact=compact,
                item_limit=item_limit,
                connector_status=connector_status,
            )

    def _list_work_orders(
        self,
        limit: int,
        *,
        compact: bool,
        item_limit: Optional[int],
        connector_status: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        documents = self._candidate_documents(limit)
        contexts = self._delivery_contexts(documents)
        work_orders = [
            self._work_order(
                int(document["id"]),
                context=contexts[int(document["id"])],
                compact=compact,
            )
            for document in documents
        ]
        summary = {
            "sourceUnavailable": _count_stage(work_orders, "source_file_unavailable"),
            "sourceIncompatible": _count_stage(work_orders, "source_incompatible"),
            "needsProcessing": _count_stage(work_orders, "needs_processing"),
            "blockedByReview": _count_stage(work_orders, "blocked_by_review"),
            "needsWaveTransaction": _count_stage(work_orders, "locate_or_create_transaction"),
            "needsAttachmentVerification": _count_stage(work_orders, "upload_and_verify_attachment"),
            "needsFreshReadback": _count_stage(work_orders, "refresh_wave_readback"),
            "readyToArchive": _count_stage(work_orders, "ready_to_archive"),
            "completed": _count_stage(work_orders, "completed"),
        }
        connector_status = connector_status or self.status()
        result = {
            "status": connector_status["status"],
            "receiptExecutor": {
                "ready": connector_status.get("waveReceiptExecutorReady") is True,
                "status": connector_status.get("waveReceiptExecutorStatus"),
                "nextAction": connector_status.get("waveReceiptExecutorNextAction"),
                "lastSeenAt": connector_status.get("waveReceiptExecutorLastSeenAt"),
                "missingCapabilities": connector_status.get("waveReceiptExecutorMissingCapabilities") or [],
            },
            "workOrderVersion": WORK_ORDER_VERSION,
            "view": "summary" if compact else "complete",
            "count": len(work_orders),
            "summary": summary,
            "workOrders": work_orders[:item_limit] if item_limit is not None else work_orders,
            "externalSubmission": "not_executed",
        }
        if not compact:
            result["evidencePolicy"] = {
                "maxAgeSeconds": self._evidence_max_age_seconds(),
                "requiredFieldMatches": list(REQUIRED_FIELD_MATCHES),
                "sourceHashRequired": True,
                "attachmentReadbackRequired": True,
                "attachmentRoundTripHashRequired": True,
                "attachmentOpenRequired": True,
                "attachmentTransactionBindingRequired": True,
                "uniqueWaveTransactionRequired": True,
                "finishedWaveTransactionRequired": True,
                "freshWaveObservationRequired": True,
                "transactionReviewRequired": True,
                "serverComputedFieldMatches": True,
                "expectedFieldsDigestRequired": True,
                "postMoveHashVerificationRequired": True,
                "archiveMode": "move_only_never_delete",
            }
        return result

    def work_order(self, document_id: int) -> Dict[str, Any]:
        document = self.ledger.get_document(int(document_id))
        if not document:
            return {
                "success": False,
                "status": "not_found",
                "documentId": int(document_id),
                "externalSubmission": "not_executed",
            }
        if not self._is_configured_source(document):
            return {
                "success": False,
                "status": "outside_configured_source",
                "documentId": int(document_id),
                "externalSubmission": "not_executed",
            }
        context = self._context_from_loaded_document(document)
        context["configured_source_verified"] = True
        return self._work_order(
            int(document_id),
            context=context,
        )

    def _candidate_documents(self, limit: int) -> list[Dict[str, Any]]:
        return [
            document
            for document in self.ledger.list_documents(limit=500)
            if self._is_configured_source(document)
        ][:_bounded_candidate_limit(limit)]

    def _delivery_contexts(
        self,
        documents: list[Dict[str, Any]],
    ) -> Dict[int, Dict[str, Any]]:
        loaded = self.ledger.get_document_delivery_context(
            [int(document["id"]) for document in documents],
            EVIDENCE_ACTION,
        )
        contexts = {}
        for document in documents:
            document_id = int(document["id"])
            related = loaded.get(document_id) or {}
            hydrated_document = dict(document)
            hydrated_document["review_items"] = list(related.get("review_items") or [])
            contexts[document_id] = {
                "document": hydrated_document,
                "record": related.get("bookkeeping_record"),
                "exports": list(related.get("export_attempts") or []),
                "evidence_event": related.get("evidence_event"),
                "configured_source_verified": True,
            }
        return contexts

    @staticmethod
    def _context_from_loaded_document(document: Dict[str, Any]) -> Dict[str, Any]:
        evidence_event = max(
            [
                item
                for item in document.get("audit_events") or []
                if str(item.get("action") or "") == EVIDENCE_ACTION
            ],
            key=lambda item: (
                str(item.get("created_at") or ""),
                int(item.get("id") or 0),
            ),
            default=None,
        )
        return {
            "document": document,
            "record": document.get("bookkeeping_record"),
            "exports": list(document.get("export_attempts") or [])[:10],
            "evidence_event": evidence_event,
        }

    def _work_order(
        self,
        document_id: int,
        *,
        context: Optional[Dict[str, Any]] = None,
        compact: bool = False,
    ) -> Dict[str, Any]:
        if context is None:
            document = self.ledger.get_document(int(document_id)) or {}
            context = self._context_from_loaded_document(document)
        document = context["document"]
        record = context.get("record")
        exports = list(context.get("exports") or [])[:10]
        latest_export = exports[0] if exports else None
        terminal_export = next(
            (
                item
                for item in exports
                if str(item.get("status") or "").lower() in TERMINAL_EXPORT_STATUSES
                and str(item.get("target_system") or "") == "waveapps_business"
            ),
            None,
        )
        evidence_event = context.get("evidence_event")
        evidence = (evidence_event or {}).get("details") or {}
        plan = self._plan_archive_for_document(
            document,
            record,
            exports,
            evidence_event,
            configured_source_verified=(
                context.get("configured_source_verified") is True
            ),
        )
        expected_fields = _expected_wave_fields(document, record)
        required_field_matches = _required_wave_fields(document, record)
        missing_expected_fields = [
            field for field in required_field_matches
            if _canonical_wave_field(field, expected_fields.get(field)) in (None, "")
        ]
        expected_fields = {
            field: value if _canonical_wave_field(field, value) not in (None, "") else None
            for field, value in expected_fields.items()
        }
        line_items, invalid_line_item_fields = _delivery_line_items(record)

        open_reviews = [
            item
            for item in document.get("review_items") or []
            if str(item.get("status") or "pending").lower() in OPEN_REVIEW_STATUSES
        ]
        unrelated_reviews = [
            item for item in open_reviews if item.get("reason") != "drive_wave_archive_blocked"
        ]
        upload_reasons = _wave_upload_reasons(document)
        stage = _work_order_stage(
            document=document,
            record=record,
            terminal_export=terminal_export,
            evidence=evidence,
            evidence_event=evidence_event,
            plan=plan,
            unrelated_reviews=unrelated_reviews,
            evidence_max_age_seconds=self._evidence_max_age_seconds(),
            missing_expected_fields=missing_expected_fields,
            invalid_line_item_fields=invalid_line_item_fields,
            upload_reasons=upload_reasons,
        )
        source_sha256 = _source_sha256(document)
        source_provider = _source_provider(document)
        external_transaction_id = str(
            evidence.get("externalTransactionId")
            or (terminal_export or {}).get("external_id")
            or ""
        )
        work_order_id = f"source-wave-{document_id}-{source_sha256[:12] or 'unhashed'}"
        if compact:
            return {
                "success": True,
                "status": "ready",
                "workOrderVersion": WORK_ORDER_VERSION,
                "workOrderId": work_order_id,
                "documentId": int(document_id),
                "stage": stage,
                "actionRequired": _stage_action(stage),
                "source": {
                    "provider": source_provider,
                    "filename": document.get("original_filename"),
                    "mimeType": document.get("mime_type"),
                    "sha256": source_sha256,
                },
                "wave": {
                    "targetSystem": "waveapps_business",
                    "externalTransactionId": external_transaction_id or None,
                },
                "reviews": {
                    "open": len(open_reviews),
                    "blocking": len(unrelated_reviews),
                    "reasons": sorted({
                        str(item.get("reason") or "unknown")
                        for item in unrelated_reviews
                    }),
                },
                "archivePlan": {
                    key: plan.get(key)
                    for key in (
                        "canArchive",
                        "evidenceVerified",
                        "externalSubmission",
                        "reasons",
                        "retentionStatus",
                        "status",
                    )
                },
                "externalSubmission": "not_executed",
            }

        provider_metadata = _provider_metadata(document)
        evidence_template = {
            "externalTransactionId": external_transaction_id,
            "businessId": self._business_id(),
            "sourceSha256": source_sha256,
            "uploadSourceSha256": source_sha256,
            "attachmentSha256": "",
            "attachmentSizeBytes": None,
            "attachmentObjectId": "",
            "attachmentMimeType": document.get("mime_type"),
            "attachmentFilename": document.get("original_filename"),
            "attachmentPresent": False,
            "attachmentOpened": False,
            "attachmentDownloaded": False,
            "attachmentTransactionId": "",
            "transactionExists": False,
            "transactionStatus": "",
            "transactionMatchCount": None,
            "matchingTransactionIds": [],
            "transactionPageUrl": "",
            "transactionReviewed": False,
            "waveObservedAt": "",
            "observedFields": {field: None for field in required_field_matches},
            "fieldMatches": {field: False for field in required_field_matches},
            "expectedFieldsDigest": _expected_fields_digest(
                expected_fields,
                required_field_matches,
            ),
            "verifier": "hai-wave-executor",
        }
        return {
            "success": True,
            "status": "ready",
            "workOrderVersion": WORK_ORDER_VERSION,
            "workOrderId": work_order_id,
            "documentId": int(document_id),
            "stage": stage,
            "actionRequired": _stage_action(stage),
            "source": {
                "provider": source_provider,
                "fileId": document.get("source_document_id"),
                "folderId": self._source_folder_id() if source_provider == "google_drive" else None,
                "messageId": provider_metadata.get("message_id") if source_provider == "gmail" else None,
                "attachmentId": provider_metadata.get("attachment_id") if source_provider == "gmail" else None,
                "scannerProfile": provider_metadata.get("scanner_profile") if source_provider == "gmail" else None,
                "deliveryPath": provider_metadata.get("delivery_path") if source_provider == "gmail" else None,
                "filename": document.get("original_filename"),
                "mimeType": document.get("mime_type"),
                "localPath": document.get("storage_path"),
                "localAvailable": bool(document.get("storage_path") and os.path.isfile(str(document.get("storage_path")))),
                "sha256": source_sha256,
                "sizeBytes": _source_size(document),
                "waveUpload": {
                    "compatible": not upload_reasons,
                    "reasons": upload_reasons,
                    "maxBytes": WAVE_RECEIPT_MAX_BYTES,
                    "allowedExtensions": sorted(WAVE_RECEIPT_ALLOWED_EXTENSIONS),
                },
                "retention": (
                    {
                        "policy": "email_unchanged_local_evidence_retained",
                        "sourceEmailMutation": "never",
                        "localEvidenceDeletion": "never",
                    }
                    if source_provider == "gmail"
                    else {
                        "policy": "move_only_after_verified_readback",
                        "sourceDeletion": "never",
                    }
                ),
            },
            "wave": {
                "businessId": self._business_id(),
                "targetSystem": "waveapps_business",
                "externalTransactionId": external_transaction_id or None,
                "expectedFields": expected_fields,
                "missingExpectedFields": missing_expected_fields,
                "lineItems": line_items,
                "invalidLineItemFields": invalid_line_item_fields,
                "latestExportAttempt": _export_reference(latest_export),
            },
            "reviews": {
                "open": len(open_reviews),
                "blocking": len(unrelated_reviews),
                "reasons": sorted({str(item.get("reason") or "unknown") for item in unrelated_reviews}),
            },
            "evidence": {
                "present": bool(evidence),
                "verifiedAt": (evidence_event or {}).get("created_at"),
                "digest": evidence.get("evidenceDigest"),
                "submission": {
                    "method": "POST",
                    "path": f"/api/drive-wave/documents/{document_id}/attachment-evidence",
                    "haiCommandId": "record_wave_attachment_verification",
                },
                "binaryReadbackSubmission": {
                    "method": "POST",
                    "path": f"/api/drive-wave/documents/{document_id}/attachment-readback",
                    "contentType": "multipart/form-data",
                    "fileField": "attachment",
                    "metadataField": "evidence",
                    "requiredForArchive": source_provider == "google_drive",
                    "requiredForCompletion": True,
                },
                "requiredFieldMatches": required_field_matches,
                "template": evidence_template,
            },
            "browserExecution": _wave_browser_contract(
                self._business_id(),
                int(document_id),
            ),
            "archivePlan": plan,
            "externalSubmission": "not_executed",
        }

    def record_attachment_readback(
        self,
        document_id: int,
        content: bytes,
        *,
        filename: Any,
        mime_type: Any,
        evidence: Dict[str, Any],
        actor: str = "hai-wave-browser",
    ) -> Dict[str, Any]:
        document = self.ledger.get_document(int(document_id))
        if not document:
            return {"success": False, "status": "not_found", "reasons": ["document_not_found"]}
        if not self._is_configured_source(document):
            return {
                "success": False,
                "status": "outside_configured_source",
                "reasons": ["document_outside_configured_source"],
                "externalSubmission": "not_executed",
            }
        if not isinstance(content, bytes) or not content:
            return self.record_attachment_evidence(
                document_id,
                evidence,
                actor=actor,
                readback_bytes_verified=False,
            )
        if len(content) > WAVE_RECEIPT_MAX_BYTES:
            return {
                "success": False,
                "status": "blocked",
                "reasons": ["wave_attachment_readback_exceeds_limit"],
                "maxBytes": WAVE_RECEIPT_MAX_BYTES,
                "externalSubmission": "not_executed",
            }
        server_evidence = dict(evidence or {})
        server_evidence.update({
            "attachmentSha256": hashlib.sha256(content).hexdigest(),
            "attachmentSizeBytes": len(content),
            "attachmentFilename": os.path.basename(str(filename or "")),
            "attachmentMimeType": str(mime_type or "application/octet-stream").lower().strip(),
            "attachmentPresent": True,
            "attachmentOpened": True,
            "attachmentDownloaded": True,
        })
        return self.record_attachment_evidence(
            document_id,
            server_evidence,
            actor=actor,
            readback_bytes_verified=True,
        )

    def record_attachment_evidence(
        self,
        document_id: int,
        evidence: Dict[str, Any],
        actor: str = "hai",
        readback_bytes_verified: bool = False,
    ) -> Dict[str, Any]:
        document = self.ledger.get_document(int(document_id))
        if not document:
            return {"success": False, "status": "not_found", "reasons": ["document_not_found"]}
        if not self._is_configured_source(document):
            return {
                "success": False,
                "status": "outside_configured_source",
                "reasons": ["document_outside_configured_source"],
                "externalSubmission": "not_executed",
            }
        normalized = _normalize_evidence(evidence)
        normalized["attachmentReadbackVerified"] = bool(readback_bytes_verified)
        normalized["readbackOrigin"] = (
            "wave_browser_download" if readback_bytes_verified else "metadata_attestation"
        )
        record = self.ledger.get_bookkeeping_record_by_document(int(document_id))
        required_fields = _required_wave_fields(document, record)
        expected_fields = _expected_wave_fields(document, record)
        observed_fields = _normalize_observed_fields(normalized.get("observedFields"))
        normalized["observedFields"] = observed_fields
        normalized["fieldMatches"] = {
            field: _wave_field_matches(field, expected_fields.get(field), observed_fields.get(field))
            for field in required_fields
        }
        normalized["expectedFieldsDigest"] = _expected_fields_digest(
            expected_fields,
            required_fields,
        )
        reasons = self._evidence_reasons(document, normalized)
        evidence_digest = _digest(normalized)
        if reasons:
            audit_event_id = self.ledger.record_audit_event({
                "action": "drive_wave.attachment_verification_rejected",
                "entityType": "bookkeeping_document",
                "entityId": str(document_id),
                "details": {
                    "actor": actor,
                    "reasons": reasons,
                    "evidenceDigest": evidence_digest,
                    "externalTransactionId": normalized.get("externalTransactionId"),
                    "externalSubmission": "not_executed",
                },
            })
            return {
                "success": False,
                "status": "blocked",
                "reasons": reasons,
                "auditEventId": audit_event_id,
                "externalSubmission": "not_executed",
            }

        verification_method = "hash_round_trip"
        normalized.update({
            "actor": str(actor or "hai")[:200],
            "evidenceDigest": evidence_digest,
            "verificationMethod": verification_method,
            "verifiedAt": _now(),
        })
        audit_event_id = self.ledger.record_audit_event({
            "action": EVIDENCE_ACTION,
            "entityType": "bookkeeping_document",
            "entityId": str(document_id),
            "details": normalized,
        })
        metadata = dict(document.get("metadata") or {})
        lifecycle = dict(metadata.get("driveWaveLifecycle") or {})
        lifecycle.update({
            "status": "attachment_verified",
            "verifiedAt": normalized["verifiedAt"],
            "verificationMethod": verification_method,
            "evidenceDigest": evidence_digest,
            "externalTransactionId": normalized["externalTransactionId"],
            "attachmentObjectId": normalized["attachmentObjectId"],
            "attachmentTransactionId": normalized["attachmentTransactionId"],
            "transactionStatus": normalized["transactionStatus"],
            "waveObservedAt": normalized["waveObservedAt"],
        })
        metadata["driveWaveLifecycle"] = lifecycle
        metadata["waveDeliveryLifecycle"] = lifecycle
        self.ledger.update_document(int(document_id), {"metadata": metadata})
        self._resolve_archive_review_items(int(document_id), actor, evidence_digest)
        return {
            "success": True,
            "status": "verified",
            "verificationMethod": verification_method,
            "auditEventId": audit_event_id,
            "archivePlan": self.plan_archive(int(document_id)),
            "externalSubmission": "verified_readback",
        }

    def plan_archive(self, document_id: int) -> Dict[str, Any]:
        document = self.ledger.get_document(int(document_id))
        if not document:
            return {
                "status": "blocked",
                "canArchive": False,
                "reasons": ["document_not_found"],
                "externalSubmission": "not_executed",
            }
        context = self._context_from_loaded_document(document)
        return self._plan_archive_for_document(
            context["document"],
            context["record"],
            context["exports"],
            context["evidence_event"],
        )

    def _plan_archive_for_document(
        self,
        document: Dict[str, Any],
        record: Optional[Dict[str, Any]],
        exports: list[Dict[str, Any]],
        evidence_event: Optional[Dict[str, Any]],
        *,
        configured_source_verified: bool = False,
    ) -> Dict[str, Any]:
        if _source_provider(document) == "gmail":
            return self._plan_gmail_retention(
                document,
                record,
                exports,
                evidence_event,
                configured_source_verified=configured_source_verified,
            )
        metadata = document.get("metadata") or {}
        lifecycle = metadata.get("driveWaveLifecycle") if isinstance(metadata.get("driveWaveLifecycle"), dict) else {}
        if lifecycle.get("status") == "archived":
            return {
                "status": "already_archived",
                "canArchive": False,
                "reasons": [],
                "archiveFolderId": lifecycle.get("archiveFolderId"),
                "externalTransactionId": lifecycle.get("externalTransactionId"),
                "externalSubmission": "already_executed",
            }

        reasons = self._document_reasons(document, record)
        evidence_event, evidence, evidence_reasons = self._wave_evidence_plan(
            document,
            exports,
            evidence_event,
        )
        reasons.extend(evidence_reasons)

        reasons = sorted(set(reasons))
        return {
            "status": "ready" if not reasons else "blocked",
            "canArchive": not reasons,
            "reasons": reasons,
            "sourceFolderId": self._source_folder_id(),
            "archiveFolderId": self._archive_folder_id(),
            "externalTransactionId": evidence.get("externalTransactionId"),
            "attachmentObjectId": evidence.get("attachmentObjectId"),
            "evidenceDigest": evidence.get("evidenceDigest"),
            "verifiedAt": (evidence_event or {}).get("created_at"),
            "verificationMethod": evidence.get("verificationMethod"),
            "moveOnly": True,
            "deleteSource": False,
            "externalSubmission": "not_executed",
        }

    def archive_document(self, document_id: int, actor: str = "local_worker") -> Dict[str, Any]:
        document_id = int(document_id)
        plan = self.plan_archive(document_id)
        if plan.get("status") == "not_applicable":
            return {"success": False, **plan, "externalSubmission": "not_executed"}
        if plan.get("status") == "already_archived":
            return {"success": True, **plan, "externalSubmission": "already_executed"}
        if not plan.get("canArchive"):
            self._ensure_review_item(document_id, plan.get("reasons") or [])
            return {"success": False, **plan, "externalSubmission": "not_executed"}

        lease_name = f"drive-wave-archive:{document_id}"
        owner_token = uuid.uuid4().hex
        lease = self.ledger.acquire_runtime_lease(
            lease_name,
            owner_token,
            ttl_seconds=300,
            metadata={"documentId": document_id, "actor": str(actor or "local_worker")[:200]},
        )
        if not lease.get("acquired"):
            return {
                "success": False,
                "status": "blocked",
                "documentId": document_id,
                "reasons": ["archive_operation_already_in_progress"],
                "externalSubmission": "not_executed",
            }
        try:
            return self._archive_document_with_lease(document_id, actor)
        finally:
            self.ledger.release_runtime_lease(lease_name, owner_token)

    def _archive_document_with_lease(self, document_id: int, actor: str) -> Dict[str, Any]:
        plan = self.plan_archive(document_id)
        if plan.get("status") == "not_applicable":
            return {"success": False, **plan, "externalSubmission": "not_executed"}
        if plan.get("status") == "already_archived":
            return {"success": True, **plan, "externalSubmission": "already_executed"}
        if not plan.get("canArchive"):
            self._ensure_review_item(document_id, plan.get("reasons") or [])
            return {"success": False, **plan, "externalSubmission": "not_executed"}

        document = self.ledger.get_document(document_id) or {}
        provider_id = str(document.get("source_document_id") or "")
        source_sha256 = _source_sha256(document)
        if self.drive_archiver is None:
            from src.document_fetchers.drive_archiver import DriveArchiveClient

            self.drive_archiver = DriveArchiveClient(self.config)
        archiver = self.drive_archiver
        move_result = None
        try:
            current = archiver.inspect_file(provider_id)
            current_sha256 = archiver.download_sha256(provider_id)
            if current_sha256 != source_sha256:
                raise RuntimeError("Drive source content changed after FAB intake.")
            self._assert_provider_identity(document, current)

            final_plan = self.plan_archive(document_id)
            if not final_plan.get("canArchive"):
                reasons = final_plan.get("reasons") or ["archive_policy_changed_before_move"]
                self._ensure_review_item(document_id, reasons)
                return {"success": False, **final_plan, "externalSubmission": "not_executed"}
            plan = final_plan
            move_result = archiver.move_file(
                provider_id,
                str(plan["sourceFolderId"]),
                str(plan["archiveFolderId"]),
            )
            archived_file = archiver.inspect_file(provider_id)
            archived_sha256 = archiver.download_sha256(provider_id)
            self._assert_provider_identity(document, archived_file)
            self._assert_archive_postcondition(
                archived_file,
                archived_sha256,
                source_sha256,
                str(plan["sourceFolderId"]),
                str(plan["archiveFolderId"]),
            )
        except Exception as exc:
            reason = _safe_reason(exc)
            rollback = None
            if move_result is not None and hasattr(archiver, "restore_file"):
                try:
                    rollback = archiver.restore_file(
                        provider_id,
                        str(plan["sourceFolderId"]),
                        str(plan["archiveFolderId"]),
                    )
                except Exception as rollback_exc:
                    rollback = {"status": "failed", "reason": _safe_reason(rollback_exc)}
            self._ensure_review_item(document_id, [reason])
            audit_event_id = self.ledger.record_audit_event({
                "action": "drive_wave.archive_failed",
                "entityType": "bookkeeping_document",
                "entityId": str(document_id),
                "details": {
                    "actor": actor,
                    "reason": reason,
                    "moveStarted": move_result is not None,
                    "rollback": rollback,
                    "externalSubmission": "not_executed",
                },
            })
            return {
                "success": False,
                "status": "blocked",
                "reasons": [reason],
                "auditEventId": audit_event_id,
                "externalSubmission": "not_executed",
            }

        evidence = (
            self.ledger.find_audit_event(EVIDENCE_ACTION, "bookkeeping_document", str(document_id)) or {}
        ).get("details") or {}
        metadata = dict(document.get("metadata") or {})
        lifecycle = dict(metadata.get("driveWaveLifecycle") or {})
        lifecycle.update({
            "status": "archived",
            "archivedAt": _now(),
            "archivedBy": str(actor or "local_worker")[:200],
            "sourceFolderId": plan["sourceFolderId"],
            "archiveFolderId": plan["archiveFolderId"],
            "sourceSha256": source_sha256,
            "externalTransactionId": evidence.get("externalTransactionId"),
            "attachmentObjectId": evidence.get("attachmentObjectId"),
            "attachmentTransactionId": evidence.get("attachmentTransactionId"),
            "transactionStatus": evidence.get("transactionStatus"),
            "waveObservedAt": evidence.get("waveObservedAt"),
            "evidenceDigest": evidence.get("evidenceDigest"),
            "verificationMethod": evidence.get("verificationMethod"),
            "providerMoveStatus": move_result.get("status"),
            "postMoveSha256": source_sha256,
            "postMoveVerifiedAt": _now(),
            "postMoveVerified": True,
        })
        metadata["driveWaveLifecycle"] = lifecycle
        metadata["waveDeliveryLifecycle"] = lifecycle
        self.ledger.update_document(document_id, {"metadata": metadata})
        audit_event_id = self.ledger.record_audit_event({
            "action": ARCHIVE_ACTION,
            "entityType": "bookkeeping_document",
            "entityId": str(document_id),
            "details": {
                "actor": actor,
                **lifecycle,
                "externalSubmission": "executed",
                "deletion": "not_performed",
            },
        })
        return {
            "success": True,
            "status": move_result.get("status") or "archived",
            "documentId": int(document_id),
            "archiveFolderId": plan["archiveFolderId"],
            "postMoveVerified": True,
            "postMoveSha256": source_sha256,
            "auditEventId": audit_event_id,
            "externalSubmission": "executed",
            "deletion": "not_performed",
        }

    @staticmethod
    def _assert_archive_postcondition(
        archived_file: Dict[str, Any],
        archived_sha256: str,
        expected_sha256: str,
        source_folder_id: str,
        archive_folder_id: str,
    ) -> None:
        parents = {str(item) for item in archived_file.get("parents") or []}
        if archive_folder_id not in parents or source_folder_id in parents:
            raise RuntimeError("Drive archive destination verification failed after move.")
        if archived_file.get("trashed"):
            raise RuntimeError("Drive source entered trash during archive move.")
        if archived_sha256 != expected_sha256:
            raise RuntimeError("Drive archive content hash verification failed after move.")

    def archive_ready(self, limit: int = 25, actor: str = "local_worker", dry_run: bool = False) -> Dict[str, Any]:
        candidates = self.list_candidates(limit=limit)["candidates"]
        ready = [item for item in candidates if item["archivePlan"].get("canArchive")]
        if dry_run:
            return {
                "success": True,
                "status": "planned",
                "ready": len(ready),
                "blocked": len(candidates) - len(ready),
                "candidates": candidates,
                "externalSubmission": "not_executed",
            }
        results = [self.archive_document(int(item["documentId"]), actor=actor) for item in ready]
        return {
            "success": all(item.get("success") for item in results),
            "status": "completed" if all(item.get("success") for item in results) else "completed_with_errors",
            "ready": len(ready),
            "archived": sum(1 for item in results if item.get("success")),
            "blocked": len(candidates) - len(ready),
            "results": results,
            "externalSubmission": "executed" if results else "not_executed",
        }

    def _plan_gmail_retention(
        self,
        document: Dict[str, Any],
        record: Optional[Dict[str, Any]],
        exports: list[Dict[str, Any]],
        evidence_event: Optional[Dict[str, Any]],
        *,
        configured_source_verified: bool = False,
    ) -> Dict[str, Any]:
        document_id = int(document["id"])
        reasons = self._common_delivery_reasons(document, record)
        if (
            not configured_source_verified
            and not self._is_gmail_scanner_source(document)
        ):
            reasons.append("gmail_scanner_source_not_trusted")
        evidence_event, evidence, evidence_reasons = self._wave_evidence_plan(
            document,
            exports,
            evidence_event,
        )
        reasons.extend(evidence_reasons)
        reasons = sorted(set(reasons))
        return {
            "status": "not_applicable",
            "canArchive": False,
            "reasons": reasons,
            "retentionStatus": "verified" if not reasons else "pending",
            "evidenceVerified": not reasons,
            "retentionPolicy": "email_unchanged_local_evidence_retained",
            "sourceEmailMutation": "never",
            "localEvidenceDeletion": "never",
            "externalTransactionId": evidence.get("externalTransactionId"),
            "attachmentObjectId": evidence.get("attachmentObjectId"),
            "evidenceDigest": evidence.get("evidenceDigest"),
            "verifiedAt": (evidence_event or {}).get("created_at"),
            "verificationMethod": evidence.get("verificationMethod"),
            "moveOnly": False,
            "deleteSource": False,
            "externalSubmission": "not_executed",
        }

    def _wave_evidence_plan(
        self,
        document: Dict[str, Any],
        exports: list[Dict[str, Any]],
        evidence_event: Optional[Dict[str, Any]],
    ) -> tuple[Optional[Dict[str, Any]], Dict[str, Any], list[str]]:
        evidence = (evidence_event or {}).get("details") or {}
        reasons = []
        if not evidence:
            reasons.append("wave_attachment_evidence_missing")
            return evidence_event, evidence, reasons

        reasons.extend(self._evidence_reasons(document, evidence))
        if not _timestamp_is_fresh(
            evidence_event.get("created_at"),
            self._evidence_max_age_seconds(),
        ):
            reasons.append("wave_attachment_verification_stale")
        terminal_exports = [
            item
            for item in exports[:5]
            if str(item.get("status") or "").lower() in TERMINAL_EXPORT_STATUSES
        ]
        if terminal_exports:
            business_exports = [
                item
                for item in terminal_exports
                if str(item.get("target_system") or "") == "waveapps_business"
            ]
            business_external_ids = {
                str(item.get("external_id") or "").strip()
                for item in business_exports
                if str(item.get("external_id") or "").strip()
            }
            if not business_exports:
                reasons.append("wave_target_is_not_business")
            elif (
                business_external_ids
                and str(evidence.get("externalTransactionId") or "") not in business_external_ids
            ):
                reasons.append("wave_transaction_id_mismatch")
        return evidence_event, evidence, reasons

    def _common_delivery_reasons(
        self,
        document: Dict[str, Any],
        record: Optional[Dict[str, Any]],
    ) -> list[str]:
        reasons = []
        if not self._business_id():
            reasons.append("wave_business_not_configured")
        if not str(document.get("original_filename") or "").strip():
            reasons.append("source_filename_missing")
        if not str(document.get("mime_type") or "").strip():
            reasons.append("source_mime_type_missing")
        if _source_size(document) is None:
            reasons.append("source_size_missing")
        if document.get("duplicate_of_document_id"):
            reasons.append("duplicate_review_unresolved")
        for review in document.get("review_items") or []:
            if str(review.get("status") or "pending").lower() in OPEN_REVIEW_STATUSES:
                reasons.append("open_review_item")
                break
        if not re.fullmatch(r"[0-9a-f]{64}", _source_sha256(document)):
            reasons.append("source_sha256_missing")
        reasons.extend(_wave_upload_reasons(document))

        if not record:
            reasons.append("bookkeeping_record_missing")
        else:
            reasons.extend(_line_item_reasons(record))
            if bool(record.get("review_required")):
                reasons.append("bookkeeping_record_review_required")
            if str(record.get("target_system") or "").strip() != "waveapps_business":
                reasons.append("bookkeeping_record_not_wave_business")
            if str(record.get("status") or "").lower() not in ARCHIVABLE_BOOKKEEPING_RECORD_STATUSES:
                reasons.append("bookkeeping_record_not_ready")
        return reasons

    def _document_reasons(
        self,
        document: Dict[str, Any],
        record: Optional[Dict[str, Any]],
    ) -> list[str]:
        reasons = self._common_delivery_reasons(document, record)
        if not self._archive_enabled():
            reasons.append("drive_archive_disabled")
        if document.get("source") != "google_drive":
            reasons.append("source_is_not_google_drive")
        if not self._source_folder_id():
            reasons.append("source_folder_not_configured")
        if not self._archive_folder_id():
            reasons.append("archive_folder_not_configured")
        if self._source_folder_id() and self._source_folder_id() == self._archive_folder_id():
            reasons.append("archive_folder_matches_source_folder")
        if self._drive_reauthorization_required():
            reasons.append("drive_reauthorization_required")
        if not self._is_configured_source(document):
            reasons.append("document_outside_configured_source_folder")
        if not str(document.get("source_document_id") or "").strip():
            reasons.append("drive_provider_file_id_missing")
        return reasons

    def _evidence_reasons(self, document: Dict[str, Any], evidence: Dict[str, Any]) -> list[str]:
        reasons = []
        source_hash = _source_sha256(document)
        if str(evidence.get("sourceSha256") or "").lower() != source_hash:
            reasons.append("evidence_source_hash_mismatch")
        if str(evidence.get("uploadSourceSha256") or "").lower() != source_hash:
            reasons.append("wave_upload_source_hash_mismatch")
        attachment_hash = str(evidence.get("attachmentSha256") or "").lower()
        if evidence.get("attachmentReadbackVerified") is not True:
            reasons.append("wave_attachment_readback_bytes_missing")
        if attachment_hash != source_hash:
            reasons.append("wave_attachment_hash_mismatch")
        attachment_size = _optional_int(evidence.get("attachmentSizeBytes"))
        source_size = _source_size(document)
        if source_size is None:
            reasons.append("source_size_missing")
        if attachment_size is None:
            reasons.append("wave_attachment_size_missing")
        elif source_size is not None and attachment_size != source_size:
            reasons.append("wave_attachment_size_mismatch")
        attachment_filename = os.path.basename(str(evidence.get("attachmentFilename") or "")).casefold()
        source_filename = os.path.basename(str(document.get("original_filename") or "")).casefold()
        if not attachment_filename:
            reasons.append("wave_attachment_filename_missing")
        elif source_filename and attachment_filename != source_filename:
            reasons.append("wave_attachment_filename_mismatch")
        if evidence.get("attachmentPresent") is not True:
            reasons.append("wave_attachment_missing")
        if evidence.get("attachmentOpened") is not True:
            reasons.append("wave_attachment_not_opened")
        if evidence.get("attachmentDownloaded") is not True:
            reasons.append("wave_attachment_not_downloaded")
        if not str(evidence.get("attachmentObjectId") or "").strip():
            reasons.append("wave_attachment_object_id_missing")
        external_transaction_id = str(evidence.get("externalTransactionId") or "").strip()
        if not external_transaction_id:
            reasons.append("wave_transaction_id_missing")
        if evidence.get("transactionExists") is not True:
            reasons.append("wave_transaction_missing")
        transaction_match_count = _optional_int(evidence.get("transactionMatchCount"))
        if transaction_match_count != 1:
            reasons.append("wave_transaction_match_not_unique")
        matching_transaction_ids = evidence.get("matchingTransactionIds")
        if (
            not isinstance(matching_transaction_ids, list)
            or len(matching_transaction_ids) != 1
            or str(matching_transaction_ids[0] or "").strip() != external_transaction_id
        ):
            reasons.append("wave_transaction_match_ids_invalid")
        transaction_status = str(evidence.get("transactionStatus") or "").strip().lower()
        if transaction_status not in FINISHED_WAVE_TRANSACTION_STATUSES:
            reasons.append("wave_transaction_not_finished")
        if evidence.get("transactionReviewed") is not True:
            reasons.append("wave_transaction_not_reviewed")
        business_id = self._business_id()
        if business_id and str(evidence.get("businessId") or "") != business_id:
            reasons.append("wave_business_mismatch")
        if not _valid_wave_transaction_url(evidence.get("transactionPageUrl"), business_id):
            reasons.append("wave_transaction_page_invalid")
        if not _timestamp_is_fresh(
            evidence.get("waveObservedAt"),
            self._evidence_max_age_seconds(),
        ):
            reasons.append("wave_observation_stale_or_missing")
        if str(evidence.get("attachmentTransactionId") or "").strip() != external_transaction_id:
            reasons.append("wave_attachment_transaction_mismatch")
        mime_type = str(evidence.get("attachmentMimeType") or "").lower()
        document_mime = str(document.get("mime_type") or "").lower()
        if document_mime and mime_type != document_mime:
            reasons.append("wave_attachment_mime_mismatch")

        record = self.ledger.get_bookkeeping_record_by_document(int(document["id"]))
        reasons.extend(_line_item_reasons(record))
        required_fields = _required_wave_fields(document, record)
        expected_fields = _expected_wave_fields(document, record)
        expected_digest = _expected_fields_digest(expected_fields, required_fields)
        if str(evidence.get("expectedFieldsDigest") or "") != expected_digest:
            reasons.append("wave_expected_fields_changed_or_unbound")
        observed_fields = _normalize_observed_fields(evidence.get("observedFields"))
        for field in required_fields:
            if not _wave_field_matches(field, expected_fields.get(field), observed_fields.get(field)):
                reasons.append(f"wave_field_mismatch:{field}")
        return reasons

    def _assert_provider_identity(self, document: Dict[str, Any], current: Dict[str, Any]) -> None:
        metadata = document.get("metadata") or {}
        provider = metadata.get("providerMetadata") if isinstance(metadata.get("providerMetadata"), dict) else {}
        expected_id = str(document.get("source_document_id") or "").strip()
        if str(current.get("id") or "").strip() != expected_id:
            raise RuntimeError("Drive provider file identity changed after FAB intake.")
        expected_name = os.path.basename(str(document.get("original_filename") or "")).casefold()
        current_name = os.path.basename(str(current.get("name") or "")).casefold()
        if not current_name or current_name != expected_name:
            raise RuntimeError("Drive provider filename changed after FAB intake.")
        expected_mime = str(
            provider.get("provider_mime_type")
            or provider.get("mime_type")
            or document.get("mime_type")
            or ""
        ).lower().strip()
        current_mime = str(current.get("mimeType") or "").lower().strip()
        if not current_mime or current_mime != expected_mime:
            raise RuntimeError("Drive provider MIME type changed after FAB intake.")
        expected_md5 = str(provider.get("md5_checksum") or provider.get("md5Checksum") or "")
        current_md5 = str(current.get("md5Checksum") or "")
        if expected_md5 and current_md5 and expected_md5 != current_md5:
            raise RuntimeError("Drive provider checksum changed after FAB intake.")
        expected_size = _source_size(document)
        current_size = _optional_int(current.get("size"))
        if expected_size is None:
            raise RuntimeError("Source byte-size metadata is missing, invalid, or inconsistent.")
        if current_size is None or current_size <= 0:
            raise RuntimeError("Drive provider byte size is missing or invalid.")
        if expected_size != current_size:
            raise RuntimeError("Drive provider size changed after FAB intake.")
        if current.get("trashed"):
            raise RuntimeError("Drive source file is in trash.")

    def _ensure_review_item(self, document_id: int, reasons: list[str]) -> None:
        document = self.ledger.get_document(document_id) or {}
        if any(
            item.get("reason") == "drive_wave_archive_blocked"
            and str(item.get("status") or "pending").lower() in OPEN_REVIEW_STATUSES
            for item in document.get("review_items") or []
        ):
            return
        self.ledger.create_review_item({
            "documentId": document_id,
            "reason": "drive_wave_archive_blocked",
            "details": "Drive source retained because Wave attachment proof did not meet policy: " + ", ".join(reasons),
            "correctedData": {"reasons": reasons, "sourceAction": "retain_in_drive"},
        })

    def _resolve_archive_review_items(self, document_id: int, actor: str, evidence_digest: str) -> None:
        resolved_ids = []
        for item in self.ledger.list_review_items(
            status=sorted(OPEN_REVIEW_STATUSES),
            limit=100,
            document_id=document_id,
        ):
            if item.get("reason") != "drive_wave_archive_blocked":
                continue
            self.ledger.resolve_review_item(
                int(item["id"]),
                status="resolved",
                resolution="Fresh Wave transaction and attachment evidence now satisfies the archive policy.",
                corrected_data={
                    "actor": str(actor or "hai")[:200],
                    "evidenceDigest": evidence_digest,
                    "sourceAction": "recheck_before_move",
                },
            )
            resolved_ids.append(int(item["id"]))
        if resolved_ids:
            self.ledger.record_audit_event({
                "action": "drive_wave.archive_reviews_resolved",
                "entityType": "bookkeeping_document",
                "entityId": str(document_id),
                "details": {
                    "actor": str(actor or "hai")[:200],
                    "reviewItemIds": resolved_ids,
                    "evidenceDigest": evidence_digest,
                },
            })

    def _is_configured_source(self, document: Dict[str, Any]) -> bool:
        if document.get("source") == "gmail":
            return self._is_gmail_scanner_source(document)
        if document.get("source") != "google_drive":
            return False
        metadata = document.get("metadata") or {}
        provider = metadata.get("providerMetadata") if isinstance(metadata.get("providerMetadata"), dict) else {}
        folder_id = provider.get("folder_id") or provider.get("folderId") or metadata.get("sourceIdentifier")
        return bool(self._source_folder_id()) and str(folder_id or "") == self._source_folder_id()

    def _is_gmail_scanner_source(self, document: Dict[str, Any]) -> bool:
        if document.get("source") != "gmail" or not self._gmail_scanner_configured():
            return False
        provider = _provider_metadata(document)
        sender = str(provider.get("sender_address") or "").strip().lower()
        if sender not in self._gmail_trusted_senders():
            return False
        if provider.get("scanner_policy_verified") is not True:
            return False
        if not str(provider.get("message_id") or "").strip():
            return False
        if not str(provider.get("attachment_id") or "").strip():
            return False
        return _path_is_within(
            document.get("storage_path"),
            self._gmail_attachment_root(),
        )

    def _gmail_scanner_configured(self) -> bool:
        return bool(
            _as_bool(self.config.get("gmail_scanner_mode"))
            and self._gmail_trusted_senders()
            and self._gmail_attachment_root()
        )

    def _gmail_trusted_senders(self) -> set[str]:
        return set(_string_values(self.config.get("gmail_trusted_senders")))

    def _gmail_attachment_root(self) -> str:
        value = _first(
            self.config,
            "gmail_attachment_download_dir",
            "gmail_download_dir",
            "attachments_save_dir",
        )
        return str(value or "").strip()

    def _archive_enabled(self) -> bool:
        return _as_bool(_first(self.config, "google_drive_archive_verified_files", "drive_archive_verified_files"), False)

    def _source_folder_id(self) -> str:
        return str(_first(self.config, "google_drive_wave_source_folder_id", "google_drive_folder_id", "drive_folder_id") or "").strip()

    def _archive_folder_id(self) -> str:
        return str(_first(self.config, "google_drive_wave_archive_folder_id", "google_drive_archive_folder_id") or "").strip()

    def _business_id(self) -> str:
        return str(_first(self.config, "waveapps_business_id", "wave_business_id") or "").strip()

    def _drive_reauthorization_required(self) -> bool:
        configured_token_path = str(
            _first(self.config, "google_drive_token_file", "drive_token_path")
            or "tokens/drive_token.json"
        )
        return GoogleOAuthTokenStore(configured_token_path, []).status()[
            "reauthorizationRequired"
        ]

    def _evidence_max_age_seconds(self) -> int:
        value = _first(
            self.config,
            "google_drive_wave_attachment_evidence_max_age_seconds",
            "drive_wave_attachment_evidence_max_age_seconds",
            "wave_attachment_evidence_max_age_seconds",
        )
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 900
        return max(60, min(parsed, 3600))


def _normalize_evidence(evidence: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(evidence, dict):
        return {}
    allowed = {
        "externalTransactionId", "businessId", "sourceSha256", "uploadSourceSha256",
        "attachmentSha256", "attachmentSizeBytes", "attachmentObjectId", "attachmentMimeType", "attachmentFilename",
        "attachmentPresent", "attachmentOpened", "attachmentDownloaded", "attachmentTransactionId",
        "transactionExists", "transactionStatus", "transactionMatchCount", "matchingTransactionIds",
        "transactionPageUrl", "transactionReviewed", "waveObservedAt", "fieldMatches",
        "observedFields", "expectedFieldsDigest",
        "verifiedAt", "verifier", "verificationMethod", "evidenceDigest", "actor",
        "attachmentReadbackVerified", "readbackOrigin",
    }
    normalized = {key: value for key, value in evidence.items() if key in allowed}
    for key in ("sourceSha256", "uploadSourceSha256", "attachmentSha256"):
        if normalized.get(key):
            normalized[key] = str(normalized[key]).lower().strip()
    return normalized


def _source_sha256(document: Dict[str, Any]) -> str:
    metadata = document.get("metadata") or {}
    return str(metadata.get("contentSha256") or "").lower().strip()


def _source_provider(document: Dict[str, Any]) -> str:
    source = str(document.get("source") or "").strip().lower()
    return "gmail" if source == "gmail" else "google_drive" if source == "google_drive" else source


def _provider_metadata(document: Dict[str, Any]) -> Dict[str, Any]:
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    provider = metadata.get("providerMetadata")
    return provider if isinstance(provider, dict) else {}


def _source_size(document: Dict[str, Any]) -> Optional[int]:
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    provider = metadata.get("providerMetadata") if isinstance(metadata.get("providerMetadata"), dict) else {}
    declared = [value for value in (provider.get("size"), metadata.get("sizeBytes"))
                if value is not None and not (isinstance(value, str) and value == "")]
    sizes = [_optional_int(value) for value in declared]
    if not sizes or any(size is None or size <= 0 for size in sizes):
        return None
    return sizes[0] if all(size == sizes[0] for size in sizes) else None


def _bounded_candidate_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 100
    return max(1, min(parsed, 500))


def _string_values(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else re.split(r"[,;\n]", str(value or ""))
    return list(dict.fromkeys(
        str(item or "").strip().lower()
        for item in values
        if str(item or "").strip()
    ))


def _path_is_within(path: Any, root: Any) -> bool:
    if not str(path or "").strip() or not str(root or "").strip():
        return False
    candidate = os.path.realpath(os.path.abspath(os.path.expanduser(os.path.expandvars(str(path)))))
    configured_root = os.path.realpath(os.path.abspath(os.path.expanduser(os.path.expandvars(str(root)))))
    try:
        return os.path.commonpath((configured_root, candidate)) == configured_root
    except ValueError:
        return False


def _expected_wave_fields(
    document: Dict[str, Any],
    record: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    record = record or {}
    extracted = document.get("extracted_data") if isinstance(document.get("extracted_data"), dict) else {}
    return {
        "vendor": record.get("vendor_name") or document.get("vendor_name"),
        "date": record.get("record_date") or document.get("transaction_date"),
        "amount": record.get("amount") if record.get("amount") is not None else document.get("total_amount"),
        "taxAmount": record.get("vat_amount") if record.get("vat_amount") is not None else document.get("vat_amount"),
        "currency": record.get("currency") or extracted.get("currency") or "EUR",
        "category": record.get("category") or document.get("category"),
        "description": record.get("description") or extracted.get("description") or document.get("original_filename"),
        "invoiceNumber": (
            extracted.get("invoice_number")
            or extracted.get("invoiceNumber")
            or extracted.get("document_number")
        ),
        "account": record.get("target_account"),
    }


def _required_wave_fields(
    document: Dict[str, Any], record: Optional[Dict[str, Any]] = None,
) -> list[str]:
    required_fields = list(REQUIRED_FIELD_MATCHES)
    extracted = document.get("extracted_data") if isinstance(document.get("extracted_data"), dict) else {}
    if any(
        extracted.get(key) not in (None, "")
        for key in ("invoice_number", "invoiceNumber", "document_number")
    ):
        required_fields.append("invoiceNumber")
    if document.get("vat_amount") is not None or (record or {}).get("vat_amount") is not None:
        required_fields.append("taxAmount")
    return required_fields


def _normalize_observed_fields(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = set(REQUIRED_FIELD_MATCHES) | {"invoiceNumber", "taxAmount"}
    return {key: value.get(key) for key in allowed if key in value}


def _expected_fields_digest(expected_fields: Dict[str, Any], required_fields: list[str]) -> str:
    return _digest({
        field: _canonical_wave_field(field, expected_fields.get(field))
        for field in required_fields
    })


def _wave_field_matches(field: str, expected: Any, observed: Any) -> bool:
    expected_value = _canonical_wave_field(field, expected)
    observed_value = _canonical_wave_field(field, observed)
    return (
        expected_value not in (None, "")
        and observed_value not in (None, "")
        and expected_value == observed_value
    )


def _finite_number(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _delivery_line_items(record: Optional[Dict[str, Any]]) -> tuple[list[Any], list[str]]:
    # Copy the persisted JSON tree; never repair ledger values during handoff.
    source = list((record or {}).get("line_items") or [])
    projected: list[Any] = []
    invalid: list[str] = []
    pending = [(source, projected, "lineItems")]
    while pending:
        original, target, path = pending.pop()
        entries = original.items() if isinstance(original, dict) else enumerate(original)
        for key, value in entries:
            child_path = f"{path}.{key}" if isinstance(original, dict) else f"{path}[{key}]"
            if isinstance(value, (dict, list)):
                copy = {} if isinstance(value, dict) else []
                pending.append((value, copy, child_path))
            elif isinstance(value, float) and not math.isfinite(value):
                copy = None
                invalid.append(child_path)
            else:
                copy = value
            if isinstance(target, dict):
                target[key] = copy
            else:
                target.append(copy)
    for index, item in enumerate(projected):
        if not isinstance(item, dict):
            invalid.append(f"lineItems[{index}]")
            continue
        for field in ("quantity", "unit_price", "amount", "tax_amount", "tax_rate", "confidence_score"):
            value = item.get(field)
            if value is not None and _finite_number(value) is None:
                item[field] = None
                invalid.append(f"lineItems[{index}].{field}")
    return projected, sorted(set(invalid))


def _line_item_reasons(record: Optional[Dict[str, Any]]) -> list[str]:
    return [f"bookkeeping_line_item_invalid:{path}" for path in _delivery_line_items(record)[1]]


def _canonical_wave_field(field: str, value: Any) -> Any:
    if field in {"amount", "taxAmount"}:
        number = _finite_number(value)
        if number is None:
            return None
        try:
            return str(number.quantize(Decimal("0.01")))
        except (InvalidOperation, TypeError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    if field == "date":
        return _canonical_date(value)
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if field == "currency":
        return text.upper()
    return text.casefold()


def _canonical_date(value: Any) -> Optional[str]:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return None
    for date_format in (
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%b %d, %Y",
        "%B %d, %Y",
        "%d %b %Y",
        "%d %B %Y",
    ):
        try:
            return datetime.strptime(text, date_format).date().isoformat()
        except ValueError:
            continue
    return None


def _export_reference(export: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not export:
        return None
    return {
        "id": export.get("id"),
        "status": export.get("status"),
        "actionId": export.get("action_id"),
        "operationId": export.get("operation_id"),
        "externalId": export.get("external_id"),
        "externalSubmission": export.get("external_submission"),
        "updatedAt": export.get("updated_at"),
    }


def _work_order_stage(
    *,
    document: Dict[str, Any],
    record: Optional[Dict[str, Any]],
    terminal_export: Optional[Dict[str, Any]],
    evidence: Dict[str, Any],
    evidence_event: Optional[Dict[str, Any]],
    plan: Dict[str, Any],
    unrelated_reviews: list[Dict[str, Any]],
    evidence_max_age_seconds: int,
    missing_expected_fields: list[str],
    invalid_line_item_fields: list[str],
    upload_reasons: list[str],
) -> str:
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    lifecycle = (
        metadata.get("waveDeliveryLifecycle")
        if isinstance(metadata.get("waveDeliveryLifecycle"), dict)
        else metadata.get("driveWaveLifecycle")
        if isinstance(metadata.get("driveWaveLifecycle"), dict)
        else {}
    )
    if not document.get("storage_path") or not os.path.isfile(str(document.get("storage_path"))):
        return "source_file_unavailable"
    if upload_reasons:
        return "source_incompatible"
    if unrelated_reviews or (record and bool(record.get("review_required"))):
        return "blocked_by_review"
    if not record or missing_expected_fields or invalid_line_item_fields or str(document.get("processing_status") or "").lower() in {
        "registered", "imported", "processing", "failed", "needs_review"
    }:
        return "needs_processing"
    if lifecycle.get("status") == "archived" or plan.get("status") == "already_archived":
        return "completed"
    if _source_provider(document) == "gmail" and plan.get("evidenceVerified") is True:
        return "completed"
    if plan.get("canArchive"):
        return "ready_to_archive"
    if evidence:
        if not _timestamp_is_fresh((evidence_event or {}).get("created_at"), evidence_max_age_seconds):
            return "refresh_wave_readback"
        return "refresh_wave_readback"
    if terminal_export and str(terminal_export.get("external_id") or "").strip():
        return "upload_and_verify_attachment"
    return "locate_or_create_transaction"


def _stage_action(stage: str) -> str:
    return {
        "source_file_unavailable": "Restore or re-download the exact retained source before Wave upload.",
        "source_incompatible": "Check source size, type, and metadata; convert or split only if needed without changing the retained original.",
        "needs_processing": "Finish OCR, validation, categorization, and review in FAB.",
        "blocked_by_review": "Resolve the blocking FAB review before downstream execution.",
        "locate_or_create_transaction": "Find an exact Wave transaction or create the approved Wave draft, then upload the source file.",
        "upload_and_verify_attachment": "Upload the exact source file and read the stored Wave attachment and transaction back.",
        "refresh_wave_readback": "Repeat Wave transaction and attachment readback and submit fresh complete evidence.",
        "ready_to_archive": "All gates pass; the worker may move the Drive source to the configured archive folder.",
        "completed": "No action required; Wave readback is verified and the source retention policy has been satisfied.",
    }.get(stage, "Inspect the work order before proceeding.")


def _count_stage(work_orders: list[Dict[str, Any]], stage: str) -> int:
    return sum(1 for item in work_orders if item.get("stage") == stage)


def _wave_upload_reasons(document: Dict[str, Any]) -> list[str]:
    reasons = []
    size = _source_size(document)
    if size is None:
        reasons.append("wave_receipt_size_missing_or_invalid")
    elif size > WAVE_RECEIPT_MAX_BYTES:
        reasons.append("wave_receipt_file_too_large")
    filename = str(document.get("original_filename") or "")
    extension = os.path.splitext(filename)[1].lower()
    mime_type = str(document.get("mime_type") or "").lower()
    if extension not in WAVE_RECEIPT_ALLOWED_EXTENSIONS:
        reasons.append("wave_receipt_file_extension_unsupported")
    if mime_type and mime_type not in WAVE_RECEIPT_ALLOWED_MIME_TYPES:
        reasons.append("wave_receipt_mime_type_unsupported")
    return sorted(set(reasons))


def _wave_browser_contract(business_id: str, document_id: int) -> Dict[str, Any]:
    business_uuid = wave_business_uuid(business_id)
    return {
        "version": "wave-transactions-browser-v1",
        "coordinator": {
            "status": "/api/wave/receipt-executor/status",
            "session": "/api/wave/receipt-executor/session",
            "claim": "/api/wave/receipt-executor/claim",
            "release": "/api/wave/receipt-executor/release",
            "requiredCapabilities": [
                "transaction_locate",
                "receipt_upload",
                "receipt_download",
                "transaction_review",
                "observed_fields",
            ],
            "credentialsAccepted": False,
        },
        "transactionListUrl": (
            f"https://next.waveapps.com/{business_uuid}/transactions"
            if business_uuid else None
        ),
        "surface": "Accounting > Transactions",
        "observedControls": {
            "searchPlaceholder": "Search transactions",
            "addTransactionButton": "Add transaction",
            "addWithdrawalMenuItem": "Add withdrawal",
            "receiptUploadButton": "select a file",
            "viewReceiptButton": "View original receipt",
            "markReviewedButton": "Mark as reviewed",
            "saveButton": "Save transaction",
        },
        "upload": {
            "maxBytes": WAVE_RECEIPT_MAX_BYTES,
            "allowedExtensions": sorted(WAVE_RECEIPT_ALLOWED_EXTENSIONS),
            "fileMustComeFromWorkOrderLocalPath": True,
        },
        "requiredReadback": {
            "transactionMustExist": True,
            "transactionMustBeFinishedAndReviewed": True,
            "matchingTransactionCountMustEqual": 1,
            "attachmentMustBeBoundToTransaction": True,
            "waveObservationMustBeFresh": True,
            "downloadStoredReceipt": True,
            "submitDownloadedBytesTo": f"/api/drive-wave/documents/{document_id}/attachment-readback",
            "serverComputedSha256MustMatchSource": True,
            "serverComputedSizeMustMatchSource": True,
            "filenameMustMatchSource": True,
            "submitTransactionPageUrl": True,
            "submitObservedTransactionFields": list(REQUIRED_FIELD_MATCHES),
            "serverComputesFieldMatches": True,
        },
        "externalSubmission": "policy_gated_browser_execution",
    }


def _optional_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value.strip()):
        parsed = int(value.strip())
    else:
        return None
    return parsed if 0 <= parsed <= 2**63 - 1 else None


def _valid_wave_transaction_url(value: Any, business_id: str) -> bool:
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.hostname not in {
        "accounting.waveapps.com", "next.waveapps.com",
    }:
        return False
    normalized_path = f"/{parsed.path.strip('/')}".casefold()
    return bool(business_id) and f"/{business_id.casefold()}/" in f"{normalized_path}/"


def _digest(payload: Dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _first(config: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = config.get(key)
        if value not in (None, ""):
            return value
    nested_drive = config.get("google_drive") if isinstance(config.get("google_drive"), dict) else {}
    nested_wave = config.get("waveapps_business") if isinstance(config.get("waveapps_business"), dict) else {}
    nested = {**nested_drive, **nested_wave}
    for key in keys:
        short = key.replace("google_drive_", "").replace("waveapps_business_", "")
        if nested.get(short) not in (None, ""):
            return nested[short]
    return None


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timestamp_is_fresh(value: Any, max_age_seconds: int) -> bool:
    try:
        observed_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    age_seconds = (datetime.now(timezone.utc) - observed_at.astimezone(timezone.utc)).total_seconds()
    return -60 <= age_seconds <= max_age_seconds


def _safe_reason(error: Exception) -> str:
    text = re.sub(r"\s+", " ", str(error or type(error).__name__)).strip()
    return text[:300] or type(error).__name__
