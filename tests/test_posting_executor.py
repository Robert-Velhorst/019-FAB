import json
import os
import tempfile
import unittest

from src.data_entry.posting_approval import PostingApprovalService, SUPERVISED_COMPLETION_PHRASE
from src.data_entry.posting_executor import PostingExecutor
from src.data_entry.safe_posting import SafePostingService
from src.dashboard.app import create_app
from src.storage.database import Database


class DummySuccessHandler:
    def enter_data(self, categorized_data):
        return {"status": "success", "external_id": "ext-123", "message": "posted"}


class DummyFailureHandler:
    def enter_data(self, categorized_data):
        return {"status": "failure", "message": "failed", "requires_manual_review": True}


class UnknownOutcomeHandler:
    def enter_data(self, categorized_data):
        return {
            "status": "outcome_unknown",
            "message": "Wave did not return a verifiable result.",
            "review_reason": "wave_api_outcome_unknown",
        }


class DeferredRateLimitHandler:
    def enter_data(self, categorized_data):
        return {
            "status": "rate_limited",
            "message": "WaveApps dispatch deferred because its configured quota is currently unavailable.",
            "retryable": True,
            "retry_after_seconds": 90,
            "requires_manual_review": False,
            "rate_limit": {"name": "WaveApps", "quotaExhausted": False},
        }


class SupervisedMijngeldzakenHandler:
    def enter_data(self, categorized_data):
        return {
            "status": "supervised_action_required",
            "message": "CSV prepared for supervised import.",
            "artifact": {
                "path": "C:/private/fab/mijngeldzaken.csv",
                "filename": "mijngeldzaken.csv",
                "sha256": "a" * 64,
                "format": "csv",
                "row_count": 1,
            },
            "external_submission": "not_executed",
            "requires_supervision": True,
            "credentials_used": False,
        }


