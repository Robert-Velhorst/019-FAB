from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from src.data_entry.waveapps_autonomous_operator import WaveappsAutonomousOperator
from src.data_entry.waveapps_account_discovery import WaveappsAccountDiscoveryService
from src.data_entry.waveapps_surface import (
    WAVE_SURFACE_CATALOG,
    build_wave_report_payload,
    get_wave_report,
    list_wave_actions,
    list_wave_report_sections,
    list_wave_reports,
    summarize_wave_parity,
)
from src.operations.local_bank_transactions import LocalBankTransactionImportService
from src.operations.local_bookkeeping_records import LocalBookkeepingRecordService
from src.operations.local_reconciliation import LocalReconciliationService
from src.workflow.autonomous_playbook import AutonomousBookkeeperPlaybook


DEFAULT_WORKFLOW_SIGNALS = [
    "ledger_period",
    "account_scope",
    "reconciliation_status",
    "source_document",
    "bank_transaction",
    "duplicate_fingerprint",
    "ledger_snapshot",
    "bank_feed",
]

MAX_WAVE_REPORT_RESULT_BYTES = 16 * 1024 * 1024
MAX_WAVE_REPORT_RESULT_ROWS = 10_000
MAX_WAVE_REPORT_RECONCILIATION_ROWS = 500


