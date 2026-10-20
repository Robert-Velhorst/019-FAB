import hashlib
from typing import Any, Dict, Optional

from src.operations.local_health import LocalOperationsHealth
from src.operations.local_ledger import LocalOperationsLedger


ACTIVE_NOTIFICATION_STATUSES = ("unread", "read", "acknowledged")
NOTIFICATION_STATUSES = {"unread", "read", "acknowledged", "resolved"}
SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}


class LocalNotificationService:
    """Materialize local operating-health events into an actionable notification inbox."""

    SOURCE = "operations_health"

    def __init__(self, ledger: LocalOperationsLedger, config: Optional[Dict[str, Any]] = None):
        self.ledger = ledger
        self.config = config or {}

    def refresh(self, health: Optional[Dict[str, Any]] = None, actor: str = "system") -> Dict[str, Any]:
        health = health or LocalOperationsHealth(self.ledger, self.config).summarize()
        created = []
        reopened = []
        suppressed = 0
        active_fingerprints = []
        with self.ledger.write_transaction():
            for issue in health.get("issues") or []:
                notification_payload = self._notification_payload(issue, health.get("generatedAt"))
                fingerprint = notification_payload["fingerprint"]
                active_fingerprints.append(fingerprint)
                preference = self.effective_preference(notification_payload["eventType"])
                visible = self._preference_allows(preference, notification_payload["severity"])
                notification_payload["status"] = "unread" if visible else "suppressed"
                result = self.ledger.upsert_notification(notification_payload)
                notification = result["notification"]
                if result["created"]:
                    created.append(notification["id"])
                if result["reopened"]:
                    reopened.append(notification["id"])
                if notification.get("status") == "suppressed":
                    suppressed += 1

            resolved = self.ledger.resolve_inactive_notifications(self.SOURCE, active_fingerprints)
            if created or reopened or resolved:
                self.ledger.record_audit_event({
                    "action": "local_notifications.health_refresh",
                    "entityType": "notification_center",
                    "entityId": "local",
                    "actorUserId": None,
                    "details": {
                        "actor": actor,
                        "createdNotificationIds": created,
                        "reopenedNotificationIds": reopened,
                        "resolvedCount": resolved,
                        "activeIssueCount": len(active_fingerprints),
                        "suppressedCount": suppressed,
                        "externalDelivery": "not_executed",
                    },
                })
        return {
            "success": True,
            "status": "refreshed",
            "created": len(created),
            "createdNotificationIds": created,
            "reopened": len(reopened),
            "reopenedNotificationIds": reopened,
            "resolved": resolved,
            "suppressed": suppressed,
            "activeIssues": len(active_fingerprints),
            "summary": self.summary(),
            "externalDelivery": "not_executed",
        }

    def summary(self) -> Dict[str, Any]:
        return {
            **self.ledger.notification_summary(),
            "externalDelivery": "not_executed",
        }

    def list_notifications(
        self,
        status: Optional[Any] = None,
        severity: Optional[Any] = None,
        event_type: Optional[str] = None,
        limit: int = 100,
    ) -> list:
        return self.ledger.list_notifications(
            status=status,
            severity=severity,
            event_type=event_type,
            limit=limit,
        )

    def update_status(self, notification_id: int, status: str, actor: str = "local_user") -> Dict[str, Any]:
        status = str(status or "").strip().lower()
        if status not in NOTIFICATION_STATUSES:
            raise ValueError(f"Unsupported notification status: {status}")
        with self.ledger.write_transaction():
            previous = self.ledger.get_notification(notification_id)
            if not previous:
                return {"success": False, "status": "not_found", "notification": None}
            notification = self.ledger.update_notification_status(notification_id, status)
            self.ledger.record_audit_event({
                "action": "local_notifications.status_changed",
                "entityType": "notification",
                "entityId": str(notification_id),
                "details": {
                    "actor": actor,
                    "fromStatus": previous.get("status"),
                    "toStatus": status,
                    "eventType": previous.get("event_type"),
                    "externalDelivery": "not_executed",
                },
            })
        return {"success": True, "status": "updated", "notification": notification}

    def update_preference(self, payload: Dict[str, Any], actor: str = "local_user") -> Dict[str, Any]:
        event_type = str(payload.get("eventType") or payload.get("event_type") or "").strip()
        with self.ledger.write_transaction():
            preference_id = self.ledger.upsert_notification_preference(payload)
            preference = self.ledger.get_notification_preference(event_type)
            self.ledger.record_audit_event({
                "action": "local_notifications.preference_changed",
                "entityType": "notification_preference",
                "entityId": str(preference_id),
                "details": {
                    "actor": actor,
                    "eventType": event_type,
                    "enabled": bool(preference.get("enabled")),
                    "inAppEnabled": bool(preference.get("in_app_enabled")),
                    "minimumSeverity": preference.get("minimum_severity"),
                    "externalDelivery": "disabled",
                },
            })
        return {
            "success": True,
            "status": "updated",
            "preference": preference,
            "externalDelivery": "disabled",
        }

    def effective_preference(self, event_type: str) -> Dict[str, Any]:
        preference = self.ledger.get_notification_preference(event_type)
        if not preference:
            preference = self.ledger.get_notification_preference("*")
        if preference:
            return preference
        return {
            "event_type": "*",
            "enabled": 1,
            "in_app_enabled": 1,
            "minimum_severity": str(
                self.config.get("fab_notification_minimum_severity")
                or self.config.get("operations_notification_minimum_severity")
                or self.config.get("notification_minimum_severity")
                or "low"
            ).strip().lower(),
            "external_delivery": "disabled",
            "metadata": {"source": "default"},
        }

    def _notification_payload(self, issue: Dict[str, Any], seen_at: Any) -> Dict[str, Any]:
        event_type = str(issue.get("type") or "operating_issue")
        entity_type = str(issue.get("entityType") or "operations")
        entity_id = str(issue.get("entityId") or "local")
        fingerprint_source = f"{self.SOURCE}|{event_type}|{entity_type}|{entity_id}"
        fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
        return {
            "fingerprint": fingerprint,
            "eventType": event_type,
            "severity": issue.get("severity") or "low",
            "title": _title(event_type),
            "message": issue.get("message") or "FAB detected an operating issue.",
            "entityType": entity_type,
            "entityId": entity_id,
            "source": self.SOURCE,
            "seenAt": seen_at,
            "payload": {
                "ageHours": issue.get("ageHours"),
                "details": issue.get("details") or {},
                "dashboardPath": _dashboard_path(event_type),
                "externalDelivery": "not_executed",
            },
        }

    @staticmethod
    def _preference_allows(preference: Dict[str, Any], severity: str) -> bool:
        if not bool(preference.get("enabled")) or not bool(preference.get("in_app_enabled")):
            return False
        minimum = str(preference.get("minimum_severity") or "low").lower()
        return SEVERITY_RANK.get(str(severity or "low").lower(), 0) >= SEVERITY_RANK.get(minimum, 0)


