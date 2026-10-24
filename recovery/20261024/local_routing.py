from typing import Any, Dict, List, Optional

from src.document_processors.document_type_classifier import is_non_posting_document_type
from src.data_entry.mijngeldzaken_autonomous_operator import MijngeldzakenAutonomousOperator
from src.data_entry.mijngeldzaken_surface import (
    build_mijngeldzaken_action_payload,
    classify_mijngeldzaken_destination,
    resolve_mijngeldzaken_action_for_document,
)
from src.data_entry.waveapps_autonomous_operator import WaveappsAutonomousOperator
from src.data_entry.waveapps_surface import (
    build_wave_action_payload,
    classify_wave_destination,
    resolve_wave_action_for_document,
)
from src.operations.local_bookkeeping_records import LocalBookkeepingRecordService
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_targets import resolve_document_target_system


ROUTABLE_DOCUMENT_STATUSES = ("processed", "reviewed", "validated", "ready_to_route")
OPEN_REVIEW_STATUSES = {"pending", "in_review"}
PREPARED_ROUTING_STATUSES = ("draft_prepared", "needs_confirmation", "queued")
WAVE_TARGET_SYSTEMS = {"wave", "waveapps", "waveapps_business", "waveapps_personal"}
MIJNGELDZAKEN_TARGET_SYSTEMS = {"mijngeldzaken", "mijngeldzaken_nl", "mijngeldzaken.nl", "category_a", "personal"}
ROUTABLE_BOOKKEEPING_RECORD_STATUSES = ("draft", "ready_to_route", "reviewed", "validated")
ROUTABLE_BOOKKEEPING_EXPORT_STATUSES = ("not_started", "ready")


