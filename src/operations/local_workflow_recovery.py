import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, Optional
from uuid import uuid4

from src.operations.local_autonomy import (
    AUTONOMY_EXECUTION_ORDER,
    AUTONOMY_LEASE_NAME,
    AUTONOMY_RECOVERY_DEPENDENCIES,
    LocalAutonomousService,
)
from src.operations.local_connector_intake import (
    CONNECTOR_INTAKE_LEASE_NAME,
    CONNECTOR_SOURCES,
    LocalConnectorIntakeService,
)
from src.operations.local_ledger import LocalOperationsLedger


AUTONOMY_RECOVERY_TRIGGER = "local_autonomous_recovery"
CONNECTOR_RECOVERY_TRIGGER = "connector_intake_recovery"
AUTONOMY_TRIGGERS = {"local_autonomous_cycle", AUTONOMY_RECOVERY_TRIGGER}
CONNECTOR_TRIGGERS = {"connector_intake", CONNECTOR_RECOVERY_TRIGGER}
RECOVERABLE_RUN_STATUSES = {"failed", "completed_with_errors", "attention_required"}
RECOVERABLE_STEP_STATUSES = {"failed", "blocked", "skipped", "not_run"}
SAFE_AUTONOMY_MODES = {"safe_auto", "safe_draft", "read_only"}
WORKFLOW_RECOVERY_SCHEDULER_LEASE_NAME = "local_workflow_recovery_scheduler"
DEFAULT_INTERRUPTED_RUN_GRACE_SECONDS = 900.0
HISTORICAL_HIGH_RISK_AUTONOMY_ACTIONS = {"execute_approved_exports"}


