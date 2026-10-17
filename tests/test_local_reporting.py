import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_reporting import (
    SCHEDULED_REPORT_FORMAT,
    LocalFinancialReportingService,
    LocalScheduledReportService,
)


class TestLocalFinancialReportingService(unittest.TestCase):
    def _ledger(self, temp_dir):
        ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
        ledger.upsert_bookkeeping_record({
            "documentId": 10,
            "sourceType": "document",
            "recordType": "expense",
            "status": "ready_to_route",
            "targetSystem": "waveapps",
            "targetAccount": "Office expenses",
            "vendorName": "Office Shop",
            "category": "Office",
            "recordDate": "2026-04-01",
            "amount": 121,
            "vatAmount": 21,
            "currency": "EUR",
            "reviewRequired": False,
            "reconciliationStatus": "reconciled",
        })
        ledger.upsert_bookkeeping_record({
            "documentId": 11,
            "sourceType": "document",
            "recordType": "income",
            "status": "ready_to_route",
            "targetSystem": "waveapps",
            "targetAccount": "Sales",
            "vendorName": "Customer BV",
            "category": "Revenue",
            "recordDate": "2026-04-02",
            "amount": 242,
            "vatAmount": 42,
            "currency": "EUR",
            "reviewRequired": False,
            "reconciliationStatus": "reconciled",
        })
        ledger.upsert_bookkeeping_record({
            "bankTransactionId": 20,
            "sourceType": "bank_transaction",
            "recordType": "expense",
            "status": "reconciled",
            "targetSystem": "waveapps",
            "targetAccount": "Office expenses",
            "vendorName": "Office Shop",
            "category": "Office",
            "recordDate": "2026-04-01",
            "amount": -121,
            "currency": "EUR",
            "reviewRequired": False,
            "reconciliationStatus": "reconciled",
        })
        ledger.upsert_bookkeeping_record({
            "bankTransactionId": 21,
            "sourceType": "bank_transaction",
            "recordType": "expense",
            "status": "draft",
            "targetSystem": "waveapps",
            "targetAccount": "Bank fees",
            "vendorName": "Bank",
            "category": "Bank fees",
            "recordDate": "2026-04-03",
            "amount": -50,
            "currency": "EUR",
            "reviewRequired": False,
            "reconciliationStatus": "not_started",
        })
        return ledger

    def test_accrual_report_avoids_reconciled_bank_double_counting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = self._ledger(temp_dir)
            report = LocalFinancialReportingService(ledger).generate(
                from_date="2026-04-01",
                to_date="2026-04-30",
                include_rows=True,
            )

            self.assertTrue(report["success"])
            self.assertEqual(report["basis"], "accrual")
            self.assertEqual(report["summary"]["scopedRecordCount"], 4)
            self.assertEqual(report["summary"]["includedRecordCount"], 3)
            self.assertEqual(report["summary"]["excludedReasons"]["reconciled_bank_evidence"], 1)
            profit_loss = report["reports"]["profitAndLoss"]["byCurrency"][0]
            self.assertEqual(profit_loss["revenueGross"], 242.0)
            self.assertEqual(profit_loss["revenueNet"], 200.0)
            self.assertEqual(profit_loss["expensesGross"], 171.0)
            self.assertEqual(profit_loss["expensesNet"], 150.0)
            self.assertEqual(profit_loss["netResult"], 50.0)
            vat = report["reports"]["vat"]["byCurrency"][0]
            self.assertEqual(vat["outputVat"], 42.0)
            self.assertEqual(vat["inputVat"], 21.0)
            self.assertEqual(vat["netVatPayable"], 21.0)
            cash = report["reports"]["cashFlow"]["byCurrency"][0]
            self.assertEqual(cash["outflow"], 171.0)
            self.assertEqual(cash["netMovement"], -171.0)

    def test_credit_note_reduces_expense_and_input_vat(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = self._ledger(temp_dir)
            ledger.upsert_bookkeeping_record({
                "documentId": 12,
                "sourceType": "document",
                "recordType": "expense",
                "status": "ready_to_route",
                "targetSystem": "waveapps",
                "targetAccount": "Office expenses",
                "vendorName": "Office Shop",
                "category": "Office",
                "recordDate": "2026-04-05",
                "amount": -60.5,
                "vatAmount": -10.5,
                "currency": "EUR",
                "reviewRequired": False,
                "reconciliationStatus": "not_started",
                "metadata": {"documentType": "credit_note"},
            })

            report = LocalFinancialReportingService(ledger).generate(
                from_date="2026-04-01",
                to_date="2026-04-30",
                include_rows=True,
            )

            profit_loss = report["reports"]["profitAndLoss"]["byCurrency"][0]
            vat = report["reports"]["vat"]["byCurrency"][0]
            self.assertEqual(profit_loss["expensesGross"], 110.5)
            self.assertEqual(profit_loss["expensesNet"], 100.0)
            self.assertEqual(vat["inputVat"], 10.5)
            credit_row = next(row for row in report["rows"] if row["signedAmount"] == -60.5)
            self.assertEqual(credit_row["grossAmount"], -60.5)
            self.assertEqual(credit_row["vatAmount"], -10.5)

    def test_cash_basis_uses_only_bank_evidence_and_keeps_currencies_separate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = self._ledger(temp_dir)
            ledger.upsert_bookkeeping_record({
                "bankTransactionId": 22,
                "sourceType": "bank_transaction",
                "recordType": "income",
                "status": "draft",
                "targetSystem": "waveapps",
                "targetAccount": "Sales",
                "vendorName": "US Customer",
                "category": "Revenue",
                "recordDate": "2026-04-04",
                "amount": 100,
                "currency": "USD",
                "reviewRequired": False,
                "reconciliationStatus": "not_started",
            })

            report = LocalFinancialReportingService(ledger).generate(
                report_type="profit_and_loss",
                basis="cash",
                from_date="2026-04-01",
                to_date="2026-04-30",
                include_rows=True,
            )

            self.assertEqual(report["summary"]["includedRecordCount"], 3)
            self.assertEqual(report["summary"]["excludedReasons"]["not_cash_evidence"], 2)
            totals = {item["currency"]: item for item in report["report"]["byCurrency"]}
            self.assertEqual(totals["EUR"]["expensesGross"], 171.0)
            self.assertEqual(totals["USD"]["revenueGross"], 100.0)
            self.assertNotIn("reports", report)

    def test_accrual_report_suppresses_pending_linked_bank_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = self._ledger(temp_dir)
            pending = ledger.get_bookkeeping_record_by_bank_transaction(21)
            ledger.update_bookkeeping_record(int(pending["id"]), {
                "status": "needs_review",
                "reconciliationStatus": "candidate",
                "reviewRequired": True,
            })

            report = LocalFinancialReportingService(ledger).generate(
                from_date="2026-04-01",
                to_date="2026-04-30",
                include_rows=True,
            )

            self.assertEqual(report["summary"]["includedRecordCount"], 2)
            self.assertEqual(report["summary"]["excludedReasons"]["pending_reconciliation_bank_evidence"], 1)
            self.assertEqual(
                {row["sourceType"] for row in report["rows"]},
                {"document"},
            )

    def test_report_surfaces_quality_blockers_and_prepares_audited_csv(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = self._ledger(temp_dir)
            ledger.upsert_bookkeeping_record({
                "documentId": 12,
                "sourceType": "document",
                "recordType": "expense",
                "status": "needs_review",
                "targetSystem": "waveapps",
                "vendorName": "Unknown",
                "amount": 10,
                "currency": "EUR",
                "reviewRequired": True,
                "reconciliationStatus": "needs_review",
            })
            ledger.upsert_bookkeeping_record({
                "documentId": 13,
                "sourceType": "document",
                "recordType": "expense",
                "status": "rejected",
                "targetSystem": "waveapps",
                "amount": 99,
                "currency": "EUR",
                "reviewRequired": False,
                "reconciliationStatus": "ignored",
            })
            service = LocalFinancialReportingService(ledger)
            report = service.generate(from_date="2026-04-01", to_date="2026-04-30")
            artifact = service.csv_artifact(from_date="2026-04-01", to_date="2026-04-30")
            service.record_generation_audit(report, actor="test")

            blocker_codes = {item["code"] for item in report["summary"]["blockers"]}
            self.assertEqual(report["summary"]["readiness"], "needs_review")
            self.assertIn("undated_records_outside_period", blocker_codes)
            self.assertEqual(report["summary"]["undatedRecordCount"], 1)
            self.assertIn("unreconciled_record", blocker_codes)
            self.assertEqual(artifact["rowCount"], 3)
            self.assertIn("recordId,recordDate,sourceType", artifact["content"])
            self.assertEqual(artifact["externalSubmission"], "not_executed")
            self.assertEqual(
                ledger.list_audit_events(limit=1)[0]["action"],
                "local_reporting.report_generated",
            )

    def test_invalid_report_inputs_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = LocalFinancialReportingService(self._ledger(temp_dir))
            with self.assertRaisesRegex(ValueError, "Unsupported report type"):
                service.generate(report_type="balance_sheet")
            with self.assertRaisesRegex(ValueError, "Unsupported reporting basis"):
                service.generate(basis="guess")
            with self.assertRaisesRegex(ValueError, "cannot be later"):
                service.generate(from_date="2026-05-01", to_date="2026-04-01")

    def test_monthly_schedule_writes_idempotent_checksum_bound_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = self._ledger(temp_dir)
            ledger.upsert_bookkeeping_record({
                "documentId": 31,
                "sourceType": "document",
                "recordType": "expense",
                "status": "ready_to_route",
                "targetSystem": "waveapps",
                "targetAccount": "Office expenses",
                "vendorName": "June Office Shop",
                "category": "Office",
                "recordDate": "2026-06-15",
                "amount": 121,
                "vatAmount": 21,
                "currency": "EUR",
                "reviewRequired": False,
                "reconciliationStatus": "reconciled",
            })
            report_dir = os.path.join(temp_dir, "scheduled-reports")
            service = LocalScheduledReportService(ledger, {
                "fab_local_report_dir": report_dir,
                "report_schedule_enabled": True,
                "report_schedule_frequency": "monthly",
                "report_schedule_month_day": 1,
                "report_schedule_hour": 6,
                "report_schedule_timezone": "Europe/Amsterdam",
                "report_schedule_period_mode": "previous_month",
                "report_schedule_formats": "json,csv",
            })
            now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)

            first = service.run_due(now=now, actor="test")
            second = service.run_due(now=now, actor="test")
            inspected = service.inspect_run(first["reportRun"]["id"])
            json_artifact = service.read_artifact(first["reportRun"]["id"], "json")

            self.assertEqual(first["status"], "prepared")
            self.assertEqual(first["reportRun"]["schedule_slot"], "monthly:2026-07")
            self.assertEqual(first["reportRun"]["period_from"], "2026-06-01")
            self.assertEqual(first["reportRun"]["period_to"], "2026-06-30")
            self.assertEqual(first["reportRun"]["row_count"], 1)
            self.assertEqual(second["status"], "already_generated")
            self.assertEqual(len(ledger.list_financial_report_runs()), 1)
            self.assertTrue(inspected["success"])
            self.assertTrue(inspected["artifacts"]["json"]["valid"])
            self.assertTrue(inspected["artifacts"]["csv"]["valid"])
            self.assertEqual(json.loads(json_artifact["content"])["format"], SCHEDULED_REPORT_FORMAT)
            self.assertEqual(json.loads(json_artifact["content"])["safety"]["externalSubmission"], "not_executed")
            self.assertEqual([name for name in os.listdir(report_dir) if name.endswith(".tmp")], [])
            self.assertEqual(ledger.dashboard_metrics()["financial_report_runs"], 1)
            self.assertEqual(
                ledger.list_audit_events(limit=1)[0]["action"],
                "local_reporting.scheduled_report_prepared",
            )

    def test_scheduled_report_with_completeness_gates_is_marked_for_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            ledger.upsert_bookkeeping_record({
                "bankTransactionId": 41,
                "sourceType": "bank_transaction",
                "recordType": "expense",
                "status": "draft",
                "targetSystem": "waveapps",
                "vendorName": "Bank",
                "category": "Bank fees",
                "recordDate": "2026-06-20",
                "amount": -10,
                "currency": "EUR",
                "reviewRequired": True,
                "reconciliationStatus": "not_started",
            })
            service = LocalScheduledReportService(ledger, {
                "fab_local_report_dir": os.path.join(temp_dir, "reports"),
                "report_schedule_frequency": "monthly",
                "report_schedule_period_mode": "previous_month",
            })

            result = service.run_due(now=datetime(2026, 7, 13, tzinfo=timezone.utc))

            self.assertEqual(result["status"], "prepared_needs_review")
            self.assertEqual(result["reportRun"]["readiness"], "needs_review")
            self.assertGreater(result["reportRun"]["blocker_count"], 0)
            self.assertEqual(ledger.dashboard_metrics()["financial_report_runs_needing_attention"], 1)

    def test_failed_schedule_write_is_deferred_then_retried_in_same_slot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = self._ledger(temp_dir)
            service = LocalScheduledReportService(ledger, {
                "fab_local_report_dir": os.path.join(temp_dir, "reports"),
                "report_schedule_frequency": "monthly",
                "report_schedule_period_mode": "previous_month",
                "report_schedule_retry_hours": 2,
            })
            now = datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc)

            with patch.object(service, "_write_artifact", side_effect=OSError("disk full")):
                failed = service.run_due(now=now)
            deferred = service.run_due(now=now + timedelta(hours=1))
            retried = service.run_due(now=now + timedelta(hours=3))

            self.assertFalse(failed["success"])
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(deferred["status"], "retry_deferred")
            self.assertTrue(retried["success"])
            self.assertTrue(retried["status"].startswith("prepared"))
            self.assertEqual(retried["reportRun"]["attempt_count"], 2)
            self.assertEqual(len(ledger.list_financial_report_runs()), 1)

    def test_schedule_uses_latest_due_local_slot_and_rejects_unsafe_artifact_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = self._ledger(temp_dir)
            report_dir = os.path.join(temp_dir, "reports")
            service = LocalScheduledReportService(ledger, {
                "fab_local_report_dir": report_dir,
                "report_schedule_frequency": "daily",
                "report_schedule_hour": 6,
                "report_schedule_timezone": "Europe/Amsterdam",
                "report_schedule_period_mode": "current_month_to_date",
                "report_schedule_formats": "json",
            })
            before_due = datetime(2026, 7, 13, 3, 0, tzinfo=timezone.utc)
            schedule = service.schedule_status(now=before_due)
            result = service.run_due(now=before_due)
            outside = os.path.join(temp_dir, "outside.json")
            with open(outside, "w", encoding="utf-8") as handle:
                handle.write("{}")
            ledger.complete_financial_report_run(result["reportRun"]["id"], {
                "status": result["status"],
                "jsonPath": outside,
                "jsonSha256": "not-the-checksum",
            })
            inspected = service.inspect_run(result["reportRun"]["id"])

            self.assertEqual(schedule["slot"]["scheduleSlot"], "daily:2026-07-12")
            self.assertEqual(schedule["slot"]["period"]["fromDate"], "2026-07-01")
            self.assertFalse(inspected["success"])
            self.assertIn("json_artifact_invalid", inspected["errors"])

    def test_schedule_rejects_artifact_symlink_that_resolves_outside_report_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            report_dir = os.path.join(temp_dir, "reports")
            outside_dir = os.path.join(temp_dir, "outside")
            service = LocalScheduledReportService(self._ledger(temp_dir), {
                "fab_local_report_dir": report_dir,
            })
            os.makedirs(outside_dir)
            link_dir = os.path.join(report_dir, "linked")
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["cmd.exe", "/c", "mklink", "/J", link_dir, outside_dir],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                else:
                    os.symlink(outside_dir, link_dir, target_is_directory=True)
            except (OSError, subprocess.CalledProcessError, NotImplementedError) as exc:
                self.skipTest(f"directory link creation is unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "inside the configured report directory"):
                service._resolve_artifact_path(os.path.join("linked", "report.json"))


if __name__ == "__main__":
    unittest.main()
