import csv
import io
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from src.data_entry.mijngeldzaken_artifacts import MijngeldzakenArtifactStore
from src.data_entry.mijngeldzaken_surface import (
    build_mijngeldzaken_action_payload,
    build_mijngeldzaken_master_ledger_draft,
    classify_mijngeldzaken_destination,
    resolve_mijngeldzaken_action_for_document,
)
from src.data_entry.waveapps_api_executor import WaveappsApiExecutor
from src.data_entry.waveapps_autonomous_operator import WaveappsAutonomousOperator
from src.operations.local_bookkeeping_records import LocalBookkeepingRecordService
from src.operations.local_ledger import LocalOperationsLedger
from src.utils.csv_safety import neutralize_csv_row
from src.operations.local_routing import PREPARED_ROUTING_STATUSES


EXPORT_APPROVAL_PHRASE = "APPROVE FAB EXPORT DRAFT"
EXPORT_REJECTION_PHRASE = "REJECT FAB EXPORT DRAFT"
EXPORT_RESULT_CONFIRMATION_PHRASE = "RECORD FAB EXPORT RESULT"
OPEN_REVIEW_STATUSES = {"pending", "in_review"}
APPROVABLE_EXPORT_STATUSES = {"approval_required", "prepared", "attention_required"}
REJECTABLE_EXPORT_STATUSES = {
    "approval_required",
    "prepared",
    "approved",
    "attention_required",
    "deferred",
    "supervision_required",
}
RESULT_STATUSES = {"executed", "submitted", "queued", "failed"}
TERMINAL_EXECUTION_STATUSES = {"executed", "submitted", "queued"}
CLAIMABLE_EXECUTION_STATUSES = {"approved", "deferred"}
DEFERRED_EXECUTION_STATUSES = {"rate_limited", "quota_exhausted"}
PRESERVED_EXPORT_STATUSES = {
    "approved",
    "attention_required",
    "deferred",
    "execution_in_progress",
    "supervision_required",
    "queued",
    "executed",
    "submitted",
    "failed",
    "rejected",
}
UNCHANGED_DRAFT_STATUSES = {"approval_required", "prepared"}


