import importlib
import json
import re
import signal
import time
from datetime import datetime, timezone
from typing import Any, Dict


_DEPENDENCY_PATHS = {
    "PostingExecutor": ("src.data_entry.posting_executor", "PostingExecutor"),
    "LocalAutonomousService": ("src.operations.local_autonomy", "LocalAutonomousService"),
    "LocalBackupService": ("src.operations.local_backup", "LocalBackupService"),
    "LocalComplianceService": ("src.operations.local_compliance", "LocalComplianceService"),
    "LocalConnectorIntakeService": (
        "src.operations.local_connector_intake",
        "LocalConnectorIntakeService",
    ),
    "DriveWaveDeliveryService": (
        "src.operations.drive_wave_delivery",
        "DriveWaveDeliveryService",
    ),
    "LocalExportAttemptService": (
        "src.operations.local_exports",
        "LocalExportAttemptService",
    ),
    "LocalFolderIntake": (
        "src.operations.local_intake",
        "LocalFolderIntake",
    ),
    "LocalNotificationService": (
        "src.operations.local_notifications",
        "LocalNotificationService",
    ),
    "LocalScheduledReportService": (
        "src.operations.local_reporting",
        "LocalScheduledReportService",
    ),
    "build_local_operations_ledger": (
        "src.operations.local_runtime",
        "build_local_operations_ledger",
    ),
    "LocalWorkflowRecoveryScheduler": (
        "src.operations.local_workflow_recovery",
        "LocalWorkflowRecoveryScheduler",
    ),
    "apply_local_wave_settings": (
        "src.security.local_secret_store",
        "apply_local_wave_settings",
    ),
    "Database": ("src.storage.database", "Database"),
}

COALESCED_WORKER_AUDITS = {
    "approved_export_cycle",
    "autonomy_cycle",
    "compliance_cycle",
    "connector_intake_cycle",
    "cycle_completed",
    "cycle_started",
    "drive_wave_archive_cycle",
    "notification_cycle",
    "scheduled_backup_cycle",
    "scheduled_report_cycle",
    "workflow_recovery_cycle",
}
WORKER_AUDIT_VOLATILE_FIELDS = {
    "autonomy_cycle": {"workflowRunId"},
    "cycle_completed": {"completedAt", "startedAt"},
    "cycle_started": {"startedAt"},
}

# Keep these names patchable in worker tests while avoiding import cost for
# inactive stages. _dependency replaces each lazy slot on first real use.
for _dependency_name in _DEPENDENCY_PATHS:
    globals()[_dependency_name] = None


def _dependency(name: str):
    dependency = globals().get(name)
    if dependency is not None:
        return dependency
    module_name, attribute_name = _DEPENDENCY_PATHS[name]
    dependency = getattr(importlib.import_module(module_name), attribute_name)
    globals()[name] = dependency
    return dependency


