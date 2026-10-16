import random
import statistics
import unittest
from unittest.mock import patch

from src.operations.local_anomalies import LocalLedgerAnomalyService, _AmountGroup


class FakeLedger:
    def __init__(self, records):
        self.records = records

    def list_bookkeeping_records(self, limit):
        return self.records[:limit]


class TestLocalLedgerAnomalyService(unittest.TestCase):
    def test_below_minimum_amount_rows_do_not_rebuild_peer_histories(self):
        records = [
            {
                "id": index,
                "status": "draft",
                "vendor_name": "Synthetic vendor",
                "category": "software",
                "currency": "EUR",
                "amount": 10,
            }
            for index in range(100)
        ]
        service = LocalLedgerAnomalyService(FakeLedger(records))
        from src.operations import local_anomalies

        original = local_anomalies._absolute_amount
        with patch.object(local_anomalies, "_absolute_amount", wraps=original) as amount_parser:
            self.assertEqual(service.list_issues(), [])

        self.assertLessEqual(amount_parser.call_count, len(records) * 2)

    def test_large_vendor_outlier_keeps_existing_anomaly_evidence(self):
        records = [
            {
                "id": index,
                "status": "draft",
                "vendor_name": "Synthetic vendor",
                "category": "software",
                "currency": "EUR",
                "amount": amount,
            }
            for index, amount in enumerate([100, 100, 100, 100, 100, 1000], start=1)
        ]
        service = LocalLedgerAnomalyService(FakeLedger(records))

        issues = service.list_issues()

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["type"], "vendor_amount_anomaly")
        self.assertEqual(issues[0]["entityId"], 6)
        self.assertEqual(issues[0]["details"]["historicalMedian"], 100)
        self.assertEqual(issues[0]["details"]["historicalSampleCount"], 5)
        self.assertEqual(issues[0]["details"]["amountToMedianRatio"], 10)

    def test_precomputed_leave_one_out_statistics_match_brute_force(self):
        rng = random.Random(42117)
        amounts = [None, "invalid", 0, -12.5, 5, 12.5, 12.5, 100, 250, 1000]
        records = [
            {
                "id": index // 2 if index in {4, 5} else index,
                "amount": rng.choice(amounts),
            }
            for index in range(60)
        ]
        group = _AmountGroup(records)

        for record in records:
            with self.subTest(record_id=record["id"], amount=record["amount"]):
                history = []
                for peer in records:
                    if peer.get("id") == record.get("id"):
                        continue
                    try:
                        amount = abs(float(peer.get("amount")))
                    except (TypeError, ValueError):
                        continue
                    if amount > 0:
                        history.append(amount)

                actual = group.history_statistics(record)
                if not history:
                    self.assertIsNone(actual)
                    continue

                baseline = statistics.median(history)
                deviation = statistics.median(abs(value - baseline) for value in history)
                self.assertEqual(actual, (len(history), baseline, deviation))


if __name__ == "__main__":
    unittest.main()
