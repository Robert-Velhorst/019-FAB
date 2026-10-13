import math
from datetime import date
from typing import Any, Dict, Optional

from src.document_processors.document_type_classifier import is_non_posting_document_type
from src.operations.local_bookkeeping_records import LocalBookkeepingRecordService
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_reconciliation import FINAL_RECONCILIATION_STATUSES, LocalReconciliationService, _positive_reference_id
from src.operations.local_targets import resolve_document_target_system
from src.validation.financial_consistency import assess_vat_amount, vat_issue_message


APPLIED_REVIEW_STATUSES = {"approved", "resolved"}
OPEN_REVIEW_STATUSES = {"pending", "in_review"}
RECONCILIATION_REVIEW_REASONS = {"reconciliation_candidate", "missing_receipt", "unmatched_document"}
CATEGORY_REVIEW_REASONS = {"low_confidence_categorization", "manual_review_category"}
BATCH_VENDOR_CATEGORY_REASONS = CATEGORY_REVIEW_REASONS
CORRECTABLE_DOCUMENT_TYPES = {
    "bank_statement",
    "credit_note",
    "estimate",
    "government_correspondence",
    "insurance_policy",
    "order_confirmation",
    "receipt",
    "vendor_invoice",
}


class _ReconciliationDecisionRejected(Exception):
    def __init__(self, result: Dict[str, Any]):
        super().__init__(result.get("error") or "Reconciliation decision rejected")
        self.result = result