class LocalExportAttemptService:
    """Persist approval-gated export attempts from prepared external routes.

    Routing creates a Wave operation plan. This service records FAB's local
    decision state around that plan so the dashboard/API can approve or record
    results without silently submitting data to Wave.
    """

    def __init__(
        self,
        ledger: LocalOperationsLedger,
        config: Optional[Dict[str, Any]] = None,
        wave_executor: Optional[WaveappsApiExecutor] = None,
    ):
        self.ledger = ledger
        self.config = config or {}
        self.wave_executor = wave_executor or WaveappsApiExecutor(self.config)

    def prepare_from_routing_attempt(
        self,
        routing_attempt_id: int,
        actor: str = "fab_local_exports",
    ) -> Dict[str, Any]:
        routing_attempt = self.ledger.get_routing_attempt(routing_attempt_id)
        if not routing_attempt:
            return {"success": False, "status": "not_found", "error": "Routing attempt not found"}

        metadata = routing_attempt.get("metadata") or {}
        document = self.ledger.get_document(int(routing_attempt["document_id"])) if routing_attempt.get("document_id") else None
        record = (document or {}).get("bookkeeping_record") or _record_from_routing_metadata(self.ledger, metadata)
        operation = metadata.get("operation") if isinstance(metadata.get("operation"), dict) else {}
        operation_payload = operation.get("payload") if isinstance(operation.get("payload"), dict) else {}
        destination = metadata.get("destination") if isinstance(metadata.get("destination"), dict) else {}
        route_status = str(routing_attempt.get("status") or "")
        target_system = metadata.get("targetSystem") or _target_system_from_target(routing_attempt.get("target"))
        existing_attempt = _existing_export_attempt_for_route(self.ledger, routing_attempt, operation)
        existing_status = str((existing_attempt or {}).get("status") or "")
        unchanged_draft = (
            existing_status in UNCHANGED_DRAFT_STATUSES
            and _same_export_operation(existing_attempt, routing_attempt, operation)
        )
        if existing_attempt and (existing_status in PRESERVED_EXPORT_STATUSES or unchanged_draft):
            if not unchanged_draft:
                preserve_details = {
                    "actor": actor,
                    "routingAttemptId": routing_attempt.get("id"),
                    "documentId": routing_attempt.get("document_id"),
                    "bookkeepingRecordId": existing_attempt.get("bookkeeping_record_id"),
                    "operationId": operation.get("operation_id"),
                    "status": existing_attempt.get("status"),
                    "externalSubmission": existing_attempt.get("external_submission"),
                }
                if _preserved_state_changed(self.ledger, existing_attempt, preserve_details):
                    self.ledger.record_audit_event({
                        "action": "local_export_attempt.prepare_preserved",
                        "entityType": "export_attempt",
                        "entityId": str(existing_attempt.get("id")),
                        "details": preserve_details,
                    })
            return {
                "success": True,
                "status": "already_prepared",
                "exportAttemptId": existing_attempt.get("id"),
                "routingAttemptId": routing_attempt.get("id"),
                "documentId": routing_attempt.get("document_id"),
                "bookkeepingRecordId": existing_attempt.get("bookkeeping_record_id"),
                "operationId": operation.get("operation_id"),
                "message": f"Existing export attempt #{existing_attempt.get('id')} is {existing_attempt.get('status')} and was not overwritten.",
                "exportAttempt": existing_attempt,
            }
        master_ledger_draft = _master_ledger_draft(
            target_system,
            operation,
            operation_payload,
            routing_attempt,
            document,
            record,
        )
        block = self._export_block_for_routing(routing_attempt, document, record, operation)

        if block:
            export_status = block["status"]
            approval_required = False
            external_submission = "not_executed"
            message = block["message"]
        else:
            export_status = "approval_required"
            approval_required = True
            external_submission = "not_executed"
            target_label = _target_label(target_system)
            message = f"{target_label} export attempt prepared; approval is required before any external submission."

        export_id = self.ledger.upsert_export_attempt({
            "bookkeepingRecordId": record.get("id"),
            "documentId": routing_attempt.get("document_id"),
            "routingAttemptId": routing_attempt.get("id"),
            "workflowRunId": routing_attempt.get("workflow_run_id"),
            "targetSystem": target_system,
            "targetAccount": _first_present(
                operation_payload.get("account"),
                operation_payload.get("incomeAccount"),
                operation_payload.get("expenseAccount"),
                record.get("target_account"),
            ),
            "actionId": operation.get("action_id"),
            "surface": operation.get("surface") or destination.get("target_surface"),
            "operationId": operation.get("operation_id"),
            "status": export_status,
            "safety": operation.get("safety") or "requires_confirmation",
            "approvalRequired": approval_required,
            "externalSubmission": external_submission,
            "message": message,
            "payload": operation_payload,
            "metadata": {
                "actor": actor,
                "routingStatus": route_status,
                "routingTarget": routing_attempt.get("target"),
                "routingMessage": routing_attempt.get("message"),
                "operationPlan": operation.get("plan"),
                "approvalRequiredForSubmit": metadata.get("approvalRequiredForSubmit", True),
                "documentSnapshot": metadata.get("documentSnapshot"),
                "bookkeepingRecordSnapshot": metadata.get("bookkeepingRecordSnapshot"),
                "masterLedgerDraft": master_ledger_draft,
                "masterLedgerChecksum": (master_ledger_draft or {}).get("checksum"),
                "blockedReason": block.get("status") if block else None,
            },
        })

        if document and routing_attempt.get("document_id"):
            LocalBookkeepingRecordService(self.ledger, self.config).record_export_state(
                int(routing_attempt["document_id"]),
                "awaiting_approval" if export_status == "approval_required" else export_status,
                status="export_approval_pending" if export_status == "approval_required" else None,
                routing_attempt_id=int(routing_attempt["id"]),
                details={
                    "exportAttemptId": export_id,
                    "operationId": operation.get("operation_id"),
                    "externalSubmission": external_submission,
                },
            )
        elif record.get("id"):
            _update_record_export_state(
                self.ledger,
                record,
                "awaiting_approval" if export_status == "approval_required" else export_status,
                status="export_approval_pending" if export_status == "approval_required" else None,
                routing_attempt_id=int(routing_attempt["id"]),
                details={
                    "exportAttemptId": export_id,
                    "operationId": operation.get("operation_id"),
                    "externalSubmission": external_submission,
                },
            )

        self.ledger.record_audit_event({
            "action": "local_export_attempt.prepared",
            "entityType": "export_attempt",
            "entityId": str(export_id),
            "details": {
                "routingAttemptId": routing_attempt.get("id"),
                "documentId": routing_attempt.get("document_id"),
                "bookkeepingRecordId": record.get("id"),
                "status": export_status,
                "externalSubmission": external_submission,
                "approvalRequired": approval_required,
                "targetSystem": target_system,
                "masterLedgerChecksum": (master_ledger_draft or {}).get("checksum"),
            },
        })
        return {
            "success": export_status == "approval_required",
            "status": export_status,
            "exportAttemptId": export_id,
            "routingAttemptId": routing_attempt.get("id"),
            "documentId": routing_attempt.get("document_id"),
            "bookkeepingRecordId": record.get("id"),
            "operationId": operation.get("operation_id"),
            "message": message,
            "exportAttempt": self.ledger.get_export_attempt(export_id),
        }

    def prepare_ready_exports(self, limit: int = 25) -> Dict[str, Any]:
        routing_attempts = self.ledger.list_routing_attempts(
            status=tuple(PREPARED_ROUTING_STATUSES),
            limit=limit,
        )
        summary = {
            "requested": len(routing_attempts),
            "prepared": 0,
            "alreadyPrepared": 0,
            "blocked": 0,
            "changed": False,
            "auditRecorded": False,
            "exportAttempts": [],
            "externalSubmission": "not_executed",
        }
        for routing_attempt in routing_attempts:
            result = self.prepare_from_routing_attempt(int(routing_attempt["id"]))
            if result.get("status") == "already_prepared":
                summary["alreadyPrepared"] += 1
            elif result.get("success"):
                summary["prepared"] += 1
            else:
                summary["blocked"] += 1
            summary["exportAttempts"].append(result)
        summary["changed"] = bool(summary["prepared"] or summary["blocked"])
        if summary["changed"]:
            self.ledger.record_audit_event({
                "action": "local_export_attempt.batch_prepare_completed",
                "entityType": "export_attempt",
                "details": {
                    "requested": summary["requested"],
                    "prepared": summary["prepared"],
                    "alreadyPrepared": summary["alreadyPrepared"],
                    "blocked": summary["blocked"],
                    "externalSubmission": "not_executed",
                },
            })
            summary["auditRecorded"] = True
        return summary

    def process_approved_attempts(
        self,
        limit: int = 20,
        actor: str = "fab_local_exports",
        force: bool = False,
        create_backup: bool = True,
    ) -> Dict[str, Any]:
        if not force and not _as_bool(self.config.get("fab_autonomy_execute_approved_exports", False)):
            return {
                "success": True,
                "status": "skipped",
                "reason": "fab_autonomy_execute_approved_exports is disabled",
                "processed": [],
                "count": 0,
            }

        candidates = self.ledger.list_export_attempts(
            status=tuple(CLAIMABLE_EXECUTION_STATUSES),
            limit=max(limit * 2, limit),
        )
        attempts = [attempt for attempt in candidates if _execution_retry_due(attempt)][:limit]
        deferred_not_due = len(candidates) - len([attempt for attempt in candidates if _execution_retry_due(attempt)])
        pre_execution_backup = None
        if attempts and create_backup:
            from src.operations.local_backup import LocalBackupService

            try:
                pre_execution_backup = LocalBackupService(self.ledger, self.config).create_backup(
                    note="Automatic pre-execution backup before approved operations-ledger exports"
                )
            except Exception as exc:
                pre_execution_backup = {"success": False, "status": "failed", "error": str(exc)}
            self.ledger.record_audit_event({
                "action": "local_export_attempt.batch_execution_preflight_backup",
                "entityType": "export_attempt",
                "details": {
                    "actor": actor,
                    "attemptCount": len(attempts),
                    "backupPath": pre_execution_backup.get("backupPath"),
                    "backupFilename": pre_execution_backup.get("backupFilename"),
                    "ledgerSha256": (pre_execution_backup.get("manifest") or {}).get("ledgerSha256"),
                    "externalSubmission": "not_executed",
                },
            })
            if not pre_execution_backup.get("success"):
                self.ledger.record_audit_event({
                    "action": "local_export_attempt.batch_execution_blocked_backup",
                    "entityType": "export_attempt",
                    "details": {
                        "actor": actor,
                        "attemptCount": len(attempts),
                        "backupStatus": pre_execution_backup.get("status"),
                        "error": pre_execution_backup.get("error"),
                        "externalSubmission": "not_executed",
                    },
                })
                return {
                    "success": False,
                    "status": "pre_execution_backup_failed",
                    "processed": [],
                    "count": 0,
                    "candidateCount": len(candidates),
                    "eligibleCount": len(attempts),
                    "deferredNotDue": deferred_not_due,
                    "preExecutionBackup": _compact_backup_result(pre_execution_backup),
                }

        processed = [
            self.execute_attempt(int(attempt["id"]), actor=actor)
            for attempt in attempts
        ]
        return {
            "success": all(result.get("success") for result in processed),
            "status": "completed",
            "processed": processed,
            "count": len(processed),
            "candidateCount": len(candidates),
            "eligibleCount": len(attempts),
            "deferredNotDue": deferred_not_due,
            "preExecutionBackup": _compact_backup_result(pre_execution_backup),
        }

    def artifact_for_attempt(
        self,
        export_attempt_id: int,
        export_format: str = "json",
        actor: str = "fab_local_exports",
    ) -> Dict[str, Any]:
        attempt = self.ledger.get_export_attempt(export_attempt_id)
        if not attempt:
            return {"success": False, "status": "not_found", "error": "Export attempt not found"}
        metadata = attempt.get("metadata") if isinstance(attempt.get("metadata"), dict) else {}
        draft = metadata.get("masterLedgerDraft") if isinstance(metadata.get("masterLedgerDraft"), dict) else None
        if not draft:
            return {
                "success": False,
                "status": "no_artifact",
                "message": "Export attempt does not have a master-ledger draft artifact.",
            }

        export_format = str(export_format or "json").strip().lower()
        filename_base = f"fab-{attempt.get('target_system') or 'export'}-{export_attempt_id}"
        if export_format == "json":
            content = json.dumps(draft, sort_keys=True, indent=2, default=str)
            result = {
                "success": True,
                "status": "prepared",
                "artifact": {
                    "format": "json",
                    "contentType": "application/json",
                    "filename": f"{filename_base}.json",
                    "content": content,
                    "checksum": draft.get("checksum"),
                    "externalSubmission": "not_executed",
                    "draftType": draft.get("draftType"),
                },
            }
            self._record_artifact_audit(export_attempt_id, attempt, result["artifact"], actor)
            return result
        if export_format == "csv" and draft.get("draftType") == "transaction_import":
            content = _csv_artifact_content(draft)
            result = {
                "success": True,
                "status": "prepared",
                "artifact": {
                    "format": "csv",
                    "contentType": "text/csv",
                    "filename": f"{filename_base}.csv",
                    "content": content,
                    "checksum": draft.get("checksum"),
                    "externalSubmission": "not_executed",
                    "draftType": draft.get("draftType"),
                },
            }
            self._record_artifact_audit(export_attempt_id, attempt, result["artifact"], actor)
            return result
        return {
            "success": False,
            "status": "unsupported_format",
            "message": f"Master-ledger draft cannot be exported as {export_format!r}.",
            "supportedFormats": ["json"] + (["csv"] if draft.get("draftType") == "transaction_import" else []),
        }

    def regenerate_attempt(
        self,
        export_attempt_id: int,
        actor: str = "fab_local_exports",
    ) -> Dict[str, Any]:
        attempt = self.ledger.get_export_attempt(export_attempt_id)
        if not attempt:
            return {"success": False, "status": "not_found", "error": "Export attempt not found"}
        if attempt.get("status") == "supervision_required":
            return {
                "success": False,
                "status": "supervision_in_progress",
                "message": "Record or reject the current supervised import before regenerating its artifact.",
                "externalSubmission": "not_executed",
            }
        if attempt.get("external_submission") in {"queued", "submitted", "executed"}:
            return {
                "success": False,
                "status": "already_submitted",
                "message": "Submitted or queued export attempts cannot be regenerated locally.",
                "externalSubmission": attempt.get("external_submission"),
            }
        target_system = _target_system_for_attempt(attempt)
        if not _is_mijngeldzaken_target(target_system):
            return {
                "success": False,
                "status": "not_supported",
                "message": "Only MijnGeldzaken master-ledger drafts can be regenerated from current FAB state.",
                "externalSubmission": "not_executed",
            }

        current_package = _current_mijngeldzaken_master_ledger_package(self.ledger, self.config, attempt)
        if not current_package:
            return {
                "success": False,
                "status": "source_missing",
                "message": "The source document or bookkeeping record for this export attempt no longer exists.",
                "externalSubmission": "not_executed",
            }
        draft = current_package["draft"]
        metadata = dict(attempt.get("metadata") or {})
        history = list(metadata.get("regenerationHistory") or [])
        history.append({
            "actor": actor,
            "regeneratedAt": _now(),
            "fromChecksum": _master_ledger_checksum(metadata),
            "toChecksum": draft.get("checksum"),
            "fromStatus": attempt.get("status"),
            "fromExternalSubmission": attempt.get("external_submission"),
        })
        metadata.update({
            "masterLedgerDraft": draft,
            "masterLedgerChecksum": draft.get("checksum"),
            "regenerationHistory": history,
            "lastRegeneration": history[-1],
            "sourceRegeneratedFromCurrentFabState": True,
        })
        self.ledger.update_export_attempt(export_attempt_id, {
            "targetAccount": current_package["payload"].get("account"),
            "actionId": current_package["actionId"],
            "surface": current_package["surface"],
            "status": "approval_required",
            "approvalRequired": True,
            "externalSubmission": "not_executed",
            "message": "MijnGeldzaken master-ledger draft regenerated from current FAB source state; approval is required before external submission.",
            "payload": current_package["payload"],
            "result": {},
            "metadata": metadata,
        })
        details = {
            "exportAttemptId": export_attempt_id,
            "actor": actor,
            "targetSystem": target_system,
            "externalSubmission": "not_executed",
            "masterLedgerChecksum": draft.get("checksum"),
            "previousMasterLedgerChecksum": history[-1].get("fromChecksum"),
        }
        if attempt.get("document_id"):
            LocalBookkeepingRecordService(self.ledger, self.config).record_export_state(
                int(attempt["document_id"]),
                "awaiting_approval",
                status="export_approval_pending",
                routing_attempt_id=attempt.get("routing_attempt_id"),
                details=details,
            )
        elif attempt.get("bookkeeping_record_id"):
            record = self.ledger.get_bookkeeping_record(int(attempt["bookkeeping_record_id"])) or {}
            if record:
                _update_record_export_state(
                    self.ledger,
                    record,
                    "awaiting_approval",
                    status="export_approval_pending",
                    routing_attempt_id=attempt.get("routing_attempt_id"),
                    details=details,
                )
        self.ledger.record_audit_event({
            "action": "local_export_attempt.regenerated",
            "entityType": "export_attempt",
            "entityId": str(export_attempt_id),
            "details": details,
        })
        return {
            "success": True,
            "status": "regenerated",
            "externalSubmission": "not_executed",
            "masterLedgerChecksum": draft.get("checksum"),
            "exportAttempt": self.ledger.get_export_attempt(export_attempt_id),
        }

    def _record_artifact_audit(
        self,
        export_attempt_id: int,
        attempt: Dict[str, Any],
        artifact: Dict[str, Any],
        actor: str,
    ) -> None:
        self.ledger.record_audit_event({
            "action": "local_export_attempt.artifact_prepared",
            "entityType": "export_attempt",
            "entityId": str(export_attempt_id),
            "details": {
                "actor": actor,
                "format": artifact.get("format"),
                "filename": artifact.get("filename"),
                "checksum": artifact.get("checksum"),
                "draftType": artifact.get("draftType"),
                "targetSystem": attempt.get("target_system"),
                "externalSubmission": "not_executed",
            },
        })

    def approve_attempt(
        self,
        export_attempt_id: int,
        actor: str = "local_user",
        confirmation: Optional[str] = None,
        resolution: Optional[str] = None,
    ) -> Dict[str, Any]:
        attempt = self.ledger.get_export_attempt(export_attempt_id)
        if not attempt:
            return {"success": False, "status": "not_found", "error": "Export attempt not found"}
        if confirmation != EXPORT_APPROVAL_PHRASE:
            return {
                "success": False,
                "status": "requires_confirmation",
                "confirmationPhrase": EXPORT_APPROVAL_PHRASE,
                "message": "Type the exact approval phrase before marking this export as approved.",
            }
        if attempt.get("status") in APPROVABLE_EXPORT_STATUSES | {"approved"}:
            source_block = self._source_processing_block(attempt, "approval", actor)
            if source_block:
                return source_block
        if attempt.get("status") == "approved":
            return {"success": True, "status": "already_approved", "exportAttempt": attempt}
        if attempt.get("status") not in APPROVABLE_EXPORT_STATUSES:
            return {
                "success": False,
                "status": "not_approvable",
                "message": f"Export attempt status {attempt.get('status')} cannot be approved.",
            }
        stale = self._master_ledger_staleness(attempt, "approval", actor)
        if stale:
            return stale

        now = _now()
        resolved_review_ids = (
            self._resolve_wave_execution_reviews(attempt, actor, "reapproved")
            if attempt.get("status") == "attention_required"
            else []
        )
        metadata = dict(attempt.get("metadata") or {})
        master_ledger_checksum = _master_ledger_checksum(metadata)
        metadata["approval"] = {
            "actor": actor,
            "approvedAt": now,
            "resolution": resolution,
            "masterLedgerChecksum": master_ledger_checksum,
        }
        self.ledger.update_export_attempt(export_attempt_id, {
            "status": "approved",
            "approvalRequired": False,
            "approvedAt": now,
            "approvedBy": actor,
            "externalSubmission": "approved_not_executed",
            "message": "Export approved locally; external submission is still not executed.",
            "metadata": metadata,
        })
        if attempt.get("document_id"):
            LocalBookkeepingRecordService(self.ledger, self.config).record_export_state(
                int(attempt["document_id"]),
                "approved_not_submitted",
                status="export_approved",
                routing_attempt_id=attempt.get("routing_attempt_id"),
                details={
                    "exportAttemptId": export_attempt_id,
                    "approvedBy": actor,
                    "externalSubmission": "approved_not_executed",
                    "masterLedgerChecksum": master_ledger_checksum,
                },
            )
        elif attempt.get("bookkeeping_record_id"):
            record = self.ledger.get_bookkeeping_record(int(attempt["bookkeeping_record_id"])) or {}
            if record:
                _update_record_export_state(
                    self.ledger,
                    record,
                    "approved_not_submitted",
                    status="export_approved",
                    routing_attempt_id=attempt.get("routing_attempt_id"),
                    details={
                        "exportAttemptId": export_attempt_id,
                        "approvedBy": actor,
                        "externalSubmission": "approved_not_executed",
                        "masterLedgerChecksum": master_ledger_checksum,
                    },
                )
        self.ledger.record_audit_event({
            "action": "local_export_attempt.approved",
            "entityType": "export_attempt",
            "entityId": str(export_attempt_id),
            "details": {
                "actor": actor,
                "documentId": attempt.get("document_id"),
                "bookkeepingRecordId": attempt.get("bookkeeping_record_id"),
                "routingAttemptId": attempt.get("routing_attempt_id"),
                "externalSubmission": "approved_not_executed",
                "masterLedgerChecksum": master_ledger_checksum,
                "resolvedReviewIds": resolved_review_ids,
            },
        })
        return {
            "success": True,
            "status": "approved",
            "resolvedReviewIds": resolved_review_ids,
            "exportAttempt": self.ledger.get_export_attempt(export_attempt_id),
        }

    def reject_attempt(
        self,
        export_attempt_id: int,
        actor: str = "local_user",
        confirmation: Optional[str] = None,
        resolution: Optional[str] = None,
    ) -> Dict[str, Any]:
        attempt = self.ledger.get_export_attempt(export_attempt_id)
        if not attempt:
            return {"success": False, "status": "not_found", "error": "Export attempt not found"}
        if confirmation != EXPORT_REJECTION_PHRASE:
            return {
                "success": False,
                "status": "requires_confirmation",
                "confirmationPhrase": EXPORT_REJECTION_PHRASE,
                "message": "Type the exact rejection phrase before rejecting this export attempt.",
            }
        if attempt.get("status") == "rejected":
            return {"success": True, "status": "already_rejected", "exportAttempt": attempt}
        if attempt.get("status") not in REJECTABLE_EXPORT_STATUSES:
            return {
                "success": False,
                "status": "not_rejectable",
                "message": f"Export attempt status {attempt.get('status')} cannot be rejected.",
            }
        if attempt.get("external_submission") in {"queued", "submitted", "executed"}:
            return {
                "success": False,
                "status": "already_submitted",
                "message": "Submitted or queued export attempts cannot be rejected locally; record the actual external result instead.",
            }

        now = _now()
        metadata = dict(attempt.get("metadata") or {})
        master_ledger_checksum = _master_ledger_checksum(metadata)
        metadata["rejection"] = {
            "actor": actor,
            "rejectedAt": now,
            "resolution": resolution,
            "masterLedgerChecksum": master_ledger_checksum,
        }
        self.ledger.update_export_attempt(export_attempt_id, {
            "status": "rejected",
            "approvalRequired": False,
            "externalSubmission": "rejected_not_executed",
            "message": "Export rejected locally; no external submission was executed.",
            "metadata": metadata,
        })
        resolved_review_ids = []
        if attempt.get("status") == "supervision_required":
            resolved_review_ids = self._resolve_mijngeldzaken_supervision_reviews(
                attempt,
                actor=actor,
                result_status="rejected",
            )
        elif attempt.get("status") == "attention_required":
            resolved_review_ids = self._resolve_wave_execution_reviews(attempt, actor, "rejected")
        details = {
            "exportAttemptId": export_attempt_id,
            "rejectedBy": actor,
            "resolution": resolution,
            "externalSubmission": "rejected_not_executed",
            "masterLedgerChecksum": master_ledger_checksum,
            "resolvedReviewIds": resolved_review_ids,
        }
        if attempt.get("document_id"):
            LocalBookkeepingRecordService(self.ledger, self.config).record_export_state(
                int(attempt["document_id"]),
                "rejected_not_executed",
                status="export_rejected",
                routing_attempt_id=attempt.get("routing_attempt_id"),
                details=details,
            )
        elif attempt.get("bookkeeping_record_id"):
            record = self.ledger.get_bookkeeping_record(int(attempt["bookkeeping_record_id"])) or {}
            if record:
                _update_record_export_state(
                    self.ledger,
                    record,
                    "rejected_not_executed",
                    status="export_rejected",
                    routing_attempt_id=attempt.get("routing_attempt_id"),
                    details=details,
                )
        self.ledger.record_audit_event({
            "action": "local_export_attempt.rejected",
            "entityType": "export_attempt",
            "entityId": str(export_attempt_id),
            "details": {
                "actor": actor,
                "documentId": attempt.get("document_id"),
                "bookkeepingRecordId": attempt.get("bookkeeping_record_id"),
                "routingAttemptId": attempt.get("routing_attempt_id"),
                "externalSubmission": "rejected_not_executed",
                "resolution": resolution,
                "masterLedgerChecksum": master_ledger_checksum,
                "resolvedReviewIds": resolved_review_ids,
            },
        })
        return {
            "success": True,
            "status": "rejected",
            "externalSubmission": "rejected_not_executed",
            "resolvedReviewIds": resolved_review_ids,
            "exportAttempt": self.ledger.get_export_attempt(export_attempt_id),
        }

    def record_result(
        self,
        export_attempt_id: int,
        status: str,
        external_id: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
        actor: str = "fab_local_exports",
        confirmation: Optional[str] = None,
    ) -> Dict[str, Any]:
        attempt = self.ledger.get_export_attempt(export_attempt_id)
        if not attempt:
            return {"success": False, "status": "not_found", "error": "Export attempt not found"}
        status = str(status or "").strip().lower()
        if status not in RESULT_STATUSES:
            return {"success": False, "status": "invalid_result_status", "error": f"Unsupported result status {status!r}"}
        if confirmation != EXPORT_RESULT_CONFIRMATION_PHRASE:
            return {
                "success": False,
                "status": "requires_confirmation",
                "confirmationPhrase": EXPORT_RESULT_CONFIRMATION_PHRASE,
                "message": "Type the exact result phrase before recording an external export result.",
            }
        if attempt.get("status") == "supervision_required" and status == "queued":
            return {
                "success": False,
                "status": "invalid_supervised_result",
                "message": "A supervised MijnGeldzaken import must be recorded as executed, submitted, or failed.",
            }
        if attempt.get("status") not in {"approved", "supervision_required"} and status in {"executed", "submitted", "queued"}:
            return {
                "success": False,
                "status": "blocked_unapproved",
                "message": "Only approved or supervised export attempts can receive submitted/executed results.",
            }
        if status in {"executed", "submitted", "queued"}:
            stale = self._master_ledger_staleness(attempt, "result", actor)
            if stale:
                return stale

        now = _now()
        external_submission = _external_submission_for_result(status)
        metadata = dict(attempt.get("metadata") or {})
        master_ledger_checksum = _master_ledger_checksum(metadata)
        metadata["lastResult"] = {
            "actor": actor,
            "recordedAt": now,
            "status": status,
            "externalSubmission": external_submission,
            "masterLedgerChecksum": master_ledger_checksum,
        }
        result_payload = dict(result or {})
        if master_ledger_checksum and "masterLedgerChecksum" not in result_payload:
            result_payload["masterLedgerChecksum"] = master_ledger_checksum
        self.ledger.update_export_attempt(export_attempt_id, {
            "status": status,
            "externalSubmission": external_submission,
            "submittedAt": now if status in {"executed", "submitted"} else None,
            "externalId": external_id,
            "message": _message_for_result(status),
            "result": result_payload,
            "metadata": metadata,
        })
        resolved_review_ids = []
        if attempt.get("status") == "supervision_required" and status in {"executed", "submitted"}:
            resolved_review_ids = self._resolve_mijngeldzaken_supervision_reviews(
                attempt,
                actor=actor,
                result_status=status,
            )
        if attempt.get("document_id"):
            LocalBookkeepingRecordService(self.ledger, self.config).record_export_state(
                int(attempt["document_id"]),
                external_submission,
                status="routed" if status in {"executed", "submitted"} else None,
                routing_attempt_id=attempt.get("routing_attempt_id"),
                details={
                    "exportAttemptId": export_attempt_id,
                    "externalId": external_id,
                    "resultStatus": status,
                    "externalSubmission": external_submission,
                    "masterLedgerChecksum": master_ledger_checksum,
                },
            )
        elif attempt.get("bookkeeping_record_id"):
            record = self.ledger.get_bookkeeping_record(int(attempt["bookkeeping_record_id"])) or {}
            if record:
                _update_record_export_state(
                    self.ledger,
                    record,
                    external_submission,
                    status="routed" if status in {"executed", "submitted"} else None,
                    routing_attempt_id=attempt.get("routing_attempt_id"),
                    details={
                        "exportAttemptId": export_attempt_id,
                        "externalId": external_id,
                        "resultStatus": status,
                        "externalSubmission": external_submission,
                        "masterLedgerChecksum": master_ledger_checksum,
                    },
                )
        self.ledger.record_audit_event({
            "action": "local_export_attempt.result_recorded",
            "entityType": "export_attempt",
            "entityId": str(export_attempt_id),
            "details": {
                "actor": actor,
                "status": status,
                "externalSubmission": external_submission,
                "externalId": external_id,
                "masterLedgerChecksum": master_ledger_checksum,
                "resolvedReviewIds": resolved_review_ids,
            },
        })
        return {
            "success": True,
            "status": status,
            "externalSubmission": external_submission,
            "resolvedReviewIds": resolved_review_ids,
            "exportAttempt": self.ledger.get_export_attempt(export_attempt_id),
        }

    def execute_attempt(
        self,
        export_attempt_id: int,
        actor: str = "fab_local_exports",
    ) -> Dict[str, Any]:
        attempt = self.ledger.get_export_attempt(export_attempt_id)
        if not attempt:
            return {"success": False, "status": "not_found", "error": "Export attempt not found"}

        status = str(attempt.get("status") or "").strip().lower()
        if status in CLAIMABLE_EXECUTION_STATUSES:
            if not _execution_retry_due(attempt):
                retry = (attempt.get("metadata") or {}).get("retry") or {}
                return {
                    "success": True,
                    "status": "retry_deferred",
                    "nextRetryAt": retry.get("nextRetryAt"),
                    "externalSubmission": "not_executed",
                    "exportAttempt": attempt,
                }
            claim = self.ledger.claim_export_attempt(
                export_attempt_id,
                allowed_statuses=CLAIMABLE_EXECUTION_STATUSES,
            )
            if claim.get("status") != "claimed":
                current = self.ledger.get_export_attempt(export_attempt_id)
                return {
                    "success": False,
                    "status": "already_claimed",
                    "currentStatus": (current or {}).get("status"),
                    "externalSubmission": (current or {}).get("external_submission"),
                    "exportAttempt": current,
                }
            attempt = claim["attempt"]
            source_block = self._source_processing_block(attempt, "execution", actor)
            if source_block:
                return source_block
            metadata = dict(attempt.get("metadata") or {})
            master_ledger_checksum = _master_ledger_checksum(metadata)
            stale = self._master_ledger_staleness(attempt, "execution", actor)
            if stale:
                self._release_execution_claim(export_attempt_id, "Master-ledger draft became stale before dispatch.")
                return stale
            action_id = attempt.get("action_id")
            operation = dict(metadata.get("operation") or {})
            if not action_id:
                self._release_execution_claim(export_attempt_id, "Executable action id is missing.")
                return {
                    "success": False,
                    "status": "invalid_plan",
                    "error": "No executable action id found for this export attempt.",
                }
            payload = dict(attempt.get("payload") or {})
            if not isinstance(payload, dict):
                payload = {}
            capability_id = metadata.get("capabilityId") or metadata.get("capability_id")
            available_signals = _list_optional_strs(metadata.get("availableSignals") or metadata.get("available_signals"))
            confidence = metadata.get("confidence")
            operation_mode = metadata.get("mode") or operation.get("mode") or operation.get("plan", {}).get("mode")
            target_system = _target_system_for_attempt(attempt)
            if _is_mijngeldzaken_target(target_system):
                return self._prepare_mijngeldzaken_supervision(attempt, actor)
            try:
                operator = WaveappsAutonomousOperator(
                    self.config,
                    action_handlers={
                        str(action_id): lambda wave_payload: self.wave_executor.execute(
                            target_system=target_system,
                            action_id=action_id,
                            payload=wave_payload,
                            idempotency_key=attempt.get("operation_id") or attempt.get("id"),
                            document_id=attempt.get("document_id"),
                            bookkeeping_record_id=attempt.get("bookkeeping_record_id"),
                        )
                    },
                )
                execution = operator.execute(
                    action_id,
                    payload,
                    surface=attempt.get("surface") or operation.get("surface"),
                    actor=actor,
                    confirmed=True,
                    idempotency_key=str(attempt.get("operation_id") or attempt.get("id")),
                    mode=operation_mode,
                    capability_id=capability_id,
                    available_signals=available_signals,
                    confidence=confidence,
                )
            except Exception as exc:
                execution_status = "failed"
                message = f"Execution failed before dispatch: {exc}"
                execution = {"status": "failed", "message": message, "operation": operation}
                execution["status"] = execution_status

            execution_status = str(execution.get("status") or "").strip().lower()
            external_id = execution.get("external_id")
            operation = execution.get("operation") or attempt
            provider_result = execution.get("result") if isinstance(execution.get("result"), dict) else {}
            external_submission, mapped_status, mapped_message = _map_execution_status(execution_status, target_system)

            now = _now()
            execution_metadata = dict(metadata.get("lastExecution") or {})
            execution_metadata["actor"] = actor
            execution_metadata["executedAt"] = now
            execution_metadata["rawStatus"] = execution_status
            execution_metadata["mappedStatus"] = mapped_status
            execution_metadata["operation"] = operation
            execution_metadata["masterLedgerChecksum"] = master_ledger_checksum
            retry_state = None
            if mapped_status == "deferred":
                retry_state = _next_retry_state(metadata, provider_result, self.config, now)

            update_payload = {
                "status": mapped_status,
                "externalSubmission": external_submission,
                "message": mapped_message,
                "result": {
                    "status": execution_status,
                    "message": execution.get("message"),
                    "externalId": external_id,
                    "operation": operation,
                    "providerResult": provider_result,
                    "masterLedgerChecksum": master_ledger_checksum,
                },
                "metadata": {
                    **dict(metadata),
                    "lastExecution": execution_metadata,
                    **({"retry": retry_state} if retry_state else {}),
                },
            }
            if mapped_status in TERMINAL_EXECUTION_STATUSES:
                if mapped_status in {"executed", "submitted"}:
                    update_payload["submittedAt"] = now
                update_payload["externalId"] = external_id
            elif mapped_status == "failed":
                update_payload["message"] = execution.get("message") or mapped_message

            self.ledger.update_export_attempt(export_attempt_id, update_payload)

            if attempt.get("document_id") and mapped_status in TERMINAL_EXECUTION_STATUSES:
                LocalBookkeepingRecordService(self.ledger, self.config).record_export_state(
                    int(attempt["document_id"]),
                    _record_status_for_execution(mapped_status),
                    status="routed",
                    routing_attempt_id=attempt.get("routing_attempt_id"),
                    details={
                        "exportAttemptId": export_attempt_id,
                        "externalId": external_id,
                        "resultStatus": mapped_status,
                        "executionStatus": execution_status,
                        "externalSubmission": external_submission,
                        "masterLedgerChecksum": master_ledger_checksum,
                    },
                )
            elif attempt.get("bookkeeping_record_id") and mapped_status in TERMINAL_EXECUTION_STATUSES:
                record = self.ledger.get_bookkeeping_record(int(attempt["bookkeeping_record_id"])) or {}
                if record:
                    _update_record_export_state(
                        self.ledger,
                        record,
                        _record_status_for_execution(mapped_status),
                        status="routed",
                        routing_attempt_id=attempt.get("routing_attempt_id"),
                        details={
                            "exportAttemptId": export_attempt_id,
                            "externalId": external_id,
                            "resultStatus": mapped_status,
                            "executionStatus": execution_status,
                            "externalSubmission": external_submission,
                            "masterLedgerChecksum": master_ledger_checksum,
                        },
                    )

            review_id = None
            if mapped_status in {"attention_required", "failed"}:
                review_id = self._queue_wave_execution_review(
                    attempt,
                    execution_status,
                    execution.get("message") or mapped_message,
                    provider_result,
                )
                self._record_attention_state(
                    attempt,
                    export_attempt_id,
                    mapped_status,
                    execution_status,
                    review_id,
                )

            audit_action = {
                "deferred": "local_export_attempt.execution_deferred",
                "attention_required": "local_export_attempt.execution_attention_required",
                "failed": "local_export_attempt.execution_failed",
            }.get(mapped_status, "local_export_attempt.executed")
            self.ledger.record_audit_event({
                "action": audit_action,
                "entityType": "export_attempt",
                "entityId": str(export_attempt_id),
                "details": {
                    "actor": actor,
                    "status": mapped_status,
                    "externalSubmission": external_submission,
                    "executionStatus": execution_status,
                    "externalId": external_id,
                    "targetSystem": target_system,
                    "masterLedgerChecksum": master_ledger_checksum,
                    "reviewItemId": review_id,
                    "nextRetryAt": (retry_state or {}).get("nextRetryAt"),
                },
            })
            return {
                "success": mapped_status in TERMINAL_EXECUTION_STATUSES or mapped_status == "deferred",
                "status": mapped_status,
                "externalSubmission": external_submission,
                "executionStatus": execution_status,
                "reviewItemId": review_id,
                "nextRetryAt": (retry_state or {}).get("nextRetryAt"),
                "exportAttempt": self.ledger.get_export_attempt(export_attempt_id),
            }

        if status in TERMINAL_EXECUTION_STATUSES:
            return {
                "success": True,
                "status": f"already_{status}",
                "externalSubmission": attempt.get("external_submission"),
                "exportAttempt": attempt,
            }

        if status == "supervision_required":
            return {
                "success": True,
                "status": "already_supervision_required",
                "externalSubmission": "not_executed",
                "exportAttempt": attempt,
            }

        if status == "execution_in_progress":
            return {
                "success": False,
                "status": "already_claimed",
                "currentStatus": "execution_in_progress",
                "externalSubmission": attempt.get("external_submission"),
                "exportAttempt": attempt,
            }

        if status == "failed":
            return {
                "success": False,
                "status": "not_executable",
                "message": "A failed attempt must be corrected before resubmitting.",
            }

        return {
            "success": False,
            "status": "not_approved",
            "message": f"Export attempt status {attempt.get('status')} cannot be executed without approval.",
        }

    def _queue_wave_execution_review(
        self,
        attempt: Dict[str, Any],
        execution_status: str,
        message: str,
        provider_result: Dict[str, Any],
    ) -> Optional[int]:
        reason = str(provider_result.get("review_reason") or "wave_execution_attention_required")
        document_id = attempt.get("document_id")
        marker = f'"exportAttemptId": {int(attempt["id"])}'
        candidates = self.ledger.list_review_items(
            status=tuple(OPEN_REVIEW_STATUSES),
            document_id=int(document_id) if document_id else None,
            limit=500,
        )
        for item in candidates:
            if item.get("reason") == reason and marker in str(item.get("details") or ""):
                return int(item["id"])
        details = {
            "exportAttemptId": int(attempt["id"]),
            "bookkeepingRecordId": attempt.get("bookkeeping_record_id"),
            "targetSystem": _target_system_for_attempt(attempt),
            "actionId": attempt.get("action_id"),
            "executionStatus": execution_status,
            "message": message,
            "missingConfiguration": provider_result.get("missing_configuration"),
            "externalSubmission": "not_executed",
        }
        return self.ledger.create_review_item({
            "documentId": document_id,
            "reason": reason,
            "details": json.dumps(details, sort_keys=True, default=str),
            "status": "pending",
        })

    def _resolve_wave_execution_reviews(
        self,
        attempt: Dict[str, Any],
        actor: str,
        result_status: str,
    ) -> list[int]:
        document_id = attempt.get("document_id")
        marker = f'"exportAttemptId": {int(attempt["id"])}'
        candidates = self.ledger.list_review_items(
            status=tuple(OPEN_REVIEW_STATUSES),
            document_id=int(document_id) if document_id else None,
            limit=500,
        )
        resolved = []
        for item in candidates:
            if marker not in str(item.get("details") or ""):
                continue
            if not str(item.get("reason") or "").startswith("wave_"):
                continue
            self.ledger.resolve_review_item(
                int(item["id"]),
                status="resolved",
                resolution=f"Wave export {result_status} by {actor}.",
                corrected_data={
                    "exportAttemptId": int(attempt["id"]),
                    "resultStatus": result_status,
                    "resolvedBy": actor,
                },
            )
            resolved.append(int(item["id"]))
        return resolved

    def _record_attention_state(
        self,
        attempt: Dict[str, Any],
        export_attempt_id: int,
        mapped_status: str,
        execution_status: str,
        review_id: Optional[int],
    ) -> None:
        export_status = "failed" if mapped_status == "failed" else "attention_required"
        record_status = "export_failed" if mapped_status == "failed" else "export_attention_required"
        details = {
            "exportAttemptId": export_attempt_id,
            "executionStatus": execution_status,
            "reviewItemId": review_id,
            "externalSubmission": "not_executed",
        }
        if attempt.get("document_id"):
            LocalBookkeepingRecordService(self.ledger, self.config).record_export_state(
                int(attempt["document_id"]),
                export_status,
                status=record_status,
                routing_attempt_id=attempt.get("routing_attempt_id"),
                details=details,
            )
            return
        if attempt.get("bookkeeping_record_id"):
            record = self.ledger.get_bookkeeping_record(int(attempt["bookkeeping_record_id"])) or {}
            if record:
                _update_record_export_state(
                    self.ledger,
                    record,
                    export_status,
                    status=record_status,
                    routing_attempt_id=attempt.get("routing_attempt_id"),
                    details=details,
                )

    def _release_execution_claim(self, export_attempt_id: int, message: str) -> None:
        self.ledger.update_export_attempt(export_attempt_id, {
            "status": "approved",
            "externalSubmission": "approved_not_executed",
            "message": message,
        })
        self.ledger.record_audit_event({
            "action": "local_export_attempt.execution_claim_released",
            "entityType": "export_attempt",
            "entityId": str(export_attempt_id),
            "details": {
                "message": message,
                "externalSubmission": "approved_not_executed",
            },
        })

    def _prepare_mijngeldzaken_supervision(
        self,
        attempt: Dict[str, Any],
        actor: str,
    ) -> Dict[str, Any]:
        export_attempt_id = int(attempt["id"])
        metadata = dict(attempt.get("metadata") or {})
        draft = metadata.get("masterLedgerDraft") if isinstance(metadata.get("masterLedgerDraft"), dict) else {}
        export_format = "csv" if draft.get("draftType") == "transaction_import" else "json"
        prepared = self.artifact_for_attempt(export_attempt_id, export_format=export_format, actor=actor)
        if not prepared.get("success"):
            return prepared

        draft_artifact = prepared["artifact"]
        try:
            stored = MijngeldzakenArtifactStore(self.config).write_text(
                draft_artifact["filename"],
                draft_artifact["content"],
                encoding="utf-8-sig" if export_format == "csv" else "utf-8",
                include_checksum=True,
            )
        except Exception as exc:
            self.ledger.update_export_attempt(export_attempt_id, {
                "status": "failed",
                "externalSubmission": "not_executed",
                "message": f"MijnGeldzaken supervised artifact could not be persisted: {exc}",
                "result": {"status": "artifact_persistence_failed", "message": str(exc)},
            })
            self.ledger.record_audit_event({
                "action": "local_export_attempt.supervised_artifact_failed",
                "entityType": "export_attempt",
                "entityId": str(export_attempt_id),
                "details": {"actor": actor, "error": str(exc), "externalSubmission": "not_executed"},
            })
            return {
                "success": False,
                "status": "artifact_persistence_failed",
                "message": str(exc),
                "externalSubmission": "not_executed",
            }

        artifact = {
            **stored,
            "format": export_format,
            "contentType": draft_artifact.get("contentType"),
            "draftType": draft_artifact.get("draftType"),
            "masterLedgerChecksum": draft_artifact.get("checksum"),
            "externalSubmission": "not_executed",
        }
        now = _now()
        execution_metadata = {
            "actor": actor,
            "executedAt": now,
            "rawStatus": "supervised_action_required",
            "mappedStatus": "supervision_required",
            "masterLedgerChecksum": draft_artifact.get("checksum"),
            "artifact": artifact,
        }
        metadata.update({
            "supervisedArtifact": artifact,
            "lastExecution": execution_metadata,
        })
        message = (
            "MijnGeldzaken artifact prepared. Complete the import in a supervised "
            "user-owned session, then record the result in FAB."
        )
        self.ledger.update_export_attempt(export_attempt_id, {
            "status": "supervision_required",
            "externalSubmission": "not_executed",
            "message": message,
            "result": {
                "status": "supervised_action_required",
                "message": message,
                "artifact": artifact,
                "masterLedgerChecksum": draft_artifact.get("checksum"),
            },
            "metadata": metadata,
        })

        document_id = attempt.get("document_id")
        if document_id:
            LocalBookkeepingRecordService(self.ledger, self.config).record_export_state(
                int(document_id),
                "supervision_required",
                status="export_supervision_required",
                routing_attempt_id=attempt.get("routing_attempt_id"),
                details={
                    "exportAttemptId": export_attempt_id,
                    "artifact": artifact,
                    "masterLedgerChecksum": draft_artifact.get("checksum"),
                    "externalSubmission": "not_executed",
                },
            )
        elif attempt.get("bookkeeping_record_id"):
            record = self.ledger.get_bookkeeping_record(int(attempt["bookkeeping_record_id"])) or {}
            if record:
                _update_record_export_state(
                    self.ledger,
                    record,
                    "supervision_required",
                    status="export_supervision_required",
                    routing_attempt_id=attempt.get("routing_attempt_id"),
                    details={
                        "exportAttemptId": export_attempt_id,
                        "artifact": artifact,
                        "masterLedgerChecksum": draft_artifact.get("checksum"),
                        "externalSubmission": "not_executed",
                    },
                )

        review_id = self.ledger.create_review_item({
            "documentId": document_id,
            "reason": "mijngeldzaken_supervision_required",
            "details": json.dumps({
                "exportAttemptId": export_attempt_id,
                "artifact": artifact,
                "message": message,
            }, sort_keys=True, default=str),
            "status": "pending",
        })
        self.ledger.record_audit_event({
            "action": "local_export_attempt.supervision_required",
            "entityType": "export_attempt",
            "entityId": str(export_attempt_id),
            "details": {
                "actor": actor,
                "reviewItemId": review_id,
                "documentId": document_id,
                "bookkeepingRecordId": attempt.get("bookkeeping_record_id"),
                "artifact": artifact,
                "masterLedgerChecksum": draft_artifact.get("checksum"),
                "externalSubmission": "not_executed",
            },
        })
        return {
            "success": True,
            "status": "supervision_required",
            "executionStatus": "supervised_action_required",
            "externalSubmission": "not_executed",
            "artifact": artifact,
            "reviewItemId": review_id,
            "exportAttempt": self.ledger.get_export_attempt(export_attempt_id),
        }

    def _resolve_mijngeldzaken_supervision_reviews(
        self,
        attempt: Dict[str, Any],
        actor: str,
        result_status: str,
    ) -> list[int]:
        document_id = attempt.get("document_id")
        candidates = self.ledger.list_review_items(
            status=tuple(OPEN_REVIEW_STATUSES),
            document_id=int(document_id) if document_id else None,
            limit=500,
        )
        resolved = []
        export_marker = f'"exportAttemptId": {int(attempt["id"])}'
        for review in candidates:
            if review.get("reason") != "mijngeldzaken_supervision_required":
                continue
            if not document_id and export_marker not in str(review.get("details") or ""):
                continue
            self.ledger.resolve_review_item(
                int(review["id"]),
                status="resolved",
                resolution=f"MijnGeldzaken supervised submission recorded as {result_status} by {actor}.",
                corrected_data={"exportAttemptId": int(attempt["id"]), "resultStatus": result_status},
            )
            resolved.append(int(review["id"]))
        return resolved

    def _source_processing_block(
        self,
        attempt: Dict[str, Any],
        stage: str,
        actor: str,
    ) -> Optional[Dict[str, Any]]:
        document_id = attempt.get("document_id")
        if not document_id and attempt.get("bookkeeping_record_id"):
            record = self.ledger.get_bookkeeping_record(int(attempt["bookkeeping_record_id"]))
            document_id = (record or {}).get("document_id")
        if not document_id:
            return None
        document = self.ledger.get_document(int(document_id))
        if not document:
            status = "blocked_source_missing"
        elif document.get("duplicate_of_document_id") or document.get("processing_status") == "duplicate":
            status = "blocked_duplicate"
        else:
            status = {
                "failed": "blocked_processing",
                "needs_review": "blocked_by_review",
            }.get(document.get("processing_status"))
        if not status:
            return None

        message = "Source document is no longer eligible. Resolve the source issue and review the draft before approving it again."
        # Remove it from the worker's claimable queue without discarding the approved payload.
        self.ledger.update_export_attempt(int(attempt["id"]), {
            "status": "attention_required",
            "approvalRequired": True,
            "externalSubmission": "not_executed",
            "message": message,
        })
        if document:
            LocalBookkeepingRecordService(self.ledger, self.config).upsert_from_document(int(document_id))
        self.ledger.record_audit_event({
            "action": "local_export_attempt.source_processing_blocked",
            "entityType": "export_attempt",
            "entityId": str(attempt["id"]),
            "details": {
                "actor": actor, "stage": stage, "documentId": document_id,
                "reason": status, "externalSubmission": "not_executed",
            },
        })
        return {
            "success": False, "status": status, "message": message,
            "externalSubmission": "not_executed",
            "exportAttempt": self.ledger.get_export_attempt(int(attempt["id"])),
        }

    def _master_ledger_staleness(
        self,
        attempt: Dict[str, Any],
        stage: str,
        actor: str,
    ) -> Optional[Dict[str, Any]]:
        check = _master_ledger_freshness(self.ledger, self.config, attempt)
        if check.get("status") in {"not_applicable", "fresh"}:
            return None
        self.ledger.record_audit_event({
            "action": "local_export_attempt.master_ledger_stale",
            "entityType": "export_attempt",
            "entityId": str(attempt.get("id")),
            "details": {
                "actor": actor,
                "stage": stage,
                "targetSystem": attempt.get("target_system"),
                "storedChecksum": check.get("storedChecksum"),
                "currentChecksum": check.get("currentChecksum"),
                "reason": check.get("status"),
                "externalSubmission": "not_executed",
            },
        })
        return {
            "success": False,
            "status": "stale_master_ledger_draft",
            "message": "Master-ledger draft no longer matches the current FAB source record. Regenerate the route/export attempt before approval or external result recording.",
            "storedChecksum": check.get("storedChecksum"),
            "currentChecksum": check.get("currentChecksum"),
            "currentDraft": check.get("currentDraft"),
            "externalSubmission": "not_executed",
        }

    def _export_block_for_routing(
        self,
        routing_attempt: Dict[str, Any],
        document: Optional[Dict[str, Any]],
        record: Optional[Dict[str, Any]],
        operation: Dict[str, Any],
    ) -> Optional[Dict[str, str]]:
        if str(routing_attempt.get("status") or "") not in PREPARED_ROUTING_STATUSES:
            return {
                "status": "blocked_routing",
                "message": f"Routing attempt status {routing_attempt.get('status')} is not ready for export approval.",
            }
        if not document and not record:
            return {"status": "blocked_source_missing", "message": "Source document or bookkeeping record is missing for this route."}
        open_reviews = [
            item for item in document.get("review_items") or []
            if item.get("status") in OPEN_REVIEW_STATUSES
        ] if document else []
        if open_reviews:
            return {"status": "blocked_by_review", "message": "Document still has open review items."}
        if record and record.get("review_required"):
            return {"status": "blocked_by_review", "message": "Bookkeeping record still requires manual review."}
        plan = operation.get("plan") if isinstance(operation.get("plan"), dict) else {}
        if not operation or plan.get("status") != "planned":
            return {"status": "blocked_invalid_plan", "message": "Wave operation plan is incomplete."}
        return None