class FabWorker:
    """Simple local scheduler for repeated FAB workflow, retry handling, and approved posting execution."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config or {}
        self.interval_seconds = _bounded_positive_int(
            self.config.get("worker_interval_seconds"),
            default=300,
            maximum=86400,
        )
        self.run_once = _as_bool(self.config.get("worker_run_once", False))
        self.process_postings = _as_bool(self.config.get("worker_process_approved_postings", True))
        self.process_retries = _as_bool(self.config.get("worker_process_due_retries", True))
        self.operations_ledger = _dependency("build_local_operations_ledger")(self.config)
        if _as_bool(self.config.get("worker_run_legacy_workflow", False)):
            raise ValueError(
                "worker_run_legacy_workflow was retired; use the authoritative "
                "connector intake and local autonomy stages instead"
            )
        self.sync_source_connectors = bool(self.operations_ledger) and _as_bool(
            self.config.get("worker_sync_source_connectors", True)
        )
        self.source_connectors = _list_config(
            self.config,
            "worker_source_connectors",
            "source_connectors",
        )
        self.run_local_autonomy = bool(self.operations_ledger) and _as_bool(
            self.config.get("worker_run_local_autonomy", True)
        )
        self.recover_workflows = bool(self.operations_ledger) and _as_bool(
            self.config.get("worker_recover_workflows", True)
        )
        self.recovery_batch_limit = _bounded_positive_int(
            self.config.get("worker_recovery_batch_limit"),
            default=5,
            maximum=50,
        )
        self.include_wave_plan = _as_bool(self.config.get("worker_include_wave_plan", True))
        self.include_wave_sync = _as_bool(self.config.get("worker_include_wave_sync", True))
        self.create_scheduled_backups = bool(self.operations_ledger) and _as_bool(
            self.config.get("worker_create_scheduled_backups", True)
        )
        self.generate_scheduled_reports = bool(self.operations_ledger) and _as_bool(
            self.config.get("worker_generate_scheduled_reports", True)
        )
        self.refresh_notifications = bool(self.operations_ledger) and _as_bool(
            self.config.get("worker_refresh_notifications", True)
        )
        self.assess_compliance = bool(self.operations_ledger) and _as_bool(
            self.config.get("worker_assess_compliance", True)
        )
        self.archive_verified_drive_sources = bool(self.operations_ledger) and _as_bool(
            self.config.get("worker_archive_verified_drive_sources", True)
        )
        self.process_legacy_postings = _as_bool(
            self.config.get("worker_process_legacy_postings", self.operations_ledger is None)
        )
        self.database = (
            _dependency("Database")(config)
            if self.process_legacy_postings or not self.operations_ledger
            else None
        )
        self._stop_requested = False
        self._recovery_held_connector_sources = set()
        self._autonomy_prepared_exports = False
        self._audit_heartbeat_seconds = _bounded_positive_int(
            self.config.get("worker_audit_heartbeat_seconds"),
            default=86400,
            maximum=604800,
        )
        self._audit_states: Dict[str, tuple[str, float]] = {}

    def install_signal_handlers(self) -> None:
        def handle_stop(signum, frame):
            self._stop_requested = True
            self._record_audit("stop_requested", {"signal": signum}, "Worker stop requested")
        signal.signal(signal.SIGINT, handle_stop)
        signal.signal(signal.SIGTERM, handle_stop)

    def run(self) -> None:
        self.install_signal_handlers()
        self._record_audit("started", {"intervalSeconds": self.interval_seconds}, "Worker started")
        while not self._stop_requested:
            _dependency("apply_local_wave_settings")(self.config, mutate=True)
            started_at = self._now()
            self._recovery_held_connector_sources = set()
            self._autonomy_prepared_exports = False
            self._record_audit("cycle_started", {"startedAt": started_at}, "Worker cycle started")
            stage_errors = []
            stages = (
                ("scheduled_backup", self._process_scheduled_backup),
                ("workflow_recovery", self._recover_workflows),
                ("connector_intake", self._sync_source_connectors),
                ("local_autonomy", self._run_local_autonomy),
                ("scheduled_reports", self._process_scheduled_reports),
                ("compliance", self._assess_compliance),
                ("notifications", self._refresh_notifications),
                ("operations_exports", self._process_operations_exports),
                ("drive_wave_archive", self._archive_verified_drive_sources),
                ("legacy_queue", self._process_legacy_queue),
            )
            for stage, action in stages:
                try:
                    action()
                except Exception as exc:
                    safe_error = _safe_worker_error(exc, self.config)
                    failure = {"stage": stage, "error": safe_error}
                    stage_errors.append(failure)
                    self._record_audit(
                        "stage_failed",
                        {"startedAt": started_at, **failure},
                        f"Worker stage {stage} failed: {safe_error}",
                    )
            if stage_errors:
                self._record_audit(
                    "cycle_finished_with_error",
                    {
                        "startedAt": started_at,
                        "completedAt": self._now(),
                        "stageErrors": stage_errors,
                    },
                    "Worker cycle finished with one or more stage errors",
                )
            else:
                self._record_audit(
                    "cycle_completed",
                    {"startedAt": started_at, "completedAt": self._now()},
                    "Worker cycle completed",
                )
            if self.run_once:
                break
            self._sleep_until_next_cycle()
        self._record_audit("stopped", {}, "Worker stopped")

    def _sync_source_connectors(self) -> None:
        if not self.sync_source_connectors or not self.operations_ledger:
            return
        service = _dependency("LocalConnectorIntakeService")(
            self.operations_ledger,
            self.config,
        )
        selected_sources = list(self.source_connectors)
        if not selected_sources:
            selected_sources = list(service.plan().get("syncableSources") or [])
        selected_sources = [
            source
            for source in selected_sources
            if source not in self._recovery_held_connector_sources
        ]
        if not selected_sources:
            status = (
                "held_back_by_recovery"
                if self._recovery_held_connector_sources
                else "no_syncable_sources"
            )
            self._record_audit(
                "connector_intake_cycle",
                {
                    "success": True,
                    "status": status,
                    "workflowRunId": None,
                    "summary": {},
                    "sources": [],
                    "heldBackSources": sorted(self._recovery_held_connector_sources),
                    "externalSubmission": "not_executed",
                },
                f"Connector intake cycle ended as {status}",
            )
            return
        result = service.sync(
            sources=selected_sources,
            actor="local_worker",
        )
        self._record_audit(
            "connector_intake_cycle",
            _compact_connector_intake(result),
            f"Connector intake cycle ended as {result.get('status')}",
        )
        if not result.get("success"):
            raise RuntimeError("One or more configured source connectors failed")

    def _recover_workflows(self) -> None:
        if not self.recover_workflows or not self.operations_ledger:
            return
        result = _dependency("LocalWorkflowRecoveryScheduler")(
            self.operations_ledger,
            self.config,
            intake_paths=_list_config(
                self.config,
                "fab_local_intake_paths",
                "operations_local_intake_paths",
                "local_intake_paths",
            ),
            intake_extensions=_list_config(
                self.config,
                "fab_local_intake_extensions",
                "operations_local_intake_extensions",
                "local_intake_extensions",
            ),
        ).run_due(
            actor="local_worker",
            limit=self.recovery_batch_limit,
        )
        self._recovery_held_connector_sources = set(
            result.get("connectorSourcesHeldBack") or []
        )
        self._record_audit(
            "workflow_recovery_cycle",
            _compact_workflow_recovery(result),
            f"Governed workflow recovery ended as {result.get('status')}",
        )
        if result.get("status") == "already_running":
            return
        if not result.get("success"):
            raise RuntimeError("One or more due governed workflow recoveries failed")

    def _run_local_autonomy(self) -> None:
        if not self.run_local_autonomy or not self.operations_ledger:
            return
        autonomy_config = dict(self.config)
        # The worker's approved-export stage remains the sole external executor.
        autonomy_config["fab_autonomy_execute_approved_exports"] = False
        # Scheduled cycles retain executed and failure boundaries without
        # storing identical skipped-step rows every five minutes.
        autonomy_config["fab_autonomy_record_skipped_steps"] = False
        intake_paths = _list_config(
            self.config,
            "fab_local_intake_paths",
            "operations_local_intake_paths",
            "local_intake_paths",
        )
        intake_extensions = _list_config(
            self.config,
            "fab_local_intake_extensions",
            "operations_local_intake_extensions",
            "local_intake_extensions",
        )
        service = _dependency("LocalAutonomousService")(
            self.operations_ledger,
            autonomy_config,
            intake_paths=intake_paths,
            intake_extensions=intake_extensions,
        )
        plan = service.plan(
            limit=25,
            include_wave_plan=self.include_wave_plan,
            include_wave_sync=self.include_wave_sync,
            include_connector_sync=False,
        )
        plan_is_valid = isinstance(plan, dict)
        runnable_action_ids = set(plan.get("runnableActionIds") or []) if plan_is_valid else set()
        proven_idle = plan_is_valid and not runnable_action_ids
        if plan_is_valid and runnable_action_ids == {"rescan_intake"}:
            proven_idle = not _dependency("LocalFolderIntake")(
                self.operations_ledger,
                allowed_extensions=intake_extensions,
            ).has_pending_changes(intake_paths)
        if proven_idle:
            result = {
                "success": True,
                "status": "idle",
                "externalSubmission": "not_executed",
                "plan": plan,
                "executedActions": [],
                "skippedActions": plan.get("actions") or [],
            }
        else:
            result = service.run_cycle(
                limit=25,
                include_wave_plan=self.include_wave_plan,
                include_wave_sync=self.include_wave_sync,
                include_connector_sync=False,
            )
        self._autonomy_prepared_exports = any(
            action.get("id") == "prepare_export_attempts"
            and action.get("status") == "completed"
            for action in result.get("executedActions") or []
            if isinstance(action, dict)
        )
        self._record_audit(
            "autonomy_cycle",
            _compact_autonomy_cycle(result),
            f"Local autonomous cycle ended as {result.get('status')}",
        )

    def _process_operations_exports(self) -> None:
        if not self.operations_ledger:
            return
        service = _dependency("LocalExportAttemptService")(
            self.operations_ledger,
            self.config,
        )
        if not self._autonomy_prepared_exports:
            preparation = service.prepare_ready_exports(limit=25)
            if preparation.get("changed"):
                self._record_audit(
                    "export_preparation_cycle",
                    _compact_export_preparation(preparation),
                    "Operations-ledger export preparation completed",
                )
        if self.process_postings:
            result = service.process_approved_attempts(limit=20, actor="local_worker")
            self._record_audit(
                "approved_export_cycle",
                _compact_export_execution(result),
                "Operations-ledger approved export cycle completed",
            )

    def _process_scheduled_backup(self) -> None:
        if not self.create_scheduled_backups or not self.operations_ledger:
            return
        result = _dependency("LocalBackupService")(
            self.operations_ledger,
            self.config,
        ).run_due(actor="local_worker")
        self._record_audit(
            "scheduled_backup_cycle",
            _compact_scheduled_backup(result),
            f"Scheduled backup cycle ended as {result.get('status')}",
        )
        if not result.get("success"):
            raise RuntimeError(result.get("error") or "Scheduled backup failed")

    def _process_scheduled_reports(self) -> None:
        if not self.generate_scheduled_reports or not self.operations_ledger:
            return
        result = _dependency("LocalScheduledReportService")(
            self.operations_ledger,
            self.config,
        ).run_due(
            actor="local_worker",
        )
        self._record_audit(
            "scheduled_report_cycle",
            _compact_scheduled_report(result),
            f"Scheduled report cycle ended as {result.get('status')}",
        )
        if not result.get("success"):
            raise RuntimeError(result.get("error") or "Scheduled report generation failed")

    def _archive_verified_drive_sources(self) -> None:
        if not self.archive_verified_drive_sources or not self.operations_ledger:
            return
        result = _dependency("DriveWaveDeliveryService")(
            self.operations_ledger,
            self.config,
        ).archive_ready(
            limit=25,
            actor="local_worker",
        )
        self._record_audit(
            "drive_wave_archive_cycle",
            result,
            f"Verified Drive archive cycle ended as {result.get('status')}",
        )

    def _refresh_notifications(self) -> None:
        if not self.refresh_notifications or not self.operations_ledger:
            return
        result = _dependency("LocalNotificationService")(
            self.operations_ledger,
            self.config,
        ).refresh(
            actor="local_worker",
        )
        self._record_audit(
            "notification_cycle",
            _compact_notification_refresh(result),
            "Local notification refresh completed",
        )

    def _assess_compliance(self) -> None:
        if not self.assess_compliance or not self.operations_ledger:
            return
        result = _dependency("LocalComplianceService")(
            self.operations_ledger,
            self.config,
        ).assess(
            actor="local_worker",
        )
        self._record_audit(
            "compliance_cycle",
            _compact_compliance_assessment(result),
            f"Local compliance cycle ended as {result.get('status')}",
        )

    def _process_legacy_queue(self) -> None:
        if not self.process_legacy_postings:
            return
        posting_executor = _dependency("PostingExecutor")(self.config)
        if self.process_retries:
            retry_result = posting_executor.process_due_retries()
            self._record_audit("legacy_retry_cycle", retry_result, "Legacy retry cycle completed")
        if self.process_postings:
            posting_result = posting_executor.process_approved_attempts()
            self._record_audit(
                "legacy_approved_posting_cycle",
                posting_result,
                "Legacy approved posting cycle completed",
            )

    def _record_audit(self, action: str, details: Dict[str, Any], reason: str) -> None:
        if self._coalesce_audit(action, details):
            return
        if self.operations_ledger:
            self.operations_ledger.record_audit_event({
                "action": f"local_worker.{action}",
                "entityType": "worker",
                "entityId": "local",
                "details": details,
                "reason": reason,
                "actor": "system",
            })
            return
        if not self.database:
            return
        self.database.add_audit_log(
            "worker",
            "local",
            action,
            None,
            details,
            reason,
            "system",
        )

    def _coalesce_audit(self, action: str, details: Dict[str, Any]) -> bool:
        if action not in COALESCED_WORKER_AUDITS:
            return False
        volatile_fields = WORKER_AUDIT_VOLATILE_FIELDS.get(action, set())
        stable_details = {
            key: value
            for key, value in (details or {}).items()
            if key not in volatile_fields
        }
        fingerprint = json.dumps(
            stable_details,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        now = time.monotonic()
        previous = self._audit_states.get(action)
        if (
            previous
            and previous[0] == fingerprint
            and now - previous[1] < self._audit_heartbeat_seconds
        ):
            return True
        self._audit_states[action] = (fingerprint, now)
        return False

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _sleep_until_next_cycle(self) -> None:
        slept = 0
        while slept < self.interval_seconds and not self._stop_requested:
            time.sleep(1)
            slept += 1


def _compact_export_preparation(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "requested": result.get("requested", 0),
        "prepared": result.get("prepared", 0),
        "alreadyPrepared": result.get("alreadyPrepared", 0),
        "blocked": result.get("blocked", 0),
        "externalSubmission": result.get("externalSubmission", "not_executed"),
    }


def _compact_scheduled_backup(result: Dict[str, Any]) -> Dict[str, Any]:
    backup = result.get("backup") if isinstance(result.get("backup"), dict) else {}
    manifest = backup.get("manifest") if isinstance(backup.get("manifest"), dict) else {}
    source_evidence = (
        manifest.get("sourceEvidence")
        if isinstance(manifest.get("sourceEvidence"), dict)
        else {}
    )
    schedule = (
        result.get("schedule")
        if isinstance(result.get("schedule"), dict)
        else {}
    )
    return {
        "success": result.get("success"),
        "status": result.get("status"),
        "backupFilename": backup.get("backupFilename"),
        "ledgerSha256": manifest.get("ledgerSha256"),
        "sourceEvidenceStatus": source_evidence.get("coverageStatus"),
        "sourceEvidenceDocuments": source_evidence.get("includedDocuments", 0),
        "sourceEvidenceFiles": source_evidence.get("includedFiles", 0),
        "sourceEvidenceGaps": source_evidence.get("gapCount", 0),
        "nextDueAt": schedule.get("nextDueAt"),
        "externalSubmission": result.get("externalSubmission", "not_executed"),
    }


def _compact_export_execution(result: Dict[str, Any]) -> Dict[str, Any]:
    processed = []
    for item in result.get("processed") or []:
        attempt = item.get("exportAttempt") if isinstance(item.get("exportAttempt"), dict) else {}
        processed.append({
            "exportAttemptId": attempt.get("id"),
            "status": item.get("status"),
            "executionStatus": item.get("executionStatus"),
            "externalSubmission": item.get("externalSubmission"),
        })
    return {
        "status": result.get("status"),
        "count": result.get("count", 0),
        "processed": processed,
        "preExecutionBackup": result.get("preExecutionBackup"),
    }


def _compact_autonomy_cycle(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "success": result.get("success"),
        "status": result.get("status"),
        "workflowRunId": result.get("workflowRunId"),
        "executedActionIds": [action.get("id") for action in result.get("executedActions") or []],
        "skippedActionIds": [action.get("id") for action in result.get("skippedActions") or []],
        "masterLedger": result.get("masterLedger"),
        "externalSubmission": "not_executed",
    }


def _compact_connector_intake(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "success": result.get("success"),
        "status": result.get("status"),
        "workflowRunId": result.get("workflowRunId"),
        "summary": result.get("summary") or {},
        "sources": [
            {
                "source": item.get("source"),
                "status": item.get("status"),
                "registered": item.get("registered", 0),
                "duplicates": item.get("duplicates", 0),
                "revisions": item.get("revisions", 0),
            }
            for item in result.get("results") or []
        ],
        "externalSubmission": "not_executed",
    }


def _compact_workflow_recovery(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "success": result.get("success"),
        "status": result.get("status"),
        "attempted": result.get("attempted", 0),
        "succeeded": result.get("succeeded", 0),
        "failed": result.get("failed", 0),
        "interruptedWorkflowRunIds": result.get("interruptedWorkflowRunIds") or [],
        "connectorSourcesHeldBack": result.get("connectorSourcesHeldBack") or [],
        "externalSubmission": "not_executed",
    }


def _compact_scheduled_report(result: Dict[str, Any]) -> Dict[str, Any]:
    report_run = result.get("reportRun") if isinstance(result.get("reportRun"), dict) else {}
    return {
        "success": result.get("success"),
        "status": result.get("status"),
        "reportRunId": report_run.get("id"),
        "scheduleId": report_run.get("schedule_id"),
        "scheduleSlot": report_run.get("schedule_slot"),
        "readiness": report_run.get("readiness"),
        "rowCount": report_run.get("row_count", 0),
        "blockerCount": report_run.get("blocker_count", 0),
        "externalSubmission": "not_executed",
    }


def _compact_notification_refresh(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "success": result.get("success"),
        "status": result.get("status"),
        "created": result.get("created", 0),
        "reopened": result.get("reopened", 0),
        "resolved": result.get("resolved", 0),
        "suppressed": result.get("suppressed", 0),
        "activeIssues": result.get("activeIssues", 0),
        "externalDelivery": "not_executed",
    }


def _compact_compliance_assessment(result: Dict[str, Any]) -> Dict[str, Any]:
    assessment = result.get("assessment") if isinstance(result.get("assessment"), dict) else {}
    return {
        "success": result.get("success"),
        "status": result.get("status"),
        "assessmentId": assessment.get("id"),
        "assessmentStatus": assessment.get("status"),
        "periodFrom": assessment.get("period_from"),
        "periodTo": assessment.get("period_to"),
        "recordCount": assessment.get("record_count", 0),
        "findingCount": assessment.get("finding_count", 0),
        "blockingCount": assessment.get("blocking_count", 0),
        "statutoryStatus": "provisional",
        "externalFiling": "not_executed",
    }


def _list_config(config: Dict[str, Any], *keys: str) -> list:
    value = None
    for key in keys:
        if config.get(key) not in (None, ""):
            value = config.get(key)
            break
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = str(value).replace("\n", ",").replace(";", ",").split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _bounded_positive_int(value: Any, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(1, min(parsed, maximum))


def _safe_worker_error(error: Any, config: Dict[str, Any]) -> str:
    message = f"{type(error).__name__}: {error}"
    for key, value in config.items():
        if not re.search(
            r"(?i)(token|secret|password|api[_-]?key|authorization|credential)",
            str(key),
        ):
            continue
        secret = str(value or "")
        if len(secret) >= 4:
            message = message.replace(secret, "[REDACTED]")
    message = re.sub(
        r"(?i)((?:access[_-]?token|refresh[_-]?token|token|password|secret|(?:x[_-]?)?api[_-]?key)\s*[:=]\s*)[^&,;\s]+",
        r"\1[REDACTED]",
        message,
    )
    message = re.sub(
        r"(?i)(Authorization\s*:\s*(?:Bearer|Basic)\s+)[^\s,;]+",
        r"\1[REDACTED]",
        message,
    )
    return message[:2000]