class LocalReviewService:
    """Apply manual review decisions to documents and local learning records."""

    def __init__(self, ledger: LocalOperationsLedger):
        self.ledger = ledger

    def resolve_review_item(
        self,
        review_item_id: int,
        status: str = "resolved",
        resolution: Optional[str] = None,
        corrections: Optional[Dict[str, Any]] = None,
        learn_rule: bool = True,
        apply_to_matching_vendor: bool = False,
    ) -> Dict[str, Any]:
        try:
            with self.ledger.write_transaction():
                return self._resolve_review_item(
                    review_item_id, status=status, resolution=resolution,
                    corrections=corrections, learn_rule=learn_rule,
                    apply_to_matching_vendor=apply_to_matching_vendor,
                )
        except _ReconciliationDecisionRejected as exc:
            return {**exc.result, "success": False, "reviewItemId": review_item_id}

    def _resolve_review_item(
        self,
        review_item_id: int,
        status: str = "resolved",
        resolution: Optional[str] = None,
        corrections: Optional[Dict[str, Any]] = None,
        learn_rule: bool = True,
        apply_to_matching_vendor: bool = False,
    ) -> Dict[str, Any]:
        review_item = self.ledger.get_review_item(review_item_id)
        if not review_item:
            return {"success": False, "error": "Review item not found", "status": "not_found"}
        if review_item.get("status") not in OPEN_REVIEW_STATUSES:
            return {
                "success": False,
                "error": "Review item is already closed",
                "status": "already_resolved",
                "reviewItemId": review_item_id,
            }

        if review_item.get("reason") in RECONCILIATION_REVIEW_REASONS:
            match_id = _reconciliation_match_id(review_item)
            match = self.ledger.get_reconciliation_match(match_id) if match_id else None
            if not match or match.get("document_id") != review_item.get("document_id"):
                return {
                    "success": False, "status": "stale_evidence",
                    "error": "Review reconciliation evidence is missing or mismatched; refresh before resolving.",
                }
            metadata = match.get("metadata") or {}
            superseded = metadata.get("supersededBy") if isinstance(metadata, dict) else None
            completed_candidate = (review_item.get("reason") == "reconciliation_candidate"
                                   and match.get("status") in FINAL_RECONCILIATION_STATUSES and status == "approved")
            superseded_missing = (review_item.get("reason") == "missing_receipt" and match.get("status") == "resolved"
                                  and isinstance(superseded, dict) and status in APPLIED_REVIEW_STATUSES)
            if completed_candidate or superseded_missing:
                if corrections:
                    return {"success": False, "status": "stale_evidence",
                            "error": "Completed reconciliation cannot accept corrections during review repair."}
                owner_id = match_id if completed_candidate else _positive_reference_id(superseded, ("reconciliationMatchId",))
                owner = self.ledger.get_reconciliation_match(owner_id) if owner_id else None
                if not owner or owner.get("status") not in FINAL_RECONCILIATION_STATUSES:
                    return {"success": False, "status": "stale_evidence",
                            "error": "Completed reconciliation owner is missing; review evidence before repair."}
                result = LocalReconciliationService(self.ledger).resolve_match(owner_id, owner["status"])
                if (not result.get("success")
                        or self.ledger.get_review_item(review_item_id).get("status") != "resolved"):
                    raise _ReconciliationDecisionRejected({"success": False, "status": "stale_evidence",
                                                          "error": "Completed reconciliation review link cannot be verified."})
                return {**result, "reviewItemId": review_item_id, "status": "resolved",
                        "reconciliationResolution": result}
            if _reconciliation_status_for_review(str(review_item["reason"]), status) == "approved":
                validation = LocalReconciliationService(self.ledger).validate_approval(match_id)
                if not validation.get("success"):
                    return validation

        document = self.ledger.get_document(int(review_item["document_id"])) if review_item.get("document_id") else None
        raw_corrections = corrections or {}
        normalized_corrections = normalize_corrections(raw_corrections)
        credit_note_normalization = None
        if document:
            (
                normalized_corrections,
                credit_note_normalization,
                financial_correction_error,
            ) = _validate_financial_corrections(
                document,
                raw_corrections,
                normalized_corrections,
            )
            if financial_correction_error:
                return {
                    "success": False,
                    "error": financial_correction_error["message"],
                    "status": "invalid_financial_correction",
                    "reviewItemId": review_item_id,
                    "documentId": document.get("id"),
                    "field": financial_correction_error.get("field"),
                    "reason": financial_correction_error.get("reason"),
                }
        if document and review_item.get("reason") == "duplicate_candidate":
            open_duplicate_candidates = [
                item
                for item in document.get("duplicate_candidates") or []
                if item.get("status") in OPEN_REVIEW_STATUSES
            ]
            if not normalized_corrections.get("duplicateCandidateId"):
                if len(open_duplicate_candidates) != 1:
                    return {
                        "success": False,
                        "error": (
                            "Select one duplicate candidate before resolving this review"
                            if open_duplicate_candidates
                            else "No open duplicate candidate belongs to this review"
                        ),
                        "status": (
                            "duplicate_candidate_selection_required"
                            if open_duplicate_candidates
                            else "duplicate_candidate_not_found"
                        ),
                    }
                selected_candidate = open_duplicate_candidates[0]
                normalized_corrections["duplicateCandidateId"] = int(selected_candidate["id"])
                if status == "approved" and not normalized_corrections.get("duplicateOfDocumentId"):
                    normalized_corrections["duplicateOfDocumentId"] = (
                        int(selected_candidate["candidate_document_id"])
                        if int(selected_candidate["document_id"]) == int(document["id"])
                        else int(selected_candidate["document_id"])
                    )
            return self._resolve_duplicate_candidate_review(
                review_item=review_item,
                document=document,
                status=status,
                resolution=resolution,
                corrections=normalized_corrections,
            )
        original_data = _document_snapshot(document) if document else {}
        document_update = {}
        rule_id = None
        reconciliation_resolution = None
        duplicate_candidate_resolution = None
        duplicate_decision = None

        if document:
            document_update = _build_document_update(document, normalized_corrections)
            duplicate_decision = _duplicate_decision(review_item, status, normalized_corrections)
            if duplicate_decision == "accepted":
                document_update["processingStatus"] = "duplicate"
                duplicate_candidate_resolution = self.ledger.resolve_duplicate_candidates_for_document(
                    int(document["id"]),
                    "approved",
                    resolution or "Duplicate candidate approved from manual review.",
                )
            elif duplicate_decision == "rejected":
                self.ledger.clear_document_duplicate(int(document["id"]))
                document_update["processingStatus"] = "needs_review"
                duplicate_candidate_resolution = self.ledger.resolve_duplicate_candidates_for_document(
                    int(document["id"]),
                    "rejected",
                    resolution or "Duplicate candidate rejected from manual review.",
                )

            if status in APPLIED_REVIEW_STATUSES and document_update.get("processingStatus") is None:
                document_update["processingStatus"] = "needs_review"

            if document_update:
                self.ledger.update_document(int(document["id"]), document_update)

        recorded_corrections = dict(normalized_corrections)
        if credit_note_normalization:
            recorded_corrections["_creditNoteEvidenceNormalization"] = (
                credit_note_normalization
            )
        correction_payload = {
            "reviewItemId": review_item_id,
            "documentId": review_item.get("document_id"),
            "originalData": original_data,
            "correctedData": recorded_corrections,
            "status": status,
        }
        correction_id = self.ledger.record_review_correction(correction_payload)
        self.ledger.resolve_review_item(
            review_item_id,
            status=status,
            resolution=resolution,
            corrected_data={
                **(review_item.get("corrected_data") or {}
                   if review_item.get("reason") in RECONCILIATION_REVIEW_REASONS else {}),
                "corrections": normalized_corrections,
                "correctionId": correction_id,
                "creditNoteEvidenceNormalization": credit_note_normalization,
            },
        )

        updated_document = self.ledger.get_document(int(review_item["document_id"])) if review_item.get("document_id") else None
        superseded_review_ids = self._resolve_superseded_document_reviews(
            review_item,
            status,
            normalized_corrections,
            updated_document,
        )
        updated_document = self.ledger.get_document(int(review_item["document_id"])) if review_item.get("document_id") else None
        remaining_review_items = [
            item
            for item in (updated_document or {}).get("review_items") or []
            if item.get("status") in OPEN_REVIEW_STATUSES
        ]
        final_processing_status = self._final_processing_status(
            review_item,
            status,
            duplicate_decision,
            remaining_review_items,
        )
        if updated_document and updated_document.get("processing_status") != final_processing_status:
            self.ledger.update_document(int(updated_document["id"]), {"processingStatus": final_processing_status})
            updated_document = self.ledger.get_document(int(updated_document["id"]))
        if learn_rule and status in APPLIED_REVIEW_STATUSES and updated_document:
            rule_id = self._learn_vendor_category_rule(updated_document, review_item_id, correction_id)

        if review_item.get("reason") in RECONCILIATION_REVIEW_REASONS:
            reconciliation_resolution = self._resolve_reconciliation_evidence(
                review_item,
                status,
                resolution,
            )

        bookkeeping_record = None
        if review_item.get("document_id"):
            bookkeeping_record = LocalBookkeepingRecordService(self.ledger).upsert_from_document(
                int(review_item["document_id"])
            )

        self.ledger.record_audit_event({
            "action": "local_review.review_item.resolve",
            "entityType": "review_item",
            "entityId": str(review_item_id),
            "details": {
                "status": status,
                "resolution": resolution,
                "documentId": review_item.get("document_id"),
                "correctionId": correction_id,
                "ruleId": rule_id,
                "reconciliationResolution": reconciliation_resolution,
                "duplicateCandidatesResolved": duplicate_candidate_resolution,
                "supersededReviewItemIds": superseded_review_ids,
                "remainingReviewItemIds": [int(item["id"]) for item in remaining_review_items],
                "processingStatus": final_processing_status,
                "bookkeepingRecordId": bookkeeping_record.get("recordId") if bookkeeping_record else None,
                "corrections": normalized_corrections,
                "creditNoteEvidenceNormalization": credit_note_normalization,
            },
        })
        if normalized_corrections:
            self.ledger.record_audit_event({
                "action": "local_review.correction_applied",
                "entityType": "bookkeeping_document",
                "entityId": str(review_item.get("document_id")),
                "details": {
                    "reviewItemId": review_item_id,
                    "correctionId": correction_id,
                    "before": original_data,
                    "after": normalized_corrections,
                    "creditNoteEvidenceNormalization": credit_note_normalization,
                },
            })

        batch_propagation = None
        if apply_to_matching_vendor:
            batch_propagation = self._apply_vendor_category_to_matching_reviews(
                primary_review_item=review_item,
                primary_document=updated_document,
                status=status,
                resolution=resolution,
                corrections=normalized_corrections,
            )

        return {
            "success": True,
            "reviewItemId": review_item_id,
            "documentId": review_item.get("document_id"),
            "status": status,
            "correctionId": correction_id,
            "ruleId": rule_id,
            "reconciliationResolution": reconciliation_resolution,
            "duplicateCandidatesResolved": duplicate_candidate_resolution,
            "supersededReviewItemIds": superseded_review_ids,
            "remainingReviewItems": remaining_review_items,
            "processingStatus": final_processing_status,
            "bookkeepingRecordId": bookkeeping_record.get("recordId") if bookkeeping_record else None,
            "corrections": normalized_corrections,
            "creditNoteEvidenceNormalization": credit_note_normalization,
            "batchPropagation": batch_propagation,
        }

    def _resolve_duplicate_candidate_review(
        self,
        review_item: Dict[str, Any],
        document: Dict[str, Any],
        status: str,
        resolution: Optional[str],
        corrections: Dict[str, Any],
    ) -> Dict[str, Any]:
        if status not in {"approved", "rejected"}:
            return {
                "success": False,
                "error": "Duplicate candidates must be approved or rejected",
                "status": "invalid_duplicate_decision",
            }

        candidate_id = int(corrections["duplicateCandidateId"])
        candidate = self.ledger.get_duplicate_candidate(candidate_id)
        if not candidate:
            return {
                "success": False,
                "error": "Duplicate candidate not found",
                "status": "duplicate_candidate_not_found",
            }
        if candidate.get("status") not in OPEN_REVIEW_STATUSES:
            return {
                "success": False,
                "error": "Duplicate candidate is already closed",
                "status": "duplicate_candidate_already_resolved",
                "duplicateCandidateId": candidate_id,
            }

        document_id = int(document["id"])
        pair_document_ids = {
            int(candidate["document_id"]),
            int(candidate["candidate_document_id"]),
        }
        if document_id not in pair_document_ids:
            return {
                "success": False,
                "error": "Duplicate candidate does not belong to this review document",
                "status": "duplicate_candidate_mismatch",
            }
        counterpart_document_id = next(
            pair_id for pair_id in pair_document_ids if pair_id != document_id
        )
        accepted = status == "approved"
        if accepted and corrections.get("duplicateOfDocumentId") != counterpart_document_id:
            return {
                "success": False,
                "error": "The canonical document must match the selected duplicate candidate",
                "status": "duplicate_candidate_mismatch",
            }

        review_item_id = int(review_item["id"])
        original_data = _document_snapshot(document)
        decision = "same_transaction" if accepted else "different_transaction"
        candidate_resolution = resolution or (
            f"Confirmed as the same transaction as document #{counterpart_document_id}."
            if accepted
            else f"Confirmed as a different transaction from document #{counterpart_document_id}."
        )
        self.ledger.resolve_duplicate_candidate(
            candidate_id,
            "approved" if accepted else "rejected",
            candidate_resolution,
            evidence={
                "reviewItemId": review_item_id,
                "operatorDecision": decision,
                "counterpartDocumentId": counterpart_document_id,
                "sourceFilesRetained": True,
            },
        )
        duplicate_candidates_resolved = 1

        if accepted:
            for other_candidate in document.get("duplicate_candidates") or []:
                other_candidate_id = int(other_candidate.get("id") or 0)
                if (
                    not other_candidate_id
                    or other_candidate_id == candidate_id
                    or other_candidate.get("status") not in OPEN_REVIEW_STATUSES
                ):
                    continue
                if self.ledger.resolve_duplicate_candidate(
                    other_candidate_id,
                    "rejected",
                    (
                        f"Superseded by approved duplicate candidate #{candidate_id}; "
                        f"document #{counterpart_document_id} is the selected canonical record."
                    ),
                    evidence={
                        "reviewItemId": review_item_id,
                        "operatorDecision": "superseded",
                        "supersededByDuplicateCandidateId": candidate_id,
                        "sourceFilesRetained": True,
                    },
                ):
                    duplicate_candidates_resolved += 1
            self.ledger.update_document(document_id, {
                "duplicateOfDocumentId": counterpart_document_id,
                "processingStatus": "duplicate",
            })
        elif document.get("duplicate_of_document_id") == counterpart_document_id:
            self.ledger.clear_document_duplicate(document_id)

        refreshed_document = self.ledger.get_document(document_id) or document
        remaining_duplicate_candidates = [
            item
            for item in refreshed_document.get("duplicate_candidates") or []
            if item.get("status") in OPEN_REVIEW_STATUSES
        ]
        close_review = accepted or not remaining_duplicate_candidates
        correction_status = status if close_review else "candidate_rejected"
        correction_id = self.ledger.record_review_correction({
            "reviewItemId": review_item_id,
            "documentId": document_id,
            "originalData": original_data,
            "correctedData": corrections,
            "status": correction_status,
        })
        if close_review:
            self.ledger.resolve_review_item(
                review_item_id,
                status=status,
                resolution=candidate_resolution,
                corrected_data={
                    "corrections": corrections,
                    "correctionId": correction_id,
                    "duplicateCandidateId": candidate_id,
                },
            )
        else:
            self.ledger.resolve_review_item(
                review_item_id,
                status="in_review",
                resolution=(
                    f"Candidate #{candidate_id} rejected; "
                    f"{len(remaining_duplicate_candidates)} candidate(s) still require review."
                ),
                corrected_data={
                    "lastDuplicateCandidateDecision": {
                        "duplicateCandidateId": candidate_id,
                        "counterpartDocumentId": counterpart_document_id,
                        "decision": decision,
                        "correctionId": correction_id,
                    },
                },
            )

        updated_document = self.ledger.get_document(document_id) or document
        superseded_review_ids = []
        if accepted:
            superseded_review_ids = self._resolve_superseded_document_reviews(
                review_item,
                status,
                corrections,
                updated_document,
            )
            updated_document = self.ledger.get_document(document_id) or updated_document

        remaining_review_items = [
            item
            for item in updated_document.get("review_items") or []
            if item.get("status") in OPEN_REVIEW_STATUSES
        ]
        if accepted:
            final_processing_status = "duplicate"
        elif remaining_review_items:
            final_processing_status = "needs_review"
        else:
            final_processing_status = "reviewed"
        if updated_document.get("processing_status") != final_processing_status:
            self.ledger.update_document(
                document_id,
                {"processingStatus": final_processing_status},
            )
            updated_document = self.ledger.get_document(document_id) or updated_document

        bookkeeping_record = LocalBookkeepingRecordService(self.ledger).upsert_from_document(
            document_id
        )
        result_status = status if close_review else "candidate_rejected"
        audit_details = {
            "status": result_status,
            "resolution": candidate_resolution,
            "documentId": document_id,
            "duplicateCandidateId": candidate_id,
            "counterpartDocumentId": counterpart_document_id,
            "operatorDecision": decision,
            "correctionId": correction_id,
            "duplicateCandidatesResolved": duplicate_candidates_resolved,
            "remainingDuplicateCandidateIds": [
                int(item["id"]) for item in remaining_duplicate_candidates
            ],
            "supersededReviewItemIds": superseded_review_ids,
            "remainingReviewItemIds": [
                int(item["id"]) for item in remaining_review_items
            ],
            "processingStatus": final_processing_status,
            "bookkeepingRecordId": bookkeeping_record.get("recordId") if bookkeeping_record else None,
            "sourceFilesRetained": True,
        }
        self.ledger.record_audit_event({
            "action": "local_review.duplicate_candidate.resolve",
            "entityType": "duplicate_candidate",
            "entityId": str(candidate_id),
            "details": audit_details,
        })
        self.ledger.record_audit_event({
            "action": "local_review.review_item.resolve",
            "entityType": "review_item",
            "entityId": str(review_item_id),
            "details": audit_details,
        })

        return {
            "success": True,
            "reviewItemId": review_item_id,
            "documentId": document_id,
            "status": result_status,
            "reviewItemStatus": status if close_review else "in_review",
            "duplicateCandidateId": candidate_id,
            "counterpartDocumentId": counterpart_document_id,
            "correctionId": correction_id,
            "duplicateCandidatesResolved": duplicate_candidates_resolved,
            "remainingDuplicateCandidateIds": [
                int(item["id"]) for item in remaining_duplicate_candidates
            ],
            "supersededReviewItemIds": superseded_review_ids,
            "remainingReviewItems": remaining_review_items,
            "processingStatus": final_processing_status,
            "bookkeepingRecordId": bookkeeping_record.get("recordId") if bookkeeping_record else None,
            "corrections": corrections,
            "sourceFilesRetained": True,
        }

    def _apply_vendor_category_to_matching_reviews(
        self,
        primary_review_item: Dict[str, Any],
        primary_document: Optional[Dict[str, Any]],
        status: str,
        resolution: Optional[str],
        corrections: Dict[str, Any],
    ) -> Dict[str, Any]:
        vendor_name = str(corrections.get("vendorName") or (primary_document or {}).get("vendor_name") or "").strip()
        category = str(corrections.get("category") or (primary_document or {}).get("category") or "").strip()
        target_system = str(corrections.get("targetSystem") or _target_system(primary_document or {})).strip()
        primary_document_id = _int(primary_review_item.get("document_id"))
        result = {
            "requested": True,
            "policy": "exact_normalized_vendor_and_target_system",
            "vendorName": vendor_name,
            "category": category,
            "targetSystem": target_system,
            "matchedDocuments": 0,
            "appliedDocuments": 0,
            "appliedReviewItemIds": [],
            "skipped": [],
        }

        invalid_reason = None
        if status not in APPLIED_REVIEW_STATUSES:
            invalid_reason = "primary_review_not_approved"
        elif str(primary_review_item.get("reason") or "") not in BATCH_VENDOR_CATEGORY_REASONS:
            invalid_reason = "primary_review_is_not_a_category_or_validation_gate"
        elif not _normalized_vendor_key(vendor_name):
            invalid_reason = "vendor_missing"
        elif not category or category.lower() in {"manual review", "uncategorized"}:
            invalid_reason = "verified_category_missing"
        elif not target_system:
            invalid_reason = "target_system_missing"
        elif primary_document_id is None:
            invalid_reason = "primary_document_missing"
        if invalid_reason:
            result["status"] = "not_applied"
            result["reason"] = invalid_reason
            self._record_vendor_category_batch_audit(primary_review_item, result)
            return result

        candidate_reviews = {}
        scan_offset = 0
        while True:
            review_page = self.ledger.list_review_items(
                status=tuple(OPEN_REVIEW_STATUSES),
                limit=500,
                offset=scan_offset,
            )
            for item in review_page:
                document_id = _int(item.get("document_id"))
                if document_id is None or document_id == primary_document_id:
                    continue
                if str(item.get("reason") or "") not in BATCH_VENDOR_CATEGORY_REASONS:
                    continue
                current = candidate_reviews.get(document_id)
                if current is None or _batch_review_priority(item) < _batch_review_priority(current):
                    candidate_reviews[document_id] = item
            if len(review_page) < 500:
                break
            scan_offset += len(review_page)

        vendor_key = _normalized_vendor_key(vendor_name)
        for document_id, candidate_review in sorted(candidate_reviews.items()):
            document = self.ledger.get_document(document_id)
            if not document:
                result["skipped"].append({"documentId": document_id, "reason": "document_missing"})
                continue
            if _normalized_vendor_key(document.get("vendor_name")) != vendor_key:
                continue
            if _target_system(document) != target_system:
                continue
            if document.get("duplicate_of_document_id") or str(document.get("processing_status") or "") == "duplicate":
                result["skipped"].append({"documentId": document_id, "reason": "document_marked_duplicate"})
                continue

            result["matchedDocuments"] += 1
            candidate_id = int(candidate_review["id"])
            candidate_result = self.resolve_review_item(
                candidate_id,
                status="approved",
                resolution=(
                    f"Exact-vendor category decision propagated from review item #{primary_review_item['id']}. "
                    f"{resolution or 'Verified by the operator.'}"
                ),
                corrections={"category": category, "targetSystem": target_system},
                learn_rule=False,
                apply_to_matching_vendor=False,
            )
            if candidate_result.get("success"):
                result["appliedDocuments"] += 1
                result["appliedReviewItemIds"].append(candidate_id)
            else:
                result["skipped"].append({
                    "documentId": document_id,
                    "reviewItemId": candidate_id,
                    "reason": candidate_result.get("status") or "resolution_failed",
                })

        result["status"] = "applied"
        result["reviewItemsScanned"] = scan_offset + len(review_page)
        self._record_vendor_category_batch_audit(primary_review_item, result)
        return result

    def _record_vendor_category_batch_audit(
        self,
        primary_review_item: Dict[str, Any],
        result: Dict[str, Any],
    ) -> None:
        self.ledger.record_audit_event({
            "action": "local_review.vendor_category_batch.resolve",
            "entityType": "review_item",
            "entityId": str(primary_review_item["id"]),
            "details": result,
        })

    def _resolve_superseded_document_reviews(
        self,
        review_item: Dict[str, Any],
        status: str,
        corrections: Dict[str, Any],
        document: Optional[Dict[str, Any]],
    ) -> list:
        if not document or status not in APPLIED_REVIEW_STATUSES:
            return []
        duplicate_decision = _duplicate_decision(review_item, status, corrections)
        reasons = set()
        if duplicate_decision == "accepted":
            reasons = {
                str(item.get("reason") or "")
                for item in document.get("review_items") or []
                if item.get("status") in OPEN_REVIEW_STATUSES
            }
        else:
            category = str(document.get("category") or "").strip().lower()
            if category and category not in {"manual review", "uncategorized"}:
                reasons.update(CATEGORY_REVIEW_REASONS)
            if _has_valid_required_fields(document):
                reasons.add("validation_failed")
            if corrections.get("documentType"):
                reasons.update({
                    "credit_note_posting_review",
                    "document_type_conflict",
                    "non_posting_document_type",
                })

        resolved_ids = []
        for item in document.get("review_items") or []:
            if item.get("status") not in OPEN_REVIEW_STATUSES:
                continue
            if str(item.get("reason") or "") not in reasons:
                continue
            item_id = int(item["id"])
            self.ledger.resolve_review_item(
                item_id,
                status="resolved",
                resolution=f"Superseded by approved review item #{review_item['id']}.",
                corrected_data={
                    "supersededByReviewItemId": int(review_item["id"]),
                    "appliedCorrections": corrections,
                },
            )
            resolved_ids.append(item_id)
        return resolved_ids

    @staticmethod
    def _final_processing_status(
        review_item: Dict[str, Any],
        status: str,
        duplicate_decision: Optional[str],
        remaining_review_items: list,
    ) -> str:
        if duplicate_decision == "accepted":
            return "duplicate"
        if remaining_review_items:
            return "needs_review"
        if duplicate_decision == "rejected":
            return "reviewed"
        if status in APPLIED_REVIEW_STATUSES:
            return "reviewed"
        return "needs_review"

    def _learn_vendor_category_rule(
        self,
        document: Dict[str, Any],
        review_item_id: int,
        correction_id: int,
    ) -> Optional[int]:
        if is_non_posting_document_type(document.get("document_type")):
            return None
        vendor_name = str(document.get("vendor_name") or "").strip()
        category = str(document.get("category") or "").strip()
        if not vendor_name or not category or category.lower() in {"manual review", "uncategorized"}:
            return None
        rule_id = self.ledger.upsert_vendor_category_rule({
            "vendorName": vendor_name,
            "category": category,
            "targetSystem": _target_system(document),
            "confidenceScore": 1.0,
            "status": "approved",
            "sourceDocumentId": document.get("source_document_id"),
            "metadata": {
                "source": "operator_approved_review_correction",
                "approval": {
                    "reviewItemId": review_item_id,
                    "correctionId": correction_id,
                },
                "documentId": document.get("id"),
                "reviewItemId": review_item_id,
                "correctionId": correction_id,
            },
        })
        self.ledger.record_audit_event({
            "action": "local_review.vendor_category_rule.approved",
            "entityType": "vendor_category_rule",
            "entityId": str(rule_id),
            "details": {
                "vendorName": vendor_name,
                "category": category,
                "documentId": document.get("id"),
                "reviewItemId": review_item_id,
            },
        })
        return rule_id

    def _resolve_reconciliation_evidence(
        self,
        review_item: Dict[str, Any],
        review_status: str,
        resolution: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        reconciliation_match_id = _reconciliation_match_id(review_item)
        if reconciliation_match_id is None:
            return None
        reconciliation_status = _reconciliation_status_for_review(
            str(review_item.get("reason") or ""),
            review_status,
        )
        result = LocalReconciliationService(self.ledger).resolve_match(
            reconciliation_match_id,
            reconciliation_status,
            resolution or f"Review item #{review_item.get('id')} resolved as {review_status}.",
        )
        if not result.get("success"):
            raise _ReconciliationDecisionRejected(result)
        return {
            "reconciliationMatchId": reconciliation_match_id,
            "requestedReviewStatus": review_status,
            "appliedReconciliationStatus": reconciliation_status,
            "success": bool(result.get("success")),
            "status": result.get("status"),
        }


def normalize_corrections(corrections: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    mapping = {
        "vendor_name": "vendorName",
        "vendorName": "vendorName",
        "category": "category",
        "transaction_date": "transactionDate",
        "transactionDate": "transactionDate",
        "total_amount": "totalAmount",
        "totalAmount": "totalAmount",
        "vat_amount": "vatAmount",
        "vatAmount": "vatAmount",
        "target_system": "targetSystem",
        "targetSystem": "targetSystem",
        "duplicate_of_document_id": "duplicateOfDocumentId",
        "duplicateOfDocumentId": "duplicateOfDocumentId",
        "duplicate_candidate_id": "duplicateCandidateId",
        "duplicateCandidateId": "duplicateCandidateId",
        "document_type": "documentType",
        "documentType": "documentType",
    }
    for key, value in corrections.items():
        mapped = mapping.get(key)
        if not mapped or value in (None, ""):
            continue
        if mapped in {"totalAmount", "vatAmount"}:
            parsed = _float(value)
            if parsed is not None:
                result[mapped] = parsed
            continue
        if mapped in {"duplicateOfDocumentId", "duplicateCandidateId"}:
            parsed_id = _int(value)
            if parsed_id is not None:
                result[mapped] = parsed_id
            continue
        if mapped == "documentType":
            document_type = str(value).strip().lower()
            if document_type in CORRECTABLE_DOCUMENT_TYPES:
                result[mapped] = document_type
            continue
        result[mapped] = str(value).strip()
    return result


def _validate_financial_corrections(
    document: Dict[str, Any],
    raw_corrections: Dict[str, Any],
    corrections: Dict[str, Any],
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]], Optional[Dict[str, str]]]:
    financial_values = {
        "totalAmount": _raw_correction_value(
            raw_corrections,
            "totalAmount",
            "total_amount",
        ),
        "vatAmount": _raw_correction_value(
            raw_corrections,
            "vatAmount",
            "vat_amount",
        ),
    }
    supplied_fields = {
        field_name
        for field_name, (supplied, _) in financial_values.items()
        if supplied
    }
    if not supplied_fields:
        return corrections, None, None

    normalized = dict(corrections)
    effective_document_type = str(
        normalized.get("documentType")
        or document.get("document_type")
        or ""
    ).strip().lower()
    is_credit_note = effective_document_type == "credit_note"
    observed_values: Dict[str, float] = {}
    normalized_values: Dict[str, float] = {}

    for field_name in ("totalAmount", "vatAmount"):
        supplied, raw_value = financial_values[field_name]
        if not supplied:
            continue
        parsed = _float(raw_value)
        if parsed is None:
            return normalized, None, {
                "field": field_name,
                "reason": "not_finite",
                "message": f"{field_name} must be a finite number.",
            }
        observed_values[field_name] = parsed
        if is_credit_note and parsed < 0:
            parsed = abs(parsed)
            normalized_values[field_name] = parsed
        if field_name == "totalAmount" and parsed <= 0:
            return normalized, None, {
                "field": field_name,
                "reason": "non_positive_total",
                "message": "totalAmount must be greater than zero.",
            }
        if field_name == "vatAmount" and parsed < 0:
            return normalized, None, {
                "field": field_name,
                "reason": "negative_vat",
                "message": "vatAmount cannot be negative.",
            }
        normalized[field_name] = parsed

    total_amount = normalized.get("totalAmount", document.get("total_amount"))
    vat_amount = normalized.get("vatAmount", document.get("vat_amount"))
    if is_credit_note:
        if isinstance(total_amount, (int, float)) and not isinstance(total_amount, bool):
            total_amount = abs(float(total_amount))
        if isinstance(vat_amount, (int, float)) and not isinstance(vat_amount, bool):
            vat_amount = abs(float(vat_amount))
    if vat_amount not in (None, ""):
        assessment = assess_vat_amount(vat_amount, total_amount)
        if not assessment["valid"]:
            return normalized, None, {
                "field": "vatAmount",
                "reason": str(assessment.get("reason") or "invalid_vat"),
                "message": vat_issue_message(assessment),
            }

    normalization = None
    if normalized_values:
        normalization = {
            "policy": "credit_note_absolute_evidence_amount",
            "normalizedFields": sorted(normalized_values),
            "observedValues": {
                field_name: observed_values[field_name]
                for field_name in sorted(normalized_values)
            },
            "normalizedValues": {
                field_name: normalized_values[field_name]
                for field_name in sorted(normalized_values)
            },
            "ledgerDirection": "credit",
        }
    return normalized, normalization, None


