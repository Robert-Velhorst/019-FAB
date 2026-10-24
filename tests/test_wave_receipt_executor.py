import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from src.operations.local_api import create_app
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_wave_receipt_executor import (
    LocalWaveReceiptExecutorService,
    REQUIRED_CAPABILITIES,
)


class FakeWorkOrders:
    def __init__(self, work_orders):
        self.work_orders = work_orders

    def list_work_orders(self, limit=100):
        return {
            "status": "ready",
            "count": len(self.work_orders[:limit]),
            "workOrders": self.work_orders[:limit],
        }


class TestLocalWaveReceiptExecutorService(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ledger = LocalOperationsLedger(os.path.join(self.temp_dir.name, "fab.sqlite3"))
        self.state_path = os.path.join(self.temp_dir.name, "wave-receipt-executor.json")
        self.config = {
            "waveapps_business_id": "wave-business",
            "wave_receipt_executor_enabled": True,
            "wave_receipt_executor_state_file": self.state_path,
            "wave_receipt_executor_heartbeat_ttl_seconds": 60,
            "wave_receipt_executor_claim_lease_seconds": 120,
        }
        self.service = LocalWaveReceiptExecutorService(self.ledger, self.config)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _session(self, **updates):
        payload = {
            "executorId": "hai-wave-browser",
            "sessionId": "session-1",
            "businessId": "wave-business",
            "status": "ready",
            "capabilities": list(REQUIRED_CAPABILITIES),
            "browser": "chromium",
            "version": "1.0.0",
            "message": "Wave transactions are available.",
        }
        payload.update(updates)
        return payload

    def test_status_is_fail_closed_until_executor_connects(self):
        status = self.service.status()

        self.assertEqual(status["status"], "not_connected")
        self.assertFalse(status["ready"])
        self.assertFalse(status["credentialFieldsAccepted"])
        self.assertEqual(status["configuredBusinessId"], "wave-business")
        self.assertEqual(status["requiredCapabilities"], list(REQUIRED_CAPABILITIES))
        self.assertEqual(status["missingCapabilities"], list(REQUIRED_CAPABILITIES))
        self.assertEqual(status["haiManifestPath"], "/api/hai/manifest")
        self.assertFalse(status["trustedProviderReadbackAvailable"])
        self.assertFalse(status["pairing"]["attachmentReadback"]["available"])
        self.assertEqual(
            status["pairing"]["attachmentReadback"]["reason"],
            "trusted_provider_readback_unavailable",
        )

    def test_status_exposes_dashboard_uuid_from_wave_graphql_id(self):
        self.service.config["waveapps_business_id"] = (
            "QnVzaW5lc3M6YzdmMTVjZjktY2U0Yi00ZWViLThlM2MtOWM0Nzc0OGE0YmQ5"
        )

        status = self.service.status()

        self.assertEqual(
            status["configuredBusinessUuid"],
            "c7f15cf9-ce4b-4eeb-8e3c-9c47748a4bd9",
        )

    def test_ready_session_requires_business_and_all_capabilities(self):
        missing = self.service.register(self._session(capabilities=["transaction_locate"]))

        self.assertTrue(missing["success"])
        self.assertEqual(missing["status"], "incompatible")
        self.assertIn("receipt_upload", missing["missingCapabilities"])

        ready = self.service.register(self._session())

        self.assertTrue(ready["success"])
        self.assertEqual(ready["status"], "readback_unavailable")
        self.assertFalse(ready["ready"])
        self.assertFalse(ready["trustedProviderReadbackAvailable"])
        self.assertIn("trusted provider attachment retrieval path", ready["nextAction"])
        self.assertTrue(ready["businessMatches"])
        self.assertNotIn("password", json.dumps(ready).lower())

    def test_sensitive_browser_state_is_rejected_and_not_written(self):
        result = self.service.register({
            **self._session(),
            "storageState": {"cookies": [{"name": "session", "value": "private"}]},
        })

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "invalid")
        self.assertIn("forbidden", result["error"])
        self.assertFalse(os.path.exists(self.state_path))

    def test_wrong_business_and_expired_heartbeat_are_not_ready(self):
        wrong_business = self.service.register(self._session(businessId="other-business"))

        self.assertEqual(wrong_business["status"], "wrong_business")
        self.service.disconnect(self._session(businessId="other-business"))
        self.service.register(self._session())
        with open(self.state_path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        state["heartbeatAt"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        with open(self.state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)

        stale = self.service.status()

        self.assertEqual(stale["status"], "stale")
        self.assertFalse(stale["ready"])

    def test_fresh_session_cannot_be_replaced_without_disconnect(self):
        self.service.register(self._session())

        conflict = self.service.register(self._session(executorId="other-executor", sessionId="session-2"))

        self.assertFalse(conflict["success"])
        self.assertEqual(conflict["status"], "session_conflict")
        self.assertEqual(self.service.status()["executorId"], "hai-wave-browser")

    def test_executor_cannot_claim_or_release_without_trusted_readback(self):
        self.service.register(self._session())

        self_assigned = self.service.register(self._session(
            status="busy",
            currentDocumentId=9,
        ))

        self.assertFalse(self_assigned["success"])
        self.assertEqual(self_assigned["status"], "active_claim_required")

        provider = FakeWorkOrders([{
            "documentId": 2,
            "stage": "upload_and_verify_attachment",
            "source": {"localAvailable": True},
            "wave": {"externalTransactionId": "wave-transaction-2"},
        }])
        claimed = self.service.claim_next(provider, self._session())

        mismatched_release = self.service.release(3, {
            "executorId": "hai-wave-browser",
            "sessionId": "session-1",
            "outcome": "verified",
        })

        self.assertFalse(mismatched_release["success"])
        self.assertEqual(mismatched_release["status"], "invalid_claim")
        self.assertEqual(claimed["status"], "executor_not_ready")
        self.assertIsNone(self.service.status()["currentDocumentId"])
        self.assertIsNone(self.ledger.get_runtime_lease("wave-receipt-execution:2"))

    def test_claims_are_blocked_until_trusted_provider_readback_exists(self):
        self.service.register(self._session())
        provider = FakeWorkOrders([
            {
                "documentId": 1,
                "stage": "locate_or_create_transaction",
                "source": {"localAvailable": True},
                "wave": {"externalTransactionId": None},
            },
            {
                "documentId": 2,
                "stage": "upload_and_verify_attachment",
                "source": {"localAvailable": True, "localPath": "C:/evidence/invoice.pdf"},
                "wave": {"externalTransactionId": "wave-transaction-2"},
            },
        ])

        claimed = self.service.claim_next(provider, self._session())

        self.assertFalse(claimed["success"])
        self.assertEqual(claimed["status"], "executor_not_ready")
        self.assertEqual(self.service.status()["status"], "readback_unavailable")
        self.assertIsNone(self.ledger.get_runtime_lease("wave-receipt-execution:2"))

        skipped_release = self.service.register(self._session())

        self.assertTrue(skipped_release["success"])
        self.assertEqual(skipped_release["status"], "readback_unavailable")

        released = self.service.release(2, {
            "executorId": "hai-wave-browser",
            "sessionId": "session-1",
            "outcome": "verified",
        })

        self.assertFalse(released["success"])
        self.assertEqual(released["status"], "invalid_claim")
        self.assertIsNone(self.ledger.get_runtime_lease("wave-receipt-execution:2"))
        self.assertEqual(self.service.status()["status"], "readback_unavailable")

    def test_api_exposes_session_status_and_manifest_contract(self):
        app = create_app({
            **self.config,
            "fab_local_ledger_path": self.ledger.path,
        })
        client = app.test_client()

        disconnected = client.get("/api/wave/receipt-executor/status")
        registered = client.post(
            "/api/wave/receipt-executor/session",
            json={**self._session(), "actor": "test-executor"},
        )
        manifest = client.get("/api/hai/manifest")

        self.assertEqual(disconnected.status_code, 200)
        self.assertEqual(disconnected.get_json()["status"], "not_connected")
        self.assertEqual(registered.status_code, 200)
        self.assertFalse(registered.get_json()["ready"])
        self.assertEqual(registered.get_json()["status"], "readback_unavailable")
        resources = {
            item["resourceId"]: item for item in manifest.get_json()["resources"]
        }
        self.assertEqual(
            resources["wave_receipt_executor_claim"]["path"],
            "/api/wave/receipt-executor/claim",
        )
        self.assertEqual(
            resources["wave_receipt_executor_session"]["mode"],
            "non_secret_executor_coordination",
        )


if __name__ == "__main__":
    unittest.main()