class LocalWorkflowRecoveryService:
    """Plan and execute linked retries without replaying high-risk actions."""

    def __init__(
        self,
        ledger: LocalOperationsLedger,
        config: Optional[Dict[str, Any]] = None,
        readiness: Optional[Any] = None,
        intake_paths: Optional[Iterable[str]] = None,
        intake_extensions: Optional[Iterable[str]] = None,
        connector_fetcher_factories: Optional[Dict[str, Callable[[Dict[str, Any]], Any]]] = None,
    ):
        self.ledger = ledger
        self.config = dict(config or {})
        self.readiness = readiness
        self.intake_paths = list(intake_paths or [])
        self.intake_extensions = list(intake_extensions or [])
        self.connector_fetcher_factories = connector_fetcher_factories

    def plan(self, workflow_run_id: int) -> Dict[str, Any]:
        workflow_run = self.ledger.get_workflow_run_with_steps(int(workflow_run_id))
        if workflow_run is None:
            return self._unavailable_plan(workflow_run_id, "not_found", "Workflow run not found.")

        child = self._recovery_child(int(workflow_run_id))
        if child:
            return {
                **self._base_plan(workflow_run),
                "status": "superseded",
                "canRetry": False,
                "supersededByWorkflowRunId": child.get("id"),
                "nextAction": f"Inspect recovery workflow #{child.get('id')}.",
            }

        if workflow_run.get("status") not in RECOVERABLE_RUN_STATUSES:
            return {
                **self._base_plan(workflow_run),
                "status": "not_retryable",
                "canRetry": False,
                "nextAction": "Only failed or attention-required workflow runs can be retried.",
            }

        trigger_source = str(workflow_run.get("trigger_source") or "")
        if trigger_source in CONNECTOR_TRIGGERS:
            return self._connector_plan(workflow_run)
        if trigger_source in AUTONOMY_TRIGGERS:
            return self._autonomy_plan(workflow_run)
        return {
            **self._base_plan(workflow_run),
            "status": "unsupported_trigger",
            "canRetry": False,
            "nextAction": f"Workflow trigger {trigger_source or 'unknown'} has no governed retry contract.",
        }

    def retry(
        self,
        workflow_run_id: int,
        actor: str = "local_workflow_recovery",
        limit: int = 25,
    ) -> Dict[str, Any]:
        recovery_plan = self.plan(workflow_run_id)
        if not recovery_plan.get("canRetry"):
            return self._not_started_result(workflow_run_id, recovery_plan)

        lease_name = f"workflow_recovery:{int(workflow_run_id)}"
        owner_token = uuid4().hex
        lease = self.ledger.acquire_runtime_lease(
            lease_name,
            owner_token,
            ttl_seconds=1800,
            metadata={"workflowRunId": int(workflow_run_id), "actor": actor},
        )
        if not lease.get("acquired"):
            return {
                **self._not_started_result(workflow_run_id, recovery_plan),
                "status": "already_running",
                "runtimeLease": lease.get("lease"),
            }

        try:
            recovery_plan = self.plan(workflow_run_id)
            if not recovery_plan.get("canRetry"):
                result = self._not_started_result(workflow_run_id, recovery_plan)
            else:
                self.ledger.record_audit_event({
                    "action": "local_workflow_recovery.started",
                    "entityType": "workflow_run",
                    "entityId": str(workflow_run_id),
                    "details": {
                        "actor": actor,
                        "recoveryType": recovery_plan.get("recoveryType"),
                        "selectedStepKeys": recovery_plan.get("selectedStepKeys") or [],
                        "continuationStepKeys": recovery_plan.get("continuationStepKeys") or [],
                        "nextAttempt": recovery_plan.get("nextAttempt"),
                        "externalSubmission": "not_executed",
                    },
                })
                if recovery_plan.get("recoveryType") == "connector_intake":
                    result = self._retry_connector(recovery_plan, actor)
                else:
                    result = self._retry_autonomy(recovery_plan, actor, limit)

                self.ledger.record_audit_event({
                    "action": "local_workflow_recovery.completed",
                    "entityType": "workflow_run",
                    "entityId": str(result.get("workflowRunId") or workflow_run_id),
                    "details": {
                        "actor": actor,
                        "success": result.get("success"),
                        "status": result.get("status"),
                        "sourceWorkflowRunId": int(workflow_run_id),
                        "recoveryType": recovery_plan.get("recoveryType"),
                        "selectedStepKeys": recovery_plan.get("selectedStepKeys") or [],
                        "continuationStepKeys": recovery_plan.get("continuationStepKeys") or [],
                        "externalSubmission": "not_executed",
                    },
                })
        finally:
            released = self.ledger.release_runtime_lease(lease_name, owner_token)
        result.setdefault("plan", recovery_plan)
        result["runtimeLease"] = {"name": lease_name, "released": released}
        return result

    def _connector_plan(self, workflow_run: Dict[str, Any]) -> Dict[str, Any]:
        source_steps = []
        for step in workflow_run.get("steps") or []:
            step_key = str(step.get("step_key") or "")
            source = step_key.partition(":")[2] if step_key.startswith("source:") else ""
            if source not in CONNECTOR_SOURCES or step.get("status") not in RECOVERABLE_STEP_STATUSES:
                continue
            source_steps.append((source, step))
        if not source_steps:
            return {
                **self._base_plan(workflow_run),
                "status": "no_failed_sources",
                "canRetry": False,
                "recoveryType": "connector_intake",
                "nextAction": "No failed connector source remains on this workflow run.",
            }

        current_sources = {
            item["source"]: item
            for item in LocalConnectorIntakeService(
                self.ledger,
                self.config,
                fetcher_factories=self.connector_fetcher_factories,
            ).plan()["sources"]
        }
        unresolved_sources = [source for source, _step in source_steps]
        ready_sources = [
            source
            for source in unresolved_sources
            if (current_sources.get(source) or {}).get("canSync")
        ]
        blocked_sources = [
            {
                "source": source,
                "status": (current_sources.get(source) or {}).get("status") or "unknown",
                "nextAction": (current_sources.get(source) or {}).get("nextAction"),
            }
            for source in unresolved_sources
            if source not in ready_sources
        ]
        attempts = {
            source: max(int(step.get("attempt") or 1) + 1, 2)
            for source, step in source_steps
            if source in ready_sources
        }
        can_retry = bool(ready_sources)
        return {
            **self._base_plan(workflow_run),
            "status": "ready" if can_retry else "waiting_for_configuration",
            "canRetry": can_retry,
            "recoveryType": "connector_intake",
            "strategy": "failed_sources_only",
            "unresolvedSources": unresolved_sources,
            "retrySources": ready_sources,
            "readySources": ready_sources,
            "blockedSources": blocked_sources,
            "selectedStepKeys": [f"source:{source}" for source in ready_sources],
            "stepAttempts": attempts,
            "nextAttempt": max(attempts.values()) if attempts else None,
            "nextAction": (
                "Retry the failed read-only connector sources."
                if can_retry
                else "Resolve connector configuration before retrying this run."
            ),
        }

    def _autonomy_plan(self, workflow_run: Dict[str, Any]) -> Dict[str, Any]:
        execution_steps = [
            step
            for step in workflow_run.get("steps") or []
            if step.get("step_key") in AUTONOMY_EXECUTION_ORDER
            or step.get("step_key") in HISTORICAL_HIGH_RISK_AUTONOMY_ACTIONS
        ]
        failed_step = next(
            (
                step
                for status in ("failed", "blocked", "skipped")
                for step in execution_steps
                if step.get("status") == status
            ),
            None,
        )
        if failed_step is None:
            return {
                **self._base_plan(workflow_run),
                "status": "no_failed_step",
                "canRetry": False,
                "recoveryType": "local_autonomy",
                "nextAction": "No failed autonomous step remains on this workflow run.",
            }

        action_id = str(failed_step.get("step_key"))
        metadata = failed_step.get("metadata") or {}
        risk = str(metadata.get("risk") or "unknown")
        mode = str(metadata.get("mode") or "unknown")
        safe = risk == "low" and mode in SAFE_AUTONOMY_MODES and action_id != "execute_approved_exports"
        if not safe:
            return {
                **self._base_plan(workflow_run),
                "status": "approval_required",
                "canRetry": False,
                "recoveryType": "local_autonomy",
                "retryActionId": action_id,
                "selectedStepKeys": [action_id],
                "excludedActions": [{"id": action_id, "risk": risk, "mode": mode}],
                "nextAction": "Open the original evidence and use the action-specific approval workflow.",
            }

        autonomy = self._autonomy_service()
        current_plan = autonomy.plan(include_wave_plan=True, include_wave_sync=True)
        current_actions = {
            str(action.get("id")): action
            for action in current_plan.get("actions") or []
            if action.get("id")
        }
        current_action = next(
            (action for action in current_plan.get("actions") or [] if action.get("id") == action_id),
            {},
        )
        current_risk = str(current_action.get("risk") or "unknown")
        current_mode = str(current_action.get("mode") or "unknown")
        if current_risk != "low" or current_mode not in SAFE_AUTONOMY_MODES:
            return {
                **self._base_plan(workflow_run),
                "status": "approval_required",
                "canRetry": False,
                "recoveryType": "local_autonomy",
                "retryActionId": action_id,
                "selectedStepKeys": [action_id],
                "excludedActions": [{
                    "id": action_id,
                    "risk": current_risk,
                    "mode": current_mode,
                    "reason": "The current autonomy policy no longer classifies this step as low risk.",
                }],
                "nextAction": "Open the original evidence and use the action-specific approval workflow.",
            }
        can_run = bool(current_action.get("canRun"))
        if action_id == "process_imported":
            can_run = can_run or bool(self.ledger.list_documents(status="failed", limit=1))
        attempt = max(int(failed_step.get("attempt") or 1) + 1, 2)
        continuation_steps, dependency_edges, excluded_continuations = self._autonomy_continuation_steps(
            workflow_run,
            failed_step,
            current_actions,
        )
        continuation_keys = [str(step["step_key"]) for step in continuation_steps]
        selected_step_keys = [action_id, *continuation_keys]
        step_attempts = {action_id: attempt}
        step_attempts.update({
            str(step["step_key"]): max(1, int(step.get("attempt") or 1))
            for step in continuation_steps
        })
        return {
            **self._base_plan(workflow_run),
            "status": "ready" if can_run else "waiting_for_precondition",
            "canRetry": can_run,
            "recoveryType": "local_autonomy",
            "strategy": "dependency_safe_resume",
            "retryActionId": action_id,
            "continuationStepKeys": continuation_keys,
            "selectedStepKeys": selected_step_keys,
            "dependencyEdges": dependency_edges,
            "stepAttempts": step_attempts,
            "nextAttempt": attempt,
            "currentAction": {
                "id": action_id,
                "canRun": can_run,
                "blockedReason": current_action.get("blockedReason"),
                "risk": risk,
                "mode": mode,
            },
            "excludedActions": [
                {
                    "id": "execute_approved_exports",
                    "risk": "high",
                    "reason": "External execution is never replayed by workflow recovery.",
                },
                *excluded_continuations,
            ],
            "nextAction": (
                (
                    f"Retry {action_id}, then resume {len(continuation_keys)} dependency-safe local step(s)."
                    if continuation_keys
                    else f"Retry autonomous step {action_id}; no dependency-safe continuation is pending."
                )
                if can_run
                else current_action.get("blockedReason") or "Resolve the failed step precondition before retrying."
            ),
        }

    def _autonomy_continuation_steps(
        self,
        workflow_run: Dict[str, Any],
        failed_step: Dict[str, Any],
        current_actions: Dict[str, Dict[str, Any]],
    ) -> tuple[list[Dict[str, Any]], list[Dict[str, str]], list[Dict[str, str]]]:
        failed_action_id = str(failed_step.get("step_key") or "")
        source_steps = {
            str(step.get("step_key")): step
            for step in workflow_run.get("steps") or []
            if step.get("step_key") in AUTONOMY_EXECUTION_ORDER
        }
        selected = {failed_action_id}
        frontier = [failed_action_id]
        edges: list[Dict[str, str]] = []
        excluded: list[Dict[str, str]] = []
        while frontier:
            parent = frontier.pop(0)
            for child in AUTONOMY_RECOVERY_DEPENDENCIES.get(parent, ()):
                if child in selected:
                    continue
                source_step = source_steps.get(child)
                source_metadata = (
                    source_step.get("metadata")
                    if isinstance((source_step or {}).get("metadata"), dict)
                    else {}
                )
                if (
                    not source_step
                    or source_step.get("status") != "not_run"
                    or source_metadata.get("reason") != "cycle_aborted_after_step_failure"
                    or str(source_metadata.get("failedAfterStep") or "") != failed_action_id
                ):
                    continue
                current_action = current_actions.get(child) or {}
                source_risk = str(source_metadata.get("risk") or "unknown")
                source_mode = str(source_metadata.get("mode") or "unknown")
                current_risk = str(current_action.get("risk") or "unknown")
                current_mode = str(current_action.get("mode") or "unknown")
                safe = (
                    child != "execute_approved_exports"
                    and source_risk == "low"
                    and source_mode in SAFE_AUTONOMY_MODES
                    and current_risk == "low"
                    and current_mode in SAFE_AUTONOMY_MODES
                )
                if not safe:
                    excluded.append({
                        "id": child,
                        "risk": current_risk,
                        "mode": current_mode,
                        "reason": "Continuation is not low-risk under both source and current policy.",
                    })
                    continue
                selected.add(child)
                frontier.append(child)
                edges.append({"from": parent, "to": child})

        ordered_steps = [
            source_steps[action_id]
            for action_id in AUTONOMY_EXECUTION_ORDER
            if action_id in selected and action_id != failed_action_id
        ]
        edges.sort(key=lambda edge: AUTONOMY_EXECUTION_ORDER.index(edge["to"]))
        return ordered_steps, edges, excluded

    def _retry_connector(self, recovery_plan: Dict[str, Any], actor: str) -> Dict[str, Any]:
        source_workflow_run_id = int(recovery_plan["workflowRunId"])
        recovery_metadata = self._recovery_metadata(recovery_plan)
        execution = LocalConnectorIntakeService(
            self.ledger,
            self.config,
            fetcher_factories=self.connector_fetcher_factories,
        ).sync(
            sources=recovery_plan["retrySources"],
            actor=actor,
            trigger_source=CONNECTOR_RECOVERY_TRIGGER,
            workflow_metadata={"recovery": recovery_metadata},
            step_attempts=recovery_plan["stepAttempts"],
        )
        new_run_id = execution.get("workflowRunId")
        detail = self.ledger.get_workflow_run_with_steps(int(new_run_id)) if new_run_id else None
        all_completed = bool(detail and detail.get("steps")) and all(
            step.get("status") == "completed"
            for step in detail.get("steps") or []
        )
        return {
            "success": all_completed,
            "status": "completed" if all_completed else execution.get("status"),
            "workflowRunId": new_run_id,
            "sourceWorkflowRunId": source_workflow_run_id,
            "recoveryType": "connector_intake",
            "selectedStepKeys": recovery_plan["selectedStepKeys"],
            "execution": execution,
            "externalSubmission": "not_executed",
        }

    def _retry_autonomy(
        self,
        recovery_plan: Dict[str, Any],
        actor: str,
        limit: int,
    ) -> Dict[str, Any]:
        source_workflow_run_id = int(recovery_plan["workflowRunId"])
        action_id = str(recovery_plan["retryActionId"])
        continuation_action_ids = list(recovery_plan.get("continuationStepKeys") or [])
        recovery_metadata = self._recovery_metadata(recovery_plan)
        recovery_metadata["actor"] = actor
        execution = self._autonomy_service().run_cycle(
            limit=limit,
            include_wave_plan=action_id == "plan_wave_daily_reconciliation",
            include_wave_sync=action_id == "refresh_wave_entity_mirror",
            allowed_action_ids=list(recovery_plan["selectedStepKeys"]),
            trigger_source=AUTONOMY_RECOVERY_TRIGGER,
            workflow_metadata={"recovery": recovery_metadata},
            step_attempts=recovery_plan["stepAttempts"],
            recovery_mode=True,
            recovery_action_id=action_id,
            recovery_continuation_action_ids=continuation_action_ids,
        )
        new_run_id = execution.get("workflowRunId")
        detail = self.ledger.get_workflow_run_with_steps(int(new_run_id)) if new_run_id else None
        steps_by_key = {
            str(step.get("step_key")): step
            for step in (detail or {}).get("steps") or []
        }
        retried_step = steps_by_key.get(action_id)
        blocking_steps = [
            step
            for step in steps_by_key.values()
            if step.get("status") in {"failed", "blocked", "not_run"}
        ]
        completed = bool(
            detail
            and retried_step
            and retried_step.get("status") == "completed"
            and not blocking_steps
            and detail.get("status") == "completed"
        )
        if detail and not completed and detail.get("status") == "completed":
            self.ledger.update_workflow_run(int(new_run_id), {"status": "completed_with_errors"})
            detail["status"] = "completed_with_errors"
        return {
            "success": completed,
            "status": "completed" if completed else (detail or {}).get("status") or execution.get("status"),
            "workflowRunId": new_run_id,
            "sourceWorkflowRunId": source_workflow_run_id,
            "recoveryType": "local_autonomy",
            "selectedStepKeys": recovery_plan["selectedStepKeys"],
            "continuationStepKeys": continuation_action_ids,
            "execution": execution,
            "externalSubmission": "not_executed",
        }

    def _autonomy_service(self) -> LocalAutonomousService:
        recovery_config = dict(self.config)
        recovery_config["fab_autonomy_execute_approved_exports"] = False
        recovery_config["fab_autonomy_ignore_health_blocks"] = True
        return LocalAutonomousService(
            self.ledger,
            recovery_config,
            readiness=self.readiness,
            intake_paths=self.intake_paths,
            intake_extensions=self.intake_extensions,
        )

    def _recovery_child(self, workflow_run_id: int) -> Optional[Dict[str, Any]]:
        return self.ledger.get_workflow_recovery_child(int(workflow_run_id))

    def _recovery_metadata(self, recovery_plan: Dict[str, Any]) -> Dict[str, Any]:
        source_run = self.ledger.get_workflow_run(int(recovery_plan["workflowRunId"])) or {}
        prior_recovery = (source_run.get("metadata") or {}).get("recovery") or {}
        root_workflow_run_id = int(
            prior_recovery.get("rootWorkflowRunId")
            or recovery_plan["workflowRunId"]
        )
        return {
            "sourceWorkflowRunId": int(recovery_plan["workflowRunId"]),
            "rootWorkflowRunId": root_workflow_run_id,
            "retryDepth": int(prior_recovery.get("retryDepth") or 0) + 1,
            "strategy": recovery_plan.get("strategy"),
            "selectedStepKeys": recovery_plan.get("selectedStepKeys") or [],
            "retryActionId": recovery_plan.get("retryActionId"),
            "continuationStepKeys": recovery_plan.get("continuationStepKeys") or [],
            "dependencyEdges": recovery_plan.get("dependencyEdges") or [],
            "externalSubmission": "not_executed",
        }

    @staticmethod
    def _base_plan(workflow_run: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "workflowRunId": int(workflow_run["id"]),
            "triggerSource": workflow_run.get("trigger_source"),
            "workflowStatus": workflow_run.get("status"),
            "externalSubmission": "not_executed",
        }

    @staticmethod
    def _unavailable_plan(workflow_run_id: int, status: str, next_action: str) -> Dict[str, Any]:
        return {
            "workflowRunId": int(workflow_run_id),
            "status": status,
            "canRetry": False,
            "nextAction": next_action,
            "externalSubmission": "not_executed",
        }

    @staticmethod
    def _not_started_result(workflow_run_id: int, recovery_plan: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "success": False,
            "status": recovery_plan.get("status"),
            "workflowRunId": None,
            "sourceWorkflowRunId": int(workflow_run_id),
            "plan": recovery_plan,
            "externalSubmission": "not_executed",
        }


