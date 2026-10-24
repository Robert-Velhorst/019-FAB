from datetime import date, datetime, timezone
import re
import time
from typing import Any, Dict, List, Optional
from uuid import uuid4

from src.data_entry.waveapps_account_discovery import resolve_wave_target_config
from src.data_entry.waveapps_entity_sync import WaveappsEntitySyncService
from src.operations.local_health import LocalOperationsHealth
from src.operations.local_intake import LocalFolderIntake
from src.operations.local_bank_transactions import LocalBankTransactionImportService
from src.operations.local_autonomy_control import AUTONOMY_EMERGENCY_STOP
from src.operations.local_bookkeeping_records import LocalBookkeepingRecordService
from src.operations.local_close_pack import LocalClosePackService
from src.operations.local_close_readiness import LocalCloseReadinessService
from src.operations.local_connector_intake import LocalConnectorIntakeService
from src.operations.local_exceptions import LocalExceptionQueueService
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_master_ledger import LocalMasterLedgerService
from src.operations.local_processing import (
    LocalDocumentProcessor,
    duplicate_candidate_reassessment_plan,
    duplicate_link_cycles,
    trusted_category_suggestion_candidates,
)
from src.operations.local_readiness import LocalReadinessService
from src.operations.local_reconciliation import LocalReconciliationService
from src.operations.local_targets import resolve_document_target_system
from src.operations.local_routing import (
    PREPARED_ROUTING_STATUSES,
    ROUTABLE_BOOKKEEPING_EXPORT_STATUSES,
    ROUTABLE_BOOKKEEPING_RECORD_STATUSES,
    LocalRoutingService,
    ROUTABLE_DOCUMENT_STATUSES,
)
from src.operations.local_exports import LocalExportAttemptService
from src.operations.local_wave_control import LocalWaveControlService


OPEN_REVIEW_STATUSES = ("pending", "in_review")
IMPORTED_DOCUMENT_STATUSES = ("imported",)
PENDING_ROUTE_STATUSES = ("draft_prepared", "needs_confirmation", "queued")
RECONCILIATION_REVIEW_STATUSES = ("candidate", "missing_receipt", "unmatched_document")
AUTONOMOUS_TRIGGER = "local_autonomous_cycle"
AUTONOMY_LEASE_NAME = "local_autonomous_cycle"
WAVE_ENTITY_TARGETS = ("waveapps_business", "waveapps_personal")
WAVE_ENTITY_TYPES = ("customer", "product", "invoice")
AUTONOMY_MASTER_LEDGER_LIMIT = 500
AUTONOMY_EXECUTION_ORDER = (
    "sync_connector_sources",
    "rescan_intake",
    "process_imported",
    "refresh_bank_records",
    "run_reconciliation",
    "refresh_wave_entity_mirror",
    "prepare_wave_drafts",
    "prepare_export_attempts",
    "regenerate_stale_export_attempts",
    "prepare_master_ledger_projection",
    "plan_wave_daily_reconciliation",
    "prepare_period_close_pack",
)

# Only local, idempotent data-flow edges belong here. Recovery follows these
# edges after an exact failed-step retry; it never crosses the external export
# boundary or schedules unrelated autonomous work.
AUTONOMY_RECOVERY_DEPENDENCIES = {
    "sync_connector_sources": ("process_imported",),
    "rescan_intake": ("process_imported",),
    "process_imported": (
        "prepare_wave_drafts",
        "prepare_master_ledger_projection",
    ),
    "refresh_bank_records": (
        "prepare_wave_drafts",
        "prepare_master_ledger_projection",
    ),
    "prepare_wave_drafts": (
        "prepare_export_attempts",
        "prepare_master_ledger_projection",
    ),
    "prepare_export_attempts": ("prepare_master_ledger_projection",),
    "regenerate_stale_export_attempts": ("prepare_master_ledger_projection",),
    "prepare_master_ledger_projection": ("prepare_period_close_pack",),
}


class AutonomyEmergencyStop(RuntimeError):
    """Raised between workflow steps when the operator stop is active."""


