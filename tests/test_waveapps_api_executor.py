import unittest

from src.data_entry.waveapps_api_executor import WaveappsApiExecutor


class _RecordingHandler:
    def __init__(self, result=None):
        self.result = result or {
            "status": "success",
            "message": "created",
            "external_id": "wave-tx-123",
        }
        self.calls = []

    def enter_data(self, data):
        self.calls.append(data)
        return dict(self.result)


class TestWaveappsApiExecutor(unittest.TestCase):
    def _config(self):
        return {
            "waveapps_business_access_token": "token-from-secret-store",
            "waveapps_business_id": "business-1",
        }

    def test_executes_supported_transaction_with_stable_fab_idempotency_data(self):
        handler = _RecordingHandler()
        executor = WaveappsApiExecutor(
            self._config(),
            handlers={"waveapps_business": handler},
        )

        result = executor.execute(
            target_system="waveapps_business",
            action_id="transaction_add",
            payload={
                "date": "2026-07-10",
                "amount": 42.5,
                "account": "Checking",
                "category": "Office Supplies",
                "description": "Printer paper",
                "vendor": "Office Shop",
                "lineItems": [{"description": "Printer paper", "amount": 42.5}],
            },
            idempotency_key="wave:stable-operation",
            document_id=7,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["external_id"], "wave-tx-123")
        self.assertEqual(result["target_system"], "waveapps_business")
        self.assertEqual(len(handler.calls), 1)
        data = handler.calls[0]
        self.assertEqual(data["document_id"], 7)
        self.assertEqual(data["idempotency_key"], "wave:stable-operation")
        self.assertEqual(data["extracted_data"]["transaction_date"], "2026-07-10")
        self.assertEqual(data["extracted_data"]["total_amount"], 42.5)

    def test_generic_wave_target_requires_an_intentional_default(self):
        result = WaveappsApiExecutor(self._config()).execute(
            target_system="waveapps",
            action_id="transaction_add",
            payload={},
            idempotency_key="wave:ambiguous",
        )

        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["review_reason"], "wave_target_ambiguous")

    def test_credit_direction_survives_export_payload_reconstruction(self):
        handler = _RecordingHandler()
        executor = WaveappsApiExecutor(
            self._config(),
            handlers={"waveapps_business": handler},
        )

        result = executor.execute(
            target_system="waveapps_business",
            action_id="transaction_add",
            payload={
                "date": "2026-07-10",
                "amount": -42.5,
                "documentType": "credit_note",
                "transactionDirection": "deposit",
                "account": "Checking",
                "category": "Office Supplies",
                "description": "Supplier refund",
                "vendor": "Office Shop",
                "lineItems": [{"description": "Returned paper", "amount": -42.5}],
            },
            idempotency_key="wave:credit-note",
            document_id=8,
        )

        self.assertEqual(result["status"], "success")
        data = handler.calls[0]
        self.assertEqual(data["document_type"], "credit_note")
        self.assertEqual(data["transaction_direction"], "deposit")
        self.assertEqual(data["extracted_data"]["document_type"], "credit_note")
        self.assertEqual(data["extracted_data"]["total_amount"], -42.5)

    def test_configured_default_resolves_generic_wave_target(self):
        handler = _RecordingHandler()
        config = {
            **self._config(),
            "waveapps_default_target": "business",
        }
        result = WaveappsApiExecutor(
            config,
            handlers={"waveapps_business": handler},
        ).execute(
            target_system="waveapps",
            action_id="transaction_add",
            payload={"category": "Office Supplies"},
            idempotency_key="wave:default-business",
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["target_system"], "waveapps_business")

    def test_missing_credentials_and_unsupported_actions_never_claim_submission(self):
        missing = WaveappsApiExecutor({}).execute(
            target_system="waveapps_personal",
            action_id="transaction_add",
            payload={},
            idempotency_key="wave:no-credentials",
        )
        unsupported = WaveappsApiExecutor(self._config()).execute(
            target_system="waveapps_business",
            action_id="bill_create",
            payload={},
            idempotency_key="wave:no-executor",
        )

        self.assertEqual(missing["status"], "blocked_requires_credentials")
        self.assertEqual(missing["review_reason"], "wave_credentials_missing")
        self.assertNotIn("access_token", missing)
        self.assertEqual(unsupported["status"], "needs_review")
        self.assertEqual(unsupported["review_reason"], "wave_executor_unavailable")

    def test_unverified_handler_exception_is_unknown_and_redacted(self):
        class ExplodingHandler:
            def enter_data(self, _data):
                raise RuntimeError("secret-token private receipt details")

        result = WaveappsApiExecutor(
            self._config(),
            handlers={"waveapps_business": ExplodingHandler()},
        ).execute(
            target_system="waveapps_business",
            action_id="transaction_add",
            payload={"category": "Office Supplies"},
            idempotency_key="wave:unknown-outcome",
        )

        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(result["review_reason"], "wave_api_outcome_unknown")
        self.assertNotIn("secret-token", result["message"])
        self.assertNotIn("private receipt", result["message"])


if __name__ == "__main__":
    unittest.main()