def _raw_correction_value(
    corrections: Dict[str, Any],
    *keys: str,
) -> tuple[bool, Any]:
    for key in keys:
        if key in corrections and corrections[key] not in (None, ""):
            return True, corrections[key]
    return False, None


def _build_document_update(document: Dict[str, Any], corrections: Dict[str, Any]) -> Dict[str, Any]:
    extracted_data = dict(document.get("extracted_data") or {})
    metadata = dict(document.get("metadata") or {})
    update: Dict[str, Any] = {}

    if "vendorName" in corrections:
        update["vendorName"] = corrections["vendorName"]
        extracted_data["vendor_name"] = corrections["vendorName"]
    if "category" in corrections:
        update["category"] = corrections["category"]
    if "transactionDate" in corrections:
        update["transactionDate"] = corrections["transactionDate"]
        extracted_data["transaction_date"] = corrections["transactionDate"]
    if "totalAmount" in corrections:
        update["totalAmount"] = corrections["totalAmount"]
        extracted_data["total_amount"] = corrections["totalAmount"]
    if "vatAmount" in corrections:
        update["vatAmount"] = corrections["vatAmount"]
        extracted_data["vat_amount"] = corrections["vatAmount"]
    if "duplicateOfDocumentId" in corrections:
        update["duplicateOfDocumentId"] = corrections["duplicateOfDocumentId"]
    if "targetSystem" in corrections:
        metadata["targetSystem"] = corrections["targetSystem"]
    if "documentType" in corrections:
        document_type = corrections["documentType"]
        update["documentType"] = document_type
        extracted_data["document_type"] = document_type
        metadata.setdefault("review", {})["documentTypeOverride"] = {
            "documentType": document_type,
            "source": "manual_review_correction",
        }
        if is_non_posting_document_type(document_type):
            update["category"] = "Supporting Evidence"

    if corrections:
        extracted_data.setdefault("manual_corrections", {}).update(corrections)
        metadata.setdefault("review", {})["lastCorrections"] = corrections
        update["extractedData"] = extracted_data
        update["metadata"] = metadata
        update["confidenceScore"] = 1.0
    return update


