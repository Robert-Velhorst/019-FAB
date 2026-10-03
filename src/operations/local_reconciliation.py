import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from src.operations.local_bookkeeping_records import LocalBookkeepingRecordService
from src.operations.local_bank_transactions import _transaction_for_reconciliation
from src.operations.local_ledger import LocalOperationsLedger
from src.reconciliation.automated_reconciliation import AutomatedReconciliation


RECONCILIABLE_DOCUMENT_STATUSES = (
    "processed",
    "reviewed",
    "validated",
    "ready_to_route",
    "export_draft_prepared",
    "routed",
)
FINAL_RECONCILIATION_STATUSES = {"approved", "reconciled"}
RESOLUTION_STATUSES = {"approved", "reconciled", "rejected", "resolved", "ignored", "needs_review"}
OPEN_REVIEW_STATUSES = ("pending", "in_review")
MAX_RECONCILIATION_TRANSACTIONS = 500
MAX_RECONCILIATION_DOCUMENT_REFERENCES = 500
MAX_RECONCILIATION_JSON_BYTES = 4 * 1024 * 1024
MAX_RECONCILIATION_JSON_DEPTH = 32
MAX_RECONCILIATION_JSON_NODES = 50_000


class LocalReconciliationService:
    """Match local ledger documents to bank transactions without posting externally."""

    def __init__(
        self,
        ledger: LocalOperationsLedger,
        config: Optional[Dict[str, Any]] = None,
        reconciler: Optional[Any] = None,
    ):
        self.ledger = ledger
        self.config = config or {}
        self.reconciler = reconciler or AutomatedReconciliation(self.config)

    def run(
        self,
        bank_transactions: List[Dict[str, Any]],
        document_ids: Optional[Iterable[int]] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        document_ids = _capture_document_selection(document_ids)
        if not isinstance(bank_transactions, list):
            raise ValueError("bankTransactions must be a list")
        if len(bank_transactions) > MAX_RECONCILIATION_TRANSACTIONS:
            raise ValueError("bankTransactions exceeds the 500-row batch limit; import and process bounded batches")
        request_snapshot = _serialize_bank_transactions(bank_transactions)
        bank_transactions = json.loads(request_snapshot)
        for transaction in bank_transactions:
            if (not isinstance(transaction, dict) or AutomatedReconciliation._amount(transaction.get("amount")) is None
                    or AutomatedReconciliation._date(transaction.get("date") or transaction.get("transaction_date")) is None):
                raise ValueError("bankTransactions rows must contain a finite amount and valid date")
            _bank_transaction_id(transaction)
            _ad_hoc_account_identifier(transaction)

        with self.ledger.read_snapshot():
            documents = self._candidate_documents(document_ids=document_ids, limit=limit)
            document_evidence = {int(item["id"]): _evidence_fingerprint(item) for item in documents}
            bank_evidence = {}
            ad_hoc_identities = set()
            for transaction in bank_transactions:
                bank_id = _ledger_bank_transaction_id(transaction)
                if bank_id is None:
                    if any(key in transaction for key in BANK_LEDGER_ID_KEYS):
                        raise ValueError("Bank reconciliation evidence changed or has an invalid reference; refresh and run again.")
                    identity = (_ad_hoc_account_identifier(transaction), _bank_transaction_id(transaction))
                    if identity in ad_hoc_identities:
                        raise ValueError("Bank reconciliation evidence is duplicated within the account; refresh and run again.")
                    ad_hoc_identities.add(identity)
                    continue
                current = self.ledger.get_bank_transaction(bank_id)
                if (not current or not _bank_input_matches(transaction, current)
                        or bank_id in bank_evidence):
                    raise ValueError("Bank reconciliation evidence changed or is duplicated; refresh and run again.")
                bank_evidence[bank_id] = _evidence_fingerprint(current)
        reconciler_documents = [self._document_for_reconciler(document) for document in documents]
        raw_results = self.reconciler.reconcile(bank_transactions, reconciler_documents)
        with self.ledger.write_transaction():
            if _serialize_bank_transactions(bank_transactions) != request_snapshot:
                raise ValueError("Bank request evidence changed during matching; refresh and run again.")
            if document_ids is not None:
                current_documents = [self.ledger.get_document_core(document_id) for document_id in document_evidence]
            else:
                current_documents = self._candidate_documents(document_ids=None, limit=limit)
            current_evidence = {int(item["id"]): _evidence_fingerprint(item) for item in current_documents if item}
            if current_evidence != document_evidence or list(current_evidence) != list(document_evidence):
                raise ValueError("Document reconciliation evidence changed; refresh and run again.")
            for bank_id, expected in bank_evidence.items():
                if _evidence_fingerprint(self.ledger.get_bank_transaction(bank_id)) != expected:
                    raise ValueError("Bank reconciliation evidence changed; refresh and run again.")
            return self._record_results(raw_results, len(bank_transactions), len(documents))

    def _record_results(
        self, raw_results: List[Dict[str, Any]], transaction_count: int, document_count: int,
    ) -> Dict[str, Any]:
        summary: Dict[str, Any] = {
            "requestedTransactions": transaction_count,
            "candidateDocuments": document_count,
            "matchedCandidates": 0,
            "missingReceipts": 0,
            "unmatchedDocuments": 0,
            "matchesRecorded": 0,
            "reviewItemsCreated": 0,
            "results": [],
        }

        for result in raw_results:
            result_type = result.get("type")
            if result_type == "match":
                summary["matchedCandidates"] += 1
                record = self._record_candidate(result)
            elif result_type == "unmatched_bank_transaction":
                summary["missingReceipts"] += 1
                record = self._record_missing_receipt(result)
            elif result_type == "unmatched_document":
                summary["unmatchedDocuments"] += 1
                record = self._record_unmatched_document(result)
            else:
                record = {"status": "ignored", "type": result_type or "unknown"}

            if record.get("recorded"):
                summary["matchesRecorded"] += 1
            if record.get("reviewItemCreated"):
                summary["reviewItemsCreated"] += 1
            summary["results"].append(record)

        self.ledger.record_audit_event({
            "action": "local_reconciliation.run_completed",
            "entityType": "reconciliation_run",
            "details": {
                "requestedTransactions": summary["requestedTransactions"],
                "candidateDocuments": summary["candidateDocuments"],
                "matchedCandidates": summary["matchedCandidates"],
                "missingReceipts": summary["missingReceipts"],
                "unmatchedDocuments": summary["unmatchedDocuments"],
                "matchesRecorded": summary["matchesRecorded"],
                "reviewItemsCreated": summary["reviewItemsCreated"],
            },
        })
        return summary

    def resolve_match(
        self,
        reconciliation_match_id: int,
        status: str,
        resolution: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self.ledger.write_transaction():
            return self._resolve_match(reconciliation_match_id, status, resolution)

    def validate_approval(self, reconciliation_match_id: int) -> Dict[str, Any]:
        with self.ledger.read_snapshot():
            match = self.ledger.get_reconciliation_match(reconciliation_match_id)
            if not match:
                return {"success": False, "status": "not_found", "error": "Reconciliation match not found"}
            return self._validate_approval(match)

    def _validate_approval(self, match: Dict[str, Any]) -> Dict[str, Any]:
        if not match.get("document_id"):
            return {
                "success": False, "status": "invalid_status",
                "error": "A bank match cannot be confirmed without a linked document.",
            }
        metadata = match.get("metadata") or {}
        document = self.ledger.get_document_core(int(match["document_id"]))
        bank_transaction = metadata.get("bankTransaction")
        bank_id = _ledger_bank_transaction_id(bank_transaction) if isinstance(bank_transaction, dict) else None
        invalid_bank = (not isinstance(bank_transaction, dict) or not bank_transaction
                        or not _valid_bank_identity(bank_transaction)
                        or metadata.get("approvalBankHash") != _evidence_fingerprint(bank_transaction)
                        or _bank_transaction_id(bank_transaction) != match.get("bank_transaction_id"))
        if isinstance(bank_transaction, dict) and any(key in bank_transaction for key in BANK_LEDGER_ID_KEYS):
            bank = self.ledger.get_bank_transaction(bank_id) if bank_id else None
            invalid_bank = invalid_bank or not bank or not _bank_input_matches(bank_transaction, bank)
        if (not document or document.get("processing_status") == "duplicate"
                or document.get("reconciliation_status") in {"approved", "reconciled", "ignored"}
                or document.get("duplicate_of_document_id")
                or metadata.get("approvalDocumentHash") != _approval_document_hash(document)
                or invalid_bank):
            return {
                "success": False, "status": "stale_evidence",
                "error": "Reconciliation evidence changed or is incomplete; refresh and run matching again before approval.",
            }
        return {"success": True}

    def _resolve_match(
        self,
        reconciliation_match_id: int,
        status: str,
        resolution: Optional[str] = None,
    ) -> Dict[str, Any]:
        status = str(status or "").strip()
        if status not in RESOLUTION_STATUSES:
            return {
                "success": False,
                "status": "invalid_status",
                "error": f"Invalid reconciliation status: {status}",
            }

        match = self.ledger.get_reconciliation_match(reconciliation_match_id)
        if not match:
            return {
                "success": False,
                "status": "not_found",
                "error": "Reconciliation match not found",
            }

        if match.get("status") in FINAL_RECONCILIATION_STATUSES and status in FINAL_RECONCILIATION_STATUSES:
            return self._acknowledge_completed_match(match, status)

        metadata = dict(match.get("metadata") or {})
        disposition = self._validate_disposition(match, status)
        if not disposition.get("success"):
            return disposition
        if status in FINAL_RECONCILIATION_STATUSES:
            validation = self._validate_approval(match)
            if not validation.get("success"):
                return validation
        metadata["resolution"] = {
            "status": status,
            "note": resolution,
            "resolvedAt": _now(),
        }
        update_payload: Dict[str, Any] = {
            "status": status,
            "metadata": metadata,
        }
        if status in FINAL_RECONCILIATION_STATUSES:
            update_payload["matchedAt"] = _now()
        self.ledger.update_reconciliation_match(reconciliation_match_id, update_payload)

        document_id = match.get("document_id")
        if document_id:
            document_reconciliation_status = _document_reconciliation_status(status)
            self.ledger.update_document(int(document_id), {
                "reconciliationStatus": document_reconciliation_status,
            })
            if status == "needs_review":
                self._queue_document_review(
                    int(document_id),
                    "unmatched_document" if metadata.get("resultType") == "unmatched_document" else "reconciliation_candidate",
                    resolution or f"Reconciliation match #{reconciliation_match_id} needs review.",
                    {"reconciliationMatchId": reconciliation_match_id},
                )
            else:
                self._resolve_linked_review_items(
                    int(document_id),
                    reconciliation_match_id,
                    resolution or f"Reconciliation match #{reconciliation_match_id} marked {status}.",
                )
            LocalBookkeepingRecordService(self.ledger, self.config).record_reconciliation_state(
                int(document_id),
                document_reconciliation_status,
                reconciliation_match_id=reconciliation_match_id,
            )
        elif status == "needs_review":
            self._queue_missing_receipt_review(reconciliation_match_id, metadata.get("bankTransaction") or {}, resolution)
        else:
            self._resolve_linked_review_items(
                None, reconciliation_match_id,
                resolution or f"Missing receipt match #{reconciliation_match_id} marked {status}.",
            )

        self._update_bank_transaction_reconciliation(
            metadata.get("bankTransaction") or {},
            _bank_reconciliation_status(status),
            reconciliation_match_id,
            document_id,
        )
        if status in FINAL_RECONCILIATION_STATUSES:
            self._supersede_missing_receipts(metadata["bankTransaction"], reconciliation_match_id, int(document_id))

        self.ledger.record_audit_event({
            "action": "local_reconciliation.match.resolve",
            "entityType": "reconciliation_match",
            "entityId": str(reconciliation_match_id),
            "details": {
                "status": status,
                "documentId": document_id,
                "bankTransactionId": match.get("bank_transaction_id"),
                "resolution": resolution,
            },
        })
        return {
            "success": True,
            "status": status,
            "reconciliationMatchId": reconciliation_match_id,
            "documentId": document_id,
        }

    def _acknowledge_completed_match(self, match: Dict[str, Any], status: str) -> Dict[str, Any]:
        stale = {
            "success": False, "status": "stale_evidence",
            "error": "Completed reconciliation evidence is incomplete or changed; review before retrying confirmation.",
        }
        metadata = match.get("metadata")
        if not isinstance(metadata, dict) or match.get("status") != status or not match.get("document_id"):
            return stale
        transaction = metadata.get("bankTransaction")
        resolution = metadata.get("resolution")
        bank_id = _ledger_bank_transaction_id(transaction) if isinstance(transaction, dict) else None
        bank = self.ledger.get_bank_transaction(bank_id) if bank_id else None
        document_id = int(match["document_id"])
        document = self.ledger.get_document_core(document_id)
        bank_metadata = (bank or {}).get("metadata")
        latest = bank_metadata.get("latestReconciliation") if isinstance(bank_metadata, dict) else None
        if (not bank or not document or not isinstance(latest, dict) or not _valid_bank_identity(transaction)
                or not isinstance(resolution, dict) or resolution.get("status") != status or not match.get("matched_at")
                or _positive_reference_id(latest, ("reconciliationMatchId",)) != match["id"]
                or _positive_reference_id(latest, ("documentId",)) != document_id
                or latest.get("status") != "reconciled" or bank.get("reconciliation_status") != "reconciled"
                or document.get("reconciliation_status") != "reconciled"
                or document.get("processing_status") == "duplicate" or document.get("duplicate_of_document_id")
                or metadata.get("approvalDocumentHash") != _approval_document_hash(document)
                or metadata.get("approvalBankHash") != _evidence_fingerprint(transaction)
                or _bank_transaction_id(transaction) != match.get("bank_transaction_id")
                or not _bank_input_matches(transaction, bank, allow_final=True)
                or self.ledger.has_other_final_document_match(document_id, int(match["id"]))):
            return stale

        repaired = self._resolve_linked_review_items(
            document_id, int(match["id"]), f"Confirmed local match #{match['id']} already completed.",
        )
        repaired += self._supersede_missing_receipts(transaction, int(match["id"]), document_id)
        if repaired:
            self.ledger.record_audit_event({
                "action": "local_reconciliation.confirmation_retry.repaired",
                "entityType": "reconciliation_match", "entityId": str(match["id"]),
                "details": {"documentId": document_id, "bankTransactionRecordId": bank_id,
                            "repairedItems": repaired, "externalMutation": "not_performed"},
            })
        return {"success": True, "status": status, "alreadyFinal": True,
                "reconciliationMatchId": match["id"], "documentId": document_id, "repairedItems": repaired}

    def _validate_disposition(self, match: Dict[str, Any], status: str) -> Dict[str, Any]:
        stale = {
            "success": False, "status": "stale_evidence",
            "error": "Reconciliation evidence changed or another match owns the completed decision; refresh before resolving.",
        }
        if match.get("document_id") and self.ledger.has_other_final_document_match(int(match["document_id"]), int(match["id"])):
            return stale
        metadata = match.get("metadata") or {}
        transaction = metadata.get("bankTransaction")
        if not transaction:
            return {"success": True} if metadata.get("resultType") == "unmatched_document" else stale
        if not isinstance(transaction, dict) or not _valid_bank_identity(transaction):
            return stale
        bank_id = _ledger_bank_transaction_id(transaction)
        if any(key in transaction for key in BANK_LEDGER_ID_KEYS):
            bank = self.ledger.get_bank_transaction(bank_id) if bank_id else None
            if not bank:
                return stale
            latest = (bank.get("metadata") or {}).get("latestReconciliation") or {}
            if latest.get("reconciliationMatchId") != match["id"]:
                return stale
            if status != "needs_review" and not _bank_input_matches(transaction, bank, allow_final=True):
                return stale
        if status != "needs_review" and metadata.get("approvalBankHash") != _evidence_fingerprint(transaction):
            return stale
        return {"success": True}

    def _supersede_missing_receipts(self, transaction: Dict[str, Any], match_id: int, document_id: int) -> int:
        bank_id = _ledger_bank_transaction_id(transaction)
        if bank_id is None:
            return 0
        repaired = 0
        before_id = None
        while True:
            items = self.ledger.list_reconciliation_matches(
                status=("missing_receipt", "needs_review", "resolved"),
                bank_transaction_id=_bank_transaction_id(transaction), documentless_only=True,
                bank_transaction_record_id=bank_id, before_id=before_id, keyset_order=True, limit=100,
            )
            for item in items:
                metadata = dict(item.get("metadata") or {})
                evidence = metadata.get("bankTransaction")
                if (metadata.get("resultType") != "unmatched_bank_transaction" or not isinstance(evidence, dict)
                        or _ledger_bank_transaction_id(evidence) != bank_id):
                    continue
                if item.get("status") == "resolved":
                    superseded = metadata.get("supersededBy")
                    if (not isinstance(superseded, dict) or superseded.get("basis") != "confirmed_local_match"
                            or _positive_reference_id(superseded, ("reconciliationMatchId",)) != match_id
                            or _positive_reference_id(superseded, ("documentId",)) != document_id):
                        continue
                    repaired += self._resolve_linked_review_items(
                        None, int(item["id"]), f"Superseded by confirmed local match #{match_id} for document #{document_id}.",
                    )
                    continue
                metadata["supersededBy"] = {
                    "reconciliationMatchId": match_id, "documentId": document_id,
                    "basis": "confirmed_local_match", "supersededAt": _now(),
                }
                self.ledger.update_reconciliation_match(int(item["id"]), {"status": "resolved", "metadata": metadata})
                repaired += 1 + self._resolve_linked_review_items(
                    None, int(item["id"]), f"Superseded by confirmed local match #{match_id} for document #{document_id}.",
                )
                self.ledger.record_audit_event({
                    "action": "local_reconciliation.missing_receipt.superseded",
                    "entityType": "reconciliation_match", "entityId": str(item["id"]),
                    "details": {"confirmedMatchId": match_id, "documentId": document_id, "bankTransactionRecordId": bank_id},
                })
            if len(items) < 100:
                break
            before_id = int(items[-1]["id"])
        return repaired

    def _candidate_documents(
        self,
        document_ids: Optional[Iterable[int]],
        limit: int,
    ) -> List[Dict[str, Any]]:
        document_ids = _capture_document_selection(document_ids)
        if document_ids is not None:
            documents = []
            candidate_limit = _bounded_limit(limit)
            for document_id in document_ids:
                document = self.ledger.get_document_core(int(document_id))
                if not document:
                    raise ValueError("documentIds includes a missing document; refresh the selection and run again")
                if document.get("reconciliation_status") in {"approved", "reconciled", "ignored"}:
                    continue
                if (document.get("processing_status") not in RECONCILIABLE_DOCUMENT_STATUSES
                        or document.get("duplicate_of_document_id")):
                    raise ValueError("documentIds includes an ineligible or duplicate document; resolve its processing/review gate first")
                if len(documents) < candidate_limit:
                    documents.append(document)
            return documents
        return self.ledger.list_reconcilable_documents(RECONCILIABLE_DOCUMENT_STATUSES, limit=limit)

    def _record_candidate(self, result: Dict[str, Any]) -> Dict[str, Any]:
        document = result.get("document") or {}
        document_id = _int_or_none(document.get("id") or result.get("document_id"))
        bank_transaction = result.get("bank_transaction") or {}
        bank_transaction_id = _bank_transaction_id(bank_transaction)
        if document_id is None:
            return {"status": "skipped", "type": "match", "reason": "missing_document_id"}

        existing = self._existing_reconciliation(document_id, bank_transaction_id, bank_transaction)
        if existing and existing.get("status") in FINAL_RECONCILIATION_STATUSES | {"rejected", "ignored"}:
            self._update_bank_transaction_reconciliation(
                bank_transaction,
                _bank_reconciliation_status(str(existing.get("status") or "")),
                int(existing["id"]),
                document_id,
            )
            return {
                "status": "already_final",
                "type": "match",
                "reconciliationMatchId": existing["id"],
                "documentId": document_id,
                "bankTransactionId": bank_transaction_id,
            }

        payload = {
            "documentId": document_id,
            "bankTransactionId": bank_transaction_id,
            "status": "candidate",
            "confidenceScore": result.get("confidence_score"),
            "amountDifference": result.get("amount_difference"),
            "metadata": {
                "resultType": "match",
                "bankTransaction": bank_transaction,
                "document": _document_snapshot(document),
                "approvalDocumentHash": _approval_document_hash(self.ledger.get_document_core(document_id)),
                "approvalBankHash": _evidence_fingerprint(bank_transaction),
                "requiresApproval": True,
                "externalMutation": "not_performed",
            },
        }
        if existing:
            reconciliation_match_id = int(existing["id"])
            self.ledger.update_reconciliation_match(reconciliation_match_id, payload)
            recorded = False
        else:
            reconciliation_match_id = self.ledger.create_reconciliation_match(payload)
            recorded = True

        self.ledger.update_document(document_id, {"reconciliationStatus": "candidate"})
        self._update_bank_transaction_reconciliation(
            bank_transaction,
            "candidate",
            reconciliation_match_id,
            document_id,
        )
        review_created = self._queue_document_review(
            document_id,
            "reconciliation_candidate",
            (
                f"Bank transaction {bank_transaction_id} matches document #{document_id} "
                f"with {float(result.get('confidence_score') or 0) * 100:.0f}% confidence. Confirm before final close."
            ),
            {
                "reconciliationMatchId": reconciliation_match_id,
                "bankTransactionId": bank_transaction_id,
            },
        )
        LocalBookkeepingRecordService(self.ledger, self.config).record_reconciliation_state(
            document_id,
            "candidate",
            status="needs_review",
            reconciliation_match_id=reconciliation_match_id,
        )
        return {
            "status": "candidate",
            "type": "match",
            "recorded": recorded,
            "reviewItemCreated": review_created,
            "reconciliationMatchId": reconciliation_match_id,
            "documentId": document_id,
            "bankTransactionId": bank_transaction_id,
            "confidenceScore": result.get("confidence_score"),
        }

    def _record_missing_receipt(self, result: Dict[str, Any]) -> Dict[str, Any]:
        bank_transaction = result.get("bank_transaction") or {}
        bank_transaction_id = _bank_transaction_id(bank_transaction)
        bank_id = _ledger_bank_transaction_id(bank_transaction)
        bank = self.ledger.get_bank_transaction(bank_id) if bank_id else None
        latest = ((bank or {}).get("metadata") or {}).get("latestReconciliation") or {}
        latest_id = _int_or_none(latest.get("reconciliationMatchId"))
        candidate = self.ledger.get_reconciliation_match(latest_id) if latest_id else None
        # A competing transaction consuming the document in this batch does not erase an earlier candidate.
        if candidate and candidate.get("document_id") and candidate.get("status") in {"candidate", "needs_review"}:
            return {
                "status": "existing_candidate", "type": "unmatched_bank_transaction",
                "reconciliationMatchId": candidate["id"], "bankTransactionId": bank_transaction_id,
            }
        existing = self._existing_reconciliation(None, bank_transaction_id, bank_transaction)
        if existing:
            if existing.get("status") not in FINAL_RECONCILIATION_STATUSES | {"ignored", "rejected"}:
                metadata = dict(existing.get("metadata") or {})
                metadata.update({"bankTransaction": bank_transaction, "approvalBankHash": _evidence_fingerprint(bank_transaction)})
                self.ledger.update_reconciliation_match(int(existing["id"]), {"metadata": metadata})
                for item in self._linked_review_items(None, int(existing["id"])):
                    evidence = dict(item.get("corrected_data") or {})
                    evidence["bankTransaction"] = bank_transaction
                    self.ledger.resolve_review_item(
                        int(item["id"]), status=str(item["status"]),
                        resolution=item.get("resolution"), corrected_data=evidence,
                    )
            self._update_bank_transaction_reconciliation(
                bank_transaction,
                str(existing.get("status") or "missing_receipt"),
                int(existing["id"]),
                None,
            )
            return {
                "status": "already_recorded",
                "type": "unmatched_bank_transaction",
                "reconciliationMatchId": existing["id"],
                "bankTransactionId": bank_transaction_id,
            }

        reconciliation_match_id = self.ledger.create_reconciliation_match({
            "bankTransactionId": bank_transaction_id,
            "status": "missing_receipt",
            "confidenceScore": 0,
            "metadata": {
                "resultType": "unmatched_bank_transaction",
                "bankTransaction": bank_transaction,
                "approvalBankHash": _evidence_fingerprint(bank_transaction),
                "requiresReceiptReview": True,
                "externalMutation": "not_performed",
            },
        })
        self._update_bank_transaction_reconciliation(
            bank_transaction,
            "missing_receipt",
            reconciliation_match_id,
            None,
        )
        review_item_id = self.ledger.create_review_item({
            "reason": "missing_receipt",
            "details": f"Bank transaction {bank_transaction_id} has no matching processed document.",
            "correctedData": {
                "reconciliationMatchId": reconciliation_match_id,
                "bankTransactionId": bank_transaction_id,
                "bankTransaction": bank_transaction,
            },
        })
        bank_record_id = _ledger_bank_transaction_id(bank_transaction)
        if bank_record_id is not None:
            LocalBookkeepingRecordService(self.ledger, self.config).upsert_from_bank_transaction(
                bank_record_id,
                status="missing_receipt",
                reconciliation_status="missing_receipt",
            )
        return {
            "status": "missing_receipt",
            "type": "unmatched_bank_transaction",
            "recorded": True,
            "reviewItemCreated": True,
            "reviewItemId": review_item_id,
            "reconciliationMatchId": reconciliation_match_id,
            "bankTransactionId": bank_transaction_id,
        }

    def _record_unmatched_document(self, result: Dict[str, Any]) -> Dict[str, Any]:
        document = result.get("document") or {}
        document_id = _int_or_none(document.get("id") or result.get("document_id"))
        if document_id is None:
            return {"status": "skipped", "type": "unmatched_document", "reason": "missing_document_id"}

        bank_transaction_id = f"unmatched_document:{document_id}"
        existing = self._existing_reconciliation(document_id, bank_transaction_id)
        if existing:
            return {
                "status": "already_recorded",
                "type": "unmatched_document",
                "reconciliationMatchId": existing["id"],
                "documentId": document_id,
            }

        reconciliation_match_id = self.ledger.create_reconciliation_match({
            "documentId": document_id,
            "bankTransactionId": bank_transaction_id,
            "status": "unmatched_document",
            "confidenceScore": 0,
            "metadata": {
                "resultType": "unmatched_document",
                "document": _document_snapshot(document),
                "requiresBankEvidenceReview": True,
                "externalMutation": "not_performed",
            },
        })
        self.ledger.update_document(document_id, {"reconciliationStatus": "unmatched"})
        LocalBookkeepingRecordService(self.ledger, self.config).record_reconciliation_state(
            document_id,
            "unmatched",
            status="needs_review",
            reconciliation_match_id=reconciliation_match_id,
        )
        review_created = self._queue_document_review(
            document_id,
            "unmatched_document",
            f"Document #{document_id} has no matching bank transaction in this reconciliation run.",
            {"reconciliationMatchId": reconciliation_match_id},
        )
        return {
            "status": "unmatched_document",
            "type": "unmatched_document",
            "recorded": True,
            "reviewItemCreated": review_created,
            "reconciliationMatchId": reconciliation_match_id,
            "documentId": document_id,
        }

    def _existing_reconciliation(
        self,
        document_id: Optional[int],
        bank_transaction_id: str,
        bank_transaction: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        bank_id = _ledger_bank_transaction_id(bank_transaction) if bank_transaction is not None else None
        account = _ad_hoc_account_identifier(bank_transaction) if bank_transaction is not None and bank_id is None else None
        matches = self.ledger.list_reconciliation_matches(
            document_id=document_id,
            bank_transaction_id=bank_transaction_id,
            limit=1,
            documentless_only=document_id is None,
            bank_transaction_record_id=bank_id,
            ad_hoc_bank_only=bank_transaction is not None and bank_id is None,
            ad_hoc_account_identifier=account,
        )
        if matches and bank_transaction is not None:
            stored = (matches[0].get("metadata") or {}).get("bankTransaction") or {}
            if not _valid_bank_identity(stored) or _bank_transaction_id(stored) != bank_transaction_id:
                raise ValueError("Stored bank reference evidence conflicts or is ambiguous; review the original record before retrying.")
            if not isinstance(stored, dict) or _ledger_bank_transaction_id(stored) != _ledger_bank_transaction_id(bank_transaction):
                raise ValueError("Stored bank reference disagrees with the selected bank record; review the original record before retrying.")
            if account is not None and _ad_hoc_account_identifier(stored) != account:
                raise ValueError("Stored bank account evidence is ambiguous; review the original record before retrying.")
        return matches[0] if matches else None

    def _queue_document_review(
        self,
        document_id: int,
        reason: str,
        details: str,
        corrected_data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        offset = 0
        while True:
            items = self.ledger.list_review_items(
                status=OPEN_REVIEW_STATUSES, document_id=document_id, limit=100, offset=offset,
            )
            if any(item.get("reason") == reason for item in items):
                return False
            if len(items) < 100:
                break
            offset += len(items)
        self.ledger.create_review_item({
            "documentId": document_id,
            "reason": reason,
            "details": details,
            "correctedData": corrected_data,
        })
        return True

    def _queue_missing_receipt_review(
        self, match_id: int, bank_transaction: Dict[str, Any], resolution: Optional[str],
    ) -> None:
        if any(self._linked_review_items(None, match_id)):
            return
        self.ledger.create_review_item({
            "reason": "missing_receipt",
            "details": resolution or f"Bank reconciliation #{match_id} requires receipt review.",
            "correctedData": {"reconciliationMatchId": match_id, "bankTransaction": bank_transaction},
        })

    def _resolve_linked_review_items(self, document_id: Optional[int], match_id: int, resolution: str) -> int:
        resolved = 0
        for item in self._linked_review_items(document_id, match_id):
            self.ledger.resolve_review_item(
                int(item["id"]), status="resolved", resolution=resolution, corrected_data=item.get("corrected_data"),
            )
            resolved += 1
        return resolved

    def _linked_review_items(self, document_id: Optional[int], match_id: int):
        offset = 0
        before_id = None
        while True:
            # Page all statuses so closing a review cannot shift the next page.
            if document_id is None:
                items = self.ledger.list_missing_receipt_review_items(
                    limit=100, before_id=before_id, reconciliation_match_id=match_id,
                )
            else:
                items = self.ledger.list_review_items(document_id=document_id, limit=100, offset=offset)
            for item in items:
                evidence = item.get("corrected_data") or {}
                if not isinstance(evidence, dict):
                    continue
                linked_id = _positive_reference_id(evidence, ("reconciliationMatchId", "reconciliation_match_id"))
                if (item.get("status") in OPEN_REVIEW_STATUSES
                        and item.get("reason") in {"reconciliation_candidate", "unmatched_document", "missing_receipt"}
                        and linked_id == match_id):
                    yield item
            if len(items) < 100:
                break
            if document_id is None:
                before_id = int(items[-1]["id"])
            else:
                offset += len(items)

    @staticmethod
    def _document_for_reconciler(document: Dict[str, Any]) -> Dict[str, Any]:
        extracted_data = dict(document.get("extracted_data") or {})
        extracted_data.setdefault("vendor_name", document.get("vendor_name"))
        extracted_data.setdefault("transaction_date", document.get("transaction_date"))
        extracted_data.setdefault("total_amount", document.get("total_amount"))
        extracted_data.setdefault("amount", document.get("total_amount"))
        return {
            **document,
            "document_id": str(document.get("id")),
            "extracted_data": extracted_data,
        }

    def _update_bank_transaction_reconciliation(
        self,
        bank_transaction: Dict[str, Any],
        status: str,
        reconciliation_match_id: Optional[int],
        document_id: Optional[int],
    ) -> None:
        bank_transaction_record_id = _ledger_bank_transaction_id(bank_transaction)
        if bank_transaction_record_id is None:
            return
        current = self.ledger.get_bank_transaction(bank_transaction_record_id)
        if not current:
            return
        metadata = dict(current.get("metadata") or {})
        metadata["latestReconciliation"] = {
            "status": status,
            "reconciliationMatchId": reconciliation_match_id,
            "documentId": document_id,
            "bankTransactionId": current.get("transaction_id"),
            "updatedAt": _now(),
        }
        self.ledger.update_bank_transaction(bank_transaction_record_id, {
            "reconciliationStatus": status,
            "metadata": metadata,
        })
        LocalBookkeepingRecordService(self.ledger, self.config).upsert_from_bank_transaction(
            bank_transaction_record_id,
            reconciliation_status=status,
        )


def _serialize_bank_transactions(transactions: List[Dict[str, Any]]) -> str:
    error = "bankTransactions must be finite JSON within 4 MiB, 32 levels and 50,000 values"
    pending = [(transactions, 0)]
    seen = 0
    while pending:
        value, depth = pending.pop()
        seen += 1
        if depth > MAX_RECONCILIATION_JSON_DEPTH or seen > MAX_RECONCILIATION_JSON_NODES:
            raise ValueError(error)
        if isinstance(value, (list, dict)):
            if seen + len(pending) + len(value) > MAX_RECONCILIATION_JSON_NODES:
                raise ValueError(error)
            if isinstance(value, dict) and any(not isinstance(key, str) or len(key) > MAX_RECONCILIATION_JSON_BYTES for key in value):
                raise ValueError(error)
            children = value.values() if isinstance(value, dict) else value
            pending.extend((child, depth + 1) for child in children)
        elif isinstance(value, str) and len(value) > MAX_RECONCILIATION_JSON_BYTES:
            raise ValueError(error)
    chunks = []
    size = 0
    try:
        for chunk in json.JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False).iterencode(transactions):
            size += len(chunk.encode("utf-8"))
            if size > MAX_RECONCILIATION_JSON_BYTES:
                raise ValueError(error)
            chunks.append(chunk)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(error) from exc
    return "".join(chunks)


def _evidence_fingerprint(row: Optional[Dict[str, Any]]) -> str:
    facts = None if row is None else {key: value for key, value in row.items() if key not in {"created_at", "updated_at"}}
    digest = hashlib.sha256()
    for part in json.JSONEncoder(sort_keys=True, separators=(",", ":")).iterencode(facts):
        digest.update(part.encode("utf-8"))
    return digest.hexdigest()


def _approval_document_hash(document: Optional[Dict[str, Any]]) -> str:
    # Workflow transitions may occur during review; source and financial facts must not change.
    keys = ("id", "source_account_id", "source", "source_document_id", "content_sha256",
            "document_type", "duplicate_of_document_id", "vendor_name", "transaction_date",
            "total_amount", "vat_amount", "extracted_data")
    return _evidence_fingerprint(None if document is None else {key: document.get(key) for key in keys})


def _bank_input_matches(transaction: Dict[str, Any], row: Dict[str, Any], *, allow_final: bool = False) -> bool:
    if not _valid_bank_identity(transaction):
        return False
    expected = _transaction_for_reconciliation(row)
    amount = AutomatedReconciliation._amount(transaction.get("amount"))
    expected_amount = AutomatedReconciliation._amount(expected["amount"])
    return (
        (allow_final or row.get("reconciliation_status") not in {"approved", "reconciled", "ignored"})
        and _bank_transaction_id(transaction) == expected["transaction_id"]
        and amount is not None and expected_amount is not None and amount == expected_amount
        and AutomatedReconciliation._date(transaction.get("date") or transaction.get("transaction_date"))
        == AutomatedReconciliation._date(expected["date"])
        and AutomatedReconciliation._vendor_text(transaction) == AutomatedReconciliation._vendor_text(expected)
        and transaction.get("description") == expected["description"]
        and transaction.get("counterparty") == expected["counterparty"]
        and transaction.get("account_identifier") == expected["account_identifier"]
        and transaction.get("currency") == expected["currency"]
    )


def _document_snapshot(document: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": document.get("id"),
        "source": document.get("source"),
        "sourceDocumentId": document.get("source_document_id"),
        "originalFilename": document.get("original_filename"),
        "vendorName": document.get("vendor_name"),
        "category": document.get("category"),
        "transactionDate": document.get("transaction_date"),
        "totalAmount": document.get("total_amount"),
        "reconciliationStatus": document.get("reconciliation_status"),
    }


def _ad_hoc_account_identifier(bank_transaction: Dict[str, Any]) -> str:
    values = [bank_transaction[key] for key in ("account_identifier", "accountIdentifier") if key in bank_transaction]
    if any(value is not None and not isinstance(value, str) for value in values):
        raise ValueError("Bank account identifiers must be strings")
    accounts = {value if value is not None else "" for value in values}
    if len(accounts) > 1:
        raise ValueError("Bank account identifier aliases conflict")
    return next(iter(accounts), "")


def _bank_transaction_id(bank_transaction: Dict[str, Any]) -> str:
    references = []
    for key in ("id", "transaction_id"):
        if key not in bank_transaction:
            continue
        value = bank_transaction[key]
        if (not isinstance(value, (str, int)) or isinstance(value, bool)
                or isinstance(value, str) and not value.strip()):
            raise ValueError("Bank transaction references must be non-empty strings or integers")
        references.append(str(value))
    if len(set(references)) > 1:
        raise ValueError("Bank transaction reference aliases conflict")
    if references:
        return references[0]
    parts = [
        str(bank_transaction.get("date") or bank_transaction.get("transaction_date") or ""),
        str(bank_transaction.get("amount") or ""),
        str(bank_transaction.get("description") or bank_transaction.get("counterparty") or ""),
    ]
    return "bank:" + "|".join(parts)


def _valid_bank_identity(transaction: Any) -> bool:
    if not isinstance(transaction, dict):
        return False
    if any(key in transaction for key in BANK_LEDGER_ID_KEYS) and _ledger_bank_transaction_id(transaction) is None:
        return False
    try:
        _bank_transaction_id(transaction)
        _ad_hoc_account_identifier(transaction)
    except ValueError:
        return False
    return True


BANK_LEDGER_ID_KEYS = ("ledgerBankTransactionId", "ledger_bank_transaction_id",
                       "bankTransactionRecordId", "bank_transaction_record_id")


def _ledger_bank_transaction_id(bank_transaction: Dict[str, Any]) -> Optional[int]:
    return _positive_reference_id(bank_transaction, BANK_LEDGER_ID_KEYS)


def _capture_document_selection(document_ids: Optional[Iterable[int]]) -> Optional[tuple[int, ...]]:
    if document_ids is None:
        return None
    if isinstance(document_ids, (str, bytes, bytearray, dict, set, frozenset)):
        raise ValueError("documentIds must be an ordered collection of positive document references")
    try:
        iterator = iter(document_ids)
    except TypeError:
        raise ValueError("documentIds must be an ordered collection of positive document references") from None
    captured = []
    seen = set()
    for index, value in enumerate(iterator):
        if index >= MAX_RECONCILIATION_DOCUMENT_REFERENCES:
            raise ValueError("documentIds exceeds the 500-reference batch limit")
        reference = _positive_reference_id({"id": value}, ("id",))
        if reference is None:
            raise ValueError("documentIds must contain positive integer references, not booleans or fractions")
        if reference not in seen:
            captured.append(reference)
            seen.add(reference)
    return tuple(captured)


def _positive_reference_id(payload: Dict[str, Any], keys: Iterable[str]) -> Optional[int]:
    values = [payload[key] for key in keys if key in payload]
    parsed = []
    for value in values:
        if not isinstance(value, (int, str)) or isinstance(value, bool):
            return None
        bank_id = _int_or_none(value)
        if bank_id is None or not 0 < bank_id <= 2**63 - 1:
            return None
        parsed.append(bank_id)
    return parsed[0] if parsed and len(set(parsed)) == 1 else None


def _document_reconciliation_status(status: str) -> str:
    if status in FINAL_RECONCILIATION_STATUSES:
        return "reconciled"
    if status == "rejected":
        return "rejected"
    if status == "ignored":
        return "ignored"
    if status == "resolved":
        return "resolved"
    return "needs_review"


def _bank_reconciliation_status(status: str) -> str:
    if status in FINAL_RECONCILIATION_STATUSES:
        return "reconciled"
    if status == "rejected":
        return "rejected"
    if status == "ignored":
        return "ignored"
    if status == "resolved":
        return "resolved"
    if status == "missing_receipt":
        return "missing_receipt"
    if status == "candidate":
        return "candidate"
    return "needs_review"


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bounded_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 100
    return max(1, min(parsed, 500))


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
