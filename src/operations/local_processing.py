import hashlib
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from src.categorizers.hybrid_categorizer import HybridCategorizer
from src.document_handling.duplicate_detector import DuplicateDetector
from src.document_processors.document_type_classifier import (
    DocumentTypeClassifier,
    is_non_posting_document_type,
)
from src.document_processors.financial_field_extractor import FinancialFieldExtractor
from src.document_processors.document_resource_limits import (
    MAX_SOURCE_DOCUMENT_BYTES,
    MAX_TEXT_DOCUMENT_BYTES,
)
from src.operations.local_backup import LocalBackupService
from src.operations.local_bookkeeping_records import LocalBookkeepingRecordService
from src.operations.local_category_suggestions import (
    trusted_category_automation_candidate,
)
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_targets import resolve_document_target_system
from src.validation.validation_manager import ValidationManager


TEXT_EXTENSIONS = {".csv", ".txt"}
SENSITIVE_REVIEW_TERMS = (
    "belastingdienst",
    "digid",
    "gemeente",
    "huurtoeslag",
    "pgb",
    "svb",
    "toeslagen",
    "uwv",
    "wmo",
    "zorgtoeslag",
)
PROCESSING_REVIEW_REASONS = {
    "credit_note_posting_review",
    "duplicate_candidate",
    "empty_ocr_text",
    "low_confidence_categorization",
    "manual_review_category",
    "document_type_conflict",
    "non_posting_document_type",
    "sensitive_government_document",
    "validation_failed",
}
OCR_RECOVERY_VERSION = "illumination_normalization_v1"
STORED_OCR_REASSESSMENT_VERSION = "financial_validation_v9_refund_totals"
STORED_OCR_REASSESSMENT_REASONS = {
    "document_type_conflict",
    "low_confidence_categorization",
    "manual_review_category",
    "validation_failed",
}


def trusted_category_suggestion_candidates(
    ledger: LocalOperationsLedger,
    config: Optional[Dict[str, Any]] = None,
    limit: int = 500,
) -> list:
    """Find exact built-in vendor matches that still have category review gates."""
    config = config or {}
    bounded_limit = max(1, min(_safe_int(limit) or 500, 5000))
    open_reviews = []
    while len(open_reviews) < 5000:
        page = ledger.list_review_items(
            status=("pending", "in_review"),
            limit=500,
            offset=len(open_reviews),
        )
        open_reviews.extend(page)
        if len(page) < 500:
            break
    reviews_by_document: Dict[int, list] = {}
    for review in open_reviews:
        document_id = _safe_int(review.get("document_id"))
        if document_id:
            reviews_by_document.setdefault(document_id, []).append(review)
    category_document_ids = [
        document_id
        for document_id, reviews in reviews_by_document.items()
        if any(
            review.get("reason") in {
                "low_confidence_categorization",
                "manual_review_category",
            }
            for review in reviews
        )
    ]
    documents_by_id = {}
    for start in range(0, len(category_document_ids), 500):
        context = ledger.get_review_work_item_context(
            category_document_ids[start:start + 500],
        )
        documents_by_id.update(context["documents"])
    documents = sorted(
        documents_by_id.values(),
        key=lambda document: (
            str(document.get("updated_at") or ""),
            int(document.get("id") or 0),
        ),
        reverse=True,
    )
    candidates = []
    for document in documents:
        if len(candidates) >= bounded_limit:
            break
        if (
            str(document.get("processing_status") or "") in {"duplicate", "failed"}
            or is_non_posting_document_type(document.get("document_type"))
        ):
            continue
        metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
        processing = metadata.get("processing") if isinstance(metadata.get("processing"), dict) else {}
        classification = (
            processing.get("documentTypeClassification")
            if isinstance(processing.get("documentTypeClassification"), dict)
            else {}
        )
        if (
            classification.get("postingEligible") is False
            and classification.get("reviewRequired") is True
        ):
            continue
        document_reviews = reviews_by_document.get(int(document["id"]), [])
        if (
            document.get("duplicate_of_document_id")
            and not any(item.get("reason") == "duplicate_candidate" for item in document_reviews)
        ):
            continue
        category_reviews = [
            item
            for item in document_reviews
            if item.get("status") in {"pending", "in_review"}
            and item.get("reason") in {
                "low_confidence_categorization",
                "manual_review_category",
            }
        ]
        if not category_reviews:
            continue
        suggestion = trusted_category_automation_candidate(document, config)
        if suggestion:
            candidates.append({
                "document": document,
                "suggestion": suggestion,
                "reviewItems": category_reviews,
            })
    return candidates


def duplicate_link_cycles(
    ledger: LocalOperationsLedger,
    limit: Optional[int] = None,
) -> list:
    """Scan full link history by default; explicit limits are diagnostic windows."""
    if limit is None:
        links = {int(row["id"]): int(row["duplicate_of_document_id"])
                 for row in ledger.iter_document_duplicate_links()}
    else:
        documents = ledger.list_document_duplicate_links(limit=max(1, min(_safe_int(limit) or 5000, 5000)))
        document_ids = {int(document["id"]) for document in documents}
        links = {
            int(document["id"]): int(document["duplicate_of_document_id"])
            for document in documents
            if document.get("duplicate_of_document_id") is not None
            and int(document["duplicate_of_document_id"]) in document_ids
        }
    cycles = []
    seen_cycles = set()
    completed = set()
    for start in links:
        path = []
        positions = {}
        current = start
        while current in links and current not in completed:
            if current in positions:
                members = tuple(sorted(path[positions[current]:]))
                if members and members not in seen_cycles:
                    seen_cycles.add(members)
                    cycles.append(list(members))
                break
            positions[current] = len(path)
            path.append(current)
            current = links[current]
        completed.update(path)
    return sorted(cycles, key=lambda members: (members[0], len(members), members))


def duplicate_candidate_reassessment_plan(
    ledger: LocalOperationsLedger,
    config: Optional[Dict[str, Any]] = None,
    limit: int = 500,
) -> list:
    """Find open duplicate pairs whose current evidence changes their disposition."""
    detector = DuplicateDetector(config or {})
    open_candidates = ledger.list_duplicate_candidates(
        status=("pending", "in_review"),
        limit=max(1, min(_safe_int(limit) or 500, 5000)),
    )
    pair_rows: Dict[tuple, list] = {}
    for candidate in open_candidates:
        document_id = _safe_int(candidate.get("document_id"))
        candidate_document_id = _safe_int(candidate.get("candidate_document_id"))
        if not document_id or not candidate_document_id or document_id == candidate_document_id:
            continue
        pair = tuple(sorted((document_id, candidate_document_id)))
        pair_rows.setdefault(pair, []).append(candidate)

    documents = ledger.get_documents_with_review_items(
        [document_id for pair in pair_rows for document_id in pair],
        review_status=("pending", "in_review"),
    )
    plans = []
    for (canonical_id, subject_id), rows in sorted(pair_rows.items()):
        canonical = documents.get(canonical_id)
        subject = documents.get(subject_id)
        if not canonical or not subject:
            continue
        if (
            str(canonical.get("processing_status") or "") == "duplicate"
            or str(subject.get("processing_status") or "") == "duplicate"
        ):
            continue
        pair_link_documents = [
            (document, counterpart_id)
            for document, counterpart_id in (
                (canonical, subject_id),
                (subject, canonical_id),
            )
            if _safe_int(document.get("duplicate_of_document_id")) == counterpart_id
        ]
        pending_pair_link_document_ids = [
            int(document["id"])
            for document, _ in pair_link_documents
            if any(
                str(item.get("reason") or "") == "duplicate_candidate"
                for item in _open_reviews(document)
            )
        ]
        if (
            pair_link_documents
            and len(pending_pair_link_document_ids) != len(pair_link_documents)
        ):
            continue

        exact_content_row = next(
            (
                row
                for row in rows
                if str(row.get("match_type") or "") == "exact_content_hash"
            ),
            None,
        )
        content_hash_matches = bool(
            canonical.get("content_sha256")
            and canonical.get("content_sha256") == subject.get("content_sha256")
        )
        if exact_content_row and not content_hash_matches:
            # Never discard an existing byte-identity claim without both current hashes.
            continue
        if content_hash_matches:
            result = {
                "is_duplicate": True,
                "reason": "exact_content_hash",
                "confidence_score": 1.0,
                "matched_document_id": canonical_id,
            }
        else:
            result = detector.is_duplicate(
                _duplicate_comparison_document(subject),
                [_duplicate_comparison_document(canonical)],
            )

        if pending_pair_link_document_ids:
            if result.get("is_duplicate"):
                continue
            plans.append({
                "pair": [canonical_id, subject_id],
                "canonicalDocument": canonical,
                "subjectDocument": subject,
                "candidateRows": rows,
                "action": "reject_pending_link",
                "pendingDuplicateLinkDocumentIds": pending_pair_link_document_ids,
                "result": result,
            })
            continue
        if canonical.get("duplicate_of_document_id") or subject.get("duplicate_of_document_id"):
            continue

        desired_type = str(result.get("reason") or "")
        desired_row = next(
            (
                row
                for row in rows
                if _safe_int(row.get("document_id")) == subject_id
                and _safe_int(row.get("candidate_document_id")) == canonical_id
                and str(row.get("match_type") or "") == desired_type
            ),
            None,
        )
        if result.get("is_duplicate") and len(rows) == 1 and desired_row:
            continue
        plans.append({
            "pair": [canonical_id, subject_id],
            "canonicalDocument": canonical,
            "subjectDocument": subject,
            "candidateRows": rows,
            "action": "canonicalize" if result.get("is_duplicate") else "reject",
            "result": result,
        })
    return plans