class LocalWorkflowRecoveryScheduler:
    """Find and retry due governed workflow failures with bounded backoff."""

    def __init__(
        self,
        ledger: LocalOperationsLedger,
        config: Optional[Dict[str, Any]] = None,
        readiness: Optional[Any] = None,
        intake_paths: Optional[Iterable[str]] = None,
        intake_extensions: Optional[Iterable[str]] = None,
        connector_fetcher_factories: Optional[Dict[str, Callable[[Dict[str, Any]], Any]]] = None,
    ):
        self.ledger = ledger
        self.config = dict(config or {})
        self.readiness = readiness
        self.intake_paths = list(intake_paths or [])
        self.intake_extensions = list(intake_extensions or [])
        self.connector_fetcher_factories = connector_fetcher_factories
        self.max_retries = _positive_int_config(
            self.config,
            "fab_workflow_recovery_max_retries",
            "operations_workflow_recovery_max_retries",
            "workflow_recovery_max_retries",
            default=3,
            maximum=20,
        )
        self.base_delay_seconds = _nonnegative_float_config(
            self.config,
            "fab_workflow_recovery_base_delay_seconds",
            "operations_workflow_recovery_base_delay_seconds",
            "workflow_recovery_base_delay_seconds",
            default=300.0,
        )
        self.max_delay_seconds = max(
            self.base_delay_seconds,
            _nonnegative_float_config(
                self.config,
                "fab_workflow_recovery_max_delay_seconds",
                "operations_workflow_recovery_max_delay_seconds",
                "workflow_recovery_max_delay_seconds",
                default=3600.0,
            ),
        )
        self.stale_after_seconds = _nonnegative_float_config(
            self.config,
            "fab_workflow_recovery_stale_seconds",
            "operations_workflow_recovery_stale_seconds",
            "workflow_recovery_stale_seconds",
            default=DEFAULT_INTERRUPTED_RUN_GRACE_SECONDS,
        )

    def plan(
        self,
        limit: int = 100,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        now_value = _as_utc(now or datetime.now(timezone.utc))
        recovery = self._recovery_service()
        runs = self.ledger.list_workflow_runs(
            status=tuple(RECOVERABLE_RUN_STATUSES),
            limit=max(1, min(int(limit), 500)),
        )
        candidates = []
        held_back_sources = set()
        for run in runs:
            if str(run.get("trigger_source") or "") not in AUTONOMY_TRIGGERS | CONNECTOR_TRIGGERS:
                continue
            recovery_plan = recovery.plan(int(run["id"]))
            if recovery_plan.get("status") == "superseded":
                continue
            metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
            recovery_metadata = (
                metadata.get("recovery")
                if isinstance(metadata.get("recovery"), dict)
                else {}
            )
            retry_depth = _nonnegative_int(recovery_metadata.get("retryDepth"), default=0)
            delay_seconds = min(
                self.max_delay_seconds,
                self.base_delay_seconds * (2 ** min(retry_depth, 30)),
            )
            reference_at = _parse_datetime(
                run.get("finished_at") or run.get("updated_at") or run.get("started_at")
            ) or now_value
            eligible_at = reference_at + timedelta(seconds=delay_seconds)
            exhausted = retry_depth >= self.max_retries
            due = now_value >= eligible_at
            can_run = bool(recovery_plan.get("canRetry")) and due and not exhausted
            if exhausted:
                status = "exhausted"
                next_action = "Inspect the failure and resolve it manually; automatic retry limit reached."
            elif recovery_plan.get("canRetry") and not due:
                status = "deferred"
                next_action = f"Automatic safe retry is deferred until {eligible_at.isoformat()}."
            elif can_run:
                status = "due"
                next_action = recovery_plan.get("nextAction")
            else:
                status = recovery_plan.get("status") or "not_retryable"
                next_action = recovery_plan.get("nextAction")
            if recovery_plan.get("recoveryType") == "connector_intake":
                held_back_sources.update(recovery_plan.get("unresolvedSources") or [])
            candidates.append({
                "workflowRunId": int(run["id"]),
                "triggerSource": run.get("trigger_source"),
                "workflowStatus": run.get("status"),
                "recoveryType": recovery_plan.get("recoveryType"),
                "status": status,
                "canRun": can_run,
                "retryDepth": retry_depth,
                "maxRetries": self.max_retries,
                "delaySeconds": delay_seconds,
                "eligibleAt": eligible_at.isoformat(),
                "selectedStepKeys": recovery_plan.get("selectedStepKeys") or [],
                "retrySources": recovery_plan.get("retrySources") or [],
                "nextAction": next_action,
                "recoveryPlan": recovery_plan,
                "externalSubmission": "not_executed",
            })

        candidates.sort(key=lambda item: (item.get("eligibleAt") or "", item["workflowRunId"]))
        status_counts: Dict[str, int] = {}
        for candidate in candidates:
            key = str(candidate.get("status") or "unknown")
            status_counts[key] = status_counts.get(key, 0) + 1
        due_count = status_counts.get("due", 0)
        return {
            "status": "due" if due_count else "idle",
            "dueCount": due_count,
            "candidateCount": len(candidates),
            "statusCounts": status_counts,
            "connectorSourcesHeldBack": sorted(held_back_sources),
            "candidates": candidates,
            "policy": {
                "maxRetries": self.max_retries,
                "baseDelaySeconds": self.base_delay_seconds,
                "maxDelaySeconds": self.max_delay_seconds,
                "staleAfterSeconds": self.stale_after_seconds,
                "externalActionsAllowed": False,
            },
            "runtimeLease": self.ledger.get_runtime_lease(
                WORKFLOW_RECOVERY_SCHEDULER_LEASE_NAME
            ),
            "externalSubmission": "not_executed",
        }

    def run_due(
        self,
        actor: str = "local_workflow_recovery_scheduler",
        limit: int = 5,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        now_value = _as_utc(now or datetime.now(timezone.utc))
        owner_token = uuid4().hex
        lease = self.ledger.acquire_runtime_lease(
            WORKFLOW_RECOVERY_SCHEDULER_LEASE_NAME,
            owner_token,
            ttl_seconds=_nonnegative_float_config(
                self.config,
                "fab_workflow_recovery_lease_seconds",
                "operations_workflow_recovery_lease_seconds",
                "workflow_recovery_lease_seconds",
                default=1800.0,
            ) or 1800.0,
            metadata={"actor": actor},
        )
        if not lease.get("acquired"):
            return {
                "success": False,
                "status": "already_running",
                "attempted": 0,
                "recoveries": [],
                "interruptedWorkflowRunIds": [],
                "runtimeLease": lease.get("lease"),
                "externalSubmission": "not_executed",
            }

        result = None
        try:
            interrupted = self._finalize_interrupted_runs(now_value)
            recovery_plan = self.plan(limit=500, now=now_value)
            due = [
                candidate
                for candidate in recovery_plan.get("candidates") or []
                if candidate.get("canRun")
            ][:_positive_int(limit, default=5, maximum=50)]
            recoveries = []
            service = self._recovery_service()
            for candidate in due:
                try:
                    attempt = service.retry(
                        int(candidate["workflowRunId"]),
                        actor=actor,
                    )
                except Exception as exc:
                    attempt = {
                        "success": False,
                        "status": "failed",
                        "workflowRunId": None,
                        "sourceWorkflowRunId": int(candidate["workflowRunId"]),
                        "error": _safe_recovery_error(exc, self.config),
                        "externalSubmission": "not_executed",
                    }
                recoveries.append({
                    "candidate": {
                        key: candidate.get(key)
                        for key in (
                            "workflowRunId",
                            "recoveryType",
                            "retryDepth",
                            "selectedStepKeys",
                            "retrySources",
                        )
                    },
                    "result": attempt,
                })
            success = all(item["result"].get("success") for item in recoveries)
            status = (
                "no_recovery_due"
                if not recoveries
                else "completed" if success else "completed_with_errors"
            )
            result = {
                "success": success,
                "status": status,
                "attempted": len(recoveries),
                "succeeded": sum(1 for item in recoveries if item["result"].get("success")),
                "failed": sum(1 for item in recoveries if not item["result"].get("success")),
                "recoveries": recoveries,
                "interruptedWorkflowRunIds": interrupted,
                "connectorSourcesHeldBack": recovery_plan.get("connectorSourcesHeldBack") or [],
                "plan": recovery_plan,
                "externalSubmission": "not_executed",
            }
            self.ledger.record_audit_event({
                "action": "local_workflow_recovery.scheduler_completed",
                "entityType": "workflow_recovery_scheduler",
                "entityId": "local",
                "details": {
                    "actor": actor,
                    "status": status,
                    "attempted": result["attempted"],
                    "succeeded": result["succeeded"],
                    "failed": result["failed"],
                    "interruptedWorkflowRunIds": interrupted,
                    "connectorSourcesHeldBack": result["connectorSourcesHeldBack"],
                    "externalSubmission": "not_executed",
                },
            })
            return result
        finally:
            released = self.ledger.release_runtime_lease(
                WORKFLOW_RECOVERY_SCHEDULER_LEASE_NAME,
                owner_token,
            )
            if isinstance(result, dict):
                result["runtimeLease"] = {
                    **(lease.get("lease") or {}),
                    "active": False if released else (lease.get("lease") or {}).get("active"),
                    "released": released,
                }

    def _finalize_interrupted_runs(self, now: datetime) -> list:
        if self.stale_after_seconds <= 0:
            return []
        finalized = []
        runs = self.ledger.list_workflow_runs(status="running", limit=500)
        for run in runs:
            trigger = str(run.get("trigger_source") or "")
            if trigger not in AUTONOMY_TRIGGERS | CONNECTOR_TRIGGERS:
                continue
            updated_at = _parse_datetime(
                run.get("updated_at") or run.get("started_at") or run.get("created_at")
            )
            if updated_at is None or (now - updated_at).total_seconds() < self.stale_after_seconds:
                continue
            lease_name = (
                AUTONOMY_LEASE_NAME
                if trigger in AUTONOMY_TRIGGERS
                else CONNECTOR_INTAKE_LEASE_NAME
            )
            runtime_lease = self.ledger.get_runtime_lease(lease_name)
            if runtime_lease and runtime_lease.get("active"):
                continue
            workflow_run_id = int(run["id"])
            for step in self.ledger.list_workflow_steps(
                workflow_run_id=workflow_run_id,
                limit=500,
            ):
                if step.get("status") == "running":
                    self.ledger.update_workflow_step(int(step["id"]), {
                        "status": "failed",
                        "finishedAt": now.isoformat(),
                        "errorMessage": "Execution was interrupted before the workflow completed.",
                    })
                elif step.get("status") == "pending":
                    self.ledger.update_workflow_step(int(step["id"]), {
                        "status": "not_run",
                        "finishedAt": now.isoformat(),
                        "durationMs": 0,
                    })
            self.ledger.update_workflow_run(workflow_run_id, {
                "status": "failed",
                "finishedAt": now.isoformat(),
                "errorMessage": "Execution was interrupted and is eligible for governed recovery.",
            })
            self.ledger.record_audit_event({
                "action": "local_workflow_recovery.interrupted_run_finalized",
                "entityType": "workflow_run",
                "entityId": str(workflow_run_id),
                "details": {
                    "triggerSource": trigger,
                    "inactiveLeaseName": lease_name,
                    "externalSubmission": "not_executed",
                },
            })
            finalized.append(workflow_run_id)
        return finalized

    def _recovery_service(self) -> LocalWorkflowRecoveryService:
        return LocalWorkflowRecoveryService(
            self.ledger,
            self.config,
            readiness=self.readiness,
            intake_paths=self.intake_paths,
            intake_extensions=self.intake_extensions,
            connector_fetcher_factories=self.connector_fetcher_factories,
        )


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _positive_int_config(
    config: Dict[str, Any],
    *keys: str,
    default: int,
    maximum: int,
) -> int:
    value = next((config.get(key) for key in keys if config.get(key) not in (None, "")), default)
    return _positive_int(value, default=default, maximum=maximum)


def _positive_int(value: Any, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def _nonnegative_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0, parsed)


def _nonnegative_float_config(
    config: Dict[str, Any],
    *keys: str,
    default: float,
) -> float:
    value = next((config.get(key) for key in keys if config.get(key) not in (None, "")), default)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, parsed)


def _safe_recovery_error(error: Any, config: Dict[str, Any]) -> str:
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
