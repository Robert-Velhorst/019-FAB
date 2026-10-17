from typing import Any, Dict, List, Optional

from src.data_entry.mijngeldzaken_handler import MijngeldzakenHandler
from src.data_entry.posting_approval import PostingApprovalService
from src.data_entry.waveapps_business_handler import WaveappsBusinessHandler
from src.data_entry.waveapps_personal_handler import WaveappsPersonalHandler
from src.queue.retry_manager import RetryManager
from src.storage.database import Database


class PostingExecutor:
    """Executes approved posting attempts against configured bookkeeping handlers."""

    DEFERRED_HANDLER_STATUSES = {"rate_limited", "quota_exhausted"}
    SUPERVISED_HANDLER_STATUSES = {"supervised_action_required"}

    def __init__(self, config: Dict[str, Any], handlers: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.database = Database(config)
        self.approval_service = PostingApprovalService(config)
        self.retry_manager = RetryManager(config)
        self.retry_manager.ensure_schema()
        self.execute_approved_postings = bool(self.config.get("execute_approved_postings", False))
        self.handlers = handlers or {
            "mijngeldzaken": MijngeldzakenHandler(config),
            "waveapps_business": WaveappsBusinessHandler(config),
            "waveapps_personal": WaveappsPersonalHandler(config),
        }

    def process_approved_attempts(self, force: bool = False, limit: int = 20) -> Dict[str, Any]:
        if not self.execute_approved_postings and not force:
            return {"status": "skipped", "reason": "execute_approved_postings is disabled", "processed": []}

        attempts = self.database.fetch_all(
            "SELECT * FROM posting_attempts WHERE status = 'approved' ORDER BY updated_at ASC LIMIT ?",
            (limit,),
        )
        processed: List[Dict[str, Any]] = []
        for attempt in attempts:
            processed.append(self.execute_attempt(int(attempt["id"]), force=True))
        return {"status": "completed", "processed": processed, "count": len(processed)}

    def process_due_retries(self, limit: int = 20) -> Dict[str, Any]:
        due = self.retry_manager.due_items()[:limit]
        processed = []
        for item in due:
            if item.get("entity_type") == "posting_attempt" and item.get("operation") == "execute_posting":
                processed.append(self.execute_attempt(int(item["entity_id"]), force=True))
        return {"status": "completed", "processed": processed, "count": len(processed)}

    def execute_attempt(self, attempt_id: int, force: bool = False) -> Dict[str, Any]:
        if not self.execute_approved_postings and not force:
            return {"status": "skipped", "reason": "execute_approved_postings is disabled", "attempt_id": attempt_id}

        claimed = self._claim_attempt(attempt_id, force=force)
        if claimed.get("status") != "claimed":
            return claimed

        attempt = claimed["attempt"]
        payload = self.approval_service.payload_for_attempt(attempt_id) or {}
        target_system = payload.get("target_system") or attempt.get("target_system")
        handler = self.handlers.get(target_system)
        if not handler:
            result = {"status": "failure", "message": f"No handler configured for target system: {target_system}"}
            self._handle_failure(attempt_id, payload, result)
            return {"attempt_id": attempt_id, **result}

        categorized_data = self._payload_to_categorized_data(payload, attempt)
        self.database.add_audit_log(
            "posting_attempt",
            str(attempt_id),
            "execution_started",
            attempt,
            {"target_system": target_system, "document_id": categorized_data.get("document_id")},
            "Approved posting execution started",
            "system",
        )

        try:
            result = handler.enter_data(categorized_data)
        except Exception as exc:
            result = {"status": "failure", "message": str(exc), "requires_manual_review": True}

        if result.get("status") == "success":
            external_id = result.get("external_id") or result.get("id")
            self.approval_service.mark_executed(attempt_id, external_id=external_id, result=result)
            self.retry_manager.mark_complete("posting_attempt", str(attempt_id), "execute_posting")
            self._mark_document_posted(categorized_data.get("document_id"), result)
            return {"attempt_id": attempt_id, "status": "posted", "result": result}

        if result.get("status") in self.DEFERRED_HANDLER_STATUSES:
            return self._defer_attempt(attempt_id, payload, categorized_data, result)

        if result.get("status") in self.SUPERVISED_HANDLER_STATUSES:
            return self._pause_for_supervision(attempt_id, categorized_data, result)

        if result.get("status") == "outcome_unknown":
            return self._record_unknown_outcome(attempt_id, categorized_data, result)

        self._handle_failure(attempt_id, payload, result)
        document_id = categorized_data.get("document_id")
        if document_id:
            self.database.add_manual_review_item(document_id, "posting_execution_failed", str(result), severity="high")
        return {"attempt_id": attempt_id, "status": "posting_failed", "result": result}

    def _claim_attempt(self, attempt_id: int, force: bool = False) -> Dict[str, Any]:
        with self.database.connect() as connection:
            attempt = connection.execute("SELECT * FROM posting_attempts WHERE id = ?", (attempt_id,)).fetchone()
            if not attempt:
                return {"status": "not_found", "attempt_id": attempt_id}
            allowed_statuses = {"approved"}
            if force:
                allowed_statuses.update({"posting_failed", "posting_in_progress", "posting_deferred", "supervision_required"})
            if attempt["status"] not in allowed_statuses:
                return {"status": "not_approved", "attempt_id": attempt_id, "current_status": attempt["status"]}
            cursor = connection.execute(
                "UPDATE posting_attempts SET status = 'posting_in_progress', updated_at = ? WHERE id = ? AND status = ?",
                (self.database.now(), attempt_id, attempt["status"]),
            )
            if cursor.rowcount != 1:
                return {"status": "already_claimed", "attempt_id": attempt_id}
            return {"status": "claimed", "attempt": dict(attempt)}

    def _defer_attempt(
        self,
        attempt_id: int,
        payload: Dict[str, Any],
        categorized_data: Dict[str, Any],
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        reason = str(result.get("message") or "Downstream rate limit is temporarily unavailable.")
        retry_after_seconds = _positive_int(
            result.get("retry_after_seconds"),
            default=_positive_int(self.config.get("rate_limit_retry_delay_seconds"), default=60),
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE posting_attempts SET status = 'posting_deferred', updated_at = ? WHERE id = ?",
                (self.database.now(), attempt_id),
            )
        retry = self.retry_manager.defer_retry(
            "posting_attempt",
            str(attempt_id),
            "execute_posting",
            reason,
            payload,
            delay_seconds=retry_after_seconds,
        )
        details = {
            "target_system": categorized_data.get("target_system"),
            "document_id": categorized_data.get("document_id"),
            "provider_status": result.get("status"),
            "retry_after_seconds": retry_after_seconds,
            "next_retry_at": retry.get("next_retry_at"),
            "rate_limit": result.get("rate_limit"),
        }
        self.database.add_audit_log(
            "posting_attempt",
            str(attempt_id),
            "execution_deferred_rate_limit",
            None,
            details,
            reason,
            "system",
        )
        return {
            "attempt_id": attempt_id,
            "status": "posting_deferred",
            "defer_reason": result.get("status"),
            "retry": retry,
            "result": result,
        }

    def _pause_for_supervision(
        self,
        attempt_id: int,
        categorized_data: Dict[str, Any],
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        reason = str(result.get("message") or "External submission requires a supervised user session.")
        public_result = _supervision_result(result)
        document_id = categorized_data.get("document_id")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE posting_attempts SET status = 'supervision_required', updated_at = ? WHERE id = ?",
                (self.database.now(), attempt_id),
            )
            if document_id:
                connection.execute(
                    "UPDATE documents SET current_state = 'awaiting_supervised_submission', updated_at = ? WHERE id = ?",
                    (self.database.now(), document_id),
                )

        details = {
            "target_system": categorized_data.get("target_system"),
            "document_id": document_id,
            "result": public_result,
        }
        self.database.add_audit_log(
            "posting_attempt",
            str(attempt_id),
            "execution_paused_for_supervision",
            None,
            details,
            reason,
            "system",
        )
        if document_id and not self.database.fetch_one(
            "SELECT id FROM manual_review_items WHERE document_id = ? AND reason = ? AND status = 'pending'",
            (document_id, "mijngeldzaken_supervision_required"),
        ):
            self.database.add_manual_review_item(
                document_id,
                "mijngeldzaken_supervision_required",
                self.database.json_dumps(details),
                severity="normal",
            )
        return {
            "attempt_id": attempt_id,
            "status": "supervision_required",
            "result": public_result,
        }

    def _record_unknown_outcome(
        self,
        attempt_id: int,
        categorized_data: Dict[str, Any],
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        document_id = categorized_data.get("document_id")
        reason = "The provider may have accepted this transaction, but FAB could not confirm the result. Verify it in Wave before taking further action."
        details = {
            "attempt_id": attempt_id,
            "document_id": document_id,
            "target_system": categorized_data.get("target_system"),
            "provider_status": "outcome_unknown",
            "review_reason": result.get("review_reason") or "wave_api_outcome_unknown",
        }
        now = self.database.now()
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE posting_attempts SET status = 'outcome_unknown', updated_at = ? WHERE id = ?",
                (now, attempt_id),
            )
            if document_id:
                connection.execute(
                    "UPDATE documents SET current_state = 'awaiting_reconciliation', updated_at = ? WHERE id = ?",
                    (now, document_id),
                )
            connection.execute(
                "UPDATE retry_queue SET status = 'completed', updated_at = ? WHERE entity_type = ? AND entity_id = ? AND operation = ?",
                (now, "posting_attempt", str(attempt_id), "execute_posting"),
            )

        self.database.add_audit_log(
            "posting_attempt",
            str(attempt_id),
            "provider_outcome_unknown",
            None,
            details,
            reason,
            "system",
        )
        if document_id:
            self.database.add_manual_review_item(
                document_id,
                "wave_api_outcome_unknown",
                self.database.json_dumps(details),
                severity="high",
            )
        return {"attempt_id": attempt_id, "status": "outcome_unknown", "result": result}

    def _handle_failure(self, attempt_id: int, payload: Dict[str, Any], result: Dict[str, Any]) -> None:
        self.approval_service.mark_failed(attempt_id, result)
        retry = self.retry_manager.schedule_retry(
            "posting_attempt",
            str(attempt_id),
            "execute_posting",
            result.get("message", "Posting failed"),
            payload,
        )
        if retry.get("status") == "dead_lettered":
            self.database.add_manual_review_item(str(attempt_id), "posting_dead_lettered", str(result), severity="high")

    def _payload_to_categorized_data(self, payload: Dict[str, Any], attempt: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "document_id": payload.get("document_id") or attempt.get("document_id"),
            "target_system": payload.get("target_system") or attempt.get("target_system"),
            "target_account": payload.get("target_account") or attempt.get("target_account"),
            "category": payload.get("category"),
            "extracted_data": payload.get("extracted_data", {}),
            "safety_result": payload.get("safety_result", {}),
            "posting_attempt_id": attempt.get("id"),
            "idempotency_key": attempt.get("idempotency_key"),
        }

    def _mark_document_posted(self, document_id: str, result: Dict[str, Any]) -> None:
        if not document_id:
            return
        with self.database.connect() as connection:
            current = connection.execute("SELECT current_state FROM documents WHERE id = ?", (document_id,)).fetchone()
            if current:
                connection.execute(
                    "UPDATE documents SET current_state = 'posted', updated_at = ? WHERE id = ?",
                    (self.database.now(), document_id),
                )
        self.database.add_audit_log("document", document_id, "posted", None, result, "Posting attempt executed successfully", "system")


def _positive_int(value: Any, default: int) -> int:
    try:
        return max(int(float(value)), 1)
    except (TypeError, ValueError):
        return default


def _supervision_result(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: result.get(key)
        for key in (
            "status",
            "message",
            "artifact",
            "external_submission",
            "requires_supervision",
            "credentials_used",
        )
        if result.get(key) is not None
    }