def _target_system_from_target(value: Any) -> str:
    text = str(value or "waveapps").strip()
    if ":" in text:
        return text.split(":", 1)[0] or "waveapps"
    return text or "waveapps"


def _target_system_for_attempt(attempt: Dict[str, Any]) -> str:
    metadata = attempt.get("metadata") if isinstance(attempt.get("metadata"), dict) else {}
    return str(
        _first_present(
            attempt.get("target_system"),
            attempt.get("targetSystem"),
            metadata.get("targetSystem"),
            metadata.get("target_system"),
            _target_system_from_target(metadata.get("routingTarget")),
            "waveapps",
        )
    ).strip().lower()


def _is_mijngeldzaken_target(target_system: Any) -> bool:
    normalized = str(target_system or "").strip().lower().replace("_", "-")
    return normalized in {"mijngeldzaken", "mijngeldzaken-nl", "mijngeldzaken.nl"} or normalized.startswith("mijngeldzaken:")


def _csv_artifact_content(draft: Dict[str, Any]) -> str:
    row = draft.get("importRow") if isinstance(draft.get("importRow"), dict) else {}
    columns = draft.get("columns") if isinstance(draft.get("columns"), list) else list(row.keys())
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=[str(column) for column in columns], extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerow(neutralize_csv_row({str(key): value for key, value in row.items()}))
    return buffer.getvalue()