class LocalDocumentProcessor:
    """Run local ledger documents through FAB's processing interfaces."""

    def __init__(
        self,
        ledger: LocalOperationsLedger,
        config: Optional[Dict[str, Any]] = None,
        processor_pipeline: Optional[Any] = None,
        categorizer: Optional[Any] = None,
        validator: Optional[Any] = None,
    ):
        self.ledger = ledger
        self.config = config or {}
        self._processor_pipeline = processor_pipeline
        self.categorizer = categorizer or HybridCategorizer(self.config)
        self.validator = validator or ValidationManager(self.config)
        self.document_type_classifier = DocumentTypeClassifier()
        self.duplicate_detector = DuplicateDetector(self.config)
        self.review_confidence_threshold = _float_config(
            self.config,
            "fab_local_review_confidence_threshold",
            "operations_local_review_confidence_threshold",
            "categorization_review_confidence_threshold",
            "operations_categorization_review_confidence_threshold",
            "ml_confidence_threshold",
            default=0.7,
        )

    def process_imported(self, limit: int = 25) -> Dict[str, Any]:
        duplicate_cycle_repair = self.repair_duplicate_cycles()
        duplicate_candidate_reassessment = self.reassess_duplicate_candidates()
        trusted_category_automation = self.apply_trusted_category_suggestions(limit=limit)
        documents = self.ledger.list_documents(status="imported", limit=limit)
        summary: Dict[str, Any] = {
            "requested": len(documents),
            "processed": 0,
            "needsReview": 0,
            "failed": 0,
            "skipped": 0,
            "documents": [],
            "duplicateCycleRepair": duplicate_cycle_repair,
            "duplicateCandidateReassessment": duplicate_candidate_reassessment,
            "trustedCategoryAutomation": trusted_category_automation,
        }
        for document in documents:
            result = self.process_document(int(document["id"]))
            status = result.get("status")
            if result.get("skipped"):
                summary["skipped"] += 1
            elif status == "processed":
                summary["processed"] += 1
            elif status == "failed":
                summary["failed"] += 1
            else:
                summary["needsReview"] += 1
            summary["documents"].append(result)

        summary["documentTypeBackfill"] = self.backfill_document_types(limit=500)
        summary["updatedRecords"] = (
            summary["processed"]
            + summary["needsReview"]
            + duplicate_cycle_repair["documentsCleared"]
            + duplicate_candidate_reassessment["affectedDocuments"]
            + trusted_category_automation["updatedDocuments"]
        )

        self.ledger.record_audit_event({
            "action": "local_processing.batch_completed",
            "entityType": "bookkeeping_document",
            "details": {
                "requested": summary["requested"],
                "processed": summary["processed"],
                "needsReview": summary["needsReview"],
                "failed": summary["failed"],
                "skipped": summary["skipped"],
                "duplicateCyclesRepaired": duplicate_cycle_repair["cyclesRepaired"],
                "duplicateCandidatePairsReassessed": duplicate_candidate_reassessment[
                    "candidatePairs"
                ],
                "trustedCategoriesApplied": trusted_category_automation["updatedDocuments"],
            },
        })
        return summary

    def repair_duplicate_cycles(
        self,
        *,
        actor: str = "fab_local_processing",
        create_backup: bool = True,
    ) -> Dict[str, Any]:
        """Remove cyclic confirmed links while preserving every duplicate review gate."""
        cycles = duplicate_link_cycles(self.ledger)
        summary = {
            "cyclesFound": len(cycles),
            "cyclesRepaired": 0,
            "cyclesSkipped": 0,
            "documentsCleared": 0,
            "reviewItemsCreated": 0,
            "cycleDocumentIds": cycles,
            "externalSubmission": "not_executed",
            "sourceFilesModified": False,
        }
        if cycles and create_backup:
            backup = LocalBackupService(self.ledger, self.config).create_backup(
                note="Automatic pre-repair backup before clearing duplicate-link cycles"
            )
            if not backup.get("success"):
                raise RuntimeError(
                    "Duplicate-link cycle repair requires a successful ledger backup."
                )
            manifest = backup.get("manifest") or {}
            if not manifest.get("ledgerSha256") or not manifest.get("ledgerBytes"):
                raise RuntimeError(
                    "Duplicate-link cycle repair requires a checksum-bound ledger backup."
                )
            summary["backup"] = {
                "status": backup.get("status"),
                "backupFilename": backup.get("backupFilename"),
                "ledgerSha256": manifest.get("ledgerSha256"),
                "ledgerBytes": manifest.get("ledgerBytes"),
            }

        for cycle in cycles:
            cycle_review_items_created = 0
            with self.ledger.write_transaction():
                current_links = self.ledger.document_duplicate_targets(cycle)
                visited = set()
                current = cycle[0]
                while current in current_links and current not in visited:
                    visited.add(current)
                    current = current_links[current]
                # The scan and backup precede the writer lock; do not undo an
                # intervening correction or repair only part of a changed cycle.
                if visited != set(cycle) or current != cycle[0]:
                    summary["cyclesSkipped"] += 1
                    self.ledger.record_audit_event({
                        "action": "local_processing.duplicate_cycle_repair_deferred",
                        "entityType": "bookkeeping_document", "entityId": str(cycle[0]),
                        "details": {"actor": actor, "cycleDocumentIds": cycle,
                                    "reason": "links_changed_since_scan", "externalSubmission": "not_executed"},
                    })
                    continue
                before_links = {str(key): value for key, value in current_links.items()}
                for document_id in cycle:
                    document = self.ledger.get_document(document_id)
                    if not document:
                        raise RuntimeError("A cycle document became unavailable during repair")
                    existing_duplicate_reviews = [
                        item for item in document.get("review_items") or []
                        if item.get("status") in {"pending", "in_review"}
                        and item.get("reason") == "duplicate_candidate"
                    ]
                    self.ledger.clear_document_duplicate(document_id)
                    self.ledger.update_document(document_id, {"processingStatus": "needs_review"})
                    if not existing_duplicate_reviews:
                        self._queue_review(
                            document, "duplicate_candidate",
                            "Cyclic duplicate links were cleared; compare the retained "
                            "source documents before confirming a canonical record.",
                            corrected_data={"cycleDocumentIds": cycle, "actor": actor},
                        )
                        cycle_review_items_created += 1
                    LocalBookkeepingRecordService(self.ledger, self.config).upsert_from_document(document_id)
                self.ledger.record_audit_event({
                    "action": "local_processing.duplicate_cycle_repaired",
                    "entityType": "bookkeeping_document", "entityId": str(cycle[0]),
                    "details": {
                        "actor": actor, "cycleDocumentIds": cycle, "previousLinks": before_links,
                        "reviewItemsCreated": cycle_review_items_created,
                        "externalSubmission": "not_executed", "sourceFilesModified": False,
                    },
                })
            summary["cyclesRepaired"] += 1
            summary["documentsCleared"] += len(cycle)
            summary["reviewItemsCreated"] += cycle_review_items_created
        if cycles:
            self.ledger.record_audit_event({
                "action": "local_processing.duplicate_cycle_repair_completed",
                "entityType": "bookkeeping_document",
                "details": dict(summary),
            })
        return summary

    def reassess_duplicate_candidates(
        self,
        *,
        actor: str = "fab_local_processing",
        create_backup: bool = True,
        limit: int = 500,
    ) -> Dict[str, Any]:
        """Revalidate open duplicate pairs from current structured evidence."""
        plans = duplicate_candidate_reassessment_plan(
            self.ledger,
            self.config,
            limit=limit,
        )
        summary = {
            "candidatePairs": len(plans),
            "rejectedPairs": 0,
            "retainedPairs": 0,
            "candidateRowsClosed": 0,
            "candidateRowsOpened": 0,
            "pendingDuplicateLinksCleared": 0,
            "resolvedReviewItems": 0,
            "reviewItemsCreated": 0,
            "affectedDocuments": 0,
            "pairDocumentIds": [plan["pair"] for plan in plans],
            "externalSubmission": "not_executed",
            "sourceFilesModified": False,
            "confirmedDuplicateLinksModified": False,
        }
        if not plans:
            return summary
        if create_backup:
            backup = LocalBackupService(self.ledger, self.config).create_backup(
                note="Automatic pre-repair backup before duplicate evidence reassessment"
            )
            manifest = backup.get("manifest") or {}
            if (
                not backup.get("success")
                or not manifest.get("ledgerSha256")
                or not manifest.get("ledgerBytes")
            ):
                raise RuntimeError(
                    "Duplicate evidence reassessment requires a checksum-bound ledger backup."
                )
            summary["backup"] = {
                "status": backup.get("status"),
                "backupFilename": backup.get("backupFilename"),
                "ledgerSha256": manifest.get("ledgerSha256"),
                "ledgerBytes": manifest.get("ledgerBytes"),
            }

        affected_document_ids = set()
        for plan in plans:
            subject = plan["subjectDocument"]
            canonical_id, subject_id = plan["pair"]
            result = plan["result"]
            closed_ids = []
            for row in plan["candidateRows"]:
                row_id = int(row["id"])
                if self.ledger.resolve_duplicate_candidate(
                    row_id,
                    "rejected",
                    (
                        "Superseded by canonical duplicate evidence reassessment."
                        if result.get("is_duplicate")
                        else "Current structured evidence no longer meets the duplicate threshold."
                    ),
                    evidence={
                        "reassessedAt": _now(),
                        "reassessedBy": actor,
                        "reassessmentResult": {
                            "isDuplicate": bool(result.get("is_duplicate")),
                            "reason": result.get("reason"),
                            "confidenceScore": result.get("confidence_score"),
                        },
                        "externalSubmission": "not_executed",
                    },
                ):
                    closed_ids.append(row_id)
                summary["candidateRowsClosed"] += 1

            opened_candidate_id = None
            cleared_pending_link_document_ids = []
            if result.get("is_duplicate"):
                had_subject_duplicate_review = any(
                    str(item.get("reason") or "") == "duplicate_candidate"
                    for item in _open_reviews(subject)
                )
                opened_candidate_id = self.ledger.record_duplicate_candidate({
                    "documentId": subject_id,
                    "candidateDocumentId": canonical_id,
                    "matchType": result.get("reason") or "fuzzy_document_match",
                    "confidenceScore": result.get("confidence_score"),
                    "status": "pending",
                    "reason": "Duplicate evidence retained after deterministic reassessment.",
                    "evidence": {
                        "matchedDocumentId": canonical_id,
                        "confidenceScore": result.get("confidence_score"),
                        "reason": result.get("reason"),
                        "vendorName": subject.get("vendor_name"),
                        "transactionDate": subject.get("transaction_date"),
                        "totalAmount": subject.get("total_amount"),
                        "invoiceNumber": (
                            (subject.get("extracted_data") or {}).get("invoice_number")
                        ),
                        "canonicalizationPolicy": "earlier_ingested_document_id",
                        "reassessedAt": _now(),
                        "reassessedBy": actor,
                        "externalSubmission": "not_executed",
                    },
                })
                self._queue_review(
                    subject,
                    "duplicate_candidate",
                    (
                        f"Compare document #{subject_id} with canonical document "
                        f"#{canonical_id}; current evidence reports "
                        f"{result.get('reason')}."
                    ),
                    corrected_data={
                        "duplicateCandidateId": opened_candidate_id,
                        "candidateDocumentId": canonical_id,
                        "confidenceScore": result.get("confidence_score"),
                    },
                )
                if not had_subject_duplicate_review:
                    summary["reviewItemsCreated"] += 1
                summary["candidateRowsOpened"] += 1
                summary["retainedPairs"] += 1
            else:
                for linked_document_id in plan.get(
                    "pendingDuplicateLinkDocumentIds",
                    [],
                ):
                    counterpart_id = (
                        subject_id
                        if int(linked_document_id) == canonical_id
                        else canonical_id
                    )
                    linked_document = self.ledger.get_document(
                        int(linked_document_id)
                    )
                    if (
                        linked_document
                        and _safe_int(
                            linked_document.get("duplicate_of_document_id")
                        )
                        == counterpart_id
                    ):
                        self.ledger.clear_document_duplicate(
                            int(linked_document_id)
                        )
                        cleared_pending_link_document_ids.append(
                            int(linked_document_id)
                        )
                        summary["pendingDuplicateLinksCleared"] += 1
                summary["rejectedPairs"] += 1

            affected_document_ids.update((canonical_id, subject_id))
            self.ledger.record_audit_event({
                "action": "local_processing.duplicate_candidate_reassessed",
                "entityType": "duplicate_candidate",
                "entityId": str(opened_candidate_id or closed_ids[0]),
                "details": {
                    "actor": actor,
                    "action": plan["action"],
                    "canonicalDocumentId": canonical_id,
                    "subjectDocumentId": subject_id,
                    "closedCandidateIds": closed_ids,
                    "openedCandidateId": opened_candidate_id,
                    "clearedPendingDuplicateLinkDocumentIds": (
                        cleared_pending_link_document_ids
                    ),
                    "result": result,
                    "externalSubmission": "not_executed",
                    "sourceFilesModified": False,
                    "confirmedDuplicateLinksModified": False,
                },
            })

        for document_id in sorted(affected_document_ids):
            open_candidates = self.ledger.list_duplicate_candidates(
                status=("pending", "in_review"),
                document_id=document_id,
                limit=100,
            )
            document = self.ledger.get_document(document_id)
            if not document:
                continue
            if not open_candidates and not document.get("duplicate_of_document_id"):
                for item in _open_reviews(document):
                    if str(item.get("reason") or "") != "duplicate_candidate":
                        continue
                    self.ledger.resolve_review_item(
                        int(item["id"]),
                        status="resolved",
                        resolution=(
                            "Resolved because current structured evidence no longer "
                            "supports an open duplicate candidate."
                        ),
                        corrected_data={
                            "actor": actor,
                            "externalSubmission": "not_executed",
                        },
                    )
                    summary["resolvedReviewItems"] += 1

            refreshed = self.ledger.get_document(document_id) or document
            remaining_reviews = _open_reviews(refreshed)
            metadata = dict(refreshed.get("metadata") or {})
            processing = dict(metadata.get("processing") or {})
            processing["reviewReasons"] = _dedupe([
                str(item.get("reason") or "")
                for item in remaining_reviews
            ])
            if not open_candidates:
                processing["duplicateMatch"] = None
            metadata["processing"] = processing
            update = {"metadata": metadata}
            if str(refreshed.get("processing_status") or "") == "needs_review":
                update["processingStatus"] = (
                    "needs_review" if remaining_reviews else "processed"
                )
            self.ledger.update_document(document_id, update)
            LocalBookkeepingRecordService(
                self.ledger,
                self.config,
            ).upsert_from_document(document_id)

        summary["affectedDocuments"] = len(affected_document_ids)
        self.ledger.record_audit_event({
            "action": "local_processing.duplicate_candidate_reassessment_completed",
            "entityType": "duplicate_candidate",
            "details": dict(summary),
        })
        return summary

    def trusted_category_suggestion_candidates(self, limit: int = 500) -> list:
        return trusted_category_suggestion_candidates(
            self.ledger,
            self.config,
            limit=limit,
        )

    def apply_trusted_category_suggestions(self, limit: int = 500) -> Dict[str, Any]:
        """Apply exact vendor taxonomy matches while preserving every other gate."""
        candidates = self.trusted_category_suggestion_candidates(limit=limit)
        pre_mutation_backup = None
        if candidates:
            try:
                backup_service = LocalBackupService(self.ledger, self.config)
                backup = backup_service.create_backup(
                    note=(
                        "Automatic pre-mutation backup before applying "
                        "trusted exact-vendor category suggestions"
                    ),
                )
                inspection = backup_service.inspect_backup(str(backup["backupPath"]))
                if not inspection.get("success") or inspection.get("status") != "valid":
                    raise RuntimeError("The pre-mutation backup did not pass validation.")
                manifest = inspection.get("manifest") or {}
                pre_mutation_backup = {
                    "backupFilename": backup.get("backupFilename"),
                    "status": inspection.get("status"),
                    "ledgerSha256": manifest.get("ledgerSha256"),
                    "ledgerBytes": manifest.get("ledgerBytes"),
                }
            except Exception as exc:
                self.ledger.record_audit_event({
                    "action": "local_processing.trusted_category_batch_blocked",
                    "entityType": "bookkeeping_document",
                    "details": {
                        "candidateCount": len(candidates),
                        "reason": "pre_mutation_backup_failed",
                        "errorType": type(exc).__name__,
                        "externalSubmission": "not_executed",
                    },
                })
                raise RuntimeError(
                    "Trusted category automation was blocked because its "
                    "pre-mutation backup could not be verified."
                ) from exc
        summary = {
            "candidates": len(candidates),
            "updatedDocuments": 0,
            "resolvedReviewItems": 0,
            "stillNeedsReview": 0,
            "readyDocuments": 0,
            "documentIds": [],
            "preMutationBackup": pre_mutation_backup,
            "externalSubmission": "not_executed",
        }
        for candidate in candidates:
            document = candidate["document"]
            suggestion = candidate["suggestion"]
            document_id = int(document["id"])
            category = str(suggestion["category"])
            confidence_score = float(suggestion["confidenceScore"])
            metadata = dict(document.get("metadata") or {})
            processing = dict(metadata.get("processing") or {})
            open_reviews = self.ledger.list_review_items(
                status=("pending", "in_review"),
                document_id=document_id,
                limit=100,
            )
            remaining_reasons = [
                str(item.get("reason") or "")
                for item in open_reviews
                if item.get("reason") not in {
                    "low_confidence_categorization",
                    "manual_review_category",
                }
            ]
            automation_evidence = {
                "policy": suggestion["automationPolicy"],
                "source": suggestion["source"],
                "matchPolicy": suggestion["matchPolicy"],
                "matchedVendor": suggestion["matchedVendor"],
                "category": category,
                "confidenceScore": confidence_score,
                "threshold": suggestion["automationThreshold"],
                "rationale": suggestion["rationale"],
                "appliedAt": _now(),
                "externalSubmission": "not_executed",
            }
            processing.update({
                "category": category,
                "confidenceScore": confidence_score,
                "reviewReasons": _dedupe(remaining_reasons),
                "trustedCategoryAutomation": automation_evidence,
            })
            metadata["processing"] = processing
            self.ledger.update_document(document_id, {
                "category": category,
                "confidenceScore": confidence_score,
                "metadata": metadata,
            })
            self._replace_trusted_category_extracted_field(
                document_id,
                category,
                confidence_score,
                automation_evidence,
            )
            resolved_ids = []
            for review_item in candidate["reviewItems"]:
                review_item_id = int(review_item["id"])
                self.ledger.resolve_review_item(
                    review_item_id,
                    status="resolved",
                    resolution=(
                        "Resolved by FAB's trusted exact-vendor category policy; "
                        "all non-category review and downstream approval gates remain active."
                    ),
                    corrected_data={
                        "automation": automation_evidence,
                        "category": category,
                    },
                )
                resolved_ids.append(review_item_id)

            remaining_reviews = self.ledger.list_review_items(
                status=("pending", "in_review"),
                document_id=document_id,
                limit=100,
            )
            processing_status = "needs_review" if remaining_reviews else "processed"
            self.ledger.update_document(document_id, {"processingStatus": processing_status})
            record = LocalBookkeepingRecordService(
                self.ledger,
                self.config,
            ).upsert_from_document(document_id)
            self.ledger.record_audit_event({
                "action": "local_processing.trusted_category_applied",
                "entityType": "bookkeeping_document",
                "entityId": str(document_id),
                "details": {
                    "previousCategory": document.get("category"),
                    "category": category,
                    "confidenceScore": confidence_score,
                    "policy": suggestion["automationPolicy"],
                    "resolvedReviewItemIds": resolved_ids,
                    "remainingReviewItemIds": [
                        int(item["id"]) for item in remaining_reviews
                    ],
                    "processingStatus": processing_status,
                    "bookkeepingRecordId": record.get("recordId"),
                    "externalSubmission": "not_executed",
                },
            })
            summary["updatedDocuments"] += 1
            summary["resolvedReviewItems"] += len(resolved_ids)
            summary["stillNeedsReview"] += int(bool(remaining_reviews))
            summary["readyDocuments"] += int(not remaining_reviews)
            summary["documentIds"].append(document_id)
        if candidates:
            self.ledger.record_audit_event({
                "action": "local_processing.trusted_category_batch_completed",
                "entityType": "bookkeeping_document",
                "details": dict(summary),
            })
        return summary

    def _replace_trusted_category_extracted_field(
        self,
        document_id: int,
        category: str,
        confidence_score: float,
        automation_evidence: Dict[str, Any],
    ) -> None:
        fields = [
            {
                "fieldName": field.get("field_name"),
                "value": field.get("field_value"),
                "confidenceScore": field.get("confidence_score"),
                "source": field.get("source"),
                "provenance": field.get("provenance") or {},
            }
            for field in self.ledger.list_extracted_fields(document_id=document_id, limit=500)
            if field.get("source") == "local_processing"
            and field.get("field_name") != "category"
        ]
        fields.append({
            "fieldName": "category",
            "value": category,
            "confidenceScore": confidence_score,
            "source": "local_processing",
            "provenance": {
                "stage": "trusted_category_automation",
                "fieldSource": automation_evidence["source"],
                "policy": automation_evidence["policy"],
                "matchPolicy": automation_evidence["matchPolicy"],
                "matchedVendor": automation_evidence["matchedVendor"],
                "threshold": automation_evidence["threshold"],
                "rationale": automation_evidence["rationale"],
            },
        })
        self.ledger.replace_extracted_fields(document_id, fields)

    def reprocess_incomplete(
        self,
        limit: int = 25,
        actor: str = "fab_local_processing",
        create_backup: bool = True,
    ) -> Dict[str, Any]:
        """Retry blank-OCR review records once without crossing approval gates."""
        bounded_limit = max(1, min(_safe_int(limit) or 25, 100))
        incomplete = [
            document
            for document in self.ledger.list_documents(status="needs_review", limit=500)
            if not str(document.get("ocr_text") or "").strip()
        ]
        previously_attempted = [
            document
            for document in incomplete
            if _ocr_recovery_version(document) == OCR_RECOVERY_VERSION
        ]
        missing_source = [
            document
            for document in incomplete
            if _ocr_recovery_version(document) != OCR_RECOVERY_VERSION
            and not _has_source_file(document)
        ]
        documents = [
            document
            for document in incomplete
            if _ocr_recovery_version(document) != OCR_RECOVERY_VERSION
            and _has_source_file(document)
        ][:bounded_limit]
        summary: Dict[str, Any] = {
            "candidates": len(incomplete),
            "requested": len(documents),
            "reprocessed": 0,
            "ocrRecovered": 0,
            "stillEmpty": 0,
            "processed": 0,
            "needsReview": 0,
            "failed": 0,
            "skipped": len(previously_attempted) + len(missing_source),
            "skippedPreviouslyAttempted": len(previously_attempted),
            "skippedMissingSource": len(missing_source),
            "externalSubmission": "not_executed",
            "sourceFilesModified": False,
            "documents": [],
        }
        if documents and create_backup:
            backup = LocalBackupService(self.ledger, self.config).create_backup(
                note="Automatic pre-recovery backup before blank OCR reprocessing"
            )
            manifest = backup.get("manifest") or {}
            summary["backup"] = {
                "status": backup.get("status"),
                "backupFilename": backup.get("backupFilename"),
                "ledgerSha256": manifest.get("ledgerSha256"),
                "ledgerBytes": manifest.get("ledgerBytes"),
            }

        for document in documents:
            document_id = int(document["id"])
            attempted_at = _now()
            self._record_ocr_recovery(document_id, document, {
                "version": OCR_RECOVERY_VERSION,
                "actor": actor,
                "attemptedAt": attempted_at,
                "previousStatus": document.get("processing_status"),
                "status": "running",
            })
            result = self.process_document(document_id)
            refreshed = self.ledger.get_document(document_id) or document
            recovered = bool(str(refreshed.get("ocr_text") or "").strip())
            self._record_ocr_recovery(document_id, refreshed, {
                "version": OCR_RECOVERY_VERSION,
                "actor": actor,
                "attemptedAt": attempted_at,
                "completedAt": _now(),
                "previousStatus": document.get("processing_status"),
                "status": "recovered" if recovered else "still_empty",
                "ocrTextLength": len(str(refreshed.get("ocr_text") or "")),
                "ocrStrategy": (refreshed.get("metadata") or {}).get("processing", {}).get("ocrStrategy"),
            })
            status = result.get("status")
            summary["reprocessed"] += 1
            summary["ocrRecovered" if recovered else "stillEmpty"] += 1
            if result.get("skipped"):
                summary["skipped"] += 1
            elif status == "processed":
                summary["processed"] += 1
            elif status == "failed":
                summary["failed"] += 1
            else:
                summary["needsReview"] += 1
            summary["documents"].append({
                **result,
                "ocrRecovered": recovered,
                "ocrTextLength": len(str(refreshed.get("ocr_text") or "")),
            })

        self.ledger.record_audit_event({
            "action": "local_processing.incomplete_ocr_reprocessed",
            "entityType": "bookkeeping_document",
            "details": {
                "actor": actor,
                "recoveryVersion": OCR_RECOVERY_VERSION,
                "candidates": summary["candidates"],
                "requested": summary["requested"],
                "reprocessed": summary["reprocessed"],
                "ocrRecovered": summary["ocrRecovered"],
                "stillEmpty": summary["stillEmpty"],
                "processed": summary["processed"],
                "needsReview": summary["needsReview"],
                "failed": summary["failed"],
                "skipped": summary["skipped"],
                "skippedPreviouslyAttempted": summary["skippedPreviouslyAttempted"],
                "skippedMissingSource": summary["skippedMissingSource"],
                "backup": summary.get("backup"),
                "externalSubmission": "not_executed",
                "sourceFilesModified": False,
            },
        })
        return summary

    def reprocess_review_queue(
        self,
        limit: int = 25,
        actor: str = "fab_local_processing",
        create_backup: bool = True,
    ) -> Dict[str, Any]:
        """Reassess machine-gated documents from retained OCR without rerunning OCR."""
        bounded_limit = max(1, min(_safe_int(limit) or 25, 100))
        review_documents = [
            self.ledger.get_document(int(document["id"])) or document
            for document in self.ledger.list_documents(status="needs_review", limit=500)
        ]
        candidates = [
            document
            for document in review_documents
            if _eligible_for_stored_ocr_reassessment(document)
        ]
        previously_attempted = [
            document
            for document in candidates
            if _stored_ocr_reassessment_version(document) == STORED_OCR_REASSESSMENT_VERSION
        ]
        documents = [
            document
            for document in candidates
            if _stored_ocr_reassessment_version(document) != STORED_OCR_REASSESSMENT_VERSION
        ][:bounded_limit]
        summary: Dict[str, Any] = {
            "candidates": len(candidates),
            "requested": len(documents),
            "reprocessed": 0,
            "processed": 0,
            "needsReview": 0,
            "failed": 0,
            "skipped": len(review_documents) - len(candidates) + len(previously_attempted),
            "skippedPreviouslyAttempted": len(previously_attempted),
            "reviewItemsBefore": _open_review_count(documents),
            "reviewItemsAfter": 0,
            "resolvedReviewItems": 0,
            "openedReviewItems": 0,
            "externalSubmission": "not_executed",
            "sourceFilesModified": False,
            "ocrRerun": False,
            "documents": [],
        }
        if documents and create_backup:
            try:
                backup_service = LocalBackupService(self.ledger, self.config)
                backup = backup_service.create_backup(
                    note=(
                        "Automatic pre-reassessment backup before stored OCR "
                        "review reprocessing"
                    )
                )
                inspection = backup_service.inspect_backup(str(backup["backupPath"]))
                if not inspection.get("success") or inspection.get("status") != "valid":
                    raise RuntimeError("The pre-reassessment backup did not pass validation.")
                manifest = inspection.get("manifest") or {}
                if not manifest.get("ledgerSha256") or not manifest.get("ledgerBytes"):
                    raise RuntimeError(
                        "The pre-reassessment backup is not checksum-bound."
                    )
                summary["backup"] = {
                    "status": inspection.get("status"),
                    "backupFilename": backup.get("backupFilename"),
                    "ledgerSha256": manifest.get("ledgerSha256"),
                    "ledgerBytes": manifest.get("ledgerBytes"),
                }
            except Exception as exc:
                self.ledger.record_audit_event({
                    "action": "local_processing.review_queue_reassessment_blocked",
                    "entityType": "bookkeeping_document",
                    "details": {
                        "actor": actor,
                        "candidateCount": len(candidates),
                        "requested": len(documents),
                        "reason": str(exc),
                        "externalSubmission": "not_executed",
                        "sourceFilesModified": False,
                    },
                })
                raise RuntimeError(
                    "Stored OCR reassessment requires a validated, "
                    "checksum-bound ledger backup."
                ) from exc

        for document in documents:
            document_id = int(document["id"])
            before_open_review_ids = {
                int(item["id"])
                for item in _open_reviews(document)
            }
            before_count = len(before_open_review_ids)
            result = self.process_document(document_id, reuse_stored_ocr=True)
            refreshed = self.ledger.get_document(document_id) or document
            after_open_review_ids = {
                int(item["id"])
                for item in _open_reviews(refreshed)
            }
            after_count = len(after_open_review_ids)
            resolved_review_item_ids = sorted(
                before_open_review_ids - after_open_review_ids
            )
            opened_review_item_ids = sorted(
                after_open_review_ids - before_open_review_ids
            )
            reassessment = {
                "version": STORED_OCR_REASSESSMENT_VERSION,
                "actor": actor,
                "completedAt": _now(),
                "previousStatus": document.get("processing_status"),
                "status": result.get("status"),
                "reviewItemsBefore": before_count,
                "reviewItemsAfter": after_count,
                "resolvedReviewItems": len(resolved_review_item_ids),
                "resolvedReviewItemIds": resolved_review_item_ids,
                "openedReviewItems": len(opened_review_item_ids),
                "openedReviewItemIds": opened_review_item_ids,
                "ocrRerun": False,
                "externalSubmission": "not_executed",
            }
            self.ledger.update_document(document_id, {
                "metadata": self._metadata(
                    refreshed,
                    {"processing": {"storedOcrReassessment": reassessment}},
                ),
            })
            summary["reprocessed"] += 1
            summary["reviewItemsAfter"] += after_count
            summary["resolvedReviewItems"] += reassessment["resolvedReviewItems"]
            summary["openedReviewItems"] += reassessment["openedReviewItems"]
            status = result.get("status")
            if status == "processed":
                summary["processed"] += 1
            elif status == "failed":
                summary["failed"] += 1
            else:
                summary["needsReview"] += 1
            summary["documents"].append({
                **result,
                "reviewItemsBefore": before_count,
                "reviewItemsAfter": after_count,
                "resolvedReviewItems": reassessment["resolvedReviewItems"],
                "resolvedReviewItemIds": resolved_review_item_ids,
                "openedReviewItems": reassessment["openedReviewItems"],
                "openedReviewItemIds": opened_review_item_ids,
            })

        self.ledger.record_audit_event({
            "action": "local_processing.review_queue_reassessed",
            "entityType": "bookkeeping_document",
            "details": {
                key: value
                for key, value in summary.items()
                if key != "documents"
            },
        })
        return summary

    def backfill_document_types(self, limit: int = 500) -> Dict[str, Any]:
        """Classify stored OCR evidence without rerunning OCR or changing categories."""
        documents = self.ledger.list_documents(limit=limit)
        summary = {
            "requested": len(documents),
            "evaluated": 0,
            "classified": 0,
            "unknown": 0,
            "alreadyClassified": 0,
            "conflicts": 0,
            "reviewQueued": 0,
            "externalSubmission": "not_executed",
        }
        for document in documents:
            metadata = dict(document.get("metadata") or {})
            processing = dict(metadata.get("processing") or {})
            existing = processing.get("documentTypeClassification") or {}
            if existing.get("classifier") == self.document_type_classifier.CLASSIFIER_VERSION:
                summary["alreadyClassified"] += 1
                continue
            summary["evaluated"] += 1

            classification = self.document_type_classifier.classify(
                document.get("ocr_text", ""),
                document.get("extracted_data") or {},
            )
            classified_type = str(classification.get("documentType") or "unknown")
            current_type = str(document.get("document_type") or "unknown").strip().lower()
            processing["documentTypeClassification"] = classification
            non_posting = is_non_posting_document_type(classified_type)
            active_review_reasons = []
            if non_posting:
                active_review_reasons.append("non_posting_document_type")
                if any(term in str(document.get("ocr_text") or "").lower() for term in SENSITIVE_REVIEW_TERMS):
                    active_review_reasons.append("sensitive_government_document")
                if document.get("duplicate_of_document_id"):
                    active_review_reasons.append("duplicate_candidate")
                processing.update({
                    "category": "Supporting Evidence",
                    "confidenceScore": 1.0,
                    "reviewReasons": _dedupe(active_review_reasons),
                })
            elif classification.get("reviewRequired"):
                active_review_reasons.append("credit_note_posting_review")
                processing["reviewReasons"] = _dedupe([
                    *[
                        reason
                        for reason in list(processing.get("reviewReasons") or [])
                        if reason != "non_posting_document_type"
                    ],
                    *active_review_reasons,
                ])
                if str(document.get("category") or "").strip().lower() == "supporting evidence":
                    processing.update({
                        "category": "Manual Review",
                        "confidenceScore": 0.0,
                    })
            metadata["processing"] = processing
            update_payload: Dict[str, Any] = {"metadata": metadata}
            applied_type = current_type
            conflict = False

            if classified_type == "unknown":
                summary["unknown"] += 1
            elif current_type in {"", "csv", "image", "pdf", "text", "unknown"}:
                applied_type = classified_type
                extracted_data = dict(document.get("extracted_data") or {})
                extracted_data["document_type"] = classified_type
                update_payload.update({
                    "documentType": classified_type,
                    "extractedData": extracted_data,
                })
                summary["classified"] += 1
            elif current_type == classified_type:
                summary["classified"] += 1
            else:
                conflict = True
                summary["conflicts"] += 1

            if non_posting and not conflict:
                extracted_data = dict(document.get("extracted_data") or {})
                extracted_data["document_type"] = classified_type
                update_payload.update({
                    "category": "Supporting Evidence",
                    "confidenceScore": 1.0,
                    "extractedData": extracted_data,
                })
            elif (
                classified_type == "credit_note"
                and not conflict
                and str(document.get("category") or "").strip().lower() == "supporting evidence"
            ):
                extracted_data = dict(document.get("extracted_data") or {})
                extracted_data["document_type"] = classified_type
                update_payload.update({
                    "category": "Manual Review",
                    "confidenceScore": 0.0,
                    "extractedData": extracted_data,
                })

            self.ledger.update_document(int(document["id"]), update_payload)
            if classified_type != "unknown" and not conflict:
                self._replace_document_type_extracted_field(document, classification)
                LocalBookkeepingRecordService(self.ledger, self.config).upsert_from_document(int(document["id"]))
                if non_posting:
                    self._resolve_inactive_processing_reviews(
                        int(document["id"]),
                        active_review_reasons,
                        actor="document_type_backfill",
                    )

            if classification.get("reviewRequired"):
                review_reason = (
                    "non_posting_document_type"
                    if non_posting
                    else "credit_note_posting_review"
                )
                before_count = len(self.ledger.list_review_items(document_id=int(document["id"]), limit=100))
                self._queue_review(
                    document,
                    review_reason,
                    _review_detail(review_reason, {}, "", 0.0),
                    corrected_data={"documentTypeClassification": classification},
                )
                after_count = len(self.ledger.list_review_items(document_id=int(document["id"]), limit=100))
                summary["reviewQueued"] += int(after_count > before_count)
                if not non_posting and not conflict:
                    preserved_reasons = [
                        str(item.get("reason") or "")
                        for item in self.ledger.list_review_items(
                            document_id=int(document["id"]),
                            limit=100,
                        )
                        if item.get("status") in {"pending", "in_review"}
                        and item.get("reason") != "non_posting_document_type"
                    ]
                    self._resolve_inactive_processing_reviews(
                        int(document["id"]),
                        preserved_reasons,
                        actor="document_type_backfill",
                    )
                if not conflict:
                    LocalBookkeepingRecordService(
                        self.ledger,
                        self.config,
                    ).upsert_from_document(int(document["id"]))
            if conflict:
                before_count = len(self.ledger.list_review_items(document_id=int(document["id"]), limit=100))
                self._queue_review(
                    document,
                    "document_type_conflict",
                    f"Stored document type {current_type!r} conflicts with classifier result {classified_type!r}.",
                    corrected_data={
                        "storedDocumentType": current_type,
                        "documentTypeClassification": classification,
                    },
                )
                after_count = len(self.ledger.list_review_items(document_id=int(document["id"]), limit=100))
                summary["reviewQueued"] += int(after_count > before_count)

            self.ledger.record_audit_event({
                "action": "local_processing.document_type_backfilled",
                "entityType": "bookkeeping_document",
                "entityId": str(document["id"]),
                "details": {
                    "previousDocumentType": current_type,
                    "appliedDocumentType": applied_type,
                    "classification": classification,
                    "conflict": conflict,
                    "externalSubmission": "not_executed",
                },
            })

        if summary["evaluated"]:
            self.ledger.record_audit_event({
                "action": "local_processing.document_type_backfill_completed",
                "entityType": "bookkeeping_document",
                "details": dict(summary),
            })
        return summary

    def _replace_document_type_extracted_field(
        self,
        document: Dict[str, Any],
        classification: Dict[str, Any],
    ) -> None:
        fields = [
            {
                "fieldName": field.get("field_name"),
                "value": field.get("field_value"),
                "confidenceScore": field.get("confidence_score"),
                "provenance": field.get("provenance") or {},
            }
            for field in document.get("extracted_fields") or []
            if field.get("source") == "local_processing" and field.get("field_name") != "document_type"
        ]
        fields.append({
            "fieldName": "document_type",
            "value": classification.get("documentType"),
            "confidenceScore": classification.get("confidenceScore"),
            "provenance": {
                "stage": "document_type_backfill",
                "fieldSource": "document_type_classifier",
                "classifier": classification.get("classifier"),
                "evidence": classification.get("evidence") or [],
                "postingEligible": bool(classification.get("postingEligible")),
                "evidencePriority": classification.get("evidencePriority"),
            },
        })
        self.ledger.replace_extracted_fields(int(document["id"]), fields)

    def retry_failed(self, limit: int = 25, actor: str = "fab_local_processing") -> Dict[str, Any]:
        documents = self.ledger.list_documents(status="failed", limit=limit)
        summary: Dict[str, Any] = {
            "requested": len(documents),
            "retried": 0,
            "processed": 0,
            "needsReview": 0,
            "failed": 0,
            "skipped": 0,
            "documents": [],
        }
        for document in documents:
            result = self.retry_document(int(document["id"]), actor=actor)
            status = result.get("status")
            summary["retried"] += 1
            if result.get("skipped"):
                summary["skipped"] += 1
            elif status == "processed":
                summary["processed"] += 1
            elif status == "failed":
                summary["failed"] += 1
            else:
                summary["needsReview"] += 1
            summary["documents"].append(result)

        self.ledger.record_audit_event({
            "action": "local_processing.retry_failed_completed",
            "entityType": "bookkeeping_document",
            "details": {
                "actor": actor,
                "requested": summary["requested"],
                "retried": summary["retried"],
                "processed": summary["processed"],
                "needsReview": summary["needsReview"],
                "failed": summary["failed"],
                "skipped": summary["skipped"],
            },
        })
        return summary

    def retry_document(self, document_id: int, actor: str = "fab_local_processing") -> Dict[str, Any]:
        document = self.ledger.get_document(document_id)
        if not document:
            return {"documentId": document_id, "status": "not_found", "error": "Document not found"}
        if document.get("processing_status") != "failed":
            return {
                "documentId": document_id,
                "status": document.get("processing_status"),
                "skipped": True,
                "reason": "document_not_failed",
            }
        retry_count = self._mark_retry_started(document, actor)
        result = self.process_document(document_id)
        result["retry"] = True
        result["retryCount"] = retry_count
        if result.get("status") != "failed":
            self._resolve_processing_failed_reviews(document_id, actor)
        return result

    def process_document(
        self,
        document_id: int,
        *,
        reuse_stored_ocr: bool = False,
    ) -> Dict[str, Any]:
        document = self.ledger.get_document_for_processing(document_id)
        if not document:
            return {"documentId": document_id, "status": "not_found", "error": "Document not found"}

        pending_duplicate_review = any(
            str(item.get("reason") or "") == "duplicate_candidate"
            for item in _open_reviews(document)
        )
        allow_duplicate_reassessment = reuse_stored_ocr and pending_duplicate_review
        if document.get("duplicate_of_document_id") and not allow_duplicate_reassessment:
            self._queue_review(
                document,
                "duplicate_candidate",
                f"Document duplicates #{document['duplicate_of_document_id']}; resolve duplicate before processing.",
            )
            record_result = LocalBookkeepingRecordService(self.ledger, self.config).upsert_from_document(document_id)
            return {
                "documentId": document_id,
                "status": document.get("processing_status", "needs_review"),
                "skipped": True,
                "reviewReasons": ["duplicate_candidate"],
                "bookkeepingRecordId": record_result.get("recordId"),
            }

        path = document.get("storage_path")
        if not path or not os.path.exists(path):
            return self._needs_review(
                document,
                ["missing_source_file"],
                "Source file is missing or not configured for this ledger document.",
            )

        try:
            if reuse_stored_ocr and str(document.get("ocr_text") or "").strip():
                processed_data = self._process_stored_ocr(document)
            else:
                with tempfile.TemporaryDirectory(prefix="fab-process-") as work_dir:
                    snapshot_path = _snapshot_source_file(document, path, work_dir)
                    processed_data = self._process_path(snapshot_path, document)
                    processed_data["document_path"] = path
            extracted_data = _sanitize_extracted_data(processed_data.get("extracted_data") or {})
            document_type_classification = self.document_type_classifier.classify(
                processed_data.get("ocr_text", ""),
                extracted_data,
            )
            provider_evidence_policy = _provider_evidence_policy(document)
            if provider_evidence_policy.get("postingEligible") is False:
                document_type_classification = {
                    "documentType": "supporting_document",
                    "confidenceScore": 1.0,
                    "evidence": [
                        f"provider_policy:{provider_evidence_policy.get('evidenceRole') or 'non_posting'}"
                    ],
                    "postingEligible": False,
                    "reviewRequired": False,
                    "evidencePriority": 10,
                    "classifier": "provider_evidence_policy_v1",
                    "sourcePolicy": provider_evidence_policy,
                }
            semantic_document_type = str(document_type_classification.get("documentType") or "unknown")
            if semantic_document_type != "unknown":
                extracted_data["document_type"] = semantic_document_type
            else:
                semantic_document_type = str(document.get("document_type") or "unknown")
            credit_note_amount_normalization = (
                _normalize_credit_note_evidence_amounts(extracted_data, processed_data)
                if semantic_document_type == "credit_note"
                else None
            )
            processed_data["document_type_classification"] = document_type_classification
            processed_data["credit_note_amount_normalization"] = credit_note_amount_normalization
            processed_data["extracted_data"] = extracted_data
            non_posting = is_non_posting_document_type(semantic_document_type)
            if non_posting:
                category = "Supporting Evidence"
                confidence_score = 1.0
                validation = {
                    "is_valid": True,
                    "errors": [],
                    "warnings": ["Document is retained as non-posting supporting evidence."],
                    "reason": "Non-posting document type requires evidence review, not receipt validation.",
                    "blocking": False,
                    "validationType": "supporting_evidence",
                }
            else:
                category_result = self.categorizer.categorize(processed_data)
                category = category_result.get("category", "Manual Review")
                confidence_score = _safe_float(category_result.get("confidence_score"), 0.0)
                applied_rule = self._approved_vendor_category_rule(document, extracted_data)
                if applied_rule and applied_rule.get("conflict"):
                    category = "Manual Review"
                    confidence_score = 0.0
                    processed_data["vendor_category_rule_conflict"] = {
                        "vendorName": extracted_data.get("vendor_name") or document.get("vendor_name"),
                        "targetSystem": applied_rule["target_system"],
                        "ruleIds": applied_rule["rule_ids"],
                        "categories": applied_rule["categories"],
                    }
                elif applied_rule:
                    category = applied_rule["category"]
                    confidence_score = max(confidence_score or 0.0, _safe_float(applied_rule.get("confidence_score"), 1.0) or 1.0)
                    processed_data["applied_vendor_category_rule"] = {
                        "ruleId": applied_rule["id"],
                        "vendorName": applied_rule["vendor_name"],
                        "category": applied_rule["category"],
                        "targetSystem": applied_rule["target_system"],
                        "status": applied_rule["status"],
                    }
                elif trusted_suggestion := trusted_category_automation_candidate(
                    {
                        **document,
                        "vendor_name": (
                            extracted_data.get("vendor_name")
                            or document.get("vendor_name")
                        ),
                        "category": category,
                        "extracted_data": extracted_data,
                    },
                    self.config,
                ):
                    category = trusted_suggestion["category"]
                    confidence_score = max(
                        confidence_score or 0.0,
                        _safe_float(trusted_suggestion.get("confidenceScore"), 0.0),
                    )
                    processed_data["applied_trusted_category_suggestion"] = trusted_suggestion
                validation = self.validator.validate_receipt(processed_data)
            processed_data["category"] = category
            processed_data["confidence_score"] = confidence_score
            duplicate_match = self._duplicate_match(document, extracted_data, processed_data)
        except SourceFileIntegrityError as exc:
            details = (
                "Source document exceeds its supported processing size limit."
                if exc.reason.endswith("exceeds_limit")
                else "Source file changed or no longer matches the document registered at intake."
            )
            return self._needs_review(
                document,
                [exc.reason],
                details,
            )
        except Exception as exc:
            self.ledger.update_document(document_id, {
                "processingStatus": "failed",
                "metadata": self._metadata(document, {"processingError": str(exc)}),
            })
            self._queue_review(document, "processing_failed", str(exc))
            self.ledger.record_audit_event({
                "action": "local_processing.document_failed",
                "entityType": "bookkeeping_document",
                "entityId": str(document_id),
                "details": {"error": str(exc)},
            })
            record_result = LocalBookkeepingRecordService(self.ledger, self.config).upsert_from_document(document_id)
            return {
                "documentId": document_id,
                "status": "failed",
                "error": str(exc),
                "reviewReasons": ["processing_failed"],
                "bookkeepingRecordId": record_result.get("recordId"),
            }

        review_reasons = self._review_reasons(processed_data, validation, category, confidence_score)
        if allow_duplicate_reassessment:
            review_reasons = _dedupe([*review_reasons, "duplicate_candidate"])
        duplicate_candidate_id = None
        duplicate_candidate_document_id = None
        duplicate_of_document_id = document.get("duplicate_of_document_id")
        duplicate_fingerprint = duplicate_match.get("duplicate_fingerprint")
        if duplicate_match.get("is_duplicate"):
            duplicate_candidate_document_id = duplicate_match.get("matched_document_id")
            duplicate_fingerprint = duplicate_match.get("duplicate_fingerprint")
            duplicate_candidate_id = self.ledger.record_duplicate_candidate({
                "documentId": document_id,
                "candidateDocumentId": duplicate_candidate_document_id,
                "matchType": duplicate_match.get("reason") or "fuzzy_document_match",
                "confidenceScore": duplicate_match.get("confidence_score"),
                "status": "pending",
                "reason": "Duplicate evidence detected after OCR/extraction.",
                "evidence": {
                    "matchedDocumentId": duplicate_candidate_document_id,
                    "confidenceScore": duplicate_match.get("confidence_score"),
                    "reason": duplicate_match.get("reason"),
                    "duplicateFingerprint": duplicate_fingerprint,
                    "vendorName": extracted_data.get("vendor_name"),
                    "transactionDate": extracted_data.get("transaction_date"),
                    "totalAmount": extracted_data.get("total_amount"),
                    "invoiceNumber": extracted_data.get("invoice_number"),
                },
            })
            review_reasons = _dedupe([*review_reasons, "duplicate_candidate"])
        status = "needs_review" if review_reasons else "processed"
        metadata = self._metadata(
            document,
            {
                "processing": {
                    "category": category,
                    "confidenceScore": confidence_score,
                    "ocrStrategy": processed_data.get("ocr_strategy", "standard"),
                    "ocrFallbackPages": _safe_int(processed_data.get("ocr_fallback_pages")),
                    "ocrFallbackRecoveredPages": _safe_int(processed_data.get("ocr_fallback_recovered_pages")),
                    "preprocessing": processed_data.get("preprocessing"),
                    "reviewReasons": review_reasons,
                    "validation": validation,
                    "fieldConfidences": processed_data.get("field_confidences") or {},
                    "fieldEvidence": processed_data.get("field_evidence") or {},
                    "duplicateMatch": duplicate_match if duplicate_match.get("is_duplicate") else None,
                    "appliedVendorCategoryRule": processed_data.get("applied_vendor_category_rule"),
                    "vendorCategoryRuleConflict": processed_data.get("vendor_category_rule_conflict"),
                    "appliedTrustedCategorySuggestion": processed_data.get("applied_trusted_category_suggestion"),
                    "documentTypeClassification": document_type_classification,
                    "providerEvidencePolicy": provider_evidence_policy or None,
                    "creditNoteAmountNormalization": credit_note_amount_normalization,
                }
            },
        )
        self.ledger.update_document(document_id, {
            "processingStatus": status,
            "documentType": semantic_document_type,
            "duplicateFingerprint": duplicate_fingerprint,
            "duplicateOfDocumentId": duplicate_of_document_id,
            "vendorName": extracted_data.get("vendor_name"),
            "category": category,
            "transactionDate": extracted_data.get("transaction_date"),
            "totalAmount": extracted_data.get("total_amount"),
            "vatAmount": extracted_data.get("vat_amount"),
            "confidenceScore": confidence_score,
            "ocrText": processed_data.get("ocr_text", ""),
            "extractedData": {
                **extracted_data,
                "language": processed_data.get("language"),
                "validation": validation,
            },
            "metadata": metadata,
        })
        if reuse_stored_ocr:
            unsupported_amount_fields = [
                field_name
                for field_name, extracted_key in (
                    ("total_amount", "total_amount"),
                    ("vat_amount", "vat_amount"),
                )
                if extracted_data.get(extracted_key) is None
            ]
            self.ledger.clear_document_financial_fields(
                document_id,
                unsupported_amount_fields,
            )
        self.ledger.replace_extracted_fields(
            document_id,
            _extracted_field_records(
                extracted_data,
                processed_data,
                category=category,
                confidence_score=confidence_score,
                validation=validation,
                applied_rule=processed_data.get("applied_vendor_category_rule"),
                applied_trusted_suggestion=processed_data.get(
                    "applied_trusted_category_suggestion"
                ),
            ),
        )
        resolved_review_item_ids = self._resolve_inactive_processing_reviews(
            document_id,
            review_reasons,
        )

        for reason in review_reasons:
            corrected_data = None
            if reason == "duplicate_candidate":
                corrected_data = {
                    "duplicateCandidateId": duplicate_candidate_id,
                    "candidateDocumentId": duplicate_candidate_document_id,
                    "duplicateFingerprint": duplicate_fingerprint,
                }
            self._queue_review(
                {**document, "duplicate_of_document_id": duplicate_of_document_id},
                reason,
                _review_detail(reason, validation, category, confidence_score, duplicate_match=duplicate_match),
                corrected_data=corrected_data,
            )

        record_result = LocalBookkeepingRecordService(self.ledger, self.config).upsert_from_document(document_id)
        self.ledger.record_audit_event({
            "action": "local_processing.document_processed",
            "entityType": "bookkeeping_document",
            "entityId": str(document_id),
            "details": {
                "status": status,
                "category": category,
                "confidenceScore": confidence_score,
                "reviewReasons": review_reasons,
                "validation": validation,
                "duplicateCandidateId": duplicate_candidate_id,
                "duplicateCandidateDocumentId": duplicate_candidate_document_id,
                "duplicateOfDocumentId": duplicate_of_document_id,
                "bookkeepingRecordId": record_result.get("recordId"),
                "appliedVendorCategoryRule": processed_data.get("applied_vendor_category_rule"),
                "vendorCategoryRuleConflict": processed_data.get("vendor_category_rule_conflict"),
                "appliedTrustedCategorySuggestion": processed_data.get("applied_trusted_category_suggestion"),
                "documentTypeClassification": document_type_classification,
                "creditNoteAmountNormalization": credit_note_amount_normalization,
                "ocrStrategy": processed_data.get("ocr_strategy", "standard"),
                "ocrFallbackPages": _safe_int(processed_data.get("ocr_fallback_pages")),
                "ocrFallbackRecoveredPages": _safe_int(processed_data.get("ocr_fallback_recovered_pages")),
                "preprocessing": processed_data.get("preprocessing"),
                "resolvedReviewItemIds": resolved_review_item_ids,
            },
        })
        return {
            "documentId": document_id,
            "status": status,
            "category": category,
            "confidenceScore": confidence_score,
            "reviewReasons": review_reasons,
            "validation": validation,
            "appliedVendorCategoryRule": processed_data.get("applied_vendor_category_rule"),
            "vendorCategoryRuleConflict": processed_data.get("vendor_category_rule_conflict"),
            "appliedTrustedCategorySuggestion": processed_data.get("applied_trusted_category_suggestion"),
            "documentType": semantic_document_type,
            "documentTypeClassification": document_type_classification,
            "creditNoteAmountNormalization": credit_note_amount_normalization,
            "ocrStrategy": processed_data.get("ocr_strategy", "standard"),
            "ocrFallbackPages": _safe_int(processed_data.get("ocr_fallback_pages")),
            "ocrFallbackRecoveredPages": _safe_int(processed_data.get("ocr_fallback_recovered_pages")),
            "preprocessing": processed_data.get("preprocessing"),
            "bookkeepingRecordId": record_result.get("recordId"),
            "resolvedReviewItemIds": resolved_review_item_ids,
        }

    def _duplicate_match(
        self,
        document: Dict[str, Any],
        extracted_data: Dict[str, Any],
        processed_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        candidate = {
            **document,
            "extracted_data": extracted_data,
            "vendor_name": extracted_data.get("vendor_name") or document.get("vendor_name"),
            "transaction_date": extracted_data.get("transaction_date") or document.get("transaction_date"),
            "total_amount": extracted_data.get("total_amount") if extracted_data.get("total_amount") is not None else document.get("total_amount"),
            "vat_amount": extracted_data.get("vat_amount") if extracted_data.get("vat_amount") is not None else document.get("vat_amount"),
            "ocr_text": processed_data.get("ocr_text") or document.get("ocr_text"),
        }
        candidate["duplicate_fingerprint"] = self.duplicate_detector.build_fingerprint(candidate)
        existing_documents = [
            _duplicate_comparison_document(item)
            for item in self.ledger.list_documents(limit=500)
            if 0 < int(item.get("id") or 0) < int(document["id"])
            and not item.get("duplicate_of_document_id")
            and str(item.get("processing_status") or "") not in {"duplicate", "failed"}
        ]
        result = self.duplicate_detector.is_duplicate(candidate, existing_documents)
        result["duplicate_fingerprint"] = candidate["duplicate_fingerprint"]
        return result

    def _approved_vendor_category_rule(
        self,
        document: Dict[str, Any],
        extracted_data: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        vendor_name = str(extracted_data.get("vendor_name") or document.get("vendor_name") or "").strip()
        if not vendor_name:
            return None
        target_system = _target_system(document, extracted_data)
        rules = self.ledger.list_vendor_category_rules(
            vendor_name=vendor_name,
            status="approved",
            limit=50,
        )
        if not rules:
            return None
        exact_target = [
            rule for rule in rules
            if str(rule.get("target_system") or "none") == target_system
        ]
        universal = [
            rule for rule in rules
            if str(rule.get("target_system") or "none") == "none"
        ]
        candidates = exact_target or universal
        if not candidates:
            return None
        categories = sorted({str(rule.get("category") or "").strip() for rule in candidates})
        if len(categories) > 1:
            return {
                "conflict": True,
                "target_system": target_system,
                "rule_ids": sorted(int(rule["id"]) for rule in candidates),
                "categories": categories,
            }
        return candidates[0]

    def _process_path(self, path: str, document: Dict[str, Any]) -> Dict[str, Any]:
        if _is_text_document(path, document):
            text = _read_text_file(path)
            return {
                "document_path": path,
                "ocr_text": text,
                "extracted_data": _extract_basic_fields(text),
                "language": "unknown",
            }
        if self._processor_pipeline is None:
            from src.document_processors.processor_pipeline import ProcessorPipeline

            self._processor_pipeline = ProcessorPipeline(self.config)
        return self._processor_pipeline.process_document(path)

    @staticmethod
    def _process_stored_ocr(document: Dict[str, Any]) -> Dict[str, Any]:
        ocr_text = str(document.get("ocr_text") or "")
        extraction = FinancialFieldExtractor().extract(ocr_text)
        existing = document.get("extracted_data")
        if not isinstance(existing, dict):
            existing = {}
        return {
            "document_path": document.get("storage_path"),
            "ocr_text": ocr_text,
            "extracted_data": extraction.get("extracted_data") or {},
            "field_confidences": extraction.get("field_confidences") or {},
            "field_evidence": extraction.get("field_evidence") or {},
            "language": existing.get("language") or "unknown",
            "ocr_confidence": 1.0,
            "ocr_strategy": "stored_ocr_reassessment",
            "ocr_fallback_pages": 0,
            "ocr_fallback_recovered_pages": 0,
        }

    def _review_reasons(
        self,
        processed_data: Dict[str, Any],
        validation: Dict[str, Any],
        category: str,
        confidence_score: float,
    ) -> list:
        reasons = []
        if processed_data.get("vendor_category_rule_conflict"):
            reasons.append("conflicting_approved_vendor_category_rules")
        if not str(processed_data.get("ocr_text") or "").strip():
            reasons.append("empty_ocr_text")
        document_type_classification = processed_data.get("document_type_classification") or {}
        if document_type_classification.get("reviewRequired") and not document_type_classification.get("postingEligible"):
            reasons.append("non_posting_document_type")
            lowered_text = str(processed_data.get("ocr_text") or "").lower()
            if any(term in lowered_text for term in SENSITIVE_REVIEW_TERMS):
                reasons.append("sensitive_government_document")
            return _dedupe(reasons)
        if document_type_classification.get("reviewRequired"):
            reasons.append("credit_note_posting_review")
        if validation.get("blocking"):
            reasons.append("validation_failed")
        if confidence_score < self.review_confidence_threshold:
            reasons.append("low_confidence_categorization")
        if str(category).strip().lower() in {"manual review", "uncategorized", ""}:
            reasons.append("manual_review_category")
        lowered_text = str(processed_data.get("ocr_text") or "").lower()
        if any(term in lowered_text for term in SENSITIVE_REVIEW_TERMS):
            reasons.append("sensitive_government_document")
        return _dedupe(reasons)

    def _needs_review(self, document: Dict[str, Any], reasons: list, details: str) -> Dict[str, Any]:
        document_id = int(document["id"])
        self.ledger.update_document(document_id, {
            "processingStatus": "needs_review",
            "metadata": self._metadata(document, {"processing": {"reviewReasons": reasons}}),
        })
        for reason in reasons:
            self._queue_review(document, reason, details)
        self.ledger.record_audit_event({
            "action": "local_processing.document_needs_review",
            "entityType": "bookkeeping_document",
            "entityId": str(document_id),
            "details": {"reviewReasons": reasons, "details": details},
        })
        record_result = LocalBookkeepingRecordService(self.ledger, self.config).upsert_from_document(document_id)
        return {
            "documentId": document_id,
            "status": "needs_review",
            "reviewReasons": reasons,
            "bookkeepingRecordId": record_result.get("recordId"),
        }

    def _queue_review(
        self,
        document: Dict[str, Any],
        reason: str,
        details: str,
        corrected_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        document_id = int(document["id"])
        for item in self.ledger.list_review_items(document_id=document_id, limit=100):
            if item.get("reason") == reason and item.get("status") in {"pending", "in_review"}:
                return
        review_data = {
            "originalFilename": document.get("original_filename"),
            "sourceDocumentId": document.get("source_document_id"),
        }
        review_data.update({key: value for key, value in (corrected_data or {}).items() if value is not None})
        self.ledger.create_review_item({
            "documentId": document_id,
            "reason": reason,
            "details": details,
            "correctedData": review_data,
        })

    def _mark_retry_started(self, document: Dict[str, Any], actor: str) -> int:
        document_id = int(document["id"])
        metadata = dict(document.get("metadata") or {})
        processing = dict(metadata.get("processing") or {})
        retry_count = _safe_int(processing.get("retryCount")) + 1
        retry_history = list(processing.get("retryHistory") or [])
        retry_history.append({
            "retryCount": retry_count,
            "startedAt": _now(),
            "actor": actor,
            "previousStatus": document.get("processing_status"),
            "previousError": metadata.get("processingError"),
        })
        processing["retryCount"] = retry_count
        processing["retryHistory"] = retry_history[-10:]
        metadata["processing"] = processing
        self.ledger.update_document(document_id, {
            "processingStatus": "imported",
            "metadata": metadata,
        })
        self.ledger.record_audit_event({
            "action": "local_processing.retry_started",
            "entityType": "bookkeeping_document",
            "entityId": str(document_id),
            "details": {
                "actor": actor,
                "retryCount": retry_count,
                "previousError": metadata.get("processingError"),
            },
        })
        return retry_count

    def _resolve_inactive_processing_reviews(
        self,
        document_id: int,
        active_reasons: list,
        actor: str = "fab_local_processing",
    ) -> list:
        active = set(active_reasons)
        resolved_ids = []
        for item in self.ledger.list_review_items(document_id=document_id, limit=100):
            reason = str(item.get("reason") or "")
            if reason not in PROCESSING_REVIEW_REASONS or reason in active:
                continue
            if item.get("status") not in {"pending", "in_review"}:
                continue
            review_item_id = int(item["id"])
            self.ledger.resolve_review_item(
                review_item_id,
                status="resolved",
                resolution="Automatically cleared because reprocessing no longer reports this condition.",
                corrected_data={"actor": actor, "reason": reason},
            )
            resolved_ids.append(review_item_id)
        if resolved_ids:
            self.ledger.record_audit_event({
                "action": "local_processing.stale_reviews_resolved",
                "entityType": "bookkeeping_document",
                "entityId": str(document_id),
                "details": {
                    "actor": actor,
                    "activeReasons": sorted(active),
                    "resolvedReviewItemIds": resolved_ids,
                },
            })
        return resolved_ids

    def _resolve_processing_failed_reviews(self, document_id: int, actor: str) -> None:
        document = self.ledger.get_document(document_id)
        if not document:
            return
        for item in document.get("review_items") or []:
            if item.get("reason") != "processing_failed" or item.get("status") not in {"pending", "in_review"}:
                continue
            self.ledger.resolve_review_item(
                int(item["id"]),
                status="resolved",
                resolution="Processing retry no longer failed.",
                corrected_data={"resolvedBy": actor, "retryResolved": True},
            )
            self.ledger.record_audit_event({
                "action": "local_processing.processing_failed_review_resolved",
                "entityType": "review_item",
                "entityId": str(item["id"]),
                "details": {
                    "actor": actor,
                    "documentId": document_id,
                    "reason": item.get("reason"),
                },
            })

    def _record_ocr_recovery(
        self,
        document_id: int,
        document: Dict[str, Any],
        recovery: Dict[str, Any],
    ) -> None:
        self.ledger.update_document(document_id, {
            "metadata": self._metadata(document, {"processing": {"ocrRecovery": recovery}}),
        })

    @staticmethod
    def _metadata(document: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
        metadata = dict(document.get("metadata") or {})
        for key, value in updates.items():
            if key == "processing" and isinstance(value, dict) and isinstance(metadata.get("processing"), dict):
                merged = dict(metadata.get("processing") or {})
                merged.update(value)
                metadata[key] = merged
            else:
                metadata[key] = value
        return metadata


def _is_text_document(path: str, document: Dict[str, Any]) -> bool:
    extension = os.path.splitext(path)[1].lower()
    mime_type = str(document.get("mime_type") or "").lower()
    return extension in TEXT_EXTENSIONS or mime_type.startswith("text/")


def _ocr_recovery_version(document: Dict[str, Any]) -> str:
    metadata = document.get("metadata") or {}
    processing = metadata.get("processing") or {}
    recovery = processing.get("ocrRecovery") or {}
    return str(recovery.get("version") or "")


def _stored_ocr_reassessment_version(document: Dict[str, Any]) -> str:
    metadata = document.get("metadata") or {}
    processing = metadata.get("processing") or {}
    reassessment = processing.get("storedOcrReassessment") or {}
    return str(reassessment.get("version") or "")


def _eligible_for_stored_ocr_reassessment(document: Dict[str, Any]) -> bool:
    if not str(document.get("ocr_text") or "").strip():
        return False
    if not _has_source_file(document):
        return False
    if document.get("review_corrections"):
        return False
    open_reviews = _open_reviews(document)
    has_pending_duplicate_review = any(
        str(item.get("reason") or "") == "duplicate_candidate"
        for item in open_reviews
    )
    if document.get("duplicate_of_document_id") and not has_pending_duplicate_review:
        return False
    return any(
        str(item.get("reason") or "") in STORED_OCR_REASSESSMENT_REASONS
        for item in open_reviews
    )


def _open_reviews(document: Dict[str, Any]) -> list:
    return [
        item
        for item in document.get("review_items") or []
        if item.get("status") in {"pending", "in_review"}
    ]


def _open_review_count(documents: list) -> int:
    return sum(len(_open_reviews(document)) for document in documents)


def _has_source_file(document: Dict[str, Any]) -> bool:
    path = str(document.get("storage_path") or "").strip()
    return bool(path and os.path.isfile(path))


def _duplicate_comparison_document(document: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(document)
    if not isinstance(normalized.get("extracted_data"), dict):
        normalized["extracted_data"] = {}
    return normalized


class SourceFileIntegrityError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _snapshot_source_file(
    document: Dict[str, Any],
    path: str,
    work_dir: str,
) -> str:
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    registered_root = str(metadata.get("folder") or "").strip()
    resolved_path = os.path.realpath(os.path.abspath(path))
    if registered_root:
        resolved_root = os.path.realpath(os.path.abspath(registered_root))
        try:
            contained = os.path.normcase(os.path.commonpath((resolved_root, resolved_path))) == os.path.normcase(resolved_root)
        except ValueError:
            contained = False
        if not contained:
            raise SourceFileIntegrityError("source_file_outside_registered_root")

    expected_hash = str(
        document.get("content_sha256") or metadata.get("contentSha256") or ""
    ).strip().lower()
    if expected_hash and not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise SourceFileIntegrityError("invalid_registered_content_hash")
    is_text = _is_text_document(path, document)
    max_bytes = MAX_TEXT_DOCUMENT_BYTES if is_text else MAX_SOURCE_DOCUMENT_BYTES
    size_limit_reason = "text_document_exceeds_limit" if is_text else "document_exceeds_limit"

    source_fd = -1
    snapshot_fd = -1
    snapshot_path = ""
    try:
        path_stat = os.stat(path, follow_symlinks=False)
        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
            raise SourceFileIntegrityError("source_file_not_regular")
        if path_stat.st_size > max_bytes:
            raise SourceFileIntegrityError(size_limit_reason)

        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        source_fd = os.open(path, flags)
        opened_stat = os.fstat(source_fd)
        if not _same_file_identity(path_stat, opened_stat):
            raise SourceFileIntegrityError("source_file_changed_during_open")

        suffix = os.path.splitext(path)[1].lower()
        snapshot_fd, snapshot_path = tempfile.mkstemp(prefix="source-", suffix=suffix, dir=work_dir)
        digest = hashlib.sha256()
        byte_count = 0
        with os.fdopen(source_fd, "rb") as source, os.fdopen(snapshot_fd, "wb") as snapshot:
            source_fd = snapshot_fd = -1
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                snapshot.write(chunk)
                byte_count += len(chunk)
                if byte_count > max_bytes:
                    raise SourceFileIntegrityError(size_limit_reason)
            snapshot.flush()
            after_stat = os.fstat(source.fileno())

        current_path_stat = os.stat(path, follow_symlinks=False)
        if (
            stat.S_ISLNK(current_path_stat.st_mode)
            or not _same_file_identity(opened_stat, after_stat)
            or not _same_file_identity(opened_stat, current_path_stat)
            or byte_count != opened_stat.st_size
        ):
            raise SourceFileIntegrityError("source_file_changed_during_snapshot")
        if registered_root:
            current_real_path = os.path.realpath(os.path.abspath(path))
            try:
                contained = os.path.normcase(
                    os.path.commonpath((resolved_root, current_real_path))
                ) == os.path.normcase(resolved_root)
            except ValueError:
                contained = False
            if not contained:
                raise SourceFileIntegrityError("source_file_outside_registered_root")
        if expected_hash and digest.hexdigest() != expected_hash:
            raise SourceFileIntegrityError("source_file_hash_mismatch")
        return snapshot_path
    except Exception:
        if source_fd >= 0:
            try:
                os.close(source_fd)
            except OSError:
                pass
        if snapshot_fd >= 0:
            try:
                os.close(snapshot_fd)
            except OSError:
                pass
        if snapshot_path:
            try:
                os.unlink(snapshot_path)
            except OSError:
                pass
        raise


def _same_file_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev == second.st_dev
        and (not first.st_ino or not second.st_ino or first.st_ino == second.st_ino)
        and first.st_size == second.st_size
        and getattr(first, "st_mtime_ns", int(first.st_mtime * 1_000_000_000))
        == getattr(second, "st_mtime_ns", int(second.st_mtime * 1_000_000_000))
    )


def _read_text_file(path: str) -> str:
    with open(path, "rb") as handle:
        content = handle.read(MAX_TEXT_DOCUMENT_BYTES + 1)
    if len(content) > MAX_TEXT_DOCUMENT_BYTES:
        raise ValueError("text_document_exceeds_limit")
    return content.decode("utf-8", errors="replace")


def _extract_basic_fields(text: str) -> Dict[str, Any]:
    return {
        "vendor_name": _first_match(text, r"(?:vendor|supplier|merchant|leverancier)\s*[:\-]\s*(.+)"),
        "transaction_date": _normalize_date(
            _first_match(text, r"(?:date|datum|transaction date)\s*[:\-]\s*([0-9]{4}[-/][0-9]{2}[-/][0-9]{2}|[0-9]{2}[-/][0-9]{2}[-/][0-9]{4})")
        ),
        "total_amount": _money_value(
            _first_match(text, r"(?:total|totaal|amount|bedrag)\s*[:\-]?\s*([EUReur\s€$£]*[0-9][0-9.,]*)")
        ),
        "currency": _currency(text),
        "vat_amount": _money_value(
            _first_match(text, r"(?:vat|btw)\s*[:\-]?\s*([EUReur\s€$£]*[0-9][0-9.,]*)")
        ),
        "line_items": [],
    }


def _first_match(text: str, pattern: str) -> Optional[str]:
    match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip()


def _normalize_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = value.replace("/", "-")
    if re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", value):
        return value
    match = re.match(r"^([0-9]{2})-([0-9]{2})-([0-9]{4})$", value)
    if match:
        day, month, year = match.groups()
        return f"{year}-{month}-{day}"
    return value


def _money_value(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    cleaned = re.sub(r"[^0-9,.\-]", "", value)
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _currency(text: str) -> Optional[str]:
    if "€" in text or re.search(r"\bEUR\b", text, re.IGNORECASE):
        return "EUR"
    if "$" in text or re.search(r"\bUSD\b", text, re.IGNORECASE):
        return "USD"
    if "£" in text or re.search(r"\bGBP\b", text, re.IGNORECASE):
        return "GBP"
    return None


def _sanitize_extracted_data(extracted_data: Dict[str, Any]) -> Dict[str, Any]:
    sanitized = dict(extracted_data)
    for key in ("vendor_name", "transaction_date", "currency"):
        if sanitized.get(key) is None:
            sanitized[key] = ""
    sanitized["total_amount"] = _safe_float(sanitized.get("total_amount"), None)
    vat_amount = _safe_float(sanitized.get("vat_amount"), None)
    if vat_amount is None:
        sanitized.pop("vat_amount", None)
    else:
        sanitized["vat_amount"] = vat_amount
    return sanitized


def _normalize_credit_note_evidence_amounts(
    extracted_data: Dict[str, Any],
    processed_data: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Keep credit-note evidence positive while the ledger records its credit direction."""
    field_evidence = (
        dict(processed_data.get("field_evidence"))
        if isinstance(processed_data.get("field_evidence"), dict)
        else {}
    )
    normalized_fields = []
    for field_name in ("total_amount", "vat_amount"):
        value = extracted_data.get(field_name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value >= 0:
            continue
        normalized_value = abs(float(value))
        extracted_data[field_name] = normalized_value
        evidence = (
            dict(field_evidence.get(field_name))
            if isinstance(field_evidence.get(field_name), dict)
            else {}
        )
        evidence.update({
            "normalization": "credit_note_absolute_evidence_amount",
            "observedValue": value,
            "normalizedValue": normalized_value,
        })
        field_evidence[field_name] = evidence
        normalized_fields.append(field_name)
    if not normalized_fields:
        return None
    processed_data["field_evidence"] = field_evidence
    return {
        "policy": "credit_note_absolute_evidence_amount",
        "normalizedFields": normalized_fields,
        "ledgerDirection": "credit",
    }


def _extracted_field_records(
    extracted_data: Dict[str, Any],
    processed_data: Dict[str, Any],
    category: str,
    confidence_score: float,
    validation: Dict[str, Any],
    applied_rule: Optional[Dict[str, Any]] = None,
    applied_trusted_suggestion: Optional[Dict[str, Any]] = None,
) -> list:
    extraction_source = (
        "local_text_regex"
        if processed_data.get("document_path")
        and os.path.splitext(str(processed_data.get("document_path")))[1].lower() in TEXT_EXTENSIONS
        else "processor_pipeline"
    )
    base_provenance = {
        "stage": "local_processing",
        "extractionSource": extraction_source,
        "language": processed_data.get("language"),
        "ocrTextPresent": bool(str(processed_data.get("ocr_text") or "").strip()),
        "validationBlocking": bool(validation.get("blocking")),
    }
    field_confidences = (
        processed_data.get("field_confidences")
        if isinstance(processed_data.get("field_confidences"), dict)
        else {}
    )
    field_evidence = (
        processed_data.get("field_evidence")
        if isinstance(processed_data.get("field_evidence"), dict)
        else {}
    )
    records = []
    for field_name in (
        "vendor_name",
        "transaction_date",
        "total_amount",
        "vat_amount",
        "currency",
        "line_items",
    ):
        value = extracted_data.get(field_name)
        if value in (None, "", []):
            continue
        provenance = {
            **base_provenance,
            "fieldSource": "extracted_data",
        }
        if isinstance(field_evidence.get(field_name), dict):
            provenance["evidence"] = field_evidence[field_name]
        records.append({
            "fieldName": field_name,
            "value": value,
            "confidenceScore": _safe_float(
                field_confidences.get(field_name),
                _field_confidence(field_name, value, confidence_score),
            ),
            "provenance": provenance,
        })
    if category:
        category_source = "categorizer"
        if applied_rule:
            category_source = "approved_vendor_rule"
        elif applied_trusted_suggestion:
            category_source = "trusted_category_automation"
        category_provenance = {
            **base_provenance,
            "fieldSource": category_source,
        }
        if applied_rule:
            category_provenance.update({
                "ruleId": applied_rule.get("ruleId"),
                "vendorName": applied_rule.get("vendorName"),
                "targetSystem": applied_rule.get("targetSystem"),
            })
        elif applied_trusted_suggestion:
            category_provenance.update({
                "policy": applied_trusted_suggestion.get("automationPolicy"),
                "source": applied_trusted_suggestion.get("source"),
                "matchPolicy": applied_trusted_suggestion.get("matchPolicy"),
                "matchedVendor": applied_trusted_suggestion.get("matchedVendor"),
                "threshold": applied_trusted_suggestion.get("automationThreshold"),
            })
        records.append({
            "fieldName": "category",
            "value": category,
            "confidenceScore": confidence_score,
            "provenance": category_provenance,
        })
    document_type_classification = processed_data.get("document_type_classification") or {}
    document_type = str(document_type_classification.get("documentType") or "unknown")
    if document_type != "unknown":
        records.append({
            "fieldName": "document_type",
            "value": document_type,
            "confidenceScore": _safe_float(document_type_classification.get("confidenceScore"), 0.0),
            "provenance": {
                **base_provenance,
                "fieldSource": "document_type_classifier",
                "classifier": document_type_classification.get("classifier"),
                "evidence": document_type_classification.get("evidence") or [],
                "postingEligible": bool(document_type_classification.get("postingEligible")),
                "evidencePriority": document_type_classification.get("evidencePriority"),
            },
        })
    return records


def _target_system(document: Dict[str, Any], extracted_data: Dict[str, Any]) -> str:
    return resolve_document_target_system(document, extracted_data, default="none")


def _provider_evidence_policy(document: Dict[str, Any]) -> Dict[str, Any]:
    if str(document.get("source") or "").strip().lower() != "freshdesk":
        return {}
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    provider = (
        metadata.get("providerMetadata")
        if isinstance(metadata.get("providerMetadata"), dict)
        else {}
    )
    evidence_role = str(
        provider.get("evidence_role")
        or provider.get("evidenceRole")
        or ""
    ).strip().lower()
    posting_eligible = provider.get(
        "posting_eligible",
        provider.get("postingEligible"),
    )
    explicitly_non_posting = (
        posting_eligible is False
        or str(posting_eligible or "").strip().lower() in {"0", "false", "no", "off"}
    )
    if evidence_role != "ticket_description" or not explicitly_non_posting:
        return {}
    provenance = (
        provider.get("source_provenance")
        if isinstance(provider.get("source_provenance"), dict)
        else {}
    )
    return {
        "postingEligible": False,
        "evidenceRole": evidence_role,
        "profileId": provider.get("profile_id"),
        "repository": provenance.get("repository"),
        "auditedCommit": provenance.get("auditedCommit"),
        "externalSubmission": "not_executed",
    }


def _field_confidence(field_name: str, value: Any, category_confidence: float) -> float:
    if value in (None, "", []):
        return 0.0
    if field_name == "line_items":
        return 0.75
    if field_name == "category":
        return category_confidence
    return 0.9


def _safe_float(value: Any, default: Optional[float]) -> Optional[float]:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _float_config(config: Dict[str, Any], *keys: str, default: float) -> float:
    for key in keys:
        value = config.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                return default
    return default


def _review_detail(
    reason: str,
    validation: Dict[str, Any],
    category: str,
    confidence_score: float,
    duplicate_match: Optional[Dict[str, Any]] = None,
) -> str:
    if reason == "duplicate_candidate":
        match = duplicate_match or {}
        matched_id = match.get("matched_document_id")
        confidence = match.get("confidence_score")
        if matched_id:
            return f"Possible duplicate of document #{matched_id} ({_format_confidence(confidence)} confidence)."
        return "Possible duplicate detected."
    if reason == "validation_failed":
        return validation.get("reason") or "Validation failed."
    if reason == "low_confidence_categorization":
        return f"Category confidence {confidence_score:.0%} is below the review threshold."
    if reason == "manual_review_category":
        return f"Category requires review: {category}."
    if reason == "empty_ocr_text":
        return "No OCR text was extracted from the document."
    if reason == "sensitive_government_document":
        return "Government, benefits, DigiD, or sensitive administration terms were detected."
    if reason == "non_posting_document_type":
        return "This document type is supporting evidence, not an automatically postable receipt or vendor invoice."
    if reason == "credit_note_posting_review":
        return "Credit note detected. Verify the vendor, date, amount, VAT, and expense category before posting the reversal."
    if reason == "document_type_conflict":
        return "Stored and inferred document types conflict; review before routing."
    return "Review required."


def _format_confidence(value: Any) -> str:
    try:
        return f"{float(value):.0%}"
    except (TypeError, ValueError):
        return "unknown"


def _dedupe(values: list) -> list:
    result = []
    for value in values:
        if value not in result:
            result.append(value)
    return result