class LocalRoutingService:
    """Prepare external bookkeeping drafts from reviewed local ledger documents.

    This service is intentionally non-submitting. It converts a local document
    into a Wave action plan, stores the plan as a routing attempt, and creates
    review items when the source evidence is not strong enough to prepare a
    draft. External writes remain behind the Wave operator and explicit approval.
    """

    def __init__(
        self,
        ledger: LocalOperationsLedger,
        config: Optional[Dict[str, Any]] = None,
        operator: Optional[WaveappsAutonomousOperator] = None,
        mijngeldzaken_operator: Optional[MijngeldzakenAutonomousOperator] = None,
    ):
        self.ledger = ledger
        self.config = config or {}
        self.operator = operator or WaveappsAutonomousOperator(self.config)
        self.mijngeldzaken_operator = mijngeldzaken_operator or MijngeldzakenAutonomousOperator(self.config)

    def prepare_document_route(
        self,
        document_id: int,
        target_system: Optional[str] = None,
        workflow_run_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        with self.ledger.write_transaction():
            return self._prepare_document_route(document_id, target_system, workflow_run_id)

    def _prepare_document_route(
        self,
        document_id: int,
        target_system: Optional[str] = None,
        workflow_run_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        document = self.ledger.get_document(document_id)
        if not document:
            return {"success": False, "status": "not_found", "error": "Document not found"}

        target_system = _normalize_target_system(target_system or _target_system(document))
        document_type_block = _document_type_routing_block(document, target_system)
        if document_type_block:
            if target_system in MIJNGELDZAKEN_TARGET_SYSTEMS:
                return self._record_mijngeldzaken_blocked_attempt(
                    document,
                    document_type_block["status"],
                    document_type_block["message"],
                    document_type_block["metadata"],
                    workflow_run_id=workflow_run_id,
                )
            if target_system in WAVE_TARGET_SYSTEMS:
                return self._record_blocked_attempt(
                    document,
                    document_type_block["status"],
                    document_type_block["message"],
                    document_type_block["metadata"],
                    workflow_run_id=workflow_run_id,
                )
        if target_system in MIJNGELDZAKEN_TARGET_SYSTEMS:
            return self._prepare_mijngeldzaken_document_route(document, target_system, workflow_run_id)
        if target_system not in WAVE_TARGET_SYSTEMS:
            return self._record_blocked_attempt(
                document,
                "unsupported_target",
                f"Routing target {target_system!r} is not supported by the local Wave draft router.",
                {"targetSystem": target_system},
                workflow_run_id=workflow_run_id,
            )

        if document.get("duplicate_of_document_id"):
            return self._record_blocked_attempt(
                document,
                "blocked_duplicate",
                f"Document duplicates #{document['duplicate_of_document_id']}; resolve duplicate review before routing.",
                {"duplicateOfDocumentId": document.get("duplicate_of_document_id")},
                workflow_run_id=workflow_run_id,
            )

        if document.get("processing_status") not in ROUTABLE_DOCUMENT_STATUSES:
            return self._record_blocked_attempt(
                document,
                "blocked_status",
                f"Document status {document.get('processing_status')} is not ready for export draft preparation.",
                {"processingStatus": document.get("processing_status")},
                workflow_run_id=workflow_run_id,
            )

        open_reviews = [
            item for item in document.get("review_items") or []
            if item.get("status") in OPEN_REVIEW_STATUSES
        ]
        if open_reviews:
            return self._record_blocked_attempt(
                document,
                "blocked_review",
                "Document still has open review items.",
                {"openReviewItemIds": [item["id"] for item in open_reviews]},
                workflow_run_id=workflow_run_id,
            )

        wave_inputs = build_wave_operation_inputs(document=document, config=self.config)
        document_data = wave_inputs["sourceData"]
        destination = wave_inputs["destination"]
        action_id = wave_inputs["actionId"]
        missing_fields = _missing_source_fields(document_data, action_id)
        if missing_fields:
            details = "Missing fields for Wave draft preparation: " + ", ".join(missing_fields)
            self._queue_review_once(
                document,
                "routing_fields_missing",
                details,
                {
                    "targetSystem": target_system,
                    "waveSurface": destination["target_surface"],
                    "waveAction": action_id,
                    "missingFields": missing_fields,
                },
            )
            return self._record_blocked_attempt(
                document,
                "needs_review",
                details,
                {
                    "targetSystem": target_system,
                    "destination": destination,
                    "actionId": action_id,
                    "missingFields": missing_fields,
                },
                workflow_run_id=workflow_run_id,
            )

        payload = wave_inputs["payload"]
        operation = self.operator.prepare_operation(
            action_id,
            payload,
            surface=destination["target_surface"],
            actor="fab_local_router",
        )
        existing_attempt = _find_existing_attempt(document, operation["operation_id"])
        if existing_attempt:
            LocalBookkeepingRecordService(self.ledger, self.config).record_export_state(
                int(document["id"]),
                "draft_prepared",
                status="export_draft_prepared",
                routing_attempt_id=int(existing_attempt["id"]),
                details={"status": "already_prepared"},
            )
            return {
                "success": True,
                "status": "already_prepared",
                "routingAttemptId": existing_attempt["id"],
                "documentId": document["id"],
                "target": existing_attempt["target"],
                "operation": existing_attempt.get("metadata", {}).get("operation"),
            }

        plan = operation["plan"]
        if plan.get("status") != "planned":
            self._queue_review_once(
                document,
                "routing_plan_incomplete",
                plan.get("message") or "Wave routing plan is incomplete.",
                {
                    "targetSystem": target_system,
                    "waveSurface": destination["target_surface"],
                    "waveAction": action_id,
                    "missingFields": plan.get("missing_fields", []),
                },
            )
            return self._record_blocked_attempt(
                document,
                "needs_review",
                plan.get("message") or "Wave routing plan is incomplete.",
                {
                    "targetSystem": target_system,
                    "destination": destination,
                    "operation": operation,
                },
                workflow_run_id=workflow_run_id,
            )

        routing_status = (
            "needs_confirmation"
            if operation.get("safety") == "requires_confirmation"
            else "draft_prepared"
        )
        routing_id = self.ledger.create_routing_attempt({
            "documentId": document["id"],
            "workflowRunId": workflow_run_id,
            "target": f"waveapps:{destination['target_surface']}",
            "status": routing_status,
            "message": "Wave draft operation prepared; external submission remains approval-gated.",
            "metadata": {
                "targetSystem": target_system,
                "destination": destination,
                "operation": operation,
                "documentSnapshot": _document_snapshot(document),
                "externalSubmission": "not_executed",
                "approvalRequiredForSubmit": True,
            },
        })
        self.ledger.update_document(int(document["id"]), {
            "processingStatus": "export_draft_prepared",
            "metadata": _merged_metadata(
                document,
                {
                    "routing": {
                        "targetSystem": target_system,
                        "routingAttemptId": routing_id,
                        "waveSurface": destination["target_surface"],
                        "waveAction": action_id,
                        "status": routing_status,
                    }
                },
            ),
        })
        LocalBookkeepingRecordService(self.ledger, self.config).record_export_state(
            int(document["id"]),
            "draft_prepared",
            status="export_draft_prepared",
            routing_attempt_id=routing_id,
            details={
                "targetSystem": target_system,
                "waveSurface": destination["target_surface"],
                "waveAction": action_id,
                "operationId": operation["operation_id"],
            },
        )
        self.ledger.record_audit_event({
            "action": "local_routing.wave_draft_prepared",
            "entityType": "bookkeeping_document",
            "entityId": str(document["id"]),
            "details": {
                "routingAttemptId": routing_id,
                "targetSystem": target_system,
                "surface": destination["target_surface"],
                "actionId": action_id,
                "operationId": operation["operation_id"],
                "safety": operation.get("safety"),
                "externalSubmission": "not_executed",
            },
        })
        return {
            "success": True,
            "status": routing_status,
            "routingAttemptId": routing_id,
            "documentId": document["id"],
            "target": f"waveapps:{destination['target_surface']}",
            "operation": operation,
        }

    def prepare_bookkeeping_record_route(
        self,
        record_id: int,
        target_system: Optional[str] = None,
        workflow_run_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        with self.ledger.write_transaction():
            return self._prepare_bookkeeping_record_route(record_id, target_system, workflow_run_id)

    def _prepare_bookkeeping_record_route(
        self,
        record_id: int,
        target_system: Optional[str] = None,
        workflow_run_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        record = self.ledger.get_bookkeeping_record(record_id)
        if not record:
            return {"success": False, "status": "not_found", "error": "Bookkeeping record not found"}

        if record.get("source_type") != "bank_transaction":
            return self._record_blocked_record_attempt(
                record,
                "blocked_source_type",
                "Document-backed records must be routed through their source document evidence.",
                {"sourceType": record.get("source_type")},
                workflow_run_id=workflow_run_id,
            )

        target_system = _normalize_target_system(target_system or record.get("target_system"))
        if target_system in MIJNGELDZAKEN_TARGET_SYSTEMS:
            return self._prepare_mijngeldzaken_bookkeeping_record_route(record, target_system, workflow_run_id)
        if target_system not in WAVE_TARGET_SYSTEMS:
            return self._record_blocked_record_attempt(
                record,
                "unsupported_target",
                f"Routing target {target_system!r} is not supported by the local Wave draft router.",
                {"targetSystem": target_system},
                workflow_run_id=workflow_run_id,
            )

        existing_attempt = _find_existing_record_attempt(
            self.ledger,
            int(record["id"]),
            statuses=PREPARED_ROUTING_STATUSES,
        )
        if existing_attempt:
            _update_record_export_state(
                self.ledger,
                record,
                "draft_prepared",
                status="export_draft_prepared",
                routing_attempt_id=int(existing_attempt["id"]),
                details={"status": "already_prepared"},
            )
            return {
                "success": True,
                "status": "already_prepared",
                "routingAttemptId": existing_attempt["id"],
                "bookkeepingRecordId": record["id"],
                "target": existing_attempt["target"],
                "operation": existing_attempt.get("metadata", {}).get("operation"),
            }

        if record.get("review_required"):
            return self._record_blocked_record_attempt(
                record,
                "blocked_review",
                "Bookkeeping record still requires manual review.",
                {"reviewRequired": True},
                workflow_run_id=workflow_run_id,
            )

        if record.get("status") not in ROUTABLE_BOOKKEEPING_RECORD_STATUSES:
            return self._record_blocked_record_attempt(
                record,
                "blocked_status",
                f"Bookkeeping record status {record.get('status')} is not ready for export draft preparation.",
                {"recordStatus": record.get("status")},
                workflow_run_id=workflow_run_id,
            )

        if record.get("export_status") not in ROUTABLE_BOOKKEEPING_EXPORT_STATUSES:
            return self._record_blocked_record_attempt(
                record,
                "blocked_export_status",
                f"Bookkeeping record export status {record.get('export_status')} is not ready for draft preparation.",
                {"exportStatus": record.get("export_status")},
                workflow_run_id=workflow_run_id,
            )

        wave_inputs = build_wave_operation_inputs(record=record, config=self.config)
        record_data = wave_inputs["sourceData"]
        destination = wave_inputs["destination"]
        action_id = wave_inputs["actionId"]
        missing_fields = _missing_source_fields(record_data, action_id)
        if missing_fields:
            return self._record_blocked_record_attempt(
                record,
                "needs_review",
                "Missing fields for Wave draft preparation: " + ", ".join(missing_fields),
                {
                    "targetSystem": target_system,
                    "destination": destination,
                    "actionId": action_id,
                    "missingFields": missing_fields,
                },
                workflow_run_id=workflow_run_id,
            )

        payload = wave_inputs["payload"]
        operation = self.operator.prepare_operation(
            action_id,
            payload,
            surface=destination["target_surface"],
            actor="fab_local_router",
        )
        operation_existing_attempt = _find_existing_record_attempt(
            self.ledger,
            int(record["id"]),
            operation_id=operation["operation_id"],
            statuses=PREPARED_ROUTING_STATUSES,
        )
        if operation_existing_attempt:
            _update_record_export_state(
                self.ledger,
                record,
                "draft_prepared",
                status="export_draft_prepared",
                routing_attempt_id=int(operation_existing_attempt["id"]),
                details={"status": "already_prepared"},
            )
            return {
                "success": True,
                "status": "already_prepared",
                "routingAttemptId": operation_existing_attempt["id"],
                "bookkeepingRecordId": record["id"],
                "target": operation_existing_attempt["target"],
                "operation": operation_existing_attempt.get("metadata", {}).get("operation"),
            }

        plan = operation["plan"]
        if plan.get("status") != "planned":
            return self._record_blocked_record_attempt(
                record,
                "needs_review",
                plan.get("message") or "Wave routing plan is incomplete.",
                {
                    "targetSystem": target_system,
                    "destination": destination,
                    "operation": operation,
                },
                workflow_run_id=workflow_run_id,
            )

        routing_status = (
            "needs_confirmation"
            if operation.get("safety") == "requires_confirmation"
            else "draft_prepared"
        )
        routing_id = self.ledger.create_routing_attempt({
            "bookkeepingRecordId": record["id"],
            "workflowRunId": workflow_run_id,
            "target": f"waveapps:{destination['target_surface']}",
            "status": routing_status,
            "message": "Wave bank transaction draft operation prepared; external submission remains approval-gated.",
            "metadata": {
                "targetSystem": target_system,
                "bookkeepingRecordId": record["id"],
                "bankTransactionId": record.get("bank_transaction_id"),
                "sourceType": record.get("source_type"),
                "destination": destination,
                "operation": operation,
                "bookkeepingRecordSnapshot": _bookkeeping_record_snapshot(record),
                "externalSubmission": "not_executed",
                "approvalRequiredForSubmit": True,
            },
        })
        _update_record_export_state(
            self.ledger,
            record,
            "draft_prepared",
            status="export_draft_prepared",
            routing_attempt_id=routing_id,
            details={
                "targetSystem": target_system,
                "waveSurface": destination["target_surface"],
                "waveAction": action_id,
                "operationId": operation["operation_id"],
            },
        )
        self.ledger.record_audit_event({
            "action": "local_routing.bank_record_wave_draft_prepared",
            "entityType": "bookkeeping_record",
            "entityId": str(record["id"]),
            "details": {
                "routingAttemptId": routing_id,
                "bankTransactionId": record.get("bank_transaction_id"),
                "targetSystem": target_system,
                "surface": destination["target_surface"],
                "actionId": action_id,
                "operationId": operation["operation_id"],
                "safety": operation.get("safety"),
                "externalSubmission": "not_executed",
            },
        })
        return {
            "success": True,
            "status": routing_status,
            "routingAttemptId": routing_id,
            "bookkeepingRecordId": record["id"],
            "bankTransactionId": record.get("bank_transaction_id"),
            "target": f"waveapps:{destination['target_surface']}",
            "operation": operation,
        }

    def prepare_ready_documents(self, limit: int = 25) -> Dict[str, Any]:
        documents = self.ledger.list_documents(status=ROUTABLE_DOCUMENT_STATUSES, limit=limit)
        summary = {
            "requested": len(documents),
            "draftPrepared": 0,
            "alreadyPrepared": 0,
            "needsReview": 0,
            "blocked": 0,
            "documents": [],
        }
        for document in documents:
            result = self.prepare_document_route(int(document["id"]))
            status = result.get("status")
            if status == "draft_prepared":
                summary["draftPrepared"] += 1
            elif status == "already_prepared":
                summary["alreadyPrepared"] += 1
            elif status == "needs_review":
                summary["needsReview"] += 1
            else:
                summary["blocked"] += 1
            summary["documents"].append(result)

        self.ledger.record_audit_event({
            "action": "local_routing.batch_prepare_completed",
            "entityType": "routing_attempt",
            "details": {
                "requested": summary["requested"],
                "draftPrepared": summary["draftPrepared"],
                "alreadyPrepared": summary["alreadyPrepared"],
                "needsReview": summary["needsReview"],
                "blocked": summary["blocked"],
            },
        })
        return summary

    def _prepare_mijngeldzaken_document_route(
        self,
        document: Dict[str, Any],
        target_system: str,
        workflow_run_id: Optional[int],
    ) -> Dict[str, Any]:
        blocked = self._mijngeldzaken_document_block(document)
        if blocked:
            return self._record_mijngeldzaken_blocked_attempt(
                document,
                blocked["status"],
                blocked["message"],
                blocked["metadata"],
                workflow_run_id=workflow_run_id,
            )

        document_data = _document_data(document)
        destination = classify_mijngeldzaken_destination(document_data)
        action_id = resolve_mijngeldzaken_action_for_document(document_data)
        missing_fields = _missing_mijngeldzaken_source_fields(document_data, action_id)
        if missing_fields:
            details = "Missing fields for MijnGeldzaken master-ledger draft: " + ", ".join(missing_fields)
            self._queue_review_once(
                document,
                "mijngeldzaken_routing_fields_missing",
                details,
                {
                    "targetSystem": target_system,
                    "mijngeldzakenSurface": destination["target_surface"],
                    "mijngeldzakenAction": action_id,
                    "missingFields": missing_fields,
                },
            )
            return self._record_mijngeldzaken_blocked_attempt(
                document,
                "needs_review",
                details,
                {
                    "targetSystem": target_system,
                    "destination": destination,
                    "actionId": action_id,
                    "missingFields": missing_fields,
                },
                workflow_run_id=workflow_run_id,
            )

        payload = build_mijngeldzaken_action_payload(
            document_data,
            _mijngeldzaken_category(document_data, self.config),
            default_account=_config_value(
                self.config,
                "fab_mijngeldzaken_default_account",
                "operations_mijngeldzaken_default_account",
                "mijngeldzaken_default_account",
                default="Huishouden",
            ),
        )
        operation = self.mijngeldzaken_operator.prepare_operation(
            action_id,
            payload,
            surface=destination["target_surface"],
            actor="fab_local_router",
        )
        existing_attempt = _find_existing_attempt(document, operation["operation_id"])
        if existing_attempt:
            LocalBookkeepingRecordService(self.ledger, self.config).record_export_state(
                int(document["id"]),
                "draft_prepared",
                status="export_draft_prepared",
                routing_attempt_id=int(existing_attempt["id"]),
                details={"status": "already_prepared"},
            )
            return {
                "success": True,
                "status": "already_prepared",
                "routingAttemptId": existing_attempt["id"],
                "documentId": document["id"],
                "target": existing_attempt["target"],
                "operation": existing_attempt.get("metadata", {}).get("operation"),
            }

        plan = operation["plan"]
        if plan.get("status") != "planned":
            return self._record_mijngeldzaken_blocked_attempt(
                document,
                "needs_review",
                plan.get("message") or "MijnGeldzaken routing plan is incomplete.",
                {
                    "targetSystem": target_system,
                    "destination": destination,
                    "operation": operation,
                },
                workflow_run_id=workflow_run_id,
            )

        routing_status = "needs_confirmation" if operation.get("safety") == "requires_confirmation" else "draft_prepared"
        routing_id = self.ledger.create_routing_attempt({
            "documentId": document["id"],
            "workflowRunId": workflow_run_id,
            "target": f"mijngeldzaken:{destination['target_surface']}",
            "status": routing_status,
            "message": "MijnGeldzaken master-ledger draft prepared; external submission remains approval-gated.",
            "metadata": {
                "targetSystem": target_system,
                "destination": destination,
                "operation": operation,
                "documentSnapshot": _document_snapshot(document),
                "externalSubmission": "not_executed",
                "approvalRequiredForSubmit": True,
                "masterLedgerDownstream": True,
            },
        })
        self.ledger.update_document(int(document["id"]), {
            "processingStatus": "export_draft_prepared",
            "metadata": _merged_metadata(
                document,
                {
                    "routing": {
                        "targetSystem": target_system,
                        "routingAttemptId": routing_id,
                        "mijngeldzakenSurface": destination["target_surface"],
                        "mijngeldzakenAction": action_id,
                        "status": routing_status,
                    }
                },
            ),
        })
        LocalBookkeepingRecordService(self.ledger, self.config).record_export_state(
            int(document["id"]),
            "draft_prepared",
            status="export_draft_prepared",
            routing_attempt_id=routing_id,
            details={
                "targetSystem": target_system,
                "mijngeldzakenSurface": destination["target_surface"],
                "mijngeldzakenAction": action_id,
                "operationId": operation["operation_id"],
            },
        )
        self.ledger.record_audit_event({
            "action": "local_routing.mijngeldzaken_draft_prepared",
            "entityType": "bookkeeping_document",
            "entityId": str(document["id"]),
            "details": {
                "routingAttemptId": routing_id,
                "targetSystem": target_system,
                "surface": destination["target_surface"],
                "actionId": action_id,
                "operationId": operation["operation_id"],
                "safety": operation.get("safety"),
                "externalSubmission": "not_executed",
                "masterLedgerDownstream": True,
            },
        })
        return {
            "success": True,
            "status": routing_status,
            "routingAttemptId": routing_id,
            "documentId": document["id"],
            "target": f"mijngeldzaken:{destination['target_surface']}",
            "operation": operation,
        }

    def _prepare_mijngeldzaken_bookkeeping_record_route(
        self,
        record: Dict[str, Any],
        target_system: str,
        workflow_run_id: Optional[int],
    ) -> Dict[str, Any]:
        if record.get("review_required"):
            return self._record_mijngeldzaken_blocked_record_attempt(
                record,
                "blocked_review",
                "Bookkeeping record still requires manual review.",
                {"reviewRequired": True},
                workflow_run_id=workflow_run_id,
            )
        if record.get("status") not in ROUTABLE_BOOKKEEPING_RECORD_STATUSES:
            return self._record_mijngeldzaken_blocked_record_attempt(
                record,
                "blocked_status",
                f"Bookkeeping record status {record.get('status')} is not ready for master-ledger draft preparation.",
                {"recordStatus": record.get("status")},
                workflow_run_id=workflow_run_id,
            )
        if record.get("export_status") not in ROUTABLE_BOOKKEEPING_EXPORT_STATUSES:
            return self._record_mijngeldzaken_blocked_record_attempt(
                record,
                "blocked_export_status",
                f"Bookkeeping record export status {record.get('export_status')} is not ready for draft preparation.",
                {"exportStatus": record.get("export_status")},
                workflow_run_id=workflow_run_id,
            )
        record_data = _bookkeeping_record_data(record)
        destination = classify_mijngeldzaken_destination(record_data)
        action_id = resolve_mijngeldzaken_action_for_document(record_data)
        missing_fields = _missing_mijngeldzaken_source_fields(record_data, action_id)
        if missing_fields:
            return self._record_mijngeldzaken_blocked_record_attempt(
                record,
                "needs_review",
                "Missing fields for MijnGeldzaken master-ledger draft: " + ", ".join(missing_fields),
                {
                    "targetSystem": target_system,
                    "destination": destination,
                    "actionId": action_id,
                    "missingFields": missing_fields,
                },
                workflow_run_id=workflow_run_id,
            )
        payload = build_mijngeldzaken_action_payload(
            record_data,
            _mijngeldzaken_category(record_data, self.config),
            default_account=_config_value(
                self.config,
                "fab_mijngeldzaken_default_account",
                "operations_mijngeldzaken_default_account",
                "mijngeldzaken_default_account",
                default="Huishouden",
            ),
        )
        operation = self.mijngeldzaken_operator.prepare_operation(
            action_id,
            payload,
            surface=destination["target_surface"],
            actor="fab_local_router",
        )
        existing_attempt = _find_existing_record_attempt(
            self.ledger,
            int(record["id"]),
            operation_id=operation["operation_id"],
            statuses=PREPARED_ROUTING_STATUSES,
        )
        if existing_attempt:
            _update_record_export_state(
                self.ledger,
                record,
                "draft_prepared",
                status="export_draft_prepared",
                routing_attempt_id=int(existing_attempt["id"]),
                details={"status": "already_prepared"},
            )
            return {
                "success": True,
                "status": "already_prepared",
                "routingAttemptId": existing_attempt["id"],
                "bookkeepingRecordId": record["id"],
                "target": existing_attempt["target"],
                "operation": existing_attempt.get("metadata", {}).get("operation"),
            }
        plan = operation["plan"]
        if plan.get("status") != "planned":
            return self._record_mijngeldzaken_blocked_record_attempt(
                record,
                "needs_review",
                plan.get("message") or "MijnGeldzaken routing plan is incomplete.",
                {
                    "targetSystem": target_system,
                    "destination": destination,
                    "operation": operation,
                },
                workflow_run_id=workflow_run_id,
            )
        routing_status = "needs_confirmation" if operation.get("safety") == "requires_confirmation" else "draft_prepared"
        routing_id = self.ledger.create_routing_attempt({
            "bookkeepingRecordId": record["id"],
            "workflowRunId": workflow_run_id,
            "target": f"mijngeldzaken:{destination['target_surface']}",
            "status": routing_status,
            "message": "MijnGeldzaken bank transaction master-ledger draft prepared; external submission remains approval-gated.",
            "metadata": {
                "targetSystem": target_system,
                "bookkeepingRecordId": record["id"],
                "bankTransactionId": record.get("bank_transaction_id"),
                "sourceType": record.get("source_type"),
                "destination": destination,
                "operation": operation,
                "bookkeepingRecordSnapshot": _bookkeeping_record_snapshot(record),
                "externalSubmission": "not_executed",
                "approvalRequiredForSubmit": True,
                "masterLedgerDownstream": True,
            },
        })
        _update_record_export_state(
            self.ledger,
            record,
            "draft_prepared",
            status="export_draft_prepared",
            routing_attempt_id=routing_id,
            details={
                "targetSystem": target_system,
                "mijngeldzakenSurface": destination["target_surface"],
                "mijngeldzakenAction": action_id,
                "operationId": operation["operation_id"],
            },
        )
        self.ledger.record_audit_event({
            "action": "local_routing.bank_record_mijngeldzaken_draft_prepared",
            "entityType": "bookkeeping_record",
            "entityId": str(record["id"]),
            "details": {
                "routingAttemptId": routing_id,
                "bankTransactionId": record.get("bank_transaction_id"),
                "targetSystem": target_system,
                "surface": destination["target_surface"],
                "actionId": action_id,
                "operationId": operation["operation_id"],
                "safety": operation.get("safety"),
                "externalSubmission": "not_executed",
                "masterLedgerDownstream": True,
            },
        })
        return {
            "success": True,
            "status": routing_status,
            "routingAttemptId": routing_id,
            "bookkeepingRecordId": record["id"],
            "bankTransactionId": record.get("bank_transaction_id"),
            "target": f"mijngeldzaken:{destination['target_surface']}",
            "operation": operation,
        }

    def prepare_ready_bookkeeping_records(self, limit: int = 25) -> Dict[str, Any]:
        records = [
            record for record in self.ledger.list_bookkeeping_records(
                status=ROUTABLE_BOOKKEEPING_RECORD_STATUSES,
                export_status=ROUTABLE_BOOKKEEPING_EXPORT_STATUSES,
                limit=limit,
            )
            if record.get("source_type") == "bank_transaction"
        ]
        summary = {
            "requested": len(records),
            "draftPrepared": 0,
            "alreadyPrepared": 0,
            "needsReview": 0,
            "blocked": 0,
            "bookkeepingRecords": [],
        }
        for record in records:
            result = self.prepare_bookkeeping_record_route(int(record["id"]))
            status = result.get("status")
            if status == "draft_prepared":
                summary["draftPrepared"] += 1
            elif status == "already_prepared":
                summary["alreadyPrepared"] += 1
            elif status == "needs_review":
                summary["needsReview"] += 1
            else:
                summary["blocked"] += 1
            summary["bookkeepingRecords"].append(result)

        self.ledger.record_audit_event({
            "action": "local_routing.bank_record_batch_prepare_completed",
            "entityType": "bookkeeping_record",
            "details": {
                "requested": summary["requested"],
                "draftPrepared": summary["draftPrepared"],
                "alreadyPrepared": summary["alreadyPrepared"],
                "needsReview": summary["needsReview"],
                "blocked": summary["blocked"],
            },
        })
        return summary

    def _record_blocked_attempt(
        self,
        document: Dict[str, Any],
        status: str,
        message: str,
        metadata: Dict[str, Any],
        workflow_run_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        routing_id = self.ledger.create_routing_attempt({
            "documentId": document["id"],
            "workflowRunId": workflow_run_id,
            "target": "waveapps",
            "status": status,
            "message": message,
            "metadata": {
                **metadata,
                "documentSnapshot": _document_snapshot(document),
                "externalSubmission": "not_executed",
            },
        })
        self.ledger.record_audit_event({
            "action": "local_routing.wave_draft_blocked",
            "entityType": "bookkeeping_document",
            "entityId": str(document["id"]),
            "details": {
                "routingAttemptId": routing_id,
                "status": status,
                "message": message,
                **metadata,
            },
        })
        if status == "needs_review":
            self.ledger.update_document(int(document["id"]), {
                "processingStatus": "needs_review",
                "metadata": _merged_metadata(
                    document,
                    {
                        "routing": {
                            "status": status,
                            "routingAttemptId": routing_id,
                            "message": message,
                        }
                    },
                ),
            })
        LocalBookkeepingRecordService(self.ledger, self.config).record_export_state(
            int(document["id"]),
            _blocked_export_status(status),
            status="needs_review" if status == "needs_review" else None,
            routing_attempt_id=routing_id,
            details={"routingStatus": status, "message": message},
        )
        return {
            "success": False,
            "status": status,
            "routingAttemptId": routing_id,
            "documentId": document["id"],
            "message": message,
        }

    def _record_blocked_record_attempt(
        self,
        record: Dict[str, Any],
        status: str,
        message: str,
        metadata: Dict[str, Any],
        workflow_run_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        routing_id = self.ledger.create_routing_attempt({
            "bookkeepingRecordId": record.get("id"),
            "workflowRunId": workflow_run_id,
            "target": "waveapps",
            "status": status,
            "message": message,
            "metadata": {
                **metadata,
                "bookkeepingRecordId": record.get("id"),
                "bankTransactionId": record.get("bank_transaction_id"),
                "sourceType": record.get("source_type"),
                "bookkeepingRecordSnapshot": _bookkeeping_record_snapshot(record),
                "externalSubmission": "not_executed",
            },
        })
        _update_record_export_state(
            self.ledger,
            record,
            _blocked_export_status(status),
            status="needs_review" if status == "needs_review" else None,
            routing_attempt_id=routing_id,
            details={"routingStatus": status, "message": message},
        )
        self.ledger.record_audit_event({
            "action": "local_routing.bank_record_wave_draft_blocked",
            "entityType": "bookkeeping_record",
            "entityId": str(record.get("id")),
            "details": {
                "routingAttemptId": routing_id,
                "bankTransactionId": record.get("bank_transaction_id"),
                "status": status,
                "message": message,
                **metadata,
            },
        })
        return {
            "success": False,
            "status": status,
            "routingAttemptId": routing_id,
            "bookkeepingRecordId": record.get("id"),
            "bankTransactionId": record.get("bank_transaction_id"),
            "message": message,
        }

    def _mijngeldzaken_document_block(self, document: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if document.get("duplicate_of_document_id"):
            return {
                "status": "blocked_duplicate",
                "message": f"Document duplicates #{document['duplicate_of_document_id']}; resolve duplicate review before routing.",
                "metadata": {"duplicateOfDocumentId": document.get("duplicate_of_document_id")},
            }
        if document.get("processing_status") not in ROUTABLE_DOCUMENT_STATUSES:
            return {
                "status": "blocked_status",
                "message": f"Document status {document.get('processing_status')} is not ready for master-ledger draft preparation.",
                "metadata": {"processingStatus": document.get("processing_status")},
            }
        open_reviews = [
            item for item in document.get("review_items") or []
            if item.get("status") in OPEN_REVIEW_STATUSES
        ]
        if open_reviews:
            return {
                "status": "blocked_review",
                "message": "Document still has open review items.",
                "metadata": {"openReviewItemIds": [item["id"] for item in open_reviews]},
            }
        return None

    def _record_mijngeldzaken_blocked_attempt(
        self,
        document: Dict[str, Any],
        status: str,
        message: str,
        metadata: Dict[str, Any],
        workflow_run_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        routing_id = self.ledger.create_routing_attempt({
            "documentId": document["id"],
            "workflowRunId": workflow_run_id,
            "target": "mijngeldzaken",
            "status": status,
            "message": message,
            "metadata": {
                **metadata,
                "documentSnapshot": _document_snapshot(document),
                "externalSubmission": "not_executed",
                "masterLedgerDownstream": True,
            },
        })
        self.ledger.record_audit_event({
            "action": "local_routing.mijngeldzaken_draft_blocked",
            "entityType": "bookkeeping_document",
            "entityId": str(document["id"]),
            "details": {
                "routingAttemptId": routing_id,
                "status": status,
                "message": message,
                **metadata,
            },
        })
        if status == "needs_review":
            self.ledger.update_document(int(document["id"]), {
                "processingStatus": "needs_review",
                "metadata": _merged_metadata(
                    document,
                    {
                        "routing": {
                            "status": status,
                            "routingAttemptId": routing_id,
                            "message": message,
                        }
                    },
                ),
            })
        LocalBookkeepingRecordService(self.ledger, self.config).record_export_state(
            int(document["id"]),
            _blocked_export_status(status),
            status="needs_review" if status == "needs_review" else None,
            routing_attempt_id=routing_id,
            details={"routingStatus": status, "message": message},
        )
        return {
            "success": False,
            "status": status,
            "routingAttemptId": routing_id,
            "documentId": document["id"],
            "message": message,
        }

    def _record_mijngeldzaken_blocked_record_attempt(
        self,
        record: Dict[str, Any],
        status: str,
        message: str,
        metadata: Dict[str, Any],
        workflow_run_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        routing_id = self.ledger.create_routing_attempt({
            "bookkeepingRecordId": record.get("id"),
            "workflowRunId": workflow_run_id,
            "target": "mijngeldzaken",
            "status": status,
            "message": message,
            "metadata": {
                **metadata,
                "bookkeepingRecordId": record.get("id"),
                "bankTransactionId": record.get("bank_transaction_id"),
                "sourceType": record.get("source_type"),
                "bookkeepingRecordSnapshot": _bookkeeping_record_snapshot(record),
                "externalSubmission": "not_executed",
                "masterLedgerDownstream": True,
            },
        })
        _update_record_export_state(
            self.ledger,
            record,
            _blocked_export_status(status),
            status="needs_review" if status == "needs_review" else None,
            routing_attempt_id=routing_id,
            details={"routingStatus": status, "message": message},
        )
        self.ledger.record_audit_event({
            "action": "local_routing.bank_record_mijngeldzaken_draft_blocked",
            "entityType": "bookkeeping_record",
            "entityId": str(record.get("id")),
            "details": {
                "routingAttemptId": routing_id,
                "bankTransactionId": record.get("bank_transaction_id"),
                "status": status,
                "message": message,
                **metadata,
            },
        })
        return {
            "success": False,
            "status": status,
            "routingAttemptId": routing_id,
            "bookkeepingRecordId": record.get("id"),
            "bankTransactionId": record.get("bank_transaction_id"),
            "message": message,
        }

    def _queue_review_once(
        self,
        document: Dict[str, Any],
        reason: str,
        details: str,
        corrected_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        for item in document.get("review_items") or []:
            if item.get("reason") == reason and item.get("status") in OPEN_REVIEW_STATUSES:
                return
        self.ledger.create_review_item({
            "documentId": document["id"],
            "reason": reason,
            "details": details,
            "correctedData": corrected_data or {},
        })


def _document_data(document: Dict[str, Any]) -> Dict[str, Any]:
    extracted = dict(document.get("extracted_data") or {})
    record = document.get("bookkeeping_record") or {}
    record_line_items = record.get("line_items") or []
    if record_line_items:
        extracted["line_items"] = [_wave_line_item_from_record_line(item) for item in record_line_items]
    extracted.setdefault("vendor_name", document.get("vendor_name"))
    extracted.setdefault("transaction_date", document.get("transaction_date"))
    extracted.setdefault("total_amount", document.get("total_amount"))
    extracted.setdefault("vat_amount", document.get("vat_amount"))
    extracted.setdefault("document_type", document.get("document_type"))
    return {
        "id": document.get("id"),
        "document_type": document.get("document_type"),
        "vendor_name": document.get("vendor_name"),
        "category": document.get("category"),
        "transaction_date": document.get("transaction_date"),
        "total_amount": document.get("total_amount"),
        "vat_amount": document.get("vat_amount"),
        "description": document.get("original_filename"),
        "line_items": extracted.get("line_items") or extracted.get("lineItems") or [],
        "extracted_data": extracted,
    }


def _wave_line_item_from_record_line(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "description": item.get("description") or item.get("item_name"),
        "amount": item.get("amount"),
        "category": item.get("category"),
        "accountName": item.get("account_name"),
        "quantity": item.get("quantity"),
        "unitPrice": item.get("unit_price"),
        "taxCode": item.get("tax_code"),
        "taxAmount": item.get("tax_amount"),
    }


def _bookkeeping_record_data(record: Dict[str, Any]) -> Dict[str, Any]:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    document_type = str(metadata.get("documentType") or "receipt").strip().lower()
    amount = (
        _negative_amount(record.get("amount"))
        if document_type == "credit_note"
        else _positive_amount(record.get("amount"))
    )
    extracted = {
        "vendor_name": record.get("vendor_name"),
        "transaction_date": record.get("record_date"),
        "total_amount": amount,
        "currency": record.get("currency"),
        "description": record.get("description"),
        "document_type": document_type,
        "line_items": [_wave_line_item_from_record_line(item) for item in record.get("line_items") or []],
    }
    return {
        "id": record.get("id"),
        "document_type": document_type,
        "vendor_name": record.get("vendor_name"),
        "category": record.get("category"),
        "transaction_date": record.get("record_date"),
        "total_amount": amount,
        "vat_amount": record.get("vat_amount"),
        "description": record.get("description"),
        "line_items": extracted["line_items"],
        "extracted_data": extracted,
    }


def build_wave_operation_inputs(
    *,
    config: Optional[Dict[str, Any]] = None,
    document: Optional[Dict[str, Any]] = None,
    record: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the same canonical Wave action inputs for routing and freshness checks."""
    if document is not None:
        source_data = _document_data(document)
    elif record is not None:
        source_data = _bookkeeping_record_data(record)
    else:
        raise ValueError("A document or bookkeeping record is required to build a Wave operation")

    settings = config or {}
    destination = classify_wave_destination(source_data)
    action_id = resolve_wave_action_for_document(source_data)
    payload = build_wave_action_payload(
        source_data,
        source_data["category"],
        default_account=_config_value(
            settings,
            "fab_waveapps_default_account",
            "operations_waveapps_default_account",
            "waveapps_default_account",
            default="Uncategorized",
        ),
    )
    return {
        "sourceData": source_data,
        "destination": destination,
        "actionId": action_id,
        "payload": payload,
    }


def _missing_source_fields(document_data: Dict[str, Any], action_id: str) -> List[str]:
    extracted = document_data.get("extracted_data") or {}
    missing = []
    if _blank(document_data.get("category")):
        missing.append("category")
    if _blank(document_data.get("total_amount")):
        missing.append("totalAmount")

    if action_id in {"transaction_add", "bill_create"} and _blank(document_data.get("transaction_date")):
        missing.append("transactionDate")
    if action_id == "bill_create" and _blank(document_data.get("vendor_name")):
        missing.append("vendorName")
    if action_id in {"invoice_create", "estimate_create"}:
        customer = extracted.get("customer_name") or document_data.get("customer_name") or document_data.get("vendor_name")
        if _blank(customer):
            missing.append("customer")
    return missing


def _missing_mijngeldzaken_source_fields(document_data: Dict[str, Any], action_id: str) -> List[str]:
    extracted = document_data.get("extracted_data") or {}
    if action_id == "document_upload_prepare":
        missing = []
        if _blank(document_data.get("id")):
            missing.append("documentId")
        if _blank(document_data.get("description")):
            missing.append("filename")
        return missing
    missing = []
    if _blank(document_data.get("category")):
        missing.append("category")
    if _blank(document_data.get("transaction_date")):
        missing.append("transactionDate")
    if _blank(document_data.get("total_amount")):
        missing.append("totalAmount")
    description = extracted.get("description") or document_data.get("description") or document_data.get("vendor_name")
    if _blank(description):
        missing.append("description")
    return missing


def _mijngeldzaken_category(document_data: Dict[str, Any], config: Dict[str, Any]) -> str:
    category = str(document_data.get("category") or "").strip()
    mapping = config.get("mijngeldzaken_category_mapping") or config.get("fab_mijngeldzaken_category_mapping") or {}
    if isinstance(mapping, dict):
        return str(mapping.get(category) or category or "Overig")
    return category or "Overig"


def _blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def _document_type_routing_block(document: Dict[str, Any], target_system: str) -> Optional[Dict[str, Any]]:
    extracted = document.get("extracted_data") if isinstance(document.get("extracted_data"), dict) else {}
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    review_metadata = metadata.get("review") if isinstance(metadata.get("review"), dict) else {}
    override = review_metadata.get("documentTypeOverride")
    override_type = (
        str(override.get("documentType") or "").strip().lower()
        if isinstance(override, dict)
        else ""
    )
    stored_type = str(document.get("document_type") or extracted.get("document_type") or "").strip().lower()
    processing = metadata.get("processing") if isinstance(metadata.get("processing"), dict) else {}
    classification = (
        processing.get("documentTypeClassification")
        if isinstance(processing.get("documentTypeClassification"), dict)
        else {}
    )
    classified_type = str(classification.get("documentType") or "").strip().lower()
    effective_type = override_type or stored_type

    if is_non_posting_document_type(effective_type):
        return {
            "status": "blocked_non_posting_document_type",
            "message": (
                f"Document type {effective_type!r} is supporting evidence and cannot be posted "
                "as a bookkeeping transaction."
            ),
            "metadata": {
                "targetSystem": target_system,
                "documentType": effective_type,
                "postingEligible": False,
                "manualDocumentTypeOverride": bool(override_type),
            },
        }
    if not override_type and is_non_posting_document_type(classified_type):
        return {
            "status": "blocked_document_type_conflict",
            "message": (
                f"Stored document type {stored_type!r} conflicts with non-posting classifier result "
                f"{classified_type!r}; an explicit document-type review is required."
            ),
            "metadata": {
                "targetSystem": target_system,
                "documentType": stored_type,
                "classifiedDocumentType": classified_type,
                "postingEligible": False,
                "documentTypeConflict": True,
            },
        }
    return None


def _blocked_export_status(status: str) -> str:
    if status in {"blocked_document_type_conflict", "blocked_non_posting_document_type"}:
        return "not_applicable"
    if status == "needs_review":
        return "blocked_by_review"
    if status == "blocked_duplicate":
        return "blocked_duplicate"
    if status == "blocked_status":
        return "blocked_status"
    if status == "unsupported_target":
        return "blocked_unsupported_target"
    return "blocked"


def _find_existing_attempt(document: Dict[str, Any], operation_id: str) -> Optional[Dict[str, Any]]:
    for attempt in document.get("routing_attempts") or []:
        metadata = attempt.get("metadata") or {}
        operation = metadata.get("operation") or {}
        if operation.get("operation_id") == operation_id and attempt.get("status") in PREPARED_ROUTING_STATUSES:
            return attempt
    return None


def _find_existing_record_attempt(
    ledger: LocalOperationsLedger,
    record_id: int,
    operation_id: Optional[str] = None,
    statuses: Optional[tuple] = None,
) -> Optional[Dict[str, Any]]:
    for attempt in ledger.list_routing_attempts(status=statuses, limit=500):
        metadata = attempt.get("metadata") or {}
        attempt_record_id = attempt.get("bookkeeping_record_id") or metadata.get("bookkeepingRecordId")
        if attempt_record_id != record_id:
            continue
        operation = metadata.get("operation") if isinstance(metadata.get("operation"), dict) else {}
        if operation_id and operation.get("operation_id") != operation_id:
            continue
        return attempt
    return None


def _document_snapshot(document: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": document.get("id"),
        "source": document.get("source"),
        "sourceDocumentId": document.get("source_document_id"),
        "originalFilename": document.get("original_filename"),
        "documentType": document.get("document_type"),
        "processingStatus": document.get("processing_status"),
        "vendorName": document.get("vendor_name"),
        "category": document.get("category"),
        "transactionDate": document.get("transaction_date"),
        "totalAmount": document.get("total_amount"),
        "vatAmount": document.get("vat_amount"),
        "confidenceScore": document.get("confidence_score"),
    }


def _bookkeeping_record_snapshot(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": record.get("id"),
        "documentId": record.get("document_id"),
        "bankTransactionId": record.get("bank_transaction_id"),
        "sourceType": record.get("source_type"),
        "recordType": record.get("record_type"),
        "status": record.get("status"),
        "targetSystem": record.get("target_system"),
        "targetAccount": record.get("target_account"),
        "vendorName": record.get("vendor_name"),
        "category": record.get("category"),
        "recordDate": record.get("record_date"),
        "amount": record.get("amount"),
        "vatAmount": record.get("vat_amount"),
        "currency": record.get("currency"),
        "confidenceScore": record.get("confidence_score"),
        "reviewRequired": bool(record.get("review_required")),
        "exportStatus": record.get("export_status"),
        "reconciliationStatus": record.get("reconciliation_status"),
    }


def _target_system(document: Dict[str, Any]) -> str:
    return resolve_document_target_system(document, default="waveapps")


def _normalize_target_system(value: Any) -> str:
    return str(value or "waveapps").strip().lower().replace("-", "_").replace(" ", "_")


def _merged_metadata(document: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    metadata = dict(document.get("metadata") or {})
    metadata.update(updates)
    return metadata


def _update_record_export_state(
    ledger: LocalOperationsLedger,
    record: Dict[str, Any],
    export_status: str,
    status: Optional[str] = None,
    routing_attempt_id: Optional[int] = None,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    metadata = dict(record.get("metadata") or {})
    metadata["latestExport"] = {
        "status": export_status,
        "routingAttemptId": routing_attempt_id,
        "details": details or {},
    }
    update: Dict[str, Any] = {
        "exportStatus": export_status,
        "metadata": metadata,
    }
    if status:
        update["status"] = status
    ledger.update_bookkeeping_record(int(record["id"]), update)


def _positive_amount(value: Any) -> Any:
    if value is None:
        return None
    try:
        return abs(float(value))
    except (TypeError, ValueError):
        return value


def _negative_amount(value: Any) -> Any:
    if value is None:
        return None
    try:
        return -abs(float(value))
    except (TypeError, ValueError):
        return value


def _config_value(config: Dict[str, Any], *keys: str, default: str) -> str:
    for key in keys:
        value = config.get(key)
        if value not in (None, ""):
            return str(value)
    return default