class LocalAutonomousService:
    """Policy-gated local autonomy loop for FAB operations.

    The loop only executes low-risk local work: read-only connector collection,
    folder intake, local processing, Wave draft preparation, reconciliation
    candidate creation, and read-only Wave planning. External posting, review
    decisions, credential changes, restore, deletion, and customer-facing
    communication stay outside this executor. It may clear stale machine-created
    gates when current deterministic evidence disproves the original condition.
    """

    def __init__(
        self,
        ledger: LocalOperationsLedger,
        config: Optional[Dict[str, Any]] = None,
        readiness: Optional[LocalReadinessService] = None,
        intake_paths: Optional[List[str]] = None,
        intake_extensions: Optional[List[str]] = None,
    ):
        self.ledger = ledger
        self.config = config or {}
        self.readiness = readiness
        self.intake_paths = list(intake_paths or [])
        self.intake_extensions = list(intake_extensions or [])

    def plan(
        self,
        limit: int = 25,
        bank_transactions: Optional[List[Dict[str, Any]]] = None,
        include_wave_plan: bool = True,
        include_wave_sync: bool = True,
        include_connector_sync: bool = True,
        *,
        readiness_summary: Optional[Dict[str, Any]] = None,
        health_summary: Optional[Dict[str, Any]] = None,
        exceptions_summary: Optional[Dict[str, Any]] = None,
        close_readiness_summary: Optional[Dict[str, Any]] = None,
        metrics_summary: Optional[Dict[str, Any]] = None,
        master_ledger_summary: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        limit = _bounded_limit(limit, default=25, maximum=100)
        emergency_stop = self.ledger.get_runtime_control(AUTONOMY_EMERGENCY_STOP)
        readiness = readiness_summary if readiness_summary is not None else self._readiness_summary()
        metrics = metrics_summary if metrics_summary is not None else self.ledger.dashboard_metrics()
        master_ledger = master_ledger_summary
        if master_ledger is None:
            master_ledger = LocalMasterLedgerService(self.ledger, self.config).project(
                limit=AUTONOMY_MASTER_LEDGER_LIMIT
            )
        health = health_summary
        if health is None:
            health = LocalOperationsHealth(self.ledger, self.config).summarize(
                metrics=metrics,
                master_ledger=master_ledger,
            )
        exceptions = exceptions_summary
        if exceptions is None:
            exceptions = LocalExceptionQueueService(self.ledger, self.config).list_exceptions(
                limit=limit,
                include_entities=False,
                health=health,
                master_ledger=master_ledger,
            )
        close_readiness = close_readiness_summary
        if close_readiness is None:
            close_readiness = LocalCloseReadinessService(self.ledger, self.config).assess(
                metrics=metrics,
                health=health,
                master_ledger=master_ledger,
            )
        counts = self._counts(limit, metrics=metrics)
        exception_summary = exceptions.get("summary") or {}
        counts["operatingExceptions"] = int(exception_summary.get("total") or 0)
        counts["highSeverityExceptions"] = int((exception_summary.get("bySeverity") or {}).get("high") or 0)
        counts["mediumSeverityExceptions"] = int((exception_summary.get("bySeverity") or {}).get("medium") or 0)
        counts["lowSeverityExceptions"] = int((exception_summary.get("bySeverity") or {}).get("low") or 0)
        counts["closeBlockingGates"] = int(close_readiness.get("blockingCount") or 0)
        counts["closeAttentionGates"] = int(close_readiness.get("attentionCount") or 0)
        counts["masterLedgerRows"] = int(master_ledger.get("summary", {}).get("totalRows") or 0)
        counts["masterLedgerBlockedRows"] = int(master_ledger.get("summary", {}).get("blockedRows") or 0)
        counts["masterLedgerReadyForDraft"] = int(master_ledger.get("summary", {}).get("readyForDraft") or 0)
        counts["masterLedgerReadyForApproval"] = int(master_ledger.get("summary", {}).get("readyForApproval") or 0)
        counts["masterLedgerReadyForExecution"] = int(master_ledger.get("summary", {}).get("readyForExternalExecution") or 0)
        stale_rows = _regenerable_stale_master_ledger_rows(master_ledger)
        counts["staleMasterLedgerDrafts"] = len(stale_rows)
        counts["staleMasterLedgerTargets"] = _target_breakdown(stale_rows, _master_row_target_system)
        wave_entity_sync = self._wave_entity_sync_plan(include_wave_sync)
        connector_sync = self._connector_sync_plan(include_connector_sync)
        counts["connectorSyncableSources"] = len(connector_sync["syncableSources"])
        counts["waveEntitySyncConfiguredTargets"] = len(wave_entity_sync["configuredTargets"])
        counts["waveEntitySyncTargetsDue"] = len(wave_entity_sync["dueTargets"])
        master_ledger_due = self._master_ledger_projection_due(master_ledger)
        wave_daily_plan_due = self._wave_daily_plan_due(include_wave_plan)
        guarded_reasons = self._blocked_reasons(readiness, health)
        if emergency_stop.get("active"):
            guarded_reasons.append("operator_emergency_stop")
        # Ledger-health failures must not freeze the read-only and local repair
        # work that can clear them. Security/readiness failures remain hard
        # stops, while unhealthy state continues to gate external execution.
        blocked_reasons = [
            reason
            for reason in guarded_reasons
            if reason != "operations_health_blocked"
        ]
        blocked = bool(blocked_reasons)
        bank_transactions = self._reconciliation_transactions(bank_transactions, limit)

        actions = [
            _action(
                "sync_connector_sources",
                "Collect configured Gmail, Drive, and other read-only document sources",
                "collect",
                "low",
                "read_only",
                connector_sync["requested"] and bool(connector_sync["syncableSources"]) and not blocked,
                _connector_sync_blocked_reason(connector_sync),
                {
                    "enabledSources": connector_sync["enabledSources"],
                    "syncableSources": connector_sync["syncableSources"],
                    "sourceStates": connector_sync["sourceStates"],
                    "externalSubmission": "not_executed",
                },
            ),
            _action(
                "rescan_intake",
                "Collect approved local/scanner folders",
                "collect",
                "low",
                "safe_auto",
                bool(self.intake_paths) and not blocked,
                "No intake folders are configured." if not self.intake_paths else None,
                {"intakePaths": self.intake_paths, "allowedExtensions": self.intake_extensions},
            ),
            _action(
                "refresh_wave_entity_mirror",
                "Refresh stale Wave customers, products/services, and invoices before downstream draft work",
                "collect",
                "low",
                "read_only",
                bool(wave_entity_sync["dueTargets"]) and wave_entity_sync["enabled"] and not blocked,
                _wave_entity_sync_blocked_reason(wave_entity_sync),
                {
                    "configuredTargets": wave_entity_sync["configuredTargets"],
                    "dueTargets": wave_entity_sync["dueTargets"],
                    "entityTypes": wave_entity_sync["entityTypes"],
                    "targetStates": wave_entity_sync["targetStates"],
                    "externalSubmission": "not_executed",
                },
            ),
            _action(
                "process_imported",
                "Process documents, repair duplicate evidence, and apply trusted categories",
                "extract_validate",
                "low",
                "safe_auto",
                (
                    counts["importedDocuments"] > 0
                    or counts["trustedCategorySuggestions"] > 0
                    or counts["duplicateLinkCycles"] > 0
                    or counts["duplicateCandidateReassessments"] > 0
                )
                and not blocked,
                "No imported documents or deterministic evidence repairs are waiting."
                if (
                    counts["importedDocuments"] == 0
                    and counts["trustedCategorySuggestions"] == 0
                    and counts["duplicateLinkCycles"] == 0
                    and counts["duplicateCandidateReassessments"] == 0
                )
                else None,
                {
                    "candidateDocuments": counts["importedDocuments"],
                    "trustedCategorySuggestions": counts["trustedCategorySuggestions"],
                    "duplicateLinkCycles": counts["duplicateLinkCycles"],
                    "duplicateCandidateReassessments": counts[
                        "duplicateCandidateReassessments"
                    ],
                    "limit": limit,
                    "externalSubmission": "not_executed",
                },
            ),
            _action(
                "prepare_wave_drafts",
                "Prepare downstream draft operations for Wave and MijnGeldzaken without external submission",
                "classify_post",
                "low",
                "safe_draft",
                (counts["routableDocuments"] + counts["routableBookkeepingRecords"]) > 0 and not blocked,
                "No reviewed/validated documents or bank records are ready for downstream draft preparation."
                if (counts["routableDocuments"] + counts["routableBookkeepingRecords"]) == 0
                else None,
                {
                    "candidateDocuments": counts["routableDocuments"],
                    "candidateBookkeepingRecords": counts["routableBookkeepingRecords"],
                    "targetBreakdown": counts["routableTargets"],
                    "externalSubmission": "not_executed",
                },
            ),
            _action(
                "prepare_export_attempts",
                "Prepare local export-attempt records from prepared routing drafts",
                "classify_post",
                "low",
                "safe_auto",
                counts["readyForExportAttempts"] > 0 and not blocked,
                "No prepared routing attempts are ready for export-attempt preparation."
                if counts["readyForExportAttempts"] == 0
                else None,
                {
                    "candidateRoutingAttempts": counts["readyForExportAttempts"],
                    "targetBreakdown": counts["readyForExportTargets"],
                    "externalSubmission": "not_executed",
                },
            ),
            _action(
                "regenerate_stale_export_attempts",
                "Regenerate stale MijnGeldzaken master-ledger drafts from current FAB source state",
                "classify_post",
                "low",
                "safe_auto",
                counts["staleMasterLedgerDrafts"] > 0 and not blocked,
                "No stale unsubmitted master-ledger drafts need regeneration."
                if counts["staleMasterLedgerDrafts"] == 0
                else None,
                {
                    "staleMasterLedgerDrafts": counts["staleMasterLedgerDrafts"],
                    "targetBreakdown": counts["staleMasterLedgerTargets"],
                    "externalSubmission": "not_executed",
                },
            ),
            _action(
                "refresh_bank_records",
                "Refresh bank-transaction bookkeeping records with approved local rules",
                "classify_post",
                "low",
                "safe_auto",
                counts["bankTransactions"] > 0 and not blocked,
                "No imported bank transactions are available." if counts["bankTransactions"] == 0 else None,
                {
                    "bankTransactions": counts["bankTransactions"],
                    "unreconciledBankTransactions": counts["unreconciledBankTransactions"],
                    "externalSubmission": "not_executed",
                },
            ),
            _action(
                "run_reconciliation",
                "Create reconciliation candidates from imported or supplied bank transactions",
                "match_reconcile",
                "low",
                "safe_draft",
                bool(bank_transactions) and not blocked,
                "No imported or supplied bank transactions are waiting." if not bank_transactions else None,
                {
                    "bankTransactions": len(bank_transactions),
                    "storedBankTransactions": counts["unreconciledBankTransactions"],
                    "candidateDocuments": counts["reconciliableDocuments"],
                    "externalSubmission": "not_executed",
                },
            ),
            _action(
                "prepare_master_ledger_projection",
                "Prepare a checksum-bound FAB master-ledger projection across Wave and MijnGeldzaken downstream state",
                "close_report",
                "low",
                "read_only",
                counts["masterLedgerRows"] > 0 and master_ledger_due and not blocked,
                "No normalized bookkeeping records are available for a master-ledger projection."
                if counts["masterLedgerRows"] == 0
                else "The current master-ledger checksum is already recorded."
                if not master_ledger_due
                else None,
                {
                    "rows": counts["masterLedgerRows"],
                    "blockedRows": counts["masterLedgerBlockedRows"],
                    "readyForDraft": counts["masterLedgerReadyForDraft"],
                    "readyForApproval": counts["masterLedgerReadyForApproval"],
                    "readyForExternalExecution": counts["masterLedgerReadyForExecution"],
                    "ledgerChecksum": master_ledger.get("ledgerChecksum"),
                    "projectionDue": master_ledger_due,
                    "externalSubmission": "not_executed",
                },
            ),
            _action(
                "plan_wave_daily_reconciliation",
                "Prepare the read-only Wave daily reconciliation workflow plan",
                "close_report",
                "low",
                "read_only",
                bool(include_wave_plan) and wave_daily_plan_due and not blocked,
                "Wave workflow planning was disabled for this request."
                if not include_wave_plan
                else "Today's Wave reconciliation plan is already recorded."
                if not wave_daily_plan_due
                else None,
                {
                    "planDate": date.today().isoformat(),
                    "planDue": wave_daily_plan_due,
                    "externalSubmission": "not_executed",
                },
            ),
            _action(
                "prepare_period_close_pack",
                "Prepare an audited period-close evidence pack when every close gate is ready",
                "close_report",
                "low",
                "read_only",
                bool(close_readiness.get("canClose")) and not blocked,
                _close_pack_blocked_reason(close_readiness) if not close_readiness.get("canClose") else None,
                {
                    "status": close_readiness.get("status"),
                    "blockingCount": close_readiness.get("blockingCount", 0),
                    "attentionCount": close_readiness.get("attentionCount", 0),
                    "externalSubmission": "not_executed",
                },
            ),
            _action(
                "exception_queue",
                "Resolve the exact operating exceptions FAB has identified",
                "exception_chase",
                "medium",
                "review_required",
                False,
                "Operating exceptions require review, correction, or an explicitly approved action.",
                {
                    "operatingExceptions": counts["operatingExceptions"],
                    "highSeverity": counts["highSeverityExceptions"],
                    "mediumSeverity": counts["mediumSeverityExceptions"],
                    "lowSeverity": counts["lowSeverityExceptions"],
                    "byType": exception_summary.get("byType") or {},
                    "externalSubmission": "not_executed",
                },
            ),
            _action(
                "review_queue",
                "Human review queue needs decisions before final bookkeeping",
                "exception_chase",
                "medium",
                "review_required",
                False,
                "Open review items require human approval, correction, or rejection.",
                {"openReviewItems": counts["openReviewItems"]},
            ),
            _action(
                "approve_routing_drafts",
                "Prepared routing drafts are waiting for explicit approval/export",
                "system_execute",
                "high",
                "approval_required",
                False,
                "External downstream submission remains outside the autonomous local cycle.",
                {
                    "pendingRoutingDrafts": counts["pendingRoutingDrafts"],
                    "targetBreakdown": counts["pendingRoutingTargets"],
                },
            ),
            _action(
                "approve_export_attempts",
                "Prepared export attempts are waiting for local approval before external submission",
                "system_execute",
                "high",
                "approval_required",
                False,
                "Export attempts can only be approved from the FAB dashboard and workflow.",
                {
                    "pendingExportApprovals": counts["pendingExportApprovals"],
                    "targetBreakdown": counts["pendingExportApprovalTargets"],
                },
            ),
            _action(
                "approve_reconciliation",
                "Reconciliation candidates need audited approval before close",
                "match_reconcile",
                "medium",
                "approval_required",
                False,
                "Candidate matches are never finalized by the autonomous local cycle.",
                {"reconciliationReviewItems": counts["reconciliationReviewItems"]},
            ),
        ]

        if blocked:
            for action in actions:
                if action["canRun"]:
                    action["canRun"] = False
                    action["blockedReason"] = "; ".join(blocked_reasons)

        runnable_actions = [
            action
            for action in actions
            if action["canRun"] and action["mode"] in {"safe_auto", "safe_draft", "read_only"}
        ]
        manual_actions = [
            action
            for action in actions
            if action["mode"] in {"review_required", "approval_required"}
            and (
                action["evidence"].get("operatingExceptions")
                or action["evidence"].get("openReviewItems")
                or action["evidence"].get("pendingRoutingDrafts")
                or action["evidence"].get("pendingExportApprovals")
                or action["evidence"].get("reconciliationReviewItems")
            )
        ]
        if blocked:
            status = "blocked"
        elif runnable_actions:
            status = "ready"
        elif manual_actions:
            status = "needs_review"
        else:
            status = "idle"

        return {
            "status": status,
            "canRunAutonomously": status == "ready",
            "externalSubmission": "not_executed",
            "blockedReasons": blocked_reasons,
            "guardedReasons": guarded_reasons,
            "counts": counts,
            "readiness": _compact_readiness(readiness),
            "health": _compact_health(health),
            "exceptions": _compact_exceptions(exceptions),
            "closeReadiness": _compact_close_readiness(close_readiness),
            "emergencyStop": emergency_stop,
            "runtimeLease": self.ledger.get_runtime_lease(AUTONOMY_LEASE_NAME),
            "actions": actions,
            "runnableActionIds": [action["id"] for action in runnable_actions],
            "manualActionIds": [action["id"] for action in manual_actions],
            "nextAction": _next_action(status, runnable_actions, manual_actions, blocked_reasons),
        }

    def run_cycle(
        self,
        limit: int = 25,
        bank_transactions: Optional[List[Dict[str, Any]]] = None,
        include_wave_plan: bool = True,
        dry_run: bool = False,
        include_wave_sync: bool = True,
        include_connector_sync: bool = True,
        allowed_action_ids: Optional[List[str]] = None,
        trigger_source: str = AUTONOMOUS_TRIGGER,
        workflow_metadata: Optional[Dict[str, Any]] = None,
        step_attempts: Optional[Dict[str, int]] = None,
        recovery_mode: bool = False,
        recovery_action_id: Optional[str] = None,
        recovery_continuation_action_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        if dry_run:
            return self._run_cycle_once(
                limit=limit,
                bank_transactions=bank_transactions,
                include_wave_plan=include_wave_plan,
                dry_run=True,
                include_wave_sync=include_wave_sync,
                include_connector_sync=include_connector_sync,
                allowed_action_ids=allowed_action_ids,
                trigger_source=trigger_source,
                workflow_metadata=workflow_metadata,
                step_attempts=step_attempts,
                recovery_mode=recovery_mode,
                recovery_action_id=recovery_action_id,
                recovery_continuation_action_ids=recovery_continuation_action_ids,
            )
        owner_token = uuid4().hex
        lease = self.ledger.acquire_runtime_lease(
            AUTONOMY_LEASE_NAME,
            owner_token,
            ttl_seconds=_positive_float_config(
                self.config,
                "fab_autonomy_lease_seconds",
                "operations_autonomy_lease_seconds",
                default=21600.0,
            ),
            metadata={"trigger": trigger_source},
        )
        if not lease.get("acquired"):
            plan = self.plan(
                limit=limit,
                bank_transactions=bank_transactions,
                include_wave_plan=include_wave_plan,
                include_wave_sync=include_wave_sync,
                include_connector_sync=include_connector_sync,
            )
            self.ledger.record_audit_event({
                "action": "local_autonomy.cycle_skipped_already_running",
                "entityType": "runtime_lease",
                "entityId": AUTONOMY_LEASE_NAME,
                "details": {
                    "lease": lease.get("lease"),
                    "externalSubmission": "not_executed",
                },
            })
            return {
                "success": False,
                "status": "already_running",
                "externalSubmission": "not_executed",
                "plan": plan,
                "runtimeLease": lease.get("lease"),
                "executedActions": [],
                "skippedActions": plan["actions"],
            }
        result = None
        try:
            result = self._run_cycle_once(
                limit=limit,
                bank_transactions=bank_transactions,
                include_wave_plan=include_wave_plan,
                dry_run=False,
                include_wave_sync=include_wave_sync,
                include_connector_sync=include_connector_sync,
                allowed_action_ids=allowed_action_ids,
                trigger_source=trigger_source,
                workflow_metadata=workflow_metadata,
                step_attempts=step_attempts,
                recovery_mode=recovery_mode,
                recovery_action_id=recovery_action_id,
                recovery_continuation_action_ids=recovery_continuation_action_ids,
            )
            return result
        finally:
            released = self.ledger.release_runtime_lease(AUTONOMY_LEASE_NAME, owner_token)
            released_lease = {
                **(lease.get("lease") or {}),
                "active": False if released else (lease.get("lease") or {}).get("active"),
                "released": released,
            }
            if isinstance(result, dict):
                result["runtimeLease"] = released_lease
                if isinstance(result.get("plan"), dict):
                    result["plan"]["runtimeLease"] = released_lease
            if not released:
                self.ledger.record_audit_event({
                    "action": "local_autonomy.lease_release_failed",
                    "entityType": "runtime_lease",
                    "entityId": AUTONOMY_LEASE_NAME,
                    "details": {"externalSubmission": "not_executed"},
                })

    def _run_cycle_once(
        self,
        limit: int = 25,
        bank_transactions: Optional[List[Dict[str, Any]]] = None,
        include_wave_plan: bool = True,
        dry_run: bool = False,
        include_wave_sync: bool = True,
        include_connector_sync: bool = True,
        allowed_action_ids: Optional[List[str]] = None,
        trigger_source: str = AUTONOMOUS_TRIGGER,
        workflow_metadata: Optional[Dict[str, Any]] = None,
        step_attempts: Optional[Dict[str, int]] = None,
        recovery_mode: bool = False,
        recovery_action_id: Optional[str] = None,
        recovery_continuation_action_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        limit = _bounded_limit(limit, default=25, maximum=100)
        selected_action_ids = (
            None
            if allowed_action_ids is None
            else {str(action_id) for action_id in allowed_action_ids if str(action_id).strip()}
        )
        if selected_action_ids is not None and not selected_action_ids:
            raise ValueError("At least one autonomous action is required")
        unknown_action_ids = (selected_action_ids or set()) - set(AUTONOMY_EXECUTION_ORDER)
        if unknown_action_ids:
            raise ValueError(
                "Unsupported autonomous action(s): " + ", ".join(sorted(unknown_action_ids))
            )
        retry_action_id = str(recovery_action_id or "").strip() or None
        continuation_action_ids = {
            str(action_id)
            for action_id in (recovery_continuation_action_ids or [])
            if str(action_id).strip()
        }
        if recovery_mode:
            if retry_action_id is None and selected_action_ids and len(selected_action_ids) == 1:
                retry_action_id = next(iter(selected_action_ids))
            if retry_action_id is None or selected_action_ids is None:
                raise ValueError("Recovery mode requires an exact selected retry action")
            if retry_action_id not in selected_action_ids:
                raise ValueError("Recovery retry action must be selected")
            if not continuation_action_ids.issubset(selected_action_ids - {retry_action_id}):
                raise ValueError("Recovery continuation actions must be selected descendants")
            if selected_action_ids != {retry_action_id, *continuation_action_ids}:
                raise ValueError("Recovery cannot select unrelated autonomous actions")
            if not continuation_action_ids.issubset(
                _autonomy_recovery_descendants(retry_action_id)
            ):
                raise ValueError("Recovery continuation actions must follow the dependency graph")
        elif retry_action_id is not None or continuation_action_ids:
            raise ValueError("Recovery actions require recovery mode")
        bank_transactions = self._reconciliation_transactions(bank_transactions, limit)
        plan = self.plan(
            limit=limit,
            bank_transactions=bank_transactions,
            include_wave_plan=include_wave_plan,
            include_wave_sync=include_wave_sync,
            include_connector_sync=include_connector_sync,
        )
        if recovery_mode:
            plan_actions = {
                str(action.get("id")): action
                for action in plan.get("actions") or []
                if action.get("id")
            }
            unsafe_recovery_actions = [
                action_id
                for action_id in selected_action_ids or set()
                if (
                    str((plan_actions.get(action_id) or {}).get("risk") or "unknown") != "low"
                    or str((plan_actions.get(action_id) or {}).get("mode") or "unknown")
                    not in {"safe_auto", "safe_draft", "read_only"}
                )
            ]
            if unsafe_recovery_actions:
                raise ValueError(
                    "Recovery cannot select unsafe autonomous action(s): "
                    + ", ".join(sorted(unsafe_recovery_actions))
                )
        if dry_run:
            return {
                "success": True,
                "status": "dry_run",
                "externalSubmission": "not_executed",
                "plan": plan,
                "executedActions": [],
                "skippedActions": plan["actions"],
            }
        if plan["status"] == "blocked":
            self.ledger.record_audit_event({
                "action": "local_autonomy.cycle_blocked",
                "entityType": "autonomous_cycle",
                "details": {
                    "blockedReasons": plan["blockedReasons"],
                    "externalSubmission": "not_executed",
                },
            })
            return {
                "success": False,
                "status": "blocked",
                "externalSubmission": "not_executed",
                "plan": plan,
                "executedActions": [],
                "skippedActions": plan["actions"],
            }

        run_metadata = {
            "plan": {
                "status": plan["status"],
                "runnableActionIds": plan["runnableActionIds"],
                "selectedActionIds": sorted(selected_action_ids) if selected_action_ids is not None else None,
                "recoveryActionId": retry_action_id,
                "recoveryContinuationActionIds": sorted(continuation_action_ids),
                "externalSubmission": "not_executed",
            }
        }
        run_metadata.update({
            key: value
            for key, value in (workflow_metadata or {}).items()
            if key != "plan"
        })
        workflow_run_id = self.ledger.create_workflow_run({
            "status": "running",
            "triggerSource": trigger_source,
            "metadata": run_metadata,
        })
        executed: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        status = "completed"
        error_message = None
        failed_step_key = None
        plan_actions = {
            action["id"]: action
            for action in plan.get("actions") or []
            if action.get("id")
        }
        ordered_action_ids = [
            action_id
            for action_id in AUTONOMY_EXECUTION_ORDER
            if action_id in plan_actions
            and (selected_action_ids is None or action_id in selected_action_ids)
        ]
        record_skipped_steps = _bool_config(
            self.config,
            "fab_autonomy_record_skipped_steps",
            "operations_autonomy_record_skipped_steps",
            default=True,
        )
        step_metadata: Dict[str, Dict[str, Any]] = {}
        step_ids: Dict[str, int] = {}
        for step_order, action_id in enumerate(ordered_action_ids, start=1):
            action = plan_actions[action_id]
            metadata = {
                "label": action.get("label"),
                "risk": action.get("risk"),
                "mode": action.get("mode"),
                "canRun": action.get("canRun"),
                "blockedReason": action.get("blockedReason"),
                "evidence": _bounded_step_value(action.get("evidence") or {}),
                "recoveryRole": (
                    "retry"
                    if action_id == retry_action_id
                    else "continuation"
                    if action_id in continuation_action_ids
                    else None
                ),
                "externalSubmission": "not_executed",
            }
            step_metadata[action_id] = metadata
            if record_skipped_steps or action.get("canRun"):
                step_ids[action_id] = self.ledger.create_workflow_step({
                    "workflowRunId": workflow_run_id,
                    "stepKey": action_id,
                    "stage": action.get("stage"),
                    "status": "pending",
                    "attempt": max(1, int((step_attempts or {}).get(action_id, 1))),
                    "stepOrder": step_order,
                    "metadata": metadata,
                })

        def ensure_step(action_id: str) -> int:
            existing_step_id = step_ids.get(action_id)
            if existing_step_id is not None:
                return existing_step_id
            action = plan_actions[action_id]
            step_id = self.ledger.create_workflow_step({
                "workflowRunId": workflow_run_id,
                "stepKey": action_id,
                "stage": action.get("stage"),
                "status": "pending",
                "attempt": max(1, int((step_attempts or {}).get(action_id, 1))),
                "stepOrder": ordered_action_ids.index(action_id) + 1,
                "metadata": step_metadata[action_id],
            })
            step_ids[action_id] = step_id
            return step_id

        def execute_step(action_id: str, should_run: bool, callback) -> Dict[str, Any]:
            nonlocal failed_step_key
            if selected_action_ids is not None and action_id not in selected_action_ids:
                return {
                    "id": action_id,
                    "status": "not_selected",
                    "reason": "not_selected_for_recovery",
                }
            emergency_stop = self.ledger.get_runtime_control(AUTONOMY_EMERGENCY_STOP)
            if emergency_stop.get("active"):
                step_id = ensure_step(action_id)
                result = {
                    "id": action_id,
                    "status": "stopped",
                    "reason": "operator_emergency_stop",
                    "externalSubmission": "not_executed",
                }
                self.ledger.update_workflow_step(step_id, {
                    "status": "not_run",
                    "finishedAt": _utc_timestamp(),
                    "durationMs": 0,
                    "metadata": {
                        **step_metadata[action_id],
                        "result": result,
                        "emergencyStop": emergency_stop,
                    },
                })
                skipped.append(result)
                raise AutonomyEmergencyStop("Operator emergency stop engaged")
            if not should_run:
                result = self._skip(plan, action_id)
                if record_skipped_steps:
                    self.ledger.update_workflow_step(ensure_step(action_id), {
                        "status": "skipped",
                        "finishedAt": _utc_timestamp(),
                        "durationMs": 0,
                        "metadata": {
                            **step_metadata[action_id],
                            "result": _compact_step_result(result),
                        },
                    })
                skipped.append(result)
                return result

            step_id = ensure_step(action_id)
            started_at = _utc_timestamp()
            started = time.perf_counter()
            self.ledger.update_workflow_step(step_id, {
                "status": "running",
                "startedAt": started_at,
            })
            try:
                result = callback()
            except Exception as exc:
                failed_step_key = action_id
                safe_error = _safe_error_message(exc, self.config)
                self.ledger.update_workflow_step(step_id, {
                    "status": "failed",
                    "finishedAt": _utc_timestamp(),
                    "durationMs": int((time.perf_counter() - started) * 1000),
                    "errorMessage": safe_error,
                    "metadata": {
                        **step_metadata[action_id],
                        "exceptionType": type(exc).__name__,
                    },
                })
                raise

            self.ledger.update_workflow_step(step_id, {
                "status": _workflow_step_status(result),
                "finishedAt": _utc_timestamp(),
                "durationMs": int((time.perf_counter() - started) * 1000),
                "metadata": {
                    **step_metadata[action_id],
                    "result": _compact_step_result(result),
                },
            })
            if str(result.get("status") or "").strip().lower() == "skipped":
                skipped.append(result)
            else:
                executed.append(result)
            return result

        self.ledger.record_audit_event({
            "action": "local_autonomy.cycle_started",
            "entityType": "workflow_run",
            "entityId": str(workflow_run_id),
            "details": {
                "runnableActionIds": plan["runnableActionIds"],
                "externalSubmission": "not_executed",
            },
        })

        try:
            connector_action = plan_actions["sync_connector_sources"]
            execute_step(
                "sync_connector_sources",
                self._can_run(plan, "sync_connector_sources"),
                lambda: self._run_connector_sync(
                    list(connector_action["evidence"].get("syncableSources") or [])
                ),
            )

            execute_step(
                "rescan_intake",
                self._can_run(plan, "rescan_intake"),
                self._run_rescan,
            )

            processing_should_run = (
                self._can_run(plan, "process_imported")
                or _registered_documents(executed)
                or (recovery_mode and retry_action_id == "process_imported")
            )
            if "process_imported" in continuation_action_ids:
                processing_should_run = _registered_documents(executed)
            execute_step(
                "process_imported",
                processing_should_run,
                lambda: (
                    self._run_processing_recovery(limit)
                    if recovery_mode and retry_action_id == "process_imported"
                    else self._run_processing(limit)
                ),
            )

            execute_step(
                "refresh_bank_records",
                self._can_run(plan, "refresh_bank_records"),
                lambda: self._run_bank_record_refresh(limit),
            )

            execute_step(
                "run_reconciliation",
                self._can_run(plan, "run_reconciliation"),
                lambda: self._run_reconciliation(bank_transactions, limit),
            )

            sync_action = plan_actions["refresh_wave_entity_mirror"]
            execute_step(
                "refresh_wave_entity_mirror",
                self._can_run(plan, "refresh_wave_entity_mirror"),
                lambda: self._run_wave_entity_sync(sync_action["evidence"]["dueTargets"]),
            )

            routing_should_run = (
                self._can_run(plan, "prepare_wave_drafts")
                or _processed_documents(executed)
                or _refreshed_bank_records(executed)
            )
            if "prepare_wave_drafts" in continuation_action_ids:
                routing_should_run = _processed_documents(executed) or _refreshed_bank_records(executed)
            execute_step(
                "prepare_wave_drafts",
                routing_should_run,
                lambda: self._run_routing(limit),
            )

            export_attempt_should_run = self._can_run(plan, "prepare_export_attempts") or self._has_prepared_routes()
            if "prepare_export_attempts" in continuation_action_ids:
                export_attempt_should_run = _prepared_routing_drafts(executed)
            execute_step(
                "prepare_export_attempts",
                export_attempt_should_run,
                lambda: self._run_export_attempt_preparation(limit),
            )

            execute_step(
                "regenerate_stale_export_attempts",
                self._can_run(plan, "regenerate_stale_export_attempts"),
                lambda: self._run_stale_export_regeneration(limit),
            )

            master_ledger_should_run = (
                self._can_run(plan, "prepare_master_ledger_projection")
                or _records_or_exports_changed(executed)
            )
            if "prepare_master_ledger_projection" in continuation_action_ids:
                master_ledger_should_run = _records_or_exports_changed(executed)
            execute_step(
                "prepare_master_ledger_projection",
                master_ledger_should_run,
                lambda: self._run_master_ledger_projection(limit),
            )

            execute_step(
                "plan_wave_daily_reconciliation",
                self._can_run(plan, "plan_wave_daily_reconciliation"),
                self._run_wave_plan,
            )

            close_pack_should_run = self._can_run(plan, "prepare_period_close_pack")
            if "prepare_period_close_pack" in continuation_action_ids:
                close_pack_should_run = close_pack_should_run and _completed_action(
                    executed,
                    "prepare_master_ledger_projection",
                )
            execute_step(
                "prepare_period_close_pack",
                close_pack_should_run,
                self._run_period_close_pack,
            )
        except AutonomyEmergencyStop:
            status = "stopped"
            if not record_skipped_steps:
                for action_id in ordered_action_ids:
                    ensure_step(action_id)
            for action_id, step_id in step_ids.items():
                step = self.ledger.get_workflow_step(step_id)
                if not step or step.get("status") != "pending":
                    continue
                self.ledger.update_workflow_step(step_id, {
                    "status": "not_run",
                    "finishedAt": _utc_timestamp(),
                    "durationMs": 0,
                    "metadata": {
                        **step_metadata[action_id],
                        "reason": "operator_emergency_stop",
                    },
                })
        except Exception as exc:
            status = "failed"
            error_message = _safe_error_message(exc, self.config)
            if not record_skipped_steps:
                for action_id in ordered_action_ids:
                    ensure_step(action_id)
            for action_id, step_id in step_ids.items():
                step = self.ledger.get_workflow_step(step_id)
                if not step or step.get("status") != "pending":
                    continue
                self.ledger.update_workflow_step(step_id, {
                    "status": "not_run",
                    "finishedAt": _utc_timestamp(),
                    "durationMs": 0,
                    "metadata": {
                        **step_metadata[action_id],
                        "reason": "cycle_aborted_after_step_failure",
                        "failedAfterStep": failed_step_key,
                    },
                })
            self.ledger.record_audit_event({
                "action": "local_autonomy.cycle_failed",
                "entityType": "workflow_run",
                "entityId": str(workflow_run_id),
                "details": {
                    "error": error_message,
                    "externalSubmission": "not_executed",
                },
            })

        final_metrics = self.ledger.dashboard_metrics()
        final_master_ledger = LocalMasterLedgerService(self.ledger, self.config).project(limit=limit)
        final_exceptions = LocalExceptionQueueService(self.ledger, self.config).list_exceptions(
            limit=limit,
            include_entities=False,
        )
        self.ledger.update_workflow_run(workflow_run_id, {
            "status": status,
            "documentsImported": _sum_nested(executed, "summary", "registered"),
            "documentsProcessed": _sum_nested(executed, "summary", "processed"),
            "documentsNeedingReview": final_metrics["pending_review"],
            "errorMessage": error_message,
            "finishedAt": _utc_timestamp(),
            "metadata": {
                **{
                    key: value
                    for key, value in run_metadata.items()
                    if key != "plan"
                },
                "plan": {
                    "status": plan["status"],
                    "runnableActionIds": plan["runnableActionIds"],
                    "selectedActionIds": sorted(selected_action_ids) if selected_action_ids is not None else None,
                    "recoveryActionId": retry_action_id,
                    "recoveryContinuationActionIds": sorted(continuation_action_ids),
                    "externalSubmission": "not_executed",
                },
                "steps": {
                    "executed": [action["id"] for action in executed],
                    "skipped": [action["id"] for action in skipped],
                    "failed": failed_step_key,
                },
            },
        })
        self.ledger.record_audit_event({
            "action": (
                "local_autonomy.cycle_completed"
                if status == "completed"
                else "local_autonomy.cycle_stopped"
                if status == "stopped"
                else "local_autonomy.cycle_finished_with_error"
            ),
            "entityType": "workflow_run",
            "entityId": str(workflow_run_id),
            "details": {
                "status": status,
                "executedActionIds": [action["id"] for action in executed],
                "skippedActionIds": [action["id"] for action in skipped],
                "externalSubmission": "not_executed",
                "metrics": final_metrics,
                "masterLedger": _compact_master_ledger(final_master_ledger),
                "exceptions": _compact_exceptions(final_exceptions),
            },
        })
        return {
            "success": status == "completed",
            "status": status,
            "workflowRunId": workflow_run_id,
            "triggerSource": trigger_source,
            "externalSubmission": "not_executed",
            "plan": plan,
            "executedActions": executed,
            "skippedActions": skipped,
            "finalMetrics": final_metrics,
            "masterLedger": _compact_master_ledger(final_master_ledger),
            "exceptions": _compact_exceptions(final_exceptions),
            "error": error_message,
        }

    def _readiness_summary(self) -> Dict[str, Any]:
        if self.readiness:
            return self.readiness.summarize()
        return {
            "status": "attention",
            "security": {"remoteExposureSafe": True, "apiTokenConfigured": False},
            "sources": [],
            "issues": [],
        }

    def _counts(
        self,
        limit: int,
        *,
        metrics: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if metrics is None:
            metrics = self.ledger.dashboard_metrics()
        trusted_category_suggestions = trusted_category_suggestion_candidates(
            self.ledger,
            self.config,
            limit=limit,
        )
        duplicate_candidate_reassessments = duplicate_candidate_reassessment_plan(
            self.ledger,
            self.config,
            limit=500,
        )
        routable_documents = self.ledger.list_documents(status=ROUTABLE_DOCUMENT_STATUSES, limit=limit)
        routable_records = [
            record for record in self.ledger.list_bookkeeping_records(
                status=ROUTABLE_BOOKKEEPING_RECORD_STATUSES,
                export_status=ROUTABLE_BOOKKEEPING_EXPORT_STATUSES,
                limit=limit,
            )
            if record.get("source_type") == "bank_transaction"
        ]
        ready_routing_attempts = self.ledger.list_routing_attempts_without_export(
            status=PREPARED_ROUTING_STATUSES,
            limit=limit,
        )
        pending_routing_attempts = self.ledger.list_routing_attempts(status=PENDING_ROUTE_STATUSES, limit=500)
        pending_export_attempts = self.ledger.list_export_attempts(status=("approval_required", "prepared"), limit=500)
        approved_export_attempts = self.ledger.list_export_attempts(status="approved", limit=500)
        return {
            "importedDocuments": len(self.ledger.list_documents(status=IMPORTED_DOCUMENT_STATUSES, limit=limit)),
            "duplicateLinkCycles": len(duplicate_link_cycles(self.ledger)),
            "duplicateCandidateReassessments": len(
                duplicate_candidate_reassessments
            ),
            "trustedCategorySuggestions": len(trusted_category_suggestions),
            "routableDocuments": len(routable_documents),
            "routableBookkeepingRecords": len(routable_records),
            "routableTargets": _merge_breakdowns(
                _target_breakdown(routable_documents, _document_target_system),
                _target_breakdown(routable_records, _record_target_system),
            ),
            "readyForExportAttempts": len(ready_routing_attempts),
            "readyForExportTargets": _target_breakdown(ready_routing_attempts, _routing_target_system),
            "reconciliableDocuments": metrics["unreconciled_documents"],
            "bankTransactions": metrics.get("bank_transactions", 0),
            "unreconciledBankTransactions": metrics.get("unreconciled_bank_transactions", 0),
            "openReviewItems": len(self.ledger.list_review_items(status=OPEN_REVIEW_STATUSES, limit=500)),
            "pendingRoutingDrafts": len(pending_routing_attempts),
            "pendingRoutingTargets": _target_breakdown(pending_routing_attempts, _routing_target_system),
            "pendingExportApprovals": len(pending_export_attempts),
            "pendingExportApprovalTargets": _target_breakdown(pending_export_attempts, _export_target_system),
            "approvedExportAttempts": len(approved_export_attempts),
            "approvedExportTargets": _target_breakdown(approved_export_attempts, _export_target_system),
            "reconciliationReviewItems": len(
                self.ledger.list_reconciliation_matches(status=RECONCILIATION_REVIEW_STATUSES, limit=500)
            ),
        }

    def _blocked_reasons(self, readiness: Dict[str, Any], health: Dict[str, Any]) -> List[str]:
        reasons: List[str] = []
        security = readiness.get("security") or {}
        if readiness.get("status") == "blocked":
            reasons.append("readiness_blocked")
        if security.get("remoteExposureSafe") is False:
            reasons.append("remote_exposure_without_token")
        if health.get("status") == "blocked" and not _truthy_config(
            self.config,
            "fab_autonomy_ignore_health_blocks",
            "operations_autonomy_ignore_health_blocks",
        ):
            reasons.append("operations_health_blocked")
        return reasons

    def _master_ledger_projection_due(self, projection: Dict[str, Any]) -> bool:
        checksum = str(projection.get("ledgerChecksum") or "").strip()
        if not checksum:
            return False
        latest = self.ledger.find_latest_audit_event(
            "local_master_ledger.projection_prepared",
            "master_ledger",
        )
        details = latest.get("details") if isinstance((latest or {}).get("details"), dict) else {}
        return str(details.get("ledgerChecksum") or "").strip() != checksum

    def _wave_daily_plan_due(self, include_wave_plan: bool) -> bool:
        if not include_wave_plan:
            return False
        today = date.today().isoformat()
        latest = self.ledger.find_latest_audit_event(
            "local_autonomy.wave_daily_plan_prepared",
            "wave_workflow",
        )
        if not latest:
            return True
        details = latest.get("details") if isinstance(latest.get("details"), dict) else {}
        recorded_date = str(details.get("planDate") or "").strip()
        if not recorded_date:
            recorded_date = str(latest.get("created_at") or "")[:10]
        return recorded_date != today

    def _connector_sync_plan(self, include_connector_sync: bool) -> Dict[str, Any]:
        plan = LocalConnectorIntakeService(self.ledger, self.config).plan()
        return {
            "requested": bool(include_connector_sync),
            "enabledSources": list(plan.get("enabledSources") or []),
            "syncableSources": (
                list(plan.get("syncableSources") or [])
                if include_connector_sync
                else []
            ),
            "sourceStates": [
                {
                    "source": source.get("source"),
                    "status": source.get("status"),
                    "enabled": bool(source.get("enabled")),
                    "canSync": bool(source.get("canSync")),
                    "targetSystem": source.get("targetSystem"),
                }
                for source in plan.get("sources") or []
            ],
        }

    def _wave_entity_sync_plan(self, include_wave_sync: bool) -> Dict[str, Any]:
        enabled = include_wave_sync and _bool_config(
            self.config,
            "fab_autonomy_sync_wave_entities",
            "operations_autonomy_sync_wave_entities",
            default=True,
        )
        stale_hours = _positive_float_config(
            self.config,
            "fab_local_wave_entity_sync_stale_hours",
            "operations_wave_entity_sync_stale_hours",
            "wave_entity_sync_stale_hours",
            default=24.0,
        )
        retry_hours = _positive_float_config(
            self.config,
            "fab_local_wave_entity_sync_retry_hours",
            "operations_wave_entity_sync_retry_hours",
            "wave_entity_sync_retry_hours",
            default=1.0,
        )
        configured_targets = []
        due_targets = []
        target_states = []
        now = datetime.now(timezone.utc)
        for target_system in WAVE_ENTITY_TARGETS:
            target = resolve_wave_target_config(self.config, target_system)
            configured = bool(
                target
                and target.get("access_token") not in (None, "")
                and target.get("business_id") not in (None, "")
            )
            if configured:
                configured_targets.append(target_system)
            runs = self.ledger.list_wave_sync_runs(target_system=target_system, limit=1)
            latest = runs[0] if runs else None
            status = str((latest or {}).get("status") or "never_synced")
            age_hours = _timestamp_age_hours(
                (latest or {}).get("finished_at") or (latest or {}).get("started_at"),
                now,
            )
            if status == "completed":
                due = configured and (age_hours is None or age_hours >= stale_hours)
                reason = "stale" if due else "current"
            elif status == "never_synced":
                due = configured
                reason = "never_synced" if configured else "not_configured"
            else:
                due = configured and (age_hours is None or age_hours >= retry_hours)
                reason = "retry_due" if due else "retry_backoff"
            if enabled and due:
                due_targets.append(target_system)
            target_states.append({
                "targetSystem": target_system,
                "configured": configured,
                "latestStatus": status,
                "ageHours": round(age_hours, 2) if age_hours is not None else None,
                "due": bool(enabled and due),
                "reason": reason,
            })
        return {
            "enabled": enabled,
            "requested": bool(include_wave_sync),
            "configuredTargets": configured_targets,
            "dueTargets": due_targets,
            "entityTypes": list(WAVE_ENTITY_TYPES),
            "staleHours": stale_hours,
            "retryHours": retry_hours,
            "targetStates": target_states,
        }

    def _can_run(self, plan: Dict[str, Any], action_id: str) -> bool:
        return action_id in set(plan.get("runnableActionIds") or [])

    @staticmethod
    def _skip(plan: Dict[str, Any], action_id: str) -> Dict[str, Any]:
        for action in plan.get("actions") or []:
            if action.get("id") == action_id:
                return {"id": action_id, "status": "skipped", "reason": action.get("blockedReason") or "not_actionable"}
        return {"id": action_id, "status": "skipped", "reason": "not_planned"}

    def _run_rescan(self) -> Dict[str, Any]:
        summary = LocalFolderIntake(
            self.ledger,
            allowed_extensions=self.intake_extensions,
        ).rescan(self.intake_paths)
        if summary.get("status") == "already_running":
            return {
                "id": "rescan_intake",
                "status": "skipped",
                "reason": "intake_already_running",
                "summary": summary,
            }
        return {"id": "rescan_intake", "status": "completed", "summary": summary}

    def _run_connector_sync(self, sources: List[str]) -> Dict[str, Any]:
        result = LocalConnectorIntakeService(self.ledger, self.config).sync(
            sources=sources,
            actor="local_autonomy",
            trigger_source=AUTONOMOUS_TRIGGER,
            workflow_metadata={"parentWorkflow": AUTONOMOUS_TRIGGER},
        )
        connector_status = str(result.get("status") or "failed")
        connector_completed = bool(result.get("success")) and connector_status in {
            "completed",
            "no_sources_enabled",
        }
        return {
            "id": "sync_connector_sources",
            # Provider failures are already isolated, audited, and recoverable
            # inside LocalConnectorIntakeService. Preserve that evidence while
            # allowing independent local intake and ledger repair to continue.
            "status": "completed" if connector_completed else "attention_required",
            "summary": {
                **(result.get("summary") or {}),
                "connectorStatus": connector_status,
            },
            "sources": sources,
            "connectorWorkflowRunId": result.get("workflowRunId"),
            "externalSubmission": "not_executed",
        }

    def _run_processing(self, limit: int) -> Dict[str, Any]:
        summary = LocalDocumentProcessor(self.ledger, self.config).process_imported(limit=limit)
        return {"id": "process_imported", "status": "completed", "summary": summary}

    def _run_processing_recovery(self, limit: int) -> Dict[str, Any]:
        summary = LocalDocumentProcessor(self.ledger, self.config).retry_failed(
            limit=limit,
            actor="local_workflow_recovery",
        )
        return {
            "id": "process_imported",
            "status": "completed" if int(summary.get("failed") or 0) == 0 else "attention_required",
            "summary": summary,
        }

    def _run_routing(self, limit: int) -> Dict[str, Any]:
        service = LocalRoutingService(self.ledger, self.config)
        document_summary = service.prepare_ready_documents(limit=limit)
        record_summary = service.prepare_ready_bookkeeping_records(limit=limit)
        prepared_routes = self.ledger.list_routing_attempts(status=PREPARED_ROUTING_STATUSES, limit=limit)
        summary = {
            "documents": document_summary,
            "bookkeepingRecords": record_summary,
            "requested": document_summary.get("requested", 0) + record_summary.get("requested", 0),
            "draftPrepared": document_summary.get("draftPrepared", 0) + record_summary.get("draftPrepared", 0),
            "alreadyPrepared": document_summary.get("alreadyPrepared", 0) + record_summary.get("alreadyPrepared", 0),
            "needsReview": document_summary.get("needsReview", 0) + record_summary.get("needsReview", 0),
            "blocked": document_summary.get("blocked", 0) + record_summary.get("blocked", 0),
            "targetBreakdown": _target_breakdown(prepared_routes, _routing_target_system),
        }
        return {"id": "prepare_wave_drafts", "status": "completed", "summary": summary}

    def _run_export_attempt_preparation(self, limit: int) -> Dict[str, Any]:
        summary = LocalExportAttemptService(self.ledger, self.config).prepare_ready_exports(limit=limit)
        prepared_attempts = [
            result.get("exportAttempt")
            for result in summary.get("exportAttempts", [])
            if isinstance(result, dict) and isinstance(result.get("exportAttempt"), dict)
        ]
        summary["targetBreakdown"] = _target_breakdown(prepared_attempts, _export_target_system)
        return {"id": "prepare_export_attempts", "status": "completed", "summary": summary}

    def _run_stale_export_regeneration(self, limit: int) -> Dict[str, Any]:
        service = LocalExportAttemptService(self.ledger, self.config)
        projection = LocalMasterLedgerService(self.ledger, self.config).project(limit=limit)
        rows = _regenerable_stale_master_ledger_rows(projection)[:limit]
        results = []
        for row in rows:
            result = service.regenerate_attempt(int(row["exportAttemptId"]), actor="local_autonomy")
            results.append({
                "exportAttemptId": row.get("exportAttemptId"),
                "targetSystem": row.get("targetSystem"),
                "success": result.get("success"),
                "status": result.get("status"),
                "masterLedgerChecksum": result.get("masterLedgerChecksum"),
            })
        return {
            "id": "regenerate_stale_export_attempts",
            "status": "completed",
            "summary": {
                "requested": len(rows),
                "regenerated": sum(1 for result in results if result.get("success")),
                "failed": sum(1 for result in results if not result.get("success")),
                "targetBreakdown": _target_breakdown(rows, _master_row_target_system),
                "attemptSummaries": results,
                "externalSubmission": "not_executed",
            },
        }

    def _run_master_ledger_projection(self, limit: int) -> Dict[str, Any]:
        service = LocalMasterLedgerService(self.ledger, self.config)
        projection = service.project(limit=AUTONOMY_MASTER_LEDGER_LIMIT)
        service.record_projection_audit(projection, actor="local_autonomy")
        return {
            "id": "prepare_master_ledger_projection",
            "status": "completed",
            "summary": _compact_master_ledger(projection),
        }

    def _run_bank_record_refresh(self, limit: int) -> Dict[str, Any]:
        summary = LocalBookkeepingRecordService(self.ledger, self.config).refresh_bank_transactions(limit=limit)
        return {"id": "refresh_bank_records", "status": "completed", "summary": summary}

    def _has_prepared_routes(self) -> bool:
        return bool(
            self.ledger.list_routing_attempts_without_export(
                status=PREPARED_ROUTING_STATUSES,
                limit=1,
            )
        )

    def _run_reconciliation(self, bank_transactions: List[Dict[str, Any]], limit: int) -> Dict[str, Any]:
        summary = LocalReconciliationService(self.ledger, self.config).run(
            bank_transactions,
            limit=limit,
            actor="local_autonomy",
        )
        return {"id": "run_reconciliation", "status": "completed", "summary": summary}

    def _reconciliation_transactions(
        self,
        bank_transactions: Optional[List[Dict[str, Any]]],
        limit: int,
    ) -> List[Dict[str, Any]]:
        if isinstance(bank_transactions, list):
            return bank_transactions
        return LocalBankTransactionImportService(self.ledger, self.config).transactions_for_reconciliation(limit=limit)

    def _run_wave_plan(self) -> Dict[str, Any]:
        today = date.today().isoformat()
        wave_control = LocalWaveControlService(self.config)
        plan = wave_control.plan_workflow({
            "workflowId": "daily_reconciliation_run",
            "fromDate": today,
            "toDate": today,
        })
        snapshot_summary = wave_control.record_workflow_report_snapshots(self.ledger, plan)
        operation_snapshot_summary = wave_control.record_workflow_operation_snapshots(self.ledger, plan)
        report_controls = wave_control.evaluate_report_controls(
            self.ledger,
            workflow_id=(plan.get("workflow_plan") or {}).get("workflow_id") or "daily_reconciliation_run",
        )
        self.ledger.record_audit_event({
            "action": "local_autonomy.wave_daily_plan_prepared",
            "entityType": "wave_workflow",
            "entityId": (plan.get("workflow_plan") or {}).get("workflow_id"),
            "details": {
                "status": plan.get("status"),
                "planDate": today,
                "operationCount": plan.get("operationCount"),
                "waveReportSnapshots": snapshot_summary,
                "waveOperationSnapshots": operation_snapshot_summary,
                "waveReportControls": {
                    "status": report_controls.get("status"),
                    "requiredReportCount": report_controls.get("requiredReportCount"),
                    "coveredReportCount": report_controls.get("coveredReportCount"),
                    "resultGapCount": report_controls.get("resultGapCount"),
                    "blockingCount": report_controls.get("blockingCount"),
                },
                "externalSubmission": "not_executed",
            },
        })
        summary = _compact_wave_plan(plan)
        summary["waveReportSnapshots"] = snapshot_summary["snapshotCount"]
        summary["waveOperationSnapshots"] = operation_snapshot_summary["snapshotCount"]
        summary["waveReportControls"] = {
            "status": report_controls.get("status"),
            "requiredReportCount": report_controls.get("requiredReportCount"),
            "coveredReportCount": report_controls.get("coveredReportCount"),
            "resultGapCount": report_controls.get("resultGapCount"),
            "blockingCount": report_controls.get("blockingCount"),
        }
        return {"id": "plan_wave_daily_reconciliation", "status": "completed", "summary": summary}

    def _run_wave_entity_sync(self, target_systems: List[str]) -> Dict[str, Any]:
        service = WaveappsEntitySyncService(self.config)
        results = []
        for target_system in target_systems:
            result = service.sync(
                self.ledger,
                target_system,
                entity_types=list(WAVE_ENTITY_TYPES),
            )
            summary = {
                "targetSystem": target_system,
                "syncRunId": result.get("syncRunId"),
                "success": bool(result.get("success")),
                "status": result.get("status"),
                "pagesFetched": result.get("pagesFetched", 0),
                "entitiesSeen": result.get("entitiesSeen", 0),
                "missingMarked": result.get("missingMarked", 0),
                "message": result.get("message"),
                "externalSubmission": "not_executed",
            }
            results.append(summary)
            self.ledger.record_audit_event({
                "action": "local_autonomy.wave_entity_mirror_refreshed",
                "entityType": "wave_sync_run",
                "entityId": str(result.get("syncRunId") or target_system),
                "details": summary,
            })
        failed = [result for result in results if not result["success"]]
        return {
            "id": "refresh_wave_entity_mirror",
            "status": "completed" if not failed else "attention_required",
            "summary": {
                "requestedTargets": len(target_systems),
                "successfulTargets": len(results) - len(failed),
                "failedTargets": len(failed),
                "pagesFetched": sum(int(result.get("pagesFetched") or 0) for result in results),
                "entitiesSeen": sum(int(result.get("entitiesSeen") or 0) for result in results),
                "missingMarked": sum(int(result.get("missingMarked") or 0) for result in results),
                "targetResults": results,
                "externalSubmission": "not_executed",
            },
        }

    def _run_period_close_pack(self) -> Dict[str, Any]:
        close_pack = LocalClosePackService(self.ledger, self.config).prepare(actor="local_autonomy")
        close_readiness = close_pack.get("closeReadiness") or {}
        summary = {
            "success": close_pack.get("success"),
            "status": close_pack.get("status"),
            "externalSubmission": close_pack.get("externalSubmission"),
            "closePackPath": close_pack.get("closePackPath"),
            "closePackFilename": close_pack.get("closePackFilename"),
            "sha256": close_pack.get("sha256"),
            "sizeBytes": close_pack.get("sizeBytes"),
            "manifest": close_pack.get("manifest"),
            "closeReadiness": _compact_close_readiness(close_readiness),
        }
        self.ledger.record_audit_event({
            "action": "local_autonomy.period_close_pack_prepared",
            "entityType": "period_close",
            "entityId": f"{close_readiness.get('fromDate')}:{close_readiness.get('toDate')}",
            "details": summary,
        })
        return {"id": "prepare_period_close_pack", "status": "completed" if close_pack.get("success") else "blocked", "summary": summary}


def _action(
    action_id: str,
    label: str,
    stage: str,
    risk: str,
    mode: str,
    can_run: bool,
    blocked_reason: Optional[str],
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "id": action_id,
        "label": label,
        "stage": stage,
        "risk": risk,
        "mode": mode,
        "canRun": bool(can_run),
        "blockedReason": blocked_reason,
        "evidence": evidence or {},
    }


def _autonomy_recovery_descendants(action_id: str) -> set[str]:
    descendants: set[str] = set()
    frontier = [str(action_id)]
    while frontier:
        parent = frontier.pop(0)
        for child in AUTONOMY_RECOVERY_DEPENDENCIES.get(parent, ()):
            if child in descendants:
                continue
            descendants.add(child)
            frontier.append(child)
    return descendants


def _workflow_step_status(result: Dict[str, Any]) -> str:
    result_status = str(result.get("status") or "completed").strip().lower()
    if result_status in {"failed", "error", "partial", "completed_with_errors"}:
        return "failed"
    if result_status in {"blocked", "attention", "attention_required", "supervision_required"}:
        return "blocked"
    if result_status == "skipped":
        return "skipped"
    return "completed"


def _compact_step_result(result: Dict[str, Any]) -> Dict[str, Any]:
    compact = {
        "status": result.get("status"),
        "reason": result.get("reason"),
    }
    if "summary" in result:
        compact["summary"] = _bounded_step_value(result.get("summary"))
    return {key: value for key, value in compact.items() if value is not None}


def _bounded_step_value(value: Any, depth: int = 0) -> Any:
    if depth >= 3:
        if isinstance(value, (dict, list, tuple)):
            return {"count": len(value)}
        return str(value)[:300]
    if isinstance(value, dict):
        return {
            str(key)[:100]: _bounded_step_value(item, depth + 1)
            for key, item in list(value.items())[:25]
        }
    if isinstance(value, (list, tuple)):
        return {"count": len(value)}
    if isinstance(value, str):
        return value[:500]
    return value


def _safe_error_message(error: Any, config: Dict[str, Any]) -> str:
    message = str(error) or type(error).__name__
    secret_markers = ("token", "password", "secret", "credential", "authorization", "api_key", "apikey")
    for key, value in config.items():
        if value in (None, "") or not any(marker in str(key).lower() for marker in secret_markers):
            continue
        message = message.replace(str(value), "<redacted>")
    message = re.sub(
        r"(?i)((?:access[_-]?token|refresh[_-]?token|token|password|secret|(?:x[_-]?)?api[_-]?key)\s*[:=]\s*)[^&,;\s]+",
        r"\1<redacted>",
        message,
    )
    message = re.sub(
        r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^,;\s]+",
        r"\1<redacted>",
        message,
    )
    message = re.sub(
        r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+",
        r"\1<redacted>",
        message,
    )
    return message[:2000]


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _compact_readiness(readiness: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": readiness.get("status"),
        "remoteExposureSafe": (readiness.get("security") or {}).get("remoteExposureSafe"),
        "apiTokenConfigured": (readiness.get("security") or {}).get("apiTokenConfigured"),
        "readySources": len([source for source in readiness.get("sources") or [] if source.get("status") == "ready"]),
        "issueCount": len(readiness.get("issues") or []),
    }


def _compact_health(health: Dict[str, Any]) -> Dict[str, Any]:
    metrics = health.get("metrics") or {}
    return {
        "status": health.get("status"),
        "openReviewItems": metrics.get("openReviewItems", 0),
        "failedDocuments": metrics.get("failedDocuments", 0),
        "routingBlocks": metrics.get("routingBlocks", 0),
        "pendingRoutingDrafts": metrics.get("pendingRoutingDrafts", 0),
        "pendingExportApprovals": metrics.get("pendingExportApprovals", 0),
        "approvedExports": metrics.get("approvedExports", 0),
        "attentionExports": metrics.get("attention_export_attempts", 0),
        "deferredExports": metrics.get("deferredExports", 0),
        "deferredExportsDue": metrics.get("deferredExportsDue", 0),
        "failedExports": metrics.get("failedExports", 0),
        "masterLedgerRows": metrics.get("masterLedgerRows", 0),
        "masterLedgerBlockedRows": metrics.get("masterLedgerBlockedRows", 0),
        "masterLedgerReadyForApproval": metrics.get("masterLedgerReadyForApproval", 0),
        "masterLedgerReadyForExternalExecution": metrics.get("masterLedgerReadyForExternalExecution", 0),
        "issueCount": len(health.get("issues") or []),
    }


def _compact_exceptions(exceptions: Dict[str, Any]) -> Dict[str, Any]:
    summary = exceptions.get("summary") or {}
    items = exceptions.get("exceptions") or []
    return {
        "status": exceptions.get("status"),
        "externalSubmission": exceptions.get("externalSubmission") or "not_executed",
        "total": summary.get("total", 0),
        "bySeverity": summary.get("bySeverity") or {},
        "byType": summary.get("byType") or {},
        "topExceptions": [
            {
                "id": item.get("id"),
                "type": item.get("type"),
                "severity": item.get("severity"),
                "entityType": item.get("entityType"),
                "entityId": item.get("entityId"),
                "message": item.get("message"),
                "nextAction": item.get("nextAction"),
                "actions": _compact_exception_actions(item.get("actions") or []),
            }
            for item in items[:5]
        ],
    }


def _compact_exception_actions(actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "id": action.get("id"),
            "label": _exception_action_label(str(action.get("id") or "open")),
            "path": action.get("dashboardPath") or action.get("path"),
            "safety": action.get("safety"),
        }
        for action in actions
        if action.get("method") == "GET" and action.get("safety") == "read_only" and (action.get("dashboardPath") or action.get("path"))
    ][:3]


def _exception_action_label(action_id: str) -> str:
    labels = {
        "open_bookkeeping_record": "Open record",
        "open_document": "Open document",
        "open_export_attempts": "Open export",
        "open_master_ledger": "Open master ledger",
        "open_reconciliation": "Open reconciliation",
        "open_review_queue": "Open review queue",
        "open_routing_attempts": "Open routing",
        "open_autonomy_plan": "Open autonomy plan",
    }
    return labels.get(action_id, action_id.replace("_", " ").title())


def _compact_close_readiness(close_readiness: Dict[str, Any]) -> Dict[str, Any]:
    metrics = close_readiness.get("metrics") or {}
    report_controls = close_readiness.get("reportControls") or {}
    return {
        "status": close_readiness.get("status"),
        "canClose": bool(close_readiness.get("canClose")),
        "workflowId": close_readiness.get("workflowId"),
        "fromDate": close_readiness.get("fromDate"),
        "toDate": close_readiness.get("toDate"),
        "blockingCount": close_readiness.get("blockingCount", 0),
        "attentionCount": close_readiness.get("attentionCount", 0),
        "reportControls": {
            "status": report_controls.get("status"),
            "requiredReportCount": report_controls.get("requiredReportCount"),
            "readyReportCount": report_controls.get("readyReportCount"),
            "resultGapCount": report_controls.get("resultGapCount"),
        },
        "metrics": {
            "pendingReview": metrics.get("pendingReview", 0),
            "unreconciledDocuments": metrics.get("unreconciledDocuments", 0),
            "unreconciledBankTransactions": metrics.get("unreconciledBankTransactions", 0),
            "exportApprovals": metrics.get("exportApprovals", 0),
            "failedDocuments": metrics.get("failedDocuments", 0),
            "routingBlocks": metrics.get("routingBlocks", 0),
        },
        "nextActions": close_readiness.get("nextActions") or [],
    }


def _compact_wave_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    workflow_plan = plan.get("workflow_plan") or {}
    return {
        "status": plan.get("status"),
        "workflowId": workflow_plan.get("workflow_id"),
        "operationCount": plan.get("operationCount"),
        "externalSubmission": plan.get("externalSubmission"),
        "canRunAutonomously": plan.get("can_run_autonomously"),
    }


def _compact_master_ledger(projection: Dict[str, Any]) -> Dict[str, Any]:
    summary = projection.get("summary") or {}
    return {
        "projectionVersion": projection.get("projectionVersion"),
        "ledgerChecksum": projection.get("ledgerChecksum"),
        "externalSubmission": projection.get("externalSubmission"),
        "targetSystem": projection.get("targetSystem"),
        "totalRows": summary.get("totalRows", 0),
        "blockedRows": summary.get("blockedRows", 0),
        "readyForDraft": summary.get("readyForDraft", 0),
        "readyForApproval": summary.get("readyForApproval", 0),
        "readyForExternalExecution": summary.get("readyForExternalExecution", 0),
        "downstreamStatuses": summary.get("downstreamStatuses") or {},
        "byTargetSystem": summary.get("byTargetSystem") or {},
    }


def _next_action(status: str, runnable_actions: List[Dict[str, Any]], manual_actions: List[Dict[str, Any]], blocked_reasons: List[str]) -> str:
    if status == "blocked":
        return "Resolve blocked safety conditions before running the autonomous local cycle: " + ", ".join(blocked_reasons)
    if runnable_actions:
        return "Run the safe local autonomous cycle. It will not submit data externally."
    if manual_actions:
        return "Work through the review/approval queue before FAB can continue autonomously."
    return "No local autonomous work is waiting."


def _close_pack_blocked_reason(close_readiness: Dict[str, Any]) -> str:
    next_actions = close_readiness.get("nextActions") or []
    if next_actions:
        return " ".join(str(action) for action in next_actions)
    return "Close readiness gates are not ready."


def _bounded_limit(value: Any, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def _truthy_config(config: Dict[str, Any], *keys: str) -> bool:
    for key in keys:
        value = config.get(key)
        if value in (None, ""):
            continue
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    return False


def _bool_config(config: Dict[str, Any], *keys: str, default: bool) -> bool:
    for key in keys:
        value = config.get(key)
        if value in (None, ""):
            continue
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    return default


def _positive_float_config(config: Dict[str, Any], *keys: str, default: float) -> float:
    for key in keys:
        value = config.get(key)
        if value in (None, ""):
            continue
        try:
            return max(float(value), 0.0)
        except (TypeError, ValueError):
            continue
    return default


def _timestamp_age_hours(value: Any, now: datetime) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max((now - parsed.astimezone(timezone.utc)).total_seconds() / 3600.0, 0.0)


def _connector_sync_blocked_reason(sync_plan: Dict[str, Any]) -> Optional[str]:
    if not sync_plan.get("requested"):
        return "Connector source collection was disabled for this request."
    if not sync_plan.get("enabledSources"):
        return "No connector document sources are enabled."
    if not sync_plan.get("syncableSources"):
        return "Enabled connector sources require credentials, consent, or an approved source scope."
    return None


def _wave_entity_sync_blocked_reason(sync_plan: Dict[str, Any]) -> Optional[str]:
    if not sync_plan.get("requested"):
        return "Wave entity mirror refresh was disabled for this request."
    if not sync_plan.get("enabled"):
        return "Autonomous Wave entity mirror refresh is disabled in configuration."
    if not sync_plan.get("configuredTargets"):
        return "No Wave target has both an access token and business id configured."
    if not sync_plan.get("dueTargets"):
        return "Every configured Wave entity mirror is current or waiting for its retry window."
    return None


def _registered_documents(executed: List[Dict[str, Any]]) -> bool:
    for action in executed:
        if action.get("id") in {"sync_connector_sources", "rescan_intake"} and (
            action.get("summary") or {}
        ).get("registered", 0) > 0:
            return True
    return False


def _processed_documents(executed: List[Dict[str, Any]]) -> bool:
    for action in executed:
        if action.get("id") != "process_imported":
            continue
        summary = action.get("summary") or {}
        trusted = summary.get("trustedCategoryAutomation") or {}
        if summary.get("processed", 0) > 0 or trusted.get("readyDocuments", 0) > 0:
            return True
    return False


def _refreshed_bank_records(executed: List[Dict[str, Any]]) -> bool:
    for action in executed:
        if action.get("id") == "refresh_bank_records" and (action.get("summary") or {}).get("updated", 0) > 0:
            return True
    return False


def _prepared_routing_drafts(executed: List[Dict[str, Any]]) -> bool:
    for action in executed:
        if action.get("id") != "prepare_wave_drafts":
            continue
        if int((action.get("summary") or {}).get("draftPrepared") or 0) > 0:
            return True
    return False


def _completed_action(executed: List[Dict[str, Any]], action_id: str) -> bool:
    return any(
        action.get("id") == action_id and action.get("status") == "completed"
        for action in executed
    )


def _records_or_exports_changed(executed: List[Dict[str, Any]]) -> bool:
    for action in executed:
        action_id = action.get("id")
        summary = action.get("summary") or {}
        if action_id == "process_imported" and any(
            int(summary.get(key) or 0) > 0
            for key in ("updatedRecords", "processed", "needsReview", "retried")
        ):
            return True
        if action_id == "refresh_bank_records" and summary.get("updated", 0) > 0:
            return True
        if action_id == "prepare_wave_drafts" and summary.get("draftPrepared", 0) > 0:
            return True
        if action_id == "prepare_export_attempts" and summary.get("prepared", 0) > 0:
            return True
        if action_id == "regenerate_stale_export_attempts" and summary.get("regenerated", 0) > 0:
            return True
    return False


def _regenerable_stale_master_ledger_rows(projection: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for row in projection.get("rows") or []:
        if row.get("downstreamStatus") != "stale_master_ledger_draft":
            continue
        if not row.get("exportAttemptId"):
            continue
        if row.get("externalSubmission") in {"queued", "submitted", "executed"}:
            continue
        rows.append(row)
    return rows


def _target_breakdown(items: List[Dict[str, Any]], resolver) -> Dict[str, int]:
    breakdown: Dict[str, int] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        target = _normalize_target_system(resolver(item))
        breakdown[target] = breakdown.get(target, 0) + 1
    return dict(sorted(breakdown.items()))


def _merge_breakdowns(*breakdowns: Dict[str, int]) -> Dict[str, int]:
    merged: Dict[str, int] = {}
    for breakdown in breakdowns:
        for target, count in (breakdown or {}).items():
            merged[target] = merged.get(target, 0) + int(count or 0)
    return dict(sorted(merged.items()))


def _document_target_system(document: Dict[str, Any]) -> str:
    return resolve_document_target_system(document, default="waveapps")


def _record_target_system(record: Dict[str, Any]) -> str:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    return _first_present(
        record.get("target_system"),
        record.get("targetSystem"),
        metadata.get("targetSystem"),
        metadata.get("target_system"),
        "waveapps",
    )


def _routing_target_system(routing_attempt: Dict[str, Any]) -> str:
    metadata = routing_attempt.get("metadata") if isinstance(routing_attempt.get("metadata"), dict) else {}
    return _first_present(
        metadata.get("targetSystem"),
        metadata.get("target_system"),
        _target_system_from_route_target(routing_attempt.get("target")),
        "waveapps",
    )


def _export_target_system(export_attempt: Dict[str, Any]) -> str:
    metadata = export_attempt.get("metadata") if isinstance(export_attempt.get("metadata"), dict) else {}
    return _first_present(
        export_attempt.get("target_system"),
        export_attempt.get("targetSystem"),
        metadata.get("targetSystem"),
        metadata.get("target_system"),
        _target_system_from_route_target(metadata.get("routingTarget")),
        "waveapps",
    )


def _master_row_target_system(row: Dict[str, Any]) -> str:
    return _first_present(row.get("targetSystem"), row.get("target_system"), "unknown")


def _target_system_from_route_target(value: Any) -> str:
    text = str(value or "").strip()
    if ":" in text:
        return text.split(":", 1)[0] or "waveapps"
    return text


def _normalize_target_system(value: Any) -> str:
    text = str(value or "waveapps").strip().lower().replace("_", "-")
    if text in {"", "none"}:
        return "waveapps"
    if text in {"mijngeldzaken-nl", "mijngeldzaken.nl"}:
        return "mijngeldzaken"
    if text.startswith("mijngeldzaken:"):
        return "mijngeldzaken"
    if text.startswith("waveapps"):
        return "waveapps"
    return text


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _sum_nested(actions: List[Dict[str, Any]], parent_key: str, child_key: str) -> int:
    total = 0
    for action in actions:
        value = (action.get(parent_key) or {}).get(child_key)
        try:
            total += int(value or 0)
        except (TypeError, ValueError):
            continue
    return total