def _master_ledger_draft(
    target_system: Any,
    operation: Dict[str, Any],
    operation_payload: Dict[str, Any],
    routing_attempt: Dict[str, Any],
    document: Optional[Dict[str, Any]],
    record: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if not _is_mijngeldzaken_target(target_system):
        return None
    source_proof = {
        "routingAttemptId": routing_attempt.get("id"),
        "documentId": routing_attempt.get("document_id"),
        "bookkeepingRecordId": (record or {}).get("id"),
        "sourceDocumentId": (document or {}).get("source_document_id"),
        "bankTransactionId": (record or {}).get("bank_transaction_id"),
        "duplicateFingerprint": (document or {}).get("duplicate_fingerprint"),
        "reconciliationStatus": (record or document or {}).get("reconciliation_status"),
    }
    source_proof = {key: value for key, value in source_proof.items() if value not in (None, "")}
    return build_mijngeldzaken_master_ledger_draft(
        str(operation.get("action_id") or ""),
        str(operation.get("surface") or ""),
        operation_payload,
        source_proof=source_proof,
    )


def _target_label(target_system: Any) -> str:
    if _is_mijngeldzaken_target(target_system):
        return "MijnGeldzaken"
    return "Wave"


def _record_from_routing_metadata(
    ledger: LocalOperationsLedger,
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    record_id = metadata.get("bookkeepingRecordId") or metadata.get("bookkeeping_record_id")
    if not record_id:
        return {}
    try:
        return ledger.get_bookkeeping_record(int(record_id)) or {}
    except (TypeError, ValueError):
        return {}


def _existing_export_attempt_for_route(
    ledger: LocalOperationsLedger,
    routing_attempt: Dict[str, Any],
    operation: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    return ledger.find_export_attempt(
        routing_attempt_id=routing_attempt.get("id"),
        operation_id=operation.get("operation_id"),
    )


def _preserved_state_changed(
    ledger: LocalOperationsLedger,
    existing_attempt: Dict[str, Any],
    details: Dict[str, Any],
) -> bool:
    latest = ledger.find_audit_event(
        "local_export_attempt.prepare_preserved",
        "export_attempt",
        str(existing_attempt.get("id")),
    )
    if not latest:
        return True
    latest_details = latest.get("details") if isinstance(latest.get("details"), dict) else {}
    state_fields = (
        "routingAttemptId",
        "documentId",
        "bookkeepingRecordId",
        "operationId",
        "status",
        "externalSubmission",
    )
    return any(latest_details.get(field) != details.get(field) for field in state_fields)


def _same_export_operation(
    existing_attempt: Optional[Dict[str, Any]],
    routing_attempt: Dict[str, Any],
    operation: Dict[str, Any],
) -> bool:
    if not existing_attempt:
        return False
    operation_id = str(operation.get("operation_id") or "").strip()
    if operation_id:
        return str(existing_attempt.get("operation_id") or "").strip() == operation_id
    return existing_attempt.get("routing_attempt_id") == routing_attempt.get("id")


def _master_ledger_checksum(metadata: Dict[str, Any]) -> Optional[str]:
    checksum = metadata.get("masterLedgerChecksum")
    if checksum:
        return str(checksum)
    draft = metadata.get("masterLedgerDraft") if isinstance(metadata.get("masterLedgerDraft"), dict) else {}
    checksum = draft.get("checksum")
    return str(checksum) if checksum else None


def master_ledger_freshness(
    ledger: LocalOperationsLedger,
    config: Dict[str, Any],
    attempt: Dict[str, Any],
) -> Dict[str, Any]:
    """Return whether an approval-gated master-ledger draft still matches FAB state."""
    return _master_ledger_freshness(ledger, config, attempt)


def _master_ledger_freshness(
    ledger: LocalOperationsLedger,
    config: Dict[str, Any],
    attempt: Dict[str, Any],
) -> Dict[str, Any]:
    target_system = _target_system_for_attempt(attempt)
    metadata = attempt.get("metadata") if isinstance(attempt.get("metadata"), dict) else {}
    stored_checksum = _master_ledger_checksum(metadata)
    if not _is_mijngeldzaken_target(target_system) or not stored_checksum:
        return {"status": "not_applicable"}

    current_draft = _current_mijngeldzaken_master_ledger_draft(ledger, config, attempt)
    current_checksum = current_draft.get("checksum") if isinstance(current_draft, dict) else None
    if not current_checksum:
        return {
            "status": "source_missing",
            "storedChecksum": stored_checksum,
            "currentChecksum": None,
            "currentDraft": current_draft,
        }
    if str(current_checksum) != str(stored_checksum):
        return {
            "status": "checksum_mismatch",
            "storedChecksum": stored_checksum,
            "currentChecksum": str(current_checksum),
            "currentDraft": current_draft,
        }
    return {
        "status": "fresh",
        "storedChecksum": stored_checksum,
        "currentChecksum": str(current_checksum),
        "currentDraft": current_draft,
    }


def _current_mijngeldzaken_master_ledger_draft(
    ledger: LocalOperationsLedger,
    config: Dict[str, Any],
    attempt: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    package = _current_mijngeldzaken_master_ledger_package(ledger, config, attempt)
    return package.get("draft") if package else None


def _current_mijngeldzaken_master_ledger_package(
    ledger: LocalOperationsLedger,
    config: Dict[str, Any],
    attempt: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    document = ledger.get_document(int(attempt["document_id"])) if attempt.get("document_id") else None
    record = (document or {}).get("bookkeeping_record")
    if not record and attempt.get("bookkeeping_record_id"):
        record = ledger.get_bookkeeping_record(int(attempt["bookkeeping_record_id"]))
    if not document and not record:
        return None

    source = _document_data_for_mijngeldzaken(document) if document else _record_data_for_mijngeldzaken(record or {})
    metadata = attempt.get("metadata") if isinstance(attempt.get("metadata"), dict) else {}
    attempt_payload = attempt.get("payload") if isinstance(attempt.get("payload"), dict) else {}
    mgz_category = _mijngeldzaken_category(source, config)
    default_account = _config_value(
        config,
        "fab_mijngeldzaken_default_account",
        "operations_mijngeldzaken_default_account",
        "mijngeldzaken_default_account",
        default="Huishouden",
    )
    if _source_category_unchanged(source, metadata):
        if attempt_payload.get("category") not in (None, ""):
            mgz_category = str(attempt_payload.get("category"))
        if attempt_payload.get("account") not in (None, ""):
            default_account = str(attempt_payload.get("account"))
    action_id = resolve_mijngeldzaken_action_for_document(source)
    destination = classify_mijngeldzaken_destination(source)
    payload = build_mijngeldzaken_action_payload(
        source,
        mgz_category,
        default_account=default_account,
    )
    routing_attempt = (
        ledger.get_routing_attempt(int(attempt["routing_attempt_id"]))
        if attempt.get("routing_attempt_id")
        else {}
    ) or {}
    source_proof = {
        "routingAttemptId": attempt.get("routing_attempt_id") or routing_attempt.get("id"),
        "documentId": attempt.get("document_id"),
        "bookkeepingRecordId": (record or {}).get("id") or attempt.get("bookkeeping_record_id"),
        "sourceDocumentId": (document or {}).get("source_document_id"),
        "bankTransactionId": (record or {}).get("bank_transaction_id"),
        "duplicateFingerprint": (document or {}).get("duplicate_fingerprint"),
        "reconciliationStatus": (record or document or {}).get("reconciliation_status"),
    }
    source_proof = {key: value for key, value in source_proof.items() if value not in (None, "")}
    surface = str(destination.get("target_surface") or attempt.get("surface") or "")
    draft = build_mijngeldzaken_master_ledger_draft(
        action_id,
        surface,
        payload,
        source_proof=source_proof,
    )
    return {
        "actionId": action_id,
        "surface": surface or draft.get("surface"),
        "payload": payload,
        "draft": draft,
        "sourceProof": source_proof,
        "destination": destination,
    }


def _source_category_unchanged(source: Dict[str, Any], metadata: Dict[str, Any]) -> bool:
    snapshot = metadata.get("documentSnapshot") if isinstance(metadata.get("documentSnapshot"), dict) else None
    if snapshot is None:
        snapshot = metadata.get("bookkeepingRecordSnapshot") if isinstance(metadata.get("bookkeepingRecordSnapshot"), dict) else None
    if not snapshot:
        return False
    snapshot_category = snapshot.get("category")
    if snapshot_category in (None, ""):
        return False
    return str(source.get("category") or "").strip() == str(snapshot_category).strip()


def _document_data_for_mijngeldzaken(document: Dict[str, Any]) -> Dict[str, Any]:
    extracted = dict(document.get("extracted_data") or {})
    record = document.get("bookkeeping_record") or {}
    record_line_items = record.get("line_items") or []
    if record_line_items:
        extracted["line_items"] = [_line_item_from_record_line(item) for item in record_line_items]
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


def _record_data_for_mijngeldzaken(record: Dict[str, Any]) -> Dict[str, Any]:
    amount = _positive_amount(record.get("amount"))
    extracted = {
        "vendor_name": record.get("vendor_name"),
        "transaction_date": record.get("record_date"),
        "total_amount": amount,
        "currency": record.get("currency"),
        "description": record.get("description"),
        "document_type": "receipt",
        "line_items": [_line_item_from_record_line(item) for item in record.get("line_items") or []],
    }
    return {
        "id": record.get("id"),
        "document_type": "receipt",
        "vendor_name": record.get("vendor_name"),
        "category": record.get("category"),
        "transaction_date": record.get("record_date"),
        "total_amount": amount,
        "vat_amount": record.get("vat_amount"),
        "description": record.get("description"),
        "line_items": extracted["line_items"],
        "extracted_data": extracted,
    }


def _line_item_from_record_line(item: Dict[str, Any]) -> Dict[str, Any]:
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


def _mijngeldzaken_category(source: Dict[str, Any], config: Dict[str, Any]) -> str:
    category = str(source.get("category") or "").strip()
    mapping = config.get("mijngeldzaken_category_mapping") or config.get("fab_mijngeldzaken_category_mapping") or {}
    if isinstance(mapping, dict):
        return str(mapping.get(category) or category or "Overig")
    return category or "Overig"


def _config_value(config: Dict[str, Any], *keys: str, default: str) -> str:
    for key in keys:
        value = config.get(key)
        if value not in (None, ""):
            return str(value)
    return default


def _positive_amount(value: Any) -> Any:
    if value is None:
        return None
    try:
        return abs(float(value))
    except (TypeError, ValueError):
        return value


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


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _external_submission_for_result(status: str) -> str:
    if status in {"executed", "submitted"}:
        return "executed"
    if status == "queued":
        return "queued"
    return "failed"


def _message_for_result(status: str) -> str:
    if status in {"executed", "submitted"}:
        return "External export result recorded."
    if status == "queued":
        return "External export queued by a separate executor."
    return "External export failure recorded."


def _compact_backup_result(result: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not result:
        return None
    manifest = result.get("manifest") if isinstance(result.get("manifest"), dict) else {}
    return {
        "status": result.get("status"),
        "backupPath": result.get("backupPath"),
        "backupFilename": result.get("backupFilename"),
        "ledgerSha256": manifest.get("ledgerSha256"),
        "error": result.get("error"),
        "externalSubmission": "not_executed",
    }


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _map_execution_status(status: str, target_system: Any = "waveapps") -> tuple[str, str, str]:
    target_label = _target_label(target_system)
    if status in {"success", "executed"}:
        return "executed", "executed", f"{target_label} execution completed successfully."
    if status == "submitted":
        return "submitted", "submitted", f"{target_label} external execution submitted."
    if status == "queued":
        return "queued", "queued", f"{target_label} execution queued for external processing."
    if status in DEFERRED_EXECUTION_STATUSES:
        return "not_executed", "deferred", f"{target_label} execution was deferred by the outbound quota guard."
    if status == "blocked_requires_confirmation":
        return "not_executed", "attention_required", "Execution needs an explicitly supported confirmed-action executor."
    if status == "blocked_requires_credentials":
        return "not_executed", "attention_required", "Execution is waiting for external credentials."
    if status == "needs_review":
        return "not_executed", "attention_required", "Execution needs additional review before it can run."
    return "failed", "failed", f"{target_label} execution could not run: {status!r}."


def _execution_retry_due(attempt: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    if str(attempt.get("status") or "") != "deferred":
        return True
    metadata = attempt.get("metadata") if isinstance(attempt.get("metadata"), dict) else {}
    retry = metadata.get("retry") if isinstance(metadata.get("retry"), dict) else {}
    next_retry_at = _parse_datetime(retry.get("nextRetryAt"))
    if not next_retry_at:
        return True
    return (now or datetime.now(timezone.utc)) >= next_retry_at


def _next_retry_state(
    metadata: Dict[str, Any],
    provider_result: Dict[str, Any],
    config: Dict[str, Any],
    now_text: str,
) -> Dict[str, Any]:
    previous = metadata.get("retry") if isinstance(metadata.get("retry"), dict) else {}
    delay_value = provider_result.get("retry_after_seconds")
    if delay_value in (None, ""):
        status = str(provider_result.get("status") or "")
        delay_value = config.get(
            "quota_exhausted_retry_delay_seconds"
            if status == "quota_exhausted"
            else "rate_limit_retry_delay_seconds",
            3600 if status == "quota_exhausted" else 60,
        )
    try:
        delay_seconds = max(float(delay_value), 1.0)
    except (TypeError, ValueError):
        delay_seconds = 60.0
    started_at = _parse_datetime(now_text) or datetime.now(timezone.utc)
    return {
        "attemptCount": int(previous.get("attemptCount") or 0) + 1,
        "reason": provider_result.get("status") or "rate_limited",
        "message": provider_result.get("message"),
        "retryAfterSeconds": delay_seconds,
        "nextRetryAt": (started_at + timedelta(seconds=delay_seconds)).isoformat(),
    }


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _record_status_for_execution(status: str) -> str:
    if status == "queued":
        return "queued"
    if status in {"executed", "submitted"}:
        return "executed"
    return "failed"


def _list_optional_strs(value: Any) -> Optional[list[str]]:
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        result = [str(item) for item in value if str(item)]
        return result or None
    text = str(value).strip()
    if not text:
        return None
    if "," in text:
        return [item.strip() for item in text.split(",") if item.strip()]
    if ";" in text:
        return [item.strip() for item in text.split(";") if item.strip()]
    return [text]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