def _duplicate_decision(review_item: Dict[str, Any], status: str, corrections: Dict[str, Any]) -> Optional[str]:
    if review_item.get("reason") != "duplicate_candidate":
        return None
    if status == "approved" or corrections.get("duplicateOfDocumentId"):
        return "accepted"
    if status == "rejected":
        return "rejected"
    return None


def _document_snapshot(document: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not document:
        return {}
    return {
        "vendorName": document.get("vendor_name"),
        "category": document.get("category"),
        "transactionDate": document.get("transaction_date"),
        "totalAmount": document.get("total_amount"),
        "vatAmount": document.get("vat_amount"),
        "processingStatus": document.get("processing_status"),
        "duplicateOfDocumentId": document.get("duplicate_of_document_id"),
        "extractedData": document.get("extracted_data"),
    }


def _has_valid_required_fields(document: Dict[str, Any]) -> bool:
    vendor_name = str(document.get("vendor_name") or "").strip()
    category = str(document.get("category") or "").strip().lower()
    transaction_date = str(document.get("transaction_date") or "").strip()
    try:
        date.fromisoformat(transaction_date)
    except ValueError:
        return False
    return bool(
        vendor_name
        and category
        and category not in {"manual review", "uncategorized"}
        and _float(document.get("total_amount")) is not None
    )


def _target_system(document: Dict[str, Any]) -> str:
    return resolve_document_target_system(document, default="none")


def _normalized_vendor_key(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _batch_review_priority(review_item: Dict[str, Any]) -> int:
    return {
        "manual_review_category": 0,
        "low_confidence_categorization": 1,
    }.get(str(review_item.get("reason") or ""), 99)


def _reconciliation_match_id(review_item: Dict[str, Any]) -> Optional[int]:
    corrected_data = review_item.get("corrected_data") or {}
    if not isinstance(corrected_data, dict):
        return None
    return _positive_reference_id(corrected_data, ("reconciliationMatchId", "reconciliation_match_id"))


def _reconciliation_status_for_review(reason: str, review_status: str) -> str:
    if review_status == "ignored":
        return "ignored"
    if reason == "reconciliation_candidate":
        if review_status == "approved":
            return "approved"
        if review_status == "rejected":
            return "rejected"
        return "resolved"
    if reason == "missing_receipt":
        if review_status == "rejected":
            return "ignored"
        return "resolved"
    if reason == "unmatched_document":
        if review_status == "rejected":
            return "ignored"
        return "resolved"
    return "resolved"


def _float(value: Any) -> Optional[float]:
    try:
        parsed = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
