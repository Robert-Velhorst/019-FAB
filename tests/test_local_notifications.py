import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_notifications import LocalNotificationService


class TestLocalNotificationService(unittest.TestCase):
    def test_refresh_batches_notifications_and_audit_into_one_durable_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalNotificationService(ledger)
            health = {
                "generatedAt": "2026-07-13T12:00:00Z",
                "issues": [
                    {
                        "type": f"issue_{index}",
                        "severity": "medium",
                        "entityType": "test",
                        "entityId": str(index),
                        "message": f"Issue {index}.",
                    }
                    for index in range(3)
                ],
            }

            with (
                patch.dict(os.environ, {"FAB_DEPLOYMENT_PROFILE": "windows"}),
                patch.object(ledger, "_connect", wraps=ledger._connect) as connect,
            ):
                result = service.refresh(health, actor="test")

            self.assertEqual(result["created"], 3)
            self.assertEqual(connect.call_count, 2)

    def test_refresh_rolls_back_all_notifications_if_resolution_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalNotificationService(ledger)
            health = {
                "generatedAt": "2026-07-13T12:00:00Z",
                "issues": [{
                    "type": "refresh_failure",
                    "severity": "high",
                    "entityType": "test",
                    "entityId": "one",
                    "message": "Injected resolution failure.",
                }],
            }

            with patch.object(
                ledger,
                "resolve_inactive_notifications",
                side_effect=RuntimeError("injected failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected failure"):
                    service.refresh(health, actor="test")

            self.assertEqual(ledger.list_notifications(limit=10), [])
            self.assertEqual(
                [
                    event
                    for event in ledger.list_audit_events(limit=50)
                    if event.get("action") == "local_notifications.health_refresh"
                ],
                [],
            )

    def test_summary_counts_all_active_notifications_without_loading_the_inbox_page(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalNotificationService(ledger)
            with ledger.write_transaction():
                for index in range(501):
                    ledger.upsert_notification({
                        "fingerprint": f"scale-{index}",
                        "eventType": "scale_test",
                        "severity": "high",
                        "status": "unread",
                        "source": "test",
                    })
                ledger.upsert_notification({
                    "fingerprint": "suppressed",
                    "eventType": "scale_test",
                    "severity": "medium",
                    "status": "suppressed",
                    "source": "test",
                })
                resolved = ledger.upsert_notification({
                    "fingerprint": "resolved",
                    "eventType": "scale_test",
                    "severity": "low",
                    "status": "unread",
                    "source": "test",
                })
            ledger.update_notification_status(resolved["notification"]["id"], "resolved")

            self.assertEqual(len(service.list_notifications(limit=500)), 500)
            self.assertEqual(service.summary(), {
                "active": 501,
                "unread": 501,
                "high": 501,
                "medium": 0,
                "low": 0,
                "acknowledged": 0,
                "externalDelivery": "not_executed",
            })

    def test_health_refresh_is_idempotent_and_resolves_cleared_issue(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            ledger = LocalOperationsLedger(ledger_path)
            document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "notification-failure",
                "originalFilename": "failed.pdf",
                "processingStatus": "failed",
            })
            service = LocalNotificationService(ledger)

            first = service.refresh(actor="test")
            second = service.refresh(actor="test")

            self.assertEqual(first["created"], 1)
            self.assertEqual(second["created"], 0)
            self.assertEqual(len(ledger.list_notifications()), 1)
            notification = ledger.list_notifications()[0]
            self.assertEqual(notification["event_type"], "failed_document")
            self.assertEqual(notification["severity"], "high")
            self.assertEqual(notification["status"], "unread")
            self.assertEqual(notification["occurrence_count"], 1)
            self.assertEqual(notification["external_delivery"], "not_executed")

            connection = sqlite3.connect(ledger_path)
            try:
                connection.execute(
                    "UPDATE bookkeeping_documents SET processing_status = 'processed' WHERE id = ?",
                    (document_id,),
                )
                connection.commit()
            finally:
                connection.close()

            cleared = service.refresh(actor="test")
            self.assertEqual(cleared["resolved"], 1)
            self.assertEqual(ledger.list_notifications()[0]["status"], "resolved")

    def test_preferences_suppress_low_severity_and_reopen_when_enabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalNotificationService(ledger)
            service.update_preference({
                "eventType": "*",
                "enabled": True,
                "inAppEnabled": True,
                "minimumSeverity": "high",
            })
            health = {
                "generatedAt": "2026-07-13T12:00:00Z",
                "issues": [{
                    "type": "stale_review_item",
                    "severity": "medium",
                    "entityType": "review_item",
                    "entityId": "7",
                    "message": "Review item is overdue.",
                    "details": {"accessToken": "must-not-persist"},
                }],
            }

            suppressed = service.refresh(health, actor="test")
            self.assertEqual(suppressed["suppressed"], 1)
            notification = ledger.list_notifications()[0]
            self.assertEqual(notification["status"], "suppressed")
            self.assertEqual(notification["payload"]["details"]["accessToken"], "<redacted>")

            service.update_preference({
                "eventType": "stale_review_item",
                "enabled": True,
                "inAppEnabled": True,
                "minimumSeverity": "low",
            })
            reopened = service.refresh(health, actor="test")
            self.assertEqual(reopened["reopened"], 1)
            self.assertEqual(ledger.list_notifications()[0]["status"], "unread")

    def test_notification_status_actions_are_audited(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalNotificationService(ledger)
            service.refresh({
                "generatedAt": "2026-07-13T12:00:00Z",
                "issues": [{
                    "type": "api_quota_exhausted",
                    "severity": "high",
                    "entityType": "api_quota",
                    "entityId": "waveapps",
                    "message": "Wave quota exhausted.",
                }],
            })
            notification_id = ledger.list_notifications()[0]["id"]

            acknowledged = service.update_status(notification_id, "acknowledged", actor="tester")

            self.assertTrue(acknowledged["success"])
            self.assertEqual(acknowledged["notification"]["status"], "acknowledged")
            self.assertIsNotNone(acknowledged["notification"]["acknowledged_at"])
            audit = ledger.list_audit_events()[0]
            self.assertEqual(audit["action"], "local_notifications.status_changed")
            self.assertEqual(audit["details"]["actor"], "tester")

    def test_notification_status_rolls_back_when_audit_write_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalNotificationService(ledger)
            service.refresh({
                "generatedAt": "2026-07-13T12:00:00Z",
                "issues": [{
                    "type": "api_quota_exhausted",
                    "severity": "high",
                    "entityType": "api_quota",
                    "entityId": "waveapps",
                    "message": "Wave quota exhausted.",
                }],
            })
            notification = ledger.list_notifications()[0]

            with patch.object(
                ledger,
                "record_audit_event",
                side_effect=RuntimeError("injected audit failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected audit failure"):
                    service.update_status(notification["id"], "acknowledged", actor="tester")

            self.assertEqual(
                ledger.get_notification(notification["id"])["status"],
                "unread",
            )
            self.assertFalse(any(
                event.get("action") == "local_notifications.status_changed"
                for event in ledger.list_audit_events(limit=50)
            ))

    def test_notification_preference_rolls_back_when_audit_write_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalNotificationService(ledger)

            with patch.object(
                ledger,
                "record_audit_event",
                side_effect=RuntimeError("injected audit failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected audit failure"):
                    service.update_preference({
                        "eventType": "failed_document",
                        "enabled": False,
                        "minimumSeverity": "high",
                    })

            self.assertIsNone(ledger.get_notification_preference("failed_document"))
            self.assertFalse(any(
                event.get("action") == "local_notifications.preference_changed"
                for event in ledger.list_audit_events(limit=50)
            ))

    def test_source_connector_alert_links_to_sources_dashboard(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            ledger.upsert_source_account({
                "sourceType": "gmail",
                "sourceIdentifier": "me",
                "label": "Gmail",
                "status": "failed",
            })

            result = LocalNotificationService(ledger).refresh(actor="test")

            self.assertEqual(result["created"], 1)
            notification = ledger.list_notifications()[0]
            self.assertEqual(notification["event_type"], "source_connector_unavailable")
            self.assertEqual(notification["payload"]["dashboardPath"], "#sources")

    def test_picker_session_alert_links_to_sources_dashboard(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalNotificationService(ledger)

            result = service.refresh({
                "generatedAt": "2026-07-13T12:00:00Z",
                "issues": [{
                    "type": "stale_picker_session",
                    "severity": "medium",
                    "entityType": "workflow_run",
                    "entityId": 7,
                    "message": "Google Photos selection is waiting.",
                }],
            })

            self.assertEqual(result["created"], 1)
            notification = ledger.list_notifications()[0]
            self.assertEqual(notification["title"], "Google Photos selection is waiting")
            self.assertEqual(notification["payload"]["dashboardPath"], "#sources")


if __name__ == "__main__":
    unittest.main()