def _title(event_type: str) -> str:
    labels = {
        "failed_document": "Document processing failed",
        "stale_review_item": "Review decision overdue",
        "stuck_document": "Document processing is stalled",
        "routing_block": "Bookkeeping route is blocked",
        "failed_export_attempt": "Downstream export failed",
        "stuck_export_execution": "Export execution needs inspection",
        "deferred_export_retry_due": "Deferred export is ready to retry",
        "failed_financial_report_run": "Scheduled report failed",
        "financial_report_needs_review": "Scheduled report needs review",
        "stale_financial_report_run": "Scheduled report generation is stalled",
        "api_quota_exhausted": "Downstream API quota exhausted",
        "master_ledger_blockers": "Master ledger has blocked rows",
        "wave_invoice_overdue": "Wave invoice is overdue",
        "wave_invoice_due_soon": "Wave invoice is due soon",
        "stale_picker_session": "Google Photos selection is waiting",
        "picker_session_attention": "Google Photos selection needs attention",
    }
    return labels.get(event_type, event_type.replace("_", " ").strip().title())


def _dashboard_path(event_type: str) -> str:
    if "source_connector" in event_type or "picker_session" in event_type:
        return "#sources"
    if event_type.startswith("compliance_"):
        return "#compliance"
    if "report" in event_type:
        return "#reports"
    if "export" in event_type or "routing" in event_type:
        return "#exports"
    if "review" in event_type or "document" in event_type:
        return "#review"
    if "wave" in event_type:
        return "#wave"
    if "reconciliation" in event_type or "receipt" in event_type:
        return "#reconciliation"
    if "master_ledger" in event_type:
        return "#master-ledger"
    return "#health"