class TestPostingExecutor(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.config = {
            "database_path": os.path.join(self.tempdir.name, "fab.sqlite3"),
            "execute_approved_postings": True,
        }
        self.database = Database(self.config)
        self.database.upsert_document({"id": "doc-1", "source": "test", "original_filename": "receipt.pdf"})

    def tearDown(self):
        self.tempdir.cleanup()

    def _create_approved_attempt(self, target_system="mijngeldzaken"):
        dry_run = SafePostingService(self.config).create_dry_run(
            {
                "document_id": "doc-1",
                "category": "A",
                "target_system": target_system,
                "confidence_score": 0.99,
                "extracted_data": {
                    "vendor_name": "Vendor",
                    "transaction_date": "2026-06-05",
                    "total_amount": 10.0,
                    "currency": "EUR",
                },
            },
            target_system,
        )
        PostingApprovalService(self.config).approve_attempt(dry_run["posting_attempt_id"], reason="Approved for test")
        return dry_run["posting_attempt_id"]

    def test_executes_approved_attempt_successfully(self):
        attempt_id = self._create_approved_attempt()
        executor = PostingExecutor(self.config, handlers={"mijngeldzaken": DummySuccessHandler()})

        result = executor.execute_attempt(attempt_id)

        self.assertEqual(result["status"], "posted")
        attempt = self.database.fetch_one("SELECT * FROM posting_attempts WHERE id = ?", (attempt_id,))
        self.assertEqual(attempt["status"], "posted")
        self.assertEqual(attempt["external_id"], "ext-123")

    def test_failed_execution_creates_manual_review(self):
        attempt_id = self._create_approved_attempt()
        executor = PostingExecutor(self.config, handlers={"mijngeldzaken": DummyFailureHandler()})

        result = executor.execute_attempt(attempt_id)

        self.assertEqual(result["status"], "posting_failed")
        review = self.database.fetch_one("SELECT * FROM manual_review_items WHERE document_id = ?", ("doc-1",))
        self.assertEqual(review["reason"], "posting_execution_failed")

    def test_unknown_wave_outcome_is_not_retried_and_requires_reconciliation(self):
        attempt_id = self._create_approved_attempt(target_system="waveapps_business")
        executor = PostingExecutor(self.config, handlers={"waveapps_business": UnknownOutcomeHandler()})
        executor.retry_manager.schedule_retry(
            "posting_attempt",
            str(attempt_id),
            "execute_posting",
            "Previously scheduled retry",
        )

        result = executor.execute_attempt(attempt_id)

        self.assertEqual(result["status"], "outcome_unknown")
        attempt = self.database.fetch_one("SELECT * FROM posting_attempts WHERE id = ?", (attempt_id,))
        document = self.database.fetch_one("SELECT * FROM documents WHERE id = ?", ("doc-1",))
        retry = self.database.fetch_one(
            "SELECT * FROM retry_queue WHERE entity_type = ? AND entity_id = ? AND operation = ?",
            ("posting_attempt", str(attempt_id), "execute_posting"),
        )
        review = self.database.fetch_one(
            "SELECT * FROM manual_review_items WHERE document_id = ? AND reason = ?",
            ("doc-1", "wave_api_outcome_unknown"),
        )
        self.assertEqual(attempt["status"], "outcome_unknown")
        self.assertEqual(document["current_state"], "awaiting_reconciliation")
        self.assertEqual(retry["status"], "completed")
        self.assertEqual(review["severity"], "high")
        self.assertEqual(executor.execute_attempt(attempt_id, force=True)["status"], "not_approved")
        self.assertEqual(executor.process_due_retries()["count"], 0)

    def test_execution_respects_disabled_flag(self):
        disabled_config = dict(self.config)
        disabled_config["execute_approved_postings"] = False
        attempt_id = self._create_approved_attempt()
        executor = PostingExecutor(disabled_config, handlers={"mijngeldzaken": DummySuccessHandler()})

        result = executor.execute_attempt(attempt_id)

        self.assertEqual(result["status"], "skipped")

    def test_rate_limited_execution_is_deferred_without_failure_or_manual_review(self):
        attempt_id = self._create_approved_attempt(target_system="waveapps_business")
        executor = PostingExecutor(self.config, handlers={"waveapps_business": DeferredRateLimitHandler()})

        result = executor.execute_attempt(attempt_id)

        self.assertEqual(result["status"], "posting_deferred")
        self.assertEqual(result["defer_reason"], "rate_limited")
        attempt = self.database.fetch_one("SELECT * FROM posting_attempts WHERE id = ?", (attempt_id,))
        self.assertEqual(attempt["status"], "posting_deferred")
        retry = self.database.fetch_one(
            "SELECT * FROM retry_queue WHERE entity_type = ? AND entity_id = ? AND operation = ?",
            ("posting_attempt", str(attempt_id), "execute_posting"),
        )
        self.assertEqual(retry["status"], "pending")
        self.assertEqual(retry["attempt_count"], 0)
        self.assertEqual(self.database.fetch_all("SELECT * FROM manual_review_items"), [])
        audit_actions = [entry["action"] for entry in self.database.fetch_all("SELECT * FROM audit_log")]
        self.assertIn("retry_deferred", audit_actions)
        self.assertIn("execution_deferred_rate_limit", audit_actions)

    def test_mijngeldzaken_execution_pauses_for_supervision_without_retry_or_failure(self):
        attempt_id = self._create_approved_attempt()
        executor = PostingExecutor(self.config, handlers={"mijngeldzaken": SupervisedMijngeldzakenHandler()})

        result = executor.execute_attempt(attempt_id)

        self.assertEqual(result["status"], "supervision_required")
        attempt = self.database.fetch_one("SELECT * FROM posting_attempts WHERE id = ?", (attempt_id,))
        document = self.database.fetch_one("SELECT * FROM documents WHERE id = ?", ("doc-1",))
        review = self.database.fetch_one(
            "SELECT * FROM manual_review_items WHERE document_id = ? AND reason = ?",
            ("doc-1", "mijngeldzaken_supervision_required"),
        )
        self.assertEqual(attempt["status"], "supervision_required")
        self.assertEqual(document["current_state"], "awaiting_supervised_submission")
        self.assertEqual(review["status"], "pending")
        self.assertEqual(self.database.fetch_all("SELECT * FROM retry_queue"), [])
        audit_actions = [entry["action"] for entry in self.database.fetch_all("SELECT * FROM audit_log")]
        self.assertIn("execution_paused_for_supervision", audit_actions)
        self.assertNotIn("posting_failed", audit_actions)

        approval = PostingApprovalService(self.config)
        blocked = approval.complete_supervised_attempt(attempt_id, confirmation="wrong")
        completed = approval.complete_supervised_attempt(
            attempt_id,
            confirmation=SUPERVISED_COMPLETION_PHRASE,
            completed_by="tester",
            external_id="mgz-import-2026-07-10",
            evidence={
                "artifact_sha256": "a" * 64,
                "note": "Observed import confirmation",
                "password": "must-not-be-logged",
            },
        )

        self.assertEqual(blocked["status"], "confirmation_required")
        self.assertEqual(completed["status"], "posted")
        attempt = self.database.fetch_one("SELECT * FROM posting_attempts WHERE id = ?", (attempt_id,))
        document = self.database.fetch_one("SELECT * FROM documents WHERE id = ?", ("doc-1",))
        review = self.database.fetch_one("SELECT * FROM manual_review_items WHERE id = ?", (review["id"],))
        completion_audit = self.database.fetch_one(
            "SELECT * FROM audit_log WHERE entity_type = 'posting_attempt' AND entity_id = ? AND action = ?",
            (str(attempt_id), "supervised_submission_completed"),
        )
        self.assertEqual(attempt["status"], "posted")
        self.assertEqual(attempt["external_id"], "mgz-import-2026-07-10")
        self.assertEqual(document["current_state"], "posted")
        self.assertEqual(review["status"], "resolved")
        self.assertNotIn("must-not-be-logged", json.dumps(completion_audit, sort_keys=True))

    def test_dashboard_records_supervised_completion_only_with_exact_confirmation(self):
        attempt_id = self._create_approved_attempt()
        PostingExecutor(
            self.config,
            handlers={"mijngeldzaken": SupervisedMijngeldzakenHandler()},
        ).execute_attempt(attempt_id)
        app = create_app({**self.config, "dashboard_access_token": "dashboard-token"})
        client = app.test_client()
        headers = {"X-FAB-Token": "dashboard-token"}

        blocked = client.post(
            f"/posting-attempts/{attempt_id}/complete-supervised",
            headers=headers,
            json={"confirmation": "wrong"},
        )
        completed = client.post(
            f"/posting-attempts/{attempt_id}/complete-supervised",
            headers=headers,
            json={
                "confirmation": SUPERVISED_COMPLETION_PHRASE,
                "external_id": "mgz-dashboard-import",
                "evidence": {"receipt_reference": "confirmation-screen"},
            },
        )

        self.assertEqual(blocked.status_code, 200)
        self.assertEqual(blocked.get_json()["status"], "confirmation_required")
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.get_json()["status"], "posted")
        attempt = self.database.fetch_one("SELECT * FROM posting_attempts WHERE id = ?", (attempt_id,))
        self.assertEqual(attempt["external_id"], "mgz-dashboard-import")


if __name__ == "__main__":
    unittest.main()