class LocalWaveControlService:
    """Expose Wave as a policy-gated downstream surface for FAB.

    This service does not submit data to Wave. It publishes the known Wave
    surface inventory and creates idempotent operation plans that an approved
    API/browser executor can later consume.
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        operator: Optional[WaveappsAutonomousOperator] = None,
    ):
        self.config = config or {}
        self.operator = operator or WaveappsAutonomousOperator(self.config)
        self.playbook = AutonomousBookkeeperPlaybook(self.config)

    def overview(self, ledger: Any = None) -> Dict[str, Any]:
        parity = summarize_wave_parity()
        report_sections = list_wave_report_sections()
        reports = list_wave_reports()
        return {
            "status": "modeled",
            "externalSubmission": "not_executed",
            "summary": parity,
            "credentials": _credential_status(self.config),
            "accountMappings": WaveappsAccountDiscoveryService(self.config).mapping_status(),
            "entityMirror": _entity_mirror_summary(ledger),
            "safetyPolicy": {
                "read_only": "FAB may plan and read report/list state without changing Wave.",
                "safe_draft": "FAB may prepare drafts, but still records the operation before execution.",
                "requires_confirmation": "FAB blocks execution until a user explicitly approves the exact action.",
                "requires_credentials": "FAB blocks provider/account authorization to the account owner.",
                "unsupported": "FAB records the surface but does not automate it.",
            },
            "modules": WAVE_SURFACE_CATALOG["modules"],
            "syncContracts": WAVE_SURFACE_CATALOG["sync_contracts"],
            "reportSections": report_sections,
            "reports": reports,
            "workflows": [
                {
                    "id": "daily_reconciliation_run",
                    "label": "Daily Wave ledger reconciliation",
                    "defaultReport": "account-transactions",
                    "defaultExport": "csv",
                    "mode": "read_only",
                },
                {
                    "id": "period_close_pack",
                    "label": "Period close report pack",
                    "defaultReports": [report["type"] for report in reports if report.get("close_pack")],
                    "mode": "read_only",
                },
            ],
            "nextActions": [
                "Use /api/wave/account-mappings to inspect the anchor and category-account mappings required for verified exports.",
                "Use /api/wave/accounts/discover for an audited, read-only Wave chart-of-accounts refresh before enabling export execution.",
                "Use /api/wave/entities/sync to mirror customers, products/services, and invoices before resolving downstream IDs.",
                "Use /api/wave/workflows/plan for general-ledger reconciliation or close-pack planning.",
                "Use /api/wave/plan to prepare a single Wave action with policy gates and idempotency.",
                "Connect an approved Wave API or browser executor only after the operation plan is reviewed.",
            ],
        }

    def actions(
        self,
        surface: Optional[str] = None,
        safety: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        actions = list_wave_actions(surface)
        if safety:
            actions = [action for action in actions if action.get("safety") == safety]
        if mode:
            actions = [action for action in actions if action.get("mode") == mode]
        return {
            "surface": surface,
            "safety": safety,
            "mode": mode,
            "count": len(actions),
            "actions": actions,
        }

    def reports(self, section: Optional[str] = None) -> Dict[str, Any]:
        reports = list_wave_reports(section)
        return {
            "section": section,
            "sections": list_wave_report_sections(),
            "count": len(reports),
            "reports": reports,
        }

    def plan_action(self, request_payload: Dict[str, Any]) -> Dict[str, Any]:
        action_id = _coalesce(request_payload, "actionId", "action_id")
        if not action_id:
            return {
                "success": False,
                "status": "unsupported",
                "error": "actionId is required.",
                "externalSubmission": "not_executed",
            }
        payload = request_payload.get("payload") or {}
        if not isinstance(payload, dict):
            return {
                "success": False,
                "status": "invalid_payload",
                "error": "payload must be an object.",
                "externalSubmission": "not_executed",
            }
        action = WAVE_SURFACE_CATALOG["actions"].get(str(action_id), {})
        operation = self.operator.prepare_operation(
            str(action_id),
            payload,
            surface=_coalesce(request_payload, "surface", "waveSurface") or action.get("surface"),
            actor=str(request_payload.get("actor") or "fab_wave_control"),
            idempotency_key=request_payload.get("idempotencyKey"),
            allow_write=bool(request_payload.get("allowWrite", False)),
            capability_id=_coalesce(request_payload, "capabilityId", "capability_id"),
            available_signals=_list_value(request_payload.get("availableSignals")),
            confidence=request_payload.get("confidence"),
        )
        return _operation_response(operation)

    def plan_report_action(
        self,
        report_type: str,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        as_of_date: Optional[str] = None,
        action_id: str = "report_table_read",
        export_format: Optional[str] = None,
        basis: str = "accrual",
        account_option: str = "-1",
        account_name: str = "All Accounts",
        contact_option: str = "0",
        contact_name: str = "All Contacts",
        cash_mode: str = "1",
    ) -> Dict[str, Any]:
        payload = build_wave_report_payload(
            report_type,
            from_date=from_date,
            to_date=to_date,
            as_of_date=as_of_date,
            basis=basis,
            account_option=account_option,
            contact_option=contact_option,
            export_format=export_format,
        )
        payload.update({
            "accountName": account_name,
            "contactName": contact_name,
            "cashMode": cash_mode,
        })
        return self.plan_action({
            "surface": "reports",
            "actionId": action_id,
            "payload": payload,
            "capabilityId": "ledger_report_reconciliation",
            "availableSignals": DEFAULT_WORKFLOW_SIGNALS,
            "confidence": 0.95,
        })

    def plan_workflow(self, request_payload: Dict[str, Any]) -> Dict[str, Any]:
        workflow_id = str(
            _coalesce(request_payload, "workflowId", "workflow_id")
            or "daily_reconciliation_run"
        )
        from_date = str(_coalesce(request_payload, "fromDate", "from_date") or _today())
        to_date = str(_coalesce(request_payload, "toDate", "to_date") or from_date)
        available_signals = (
            _list_value(request_payload.get("availableSignals"))
            or DEFAULT_WORKFLOW_SIGNALS
        )
        confidence = request_payload.get("confidence")
        if confidence is None:
            confidence = 0.95
        approvals = _list_value(request_payload.get("approvals"))
        result = self.operator.prepare_workflow(
            workflow_id,
            from_date,
            to_date,
            actor=str(request_payload.get("actor") or "fab_wave_control"),
            available_signals=available_signals,
            confidence=confidence,
            approvals=approvals,
            as_of_date=request_payload.get("asOfDate") or request_payload.get("as_of_date"),
            account_option=request_payload.get("accountOption") or request_payload.get("account_option") or "-1",
            account_name=request_payload.get("accountName") or request_payload.get("account_name") or "All Accounts",
            contact_option=request_payload.get("contactOption") or request_payload.get("contact_option") or "0",
            contact_name=request_payload.get("contactName") or request_payload.get("contact_name") or "All Contacts",
            cash_mode=request_payload.get("cashMode") or request_payload.get("cash_mode") or "1",
            include_exports=request_payload.get("includeExports", True),
        )
        result["externalSubmission"] = "not_executed"
        result["operationCount"] = len(result.get("operations") or [])
        result["guardrail"] = "Prepared only. FAB has not read from or written to Wave."
        return result

    def record_report_operation_snapshot(
        self,
        ledger: Any,
        operation: Dict[str, Any],
        workflow_id: Optional[str] = None,
        workflow_run_id: Optional[int] = None,
        status: str = "planned",
    ) -> Optional[int]:
        if (operation.get("surface") or "") != "reports":
            return None
        payload = operation.get("payload") or {}
        report_type = payload.get("reportType") or payload.get("report_type")
        if not report_type:
            return None
        report = get_wave_report(str(report_type)) or {}
        return ledger.record_wave_report_snapshot({
            "workflowRunId": workflow_run_id,
            "operationId": operation.get("operation_id"),
            "workflowId": workflow_id,
            "reportType": report_type,
            "reportSection": report.get("section"),
            "actionId": operation.get("action_id"),
            "status": status,
            "safety": operation.get("safety"),
            "fromDate": payload.get("fromDate"),
            "toDate": payload.get("toDate"),
            "asOfDate": payload.get("asOfDate"),
            "basis": payload.get("basis"),
            "accountOption": payload.get("accountOption"),
            "accountName": payload.get("accountName"),
            "contactOption": payload.get("contactOption"),
            "contactName": payload.get("contactName"),
            "cashMode": payload.get("cashMode"),
            "format": payload.get("format"),
            "externalSubmission": "not_executed",
            "metadata": {
                "operation": operation,
                "report": report,
                "source": "wave_control_plan",
            },
        })

    def record_operation_snapshot(
        self,
        ledger: Any,
        operation: Dict[str, Any],
        workflow_id: Optional[str] = None,
        workflow_run_id: Optional[int] = None,
        status: str = "planned",
    ) -> Optional[int]:
        if not operation:
            return None
        payload = operation.get("payload") or {}
        return ledger.record_wave_operation_snapshot({
            "workflowRunId": workflow_run_id,
            "operationId": operation.get("operation_id"),
            "workflowId": workflow_id,
            "surface": operation.get("surface"),
            "actionId": operation.get("action_id"),
            "mode": operation.get("mode"),
            "safety": operation.get("safety"),
            "status": status,
            "plan": operation.get("plan") or {},
            "planStatus": operation.get("plan", {}).get("status"),
            "capability_plan": operation.get("capability_plan"),
            "requires_confirmation": operation.get("plan", {}).get("requires_confirmation"),
            "requires_credentials": (operation.get("safety") == "requires_credentials"),
            "requiredFields": operation.get("plan", {}).get("required_fields"),
            "missingFields": operation.get("plan", {}).get("missing_fields"),
            "externalSubmission": "not_executed",
            "payload": payload,
            "metadata": {
                "operation": operation,
                "source": "wave_control_plan",
                **(operation.get("metadata") if isinstance(operation.get("metadata"), dict) else {}),
            },
        })

    def record_workflow_report_snapshots(
        self,
        ledger: Any,
        workflow_plan: Dict[str, Any],
        workflow_run_id: Optional[int] = None,
        status: str = "planned",
    ) -> Dict[str, Any]:
        workflow_id = (workflow_plan.get("workflow_plan") or {}).get("workflow_id")
        snapshot_ids = []
        for operation in workflow_plan.get("operations") or []:
            snapshot_id = self.record_report_operation_snapshot(
                ledger,
                operation,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
                status=status,
            )
            if snapshot_id is not None:
                snapshot_ids.append(snapshot_id)
        return {
            "snapshotCount": len(snapshot_ids),
            "snapshotIds": snapshot_ids,
            "workflowId": workflow_id,
            "externalSubmission": "not_executed",
        }

    def record_workflow_operation_snapshots(
        self,
        ledger: Any,
        workflow_plan: Dict[str, Any],
        workflow_run_id: Optional[int] = None,
        status: str = "planned",
    ) -> Dict[str, Any]:
        workflow_id = (workflow_plan.get("workflow_plan") or {}).get("workflow_id")
        snapshot_ids = []
        for operation in workflow_plan.get("operations") or []:
            snapshot_id = self.record_operation_snapshot(
                ledger,
                operation,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
                status=status,
            )
            if snapshot_id is not None:
                snapshot_ids.append(snapshot_id)
        return {
            "snapshotCount": len(snapshot_ids),
            "snapshotIds": snapshot_ids,
            "workflowId": workflow_id,
            "externalSubmission": "not_executed",
        }

    def evaluate_report_controls(
        self,
        ledger: Any,
        workflow_id: str = "daily_reconciliation_run",
        limit: int = 250,
    ) -> Dict[str, Any]:
        """Turn Wave report snapshots into autonomous close/reconciliation gates."""
        workflow_id = str(workflow_id or "daily_reconciliation_run")
        snapshots = ledger.list_wave_report_snapshots(workflow_id=workflow_id, limit=limit)
        required_reports = _workflow_required_reports(workflow_id)
        by_type: Dict[str, List[Dict[str, Any]]] = {}
        for snapshot in snapshots:
            by_type.setdefault(str(snapshot.get("report_type") or "unknown"), []).append(snapshot)

        gates = []
        for report in required_reports:
            report_type = str(report["type"])
            report_snapshots = by_type.get(report_type, [])
            action_ids = {str(snapshot.get("action_id")) for snapshot in report_snapshots if snapshot.get("action_id")}
            statuses = {str(snapshot.get("status")) for snapshot in report_snapshots if snapshot.get("status")}
            has_table_read = "report_table_read" in action_ids
            has_export = "report_export" in action_ids
            needs_export = bool(report.get("default_export"))
            has_result_payload = any(
                snapshot.get("row_count") is not None
                or snapshot.get("total_amount") is not None
                or snapshot.get("total_debits") is not None
                or snapshot.get("total_credits") is not None
                for snapshot in report_snapshots
            )
            if not report_snapshots:
                status = "missing_plan"
            elif not has_table_read:
                status = "missing_read"
            elif needs_export and not has_export:
                status = "missing_export"
            elif statuses <= {"planned"} and not has_result_payload:
                status = "needs_report_results"
            else:
                status = "ready"
            gates.append({
                "reportType": report_type,
                "label": report.get("label") or report_type,
                "section": report.get("section"),
                "status": status,
                "requiredActions": ["report_table_read"] + (["report_export"] if needs_export else []),
                "plannedActions": sorted(action_ids),
                "snapshotCount": len(report_snapshots),
                "latestSnapshotId": report_snapshots[0]["id"] if report_snapshots else None,
                "latestStatus": report_snapshots[0].get("status") if report_snapshots else None,
                "latestUpdatedAt": report_snapshots[0].get("updated_at") if report_snapshots else None,
                "hasResultPayload": has_result_payload,
            })

        blocking_statuses = {"missing_plan", "missing_read", "missing_export"}
        result_gap_statuses = {"needs_report_results"}
        blocking_gates = [gate for gate in gates if gate["status"] in blocking_statuses]
        result_gap_gates = [gate for gate in gates if gate["status"] in result_gap_statuses]
        if blocking_gates:
            status = "blocked_missing_report_plan"
        elif result_gap_gates:
            status = "ready_for_wave_read"
        else:
            status = "ready"
        return {
            "workflowId": workflow_id,
            "status": status,
            "externalSubmission": "not_executed",
            "requiredReportCount": len(required_reports),
            "snapshotCount": len(snapshots),
            "coveredReportCount": sum(1 for gate in gates if gate["snapshotCount"] > 0),
            "readyReportCount": sum(1 for gate in gates if gate["status"] == "ready"),
            "blockingCount": len(blocking_gates),
            "resultGapCount": len(result_gap_gates),
            "gates": gates,
            "nextActions": _report_control_next_actions(status, blocking_gates, result_gap_gates),
        }

    def record_report_result(self, ledger: Any, request_payload: Dict[str, Any]) -> Dict[str, Any]:
        """Attach read/export result evidence to a planned Wave report snapshot."""
        snapshot = _resolve_report_snapshot(ledger, request_payload)
        if not snapshot:
            return {
                "success": False,
                "status": "not_found",
                "error": "A matching Wave report snapshot was not found.",
                "externalSubmission": "not_executed",
            }
        try:
            result_payload = _report_result_payload(request_payload)
        except ValueError as exc:
            return {
                "success": False,
                "status": "invalid_payload",
                "error": str(exc),
                "externalSubmission": "not_executed",
            }
        row_count = _coalesce(result_payload, "rowCount", "row_count")
        total_debits = _coalesce(result_payload, "totalDebits", "total_debits")
        total_credits = _coalesce(result_payload, "totalCredits", "total_credits")
        total_amount = _coalesce(result_payload, "totalAmount", "total_amount")
        empty_state = _coalesce(result_payload, "emptyState", "empty_state")
        export_uri = _coalesce(result_payload, "exportUri", "export_uri", "exportPath", "export_path")
        if all(value in (None, "") for value in [row_count, total_debits, total_credits, total_amount, empty_state, export_uri]):
            return {
                "success": False,
                "status": "invalid_payload",
                "error": "At least one report result field is required.",
                "externalSubmission": "not_executed",
            }

        workflow_id = (
            _coalesce(request_payload, "workflowId", "workflow_id")
            or snapshot.get("workflow_id")
            or "daily_reconciliation_run"
        )
        metadata = dict(snapshot.get("metadata") or {})
        metadata["resultCapture"] = {
            "actor": str(request_payload.get("actor") or "fab_wave_control"),
            "capturedAt": datetime.now(timezone.utc).isoformat(),
            "source": str(request_payload.get("source") or "wave_report_result"),
            "rowCount": row_count,
            "totalDebits": total_debits,
            "totalCredits": total_credits,
            "totalAmount": total_amount,
            "emptyState": empty_state,
            "exportUri": export_uri,
            "notes": request_payload.get("notes"),
            "externalSubmission": "not_executed",
        }
        bank_import_summary = None
        bookkeeping_refresh_summary = None
        reconciliation_summary = None
        if _truthy(_coalesce(request_payload, "importTransactions", "import_transactions", "importRowsAsTransactions")):
            rows = result_payload.get("rows")
            if snapshot.get("report_type") != "account-transactions":
                return {
                    "success": False,
                    "status": "invalid_payload",
                    "error": "Only account-transactions report rows can be imported as transactions.",
                    "externalSubmission": "not_executed",
                }
            if not isinstance(rows, list) or not rows:
                return {
                    "success": False,
                    "status": "invalid_payload",
                    "error": "Report rows are required when importTransactions is enabled.",
                    "externalSubmission": "not_executed",
                }
            if (
                _truthy(_coalesce(request_payload, "runReconciliation", "run_reconciliation"))
                and len(rows) > MAX_WAVE_REPORT_RECONCILIATION_ROWS
            ):
                return {
                    "success": False,
                    "status": "invalid_payload",
                    "error": "Report rows exceed the 500-row reconciliation batch limit; import and reconcile in bounded batches.",
                    "externalSubmission": "not_executed",
                }
            bank_import_summary = LocalBankTransactionImportService(ledger, self.config).import_transactions(
                rows,
                account_identifier=str(
                    _coalesce(request_payload, "accountIdentifier", "account_identifier")
                    or _report_account_identifier(snapshot)
                ),
                source=str(_coalesce(request_payload, "source") or "wave_report_result"),
                filename=_coalesce(request_payload, "filename", "fileName", "exportName"),
                format="wave_report",
            )
            if _truthy(_coalesce(request_payload, "refreshBookkeepingRecords", "refresh_bookkeeping_records", "refreshRecords")):
                bookkeeping_refresh_summary = LocalBookkeepingRecordService(
                    ledger,
                    self.config,
                ).refresh_bank_transactions(
                    limit=max(100, len(bank_import_summary.get("bankTransactionIds") or []))
                )
            if _truthy(_coalesce(request_payload, "runReconciliation", "run_reconciliation")):
                bank_service = LocalBankTransactionImportService(ledger, self.config)
                bank_transactions = bank_service.transactions_for_reconciliation(
                    limit=max(100, len(bank_import_summary.get("bankTransactionIds") or []))
                )
                reconciliation_summary = LocalReconciliationService(ledger, self.config).run(
                    bank_transactions,
                    limit=max(100, len(bank_transactions)),
                )
            metadata["resultCapture"]["bankTransactionImport"] = {
                "bankStatementImportId": bank_import_summary.get("bankStatementImportId"),
                "status": bank_import_summary.get("status"),
                "rowsSeen": bank_import_summary.get("rowsSeen"),
                "rowsImported": bank_import_summary.get("rowsImported"),
                "duplicates": bank_import_summary.get("duplicates"),
                "identityConflicts": bank_import_summary.get("identityConflicts"),
                "identityConflictRows": bank_import_summary.get("identityConflictRows"),
                "identityConflictsTruncated": bank_import_summary.get("identityConflictsTruncated"),
                "skipped": bank_import_summary.get("skipped"),
                "accountIdentifier": bank_import_summary.get("accountIdentifier"),
                "externalSubmission": bank_import_summary.get("externalSubmission"),
            }
            if bookkeeping_refresh_summary is not None:
                metadata["resultCapture"]["bookkeepingRecordRefresh"] = {
                    "requested": bookkeeping_refresh_summary.get("requested"),
                    "updated": bookkeeping_refresh_summary.get("updated"),
                    "failed": bookkeeping_refresh_summary.get("failed"),
                    "ruleApplied": bookkeeping_refresh_summary.get("ruleApplied"),
                    "externalSubmission": "not_executed",
                }
            if reconciliation_summary is not None:
                metadata["resultCapture"]["reconciliation"] = {
                    "requestedTransactions": reconciliation_summary.get("requestedTransactions"),
                    "candidateDocuments": reconciliation_summary.get("candidateDocuments"),
                    "matchedCandidates": reconciliation_summary.get("matchedCandidates"),
                    "missingReceipts": reconciliation_summary.get("missingReceipts"),
                    "unmatchedDocuments": reconciliation_summary.get("unmatchedDocuments"),
                    "matchesRecorded": reconciliation_summary.get("matchesRecorded"),
                    "reviewItemsCreated": reconciliation_summary.get("reviewItemsCreated"),
                    "externalSubmission": "not_executed",
                }
        snapshot_id = ledger.record_wave_report_snapshot({
            "workflowRunId": snapshot.get("workflow_run_id"),
            "operationId": snapshot.get("operation_id"),
            "workflowId": workflow_id,
            "reportType": snapshot.get("report_type"),
            "reportSection": snapshot.get("report_section"),
            "actionId": snapshot.get("action_id"),
            "status": str(_coalesce(request_payload, "status") or "read_result_captured"),
            "safety": snapshot.get("safety") or "read_only",
            "fromDate": snapshot.get("from_date"),
            "toDate": snapshot.get("to_date"),
            "asOfDate": snapshot.get("as_of_date"),
            "basis": snapshot.get("basis"),
            "accountOption": snapshot.get("account_option"),
            "accountName": snapshot.get("account_name"),
            "contactOption": snapshot.get("contact_option"),
            "contactName": snapshot.get("contact_name"),
            "cashMode": snapshot.get("cash_mode"),
            "format": _coalesce(request_payload, "format", "exportFormat", "export_format") or snapshot.get("export_format"),
            "rowCount": row_count,
            "totalDebits": total_debits,
            "totalCredits": total_credits,
            "totalAmount": total_amount,
            "externalSubmission": "not_executed",
            "metadata": metadata,
        })
        updated_snapshot = ledger.get_wave_report_snapshot(snapshot_id)
        controls = self.evaluate_report_controls(ledger, workflow_id=workflow_id)
        return {
            "success": True,
            "status": "read_result_captured",
            "externalSubmission": "not_executed",
            "waveReportSnapshotId": snapshot_id,
            "waveReportSnapshot": updated_snapshot,
            "waveReportControls": controls,
            "bankTransactionImport": bank_import_summary,
            "bookkeepingRecordRefresh": bookkeeping_refresh_summary,
            "reconciliation": reconciliation_summary,
        }


def _operation_response(operation: Dict[str, Any]) -> Dict[str, Any]:
    plan = operation.get("plan") or {}
    capability_plan = operation.get("capability_plan")
    status = plan.get("status") or "unsupported"
    if status == "planned" and capability_plan and capability_plan.get("status") != "ready":
        status = "needs_review"
    elif status == "planned" and operation.get("safety") == "requires_credentials":
        status = "requires_credentials"
    elif status == "planned" and operation.get("safety") == "requires_confirmation":
        status = "requires_confirmation"
    return {
        "success": status == "planned",
        "status": status,
        "externalSubmission": "not_executed",
        "guardrail": "Prepared only. External Wave execution remains approval-gated.",
        "operation": operation,
    }


def _credential_status(config: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "waveappsBusiness": {
            "accessTokenConfigured": _has_config(config, "waveapps_business_access_token", "waveapps_business.access_token"),
            "businessIdConfigured": _has_config(config, "waveapps_business_id", "waveapps_business.business_id"),
        },
        "waveappsPersonal": {
            "accessTokenConfigured": _has_config(config, "waveapps_personal_access_token", "waveapps_personal.access_token"),
            "personalIdConfigured": _has_config(config, "waveapps_personal_id", "waveapps_personal.personal_id"),
        },
        "autonomousMode": str(config.get("waveapps_autonomous_mode") or "prepare"),
        "confirmedActionsEnabled": bool(config.get("waveapps_allow_confirmed_actions", False)),
        "credentialActionsEnabled": bool(config.get("waveapps_allow_credential_actions", False)),
    }


def _has_config(config: Dict[str, Any], *keys: str) -> bool:
    for key in keys:
        if "." in key:
            section, option = key.split(".", 1)
            section_values = config.get(section)
            if isinstance(section_values, dict) and section_values.get(option) not in (None, ""):
                return True
        if config.get(key) not in (None, ""):
            return True
    return False


def _coalesce(values: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = values.get(key)
        if value not in (None, ""):
            return value
    return None


def _resolve_report_snapshot(ledger: Any, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    snapshot_id = _coalesce(payload, "snapshotId", "snapshot_id", "waveReportSnapshotId", "wave_report_snapshot_id")
    if snapshot_id:
        try:
            return ledger.get_wave_report_snapshot(int(snapshot_id))
        except (TypeError, ValueError):
            return None
    operation_id = _coalesce(payload, "operationId", "operation_id")
    workflow_id = _coalesce(payload, "workflowId", "workflow_id")
    report_type = _coalesce(payload, "reportType", "report_type")
    action_id = _coalesce(payload, "actionId", "action_id")
    snapshots = ledger.list_wave_report_snapshots(
        report_type=str(report_type) if report_type else None,
        workflow_id=str(workflow_id) if workflow_id else None,
        limit=500,
    )
    for snapshot in snapshots:
        if operation_id and snapshot.get("operation_id") != operation_id:
            continue
        if action_id and snapshot.get("action_id") != action_id:
            continue
        return snapshot
    return None


def _report_result_payload(request_payload: Dict[str, Any]) -> Dict[str, Any]:
    result_text = _coalesce(request_payload, "resultText", "result_text", "reportText", "report_text")
    if result_text is not None:
        try:
            result_size = len(str(result_text).encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError("Wave report result must contain valid Unicode text") from exc
        if result_size > MAX_WAVE_REPORT_RESULT_BYTES:
            raise ValueError(f"Wave report result exceeds the {MAX_WAVE_REPORT_RESULT_BYTES}-byte processing limit")
    raw_result = request_payload.get("result")
    if isinstance(raw_result, dict):
        result_payload = dict(raw_result)
    elif isinstance(raw_result, list):
        result_payload = {"rows": raw_result}
    else:
        result_payload = {}
    rows = result_payload.get("rows")
    if not isinstance(rows, list):
        rows = request_payload.get("rows")
    if not isinstance(rows, list):
        if result_text:
            rows = _parse_report_rows(str(result_text), str(_coalesce(request_payload, "format", "exportFormat", "export_format") or "csv"))
    if isinstance(rows, list):
        if len(rows) > MAX_WAVE_REPORT_RESULT_ROWS:
            raise ValueError(f"Wave report result exceeds the {MAX_WAVE_REPORT_RESULT_ROWS}-row processing limit")
        summary = _summarize_report_rows(rows)
        for key, value in summary.items():
            result_payload.setdefault(key, value)
        result_payload["rows"] = rows
    return result_payload


def _parse_report_rows(text: str, format_name: str) -> List[Dict[str, Any]]:
    normalized_format = str(format_name or "csv").strip().lower()
    if normalized_format == "json":
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            parsed = parsed.get("rows") or parsed.get("reportRows") or parsed.get("transactions") or []
        if not isinstance(parsed, list):
            raise ValueError("JSON report result text must be a list or contain rows")
        return [row for row in parsed if isinstance(row, dict)]
    if normalized_format == "csv":
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        return [dict(row) for row in reader if row]
    raise ValueError(f"Unsupported Wave report result format: {format_name}")


def _summarize_report_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_debits = Decimal("0")
    total_credits = Decimal("0")
    total_amount = Decimal("0")
    saw_debits = False
    saw_credits = False
    saw_amount = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        debit = _row_number(row, "debit", "debits", "withdrawal", "moneyout", "money_out", "af")
        credit = _row_number(row, "credit", "credits", "deposit", "moneyin", "money_in", "bij")
        amount = _row_number(row, "amount", "total", "totalamount", "total_amount", "netamount", "net_amount")
        if debit is not None:
            total_debits += abs(debit)
            saw_debits = True
        if credit is not None:
            total_credits += abs(credit)
            saw_credits = True
        if amount is not None:
            total_amount += amount
            saw_amount = True
            if debit is None and credit is None:
                if amount < 0:
                    total_debits += abs(amount)
                    saw_debits = True
                elif amount > 0:
                    total_credits += amount
                    saw_credits = True
    summary: Dict[str, Any] = {"rowCount": len([row for row in rows if isinstance(row, dict)])}
    if saw_debits:
        summary["totalDebits"] = float(total_debits)
    if saw_credits:
        summary["totalCredits"] = float(total_credits)
    if saw_amount:
        summary["totalAmount"] = float(total_amount)
    return summary


def _row_number(row: Dict[str, Any], *keys: str) -> Optional[Decimal]:
    normalized = {_report_key(key): value for key, value in row.items()}
    for key in keys:
        value = normalized.get(_report_key(key))
        parsed = _parse_report_number(value)
        if parsed is not None:
            return parsed
    return None


def _parse_report_number(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    text = str(value).strip()
    if not text or text in {"-", "--"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    text = text.replace("€", "").replace("$", "").replace(" ", "")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        return None
    return -parsed if negative else parsed


def _report_key(value: Any) -> str:
    return "".join(character for character in str(value or "").lower() if character.isalnum())


def _report_account_identifier(snapshot: Dict[str, Any]) -> str:
    account_name = str(snapshot.get("account_name") or "").strip()
    if account_name and account_name.lower() not in {"all accounts", "all"}:
        return account_name
    account_option = str(snapshot.get("account_option") or "").strip()
    if account_option and account_option not in {"-1", "0"}:
        return f"wave-account-{account_option}"
    return "wave-account-transactions"


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}


def _list_value(value: Any) -> List[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    return [item.strip() for item in str(value).replace(";", ",").split(",") if item.strip()]


def _entity_mirror_summary(ledger: Any) -> Dict[str, Any]:
    if ledger is None:
        return {
            "status": "ledger_not_attached",
            "entityCount": 0,
            "missingDownstream": 0,
            "byType": {},
            "byTarget": {},
            "latestSyncByTarget": {},
        }
    entities = ledger.list_wave_entities(limit=500)
    sync_runs = ledger.list_wave_sync_runs(limit=100)
    by_type: Dict[str, int] = {}
    by_target: Dict[str, int] = {}
    for entity in entities:
        entity_type = str(entity.get("entity_type") or "unknown")
        target_system = str(entity.get("target_system") or "waveapps")
        by_type[entity_type] = by_type.get(entity_type, 0) + 1
        by_target[target_system] = by_target.get(target_system, 0) + 1
    latest_sync_by_target = {}
    for sync_run in sync_runs:
        target_system = str(sync_run.get("target_system") or "waveapps")
        if target_system not in latest_sync_by_target:
            latest_sync_by_target[target_system] = {
                "syncRunId": sync_run.get("id"),
                "status": sync_run.get("status"),
                "entitiesSeen": sync_run.get("entities_seen"),
                "finishedAt": sync_run.get("finished_at"),
            }
    missing = sum(1 for entity in entities if entity.get("presence_status") == "missing_downstream")
    return {
        "status": "attention" if missing else ("ready" if sync_runs else "not_synced"),
        "entityCount": len(entities),
        "missingDownstream": missing,
        "byType": dict(sorted(by_type.items())),
        "byTarget": dict(sorted(by_target.items())),
        "latestSyncByTarget": latest_sync_by_target,
    }


def _today() -> str:
    return date.today().isoformat()


def _workflow_required_reports(workflow_id: str) -> List[Dict[str, Any]]:
    if workflow_id == "daily_reconciliation_run":
        return [report for report in list_wave_reports() if report["type"] == "account-transactions"]
    elif workflow_id == "period_close_pack":
        predicate = lambda report: bool(report.get("close_pack"))
    else:
        predicate = lambda report: bool(report.get("reconciliation_pack") or report.get("close_pack"))
    return [report for report in list_wave_reports() if predicate(report)]


def _report_control_next_actions(
    status: str,
    blocking_gates: List[Dict[str, Any]],
    result_gap_gates: List[Dict[str, Any]],
) -> List[str]:
    if status == "blocked_missing_report_plan":
        missing = ", ".join(gate["label"] for gate in blocking_gates[:4])
        return [
            f"Plan the missing Wave report operations before FAB uses this workflow as source-of-truth evidence: {missing}.",
            "Use /api/wave/workflows/plan to create the report-read and export operation snapshots.",
        ]
    if status == "ready_for_wave_read":
        pending = ", ".join(gate["label"] for gate in result_gap_gates[:4])
        return [
            f"Wave report operations are planned; execute the approved read/export connector and attach result payloads for: {pending}.",
            "Keep external submission disabled until report results are captured and reviewed.",
        ]
    return [
        "Required Wave report evidence is present and can be used by FAB close/reconciliation checks.",
    ]
