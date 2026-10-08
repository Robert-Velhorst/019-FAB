import json
import os
import tempfile
import unittest
from unittest.mock import patch

from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_bank_transactions import LocalBankTransactionImportService
from src.operations.local_wave_control import LocalWaveControlService


class TestLocalWaveControlService(unittest.TestCase):
    def test_overview_models_wave_reports_without_leaking_credentials(self):
        service = LocalWaveControlService({
            "waveapps_business_access_token": "business-secret-token",
            "waveapps_business_id": "business-id",
            "waveapps_personal": {
                "access_token": "personal-secret-token",
                "personal_id": "personal-id",
            },
        })

        overview = service.overview()
        rendered = json.dumps(overview, sort_keys=True)

        self.assertEqual(overview["status"], "modeled")
        self.assertEqual(overview["summary"]["report_sections"], 5)
        self.assertEqual(overview["summary"]["reports"], 12)
        self.assertTrue(overview["credentials"]["waveappsBusiness"]["accessTokenConfigured"])
        self.assertTrue(overview["credentials"]["waveappsPersonal"]["accessTokenConfigured"])
        self.assertIn("accountMappings", overview)
        self.assertEqual(len(overview["accountMappings"]["targets"]), 2)
        self.assertIn("detailed_reporting", {section["id"] for section in overview["reportSections"]})
        self.assertIn("account-transactions", {report["type"] for report in overview["reports"]})
        self.assertNotIn("business-secret-token", rendered)
        self.assertNotIn("personal-secret-token", rendered)

    def test_report_catalog_can_filter_detailed_reporting(self):
        service = LocalWaveControlService()

        reports = service.reports(section="detailed_reporting")

        self.assertEqual(reports["section"], "detailed_reporting")
        self.assertEqual(reports["count"], 3)
        self.assertEqual(
            {report["type"] for report in reports["reports"]},
            {"account-balances", "trial-balance", "account-transactions"},
        )

    def test_report_action_plan_is_read_only_and_not_executed(self):
        service = LocalWaveControlService()

        result = service.plan_report_action(
            "account-transactions",
            from_date="2026-06-28",
            to_date="2026-06-28",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["externalSubmission"], "not_executed")
        self.assertEqual(result["operation"]["safety"], "read_only")
        self.assertEqual(result["operation"]["payload"]["reportType"], "account-transactions")

    def test_report_action_plan_can_be_persisted_as_wave_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalWaveControlService()

            result = service.plan_report_action(
                "account-transactions",
                from_date="2026-06-28",
                to_date="2026-06-28",
                basis="cash",
                account_option="-1",
                contact_option="0",
                cash_mode="1",
            )
            snapshot_id = service.record_report_operation_snapshot(
                ledger,
                result["operation"],
                workflow_id="daily_reconciliation_run",
            )
            snapshots = ledger.list_wave_report_snapshots(workflow_id="daily_reconciliation_run")

            self.assertIsNotNone(snapshot_id)
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0]["report_type"], "account-transactions")
            self.assertEqual(snapshots[0]["report_section"], "detailed_reporting")
            self.assertEqual(snapshots[0]["basis"], "cash")
            self.assertEqual(snapshots[0]["external_submission"], "not_executed")

    def test_action_plan_can_be_persisted_as_generic_wave_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalWaveControlService()

            result = service.plan_action({
                "surface": "transactions",
                "actionId": "transaction_add",
                "payload": {"lineItems": [{"account": "Office Supplies", "amount": 42.5}]},
            })
            snapshot_id = service.record_operation_snapshot(
                ledger,
                result["operation"],
                workflow_id="manual_transaction_plan",
            )
            snapshots = ledger.list_wave_operation_snapshots(action_id="transaction_add")

            self.assertIsNotNone(snapshot_id)
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0]["action_id"], "transaction_add")
            self.assertEqual(snapshots[0]["surface"], "transactions")
            self.assertEqual(snapshots[0]["workflow_id"], "manual_transaction_plan")
            self.assertEqual(snapshots[0]["payload"]["lineItems"][0]["account"], "Office Supplies")

    def test_workflow_plan_expands_to_wave_report_operations(self):
        service = LocalWaveControlService()

        result = service.plan_workflow({
            "workflowId": "daily_reconciliation_run",
            "fromDate": "2026-06-28",
            "toDate": "2026-06-28",
            "accountOption": "-1",
            "contactOption": "0",
        })

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["externalSubmission"], "not_executed")
        self.assertGreaterEqual(result["operationCount"], 8)
        actions = [operation["action_id"] for operation in result["operations"]]
        self.assertIn("report_table_read", actions)
        self.assertIn("report_empty_state_read", actions)
        self.assertIn("report_export", actions)
        self.assertTrue(all(operation["safety"] == "read_only" for operation in result["operations"]))

    def test_workflow_plan_records_report_snapshots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalWaveControlService()

            result = service.plan_workflow({
                "workflowId": "daily_reconciliation_run",
                "fromDate": "2026-06-28",
                "toDate": "2026-06-28",
                "accountOption": "-1",
                "contactOption": "0",
            })
            summary = service.record_workflow_report_snapshots(ledger, result)

            self.assertEqual(summary["workflowId"], "daily_reconciliation_run")
            self.assertEqual(summary["snapshotCount"], result["operationCount"])
            self.assertEqual(len(ledger.list_wave_report_snapshots(workflow_id="daily_reconciliation_run")), result["operationCount"])

    def test_report_controls_block_until_required_wave_reports_are_planned(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalWaveControlService()

            controls = service.evaluate_report_controls(ledger, workflow_id="daily_reconciliation_run")

            self.assertEqual(controls["status"], "blocked_missing_report_plan")
            self.assertEqual(controls["requiredReportCount"], 1)
            self.assertEqual(controls["coveredReportCount"], 0)
            self.assertTrue(all(gate["status"] == "missing_plan" for gate in controls["gates"]))

    def test_report_controls_summarize_planned_wave_report_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalWaveControlService()
            result = service.plan_workflow({
                "workflowId": "daily_reconciliation_run",
                "fromDate": "2026-06-28",
                "toDate": "2026-06-28",
                "accountOption": "-1",
                "contactOption": "0",
            })
            service.record_workflow_report_snapshots(ledger, result)

            controls = service.evaluate_report_controls(ledger, workflow_id="daily_reconciliation_run")

            self.assertEqual(controls["status"], "ready_for_wave_read")
            self.assertEqual(controls["blockingCount"], 0)
            self.assertEqual(controls["coveredReportCount"], controls["requiredReportCount"])
            self.assertGreaterEqual(controls["resultGapCount"], 1)
            account_transactions = next(gate for gate in controls["gates"] if gate["reportType"] == "account-transactions")
            self.assertIn("report_table_read", account_transactions["plannedActions"])
            self.assertIn("report_export", account_transactions["plannedActions"])

    def test_report_result_capture_marks_wave_report_control_ready(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalWaveControlService()
            result = service.plan_workflow({
                "workflowId": "daily_reconciliation_run",
                "fromDate": "2026-06-28",
                "toDate": "2026-06-28",
                "accountOption": "-1",
                "contactOption": "0",
            })
            service.record_workflow_report_snapshots(ledger, result)

            capture = service.record_report_result(ledger, {
                "workflowId": "daily_reconciliation_run",
                "reportType": "account-transactions",
                "actionId": "report_table_read",
                "result": {
                    "rowCount": 3,
                    "totalDebits": 120.50,
                    "totalCredits": 120.50,
                },
            })

            self.assertTrue(capture["success"])
            self.assertEqual(capture["status"], "read_result_captured")
            self.assertEqual(capture["externalSubmission"], "not_executed")
            self.assertEqual(capture["waveReportControls"]["status"], "ready")
            updated = ledger.get_wave_report_snapshot(capture["waveReportSnapshotId"])
            self.assertEqual(updated["status"], "read_result_captured")
            self.assertEqual(updated["row_count"], 3)
            self.assertEqual(updated["total_debits"], 120.50)
            self.assertEqual(updated["metadata"]["resultCapture"]["externalSubmission"], "not_executed")

    def test_report_result_capture_derives_totals_from_csv_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalWaveControlService()
            result = service.plan_workflow({
                "workflowId": "daily_reconciliation_run",
                "fromDate": "2026-06-28",
                "toDate": "2026-06-28",
            })
            service.record_workflow_report_snapshots(ledger, result)

            capture = service.record_report_result(ledger, {
                "workflowId": "daily_reconciliation_run",
                "reportType": "account-transactions",
                "actionId": "report_table_read",
                "format": "csv",
                "resultText": (
                    "Date;Description;Debit;Credit;Reference\n"
                    "2026-06-28;Office Shop;42,50;;wave-1\n"
                    "2026-06-29;Client payment;;100,00;wave-2\n"
                ),
            })

            self.assertTrue(capture["success"])
            self.assertEqual(capture["waveReportControls"]["status"], "ready")
            snapshot = capture["waveReportSnapshot"]
            self.assertEqual(snapshot["row_count"], 2)
            self.assertEqual(snapshot["total_debits"], 42.5)
            self.assertEqual(snapshot["total_credits"], 100.0)

    def test_report_result_capture_can_import_account_transaction_rows_for_reconciliation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalWaveControlService()
            result = service.plan_workflow({
                "workflowId": "daily_reconciliation_run",
                "fromDate": "2026-06-28",
                "toDate": "2026-06-28",
            })
            service.record_workflow_report_snapshots(ledger, result)

            capture = service.record_report_result(ledger, {
                "workflowId": "daily_reconciliation_run",
                "reportType": "account-transactions",
                "actionId": "report_table_read",
                "format": "csv",
                "accountIdentifier": "wave-checking",
                "importTransactions": True,
                "refreshBookkeepingRecords": True,
                "runReconciliation": True,
                "resultText": (
                    "Date;Description;Debit;Credit;Reference\n"
                    "2026-06-28;Office Shop;42,50;;wave-1\n"
                    "2026-06-29;Client payment;;100,00;wave-2\n"
                ),
            })

            transactions = ledger.list_bank_transactions(account_identifier="wave-checking", limit=10)
            by_id = {transaction["transaction_id"]: transaction for transaction in transactions}
            records = ledger.list_bookkeeping_records(source_type="bank_transaction", limit=10)

            self.assertTrue(capture["success"])
            self.assertEqual(capture["waveReportControls"]["status"], "ready")
            self.assertEqual(capture["bankTransactionImport"]["rowsImported"], 2)
            self.assertEqual(capture["bookkeepingRecordRefresh"]["updated"], 2)
            self.assertEqual(capture["reconciliation"]["missingReceipts"], 2)
            self.assertEqual(by_id["wave-1"]["amount"], -42.5)
            self.assertEqual(by_id["wave-2"]["amount"], 100.0)
            self.assertEqual(by_id["wave-1"]["reconciliation_status"], "missing_receipt")
            self.assertEqual({record["reconciliation_status"] for record in records}, {"missing_receipt"})
            self.assertEqual(
                capture["waveReportSnapshot"]["metadata"]["resultCapture"]["bankTransactionImport"]["accountIdentifier"],
                "wave-checking",
            )
            self.assertEqual(
                capture["waveReportSnapshot"]["metadata"]["resultCapture"]["reconciliation"]["missingReceipts"],
                2,
            )

    def test_wave_report_import_surfaces_identity_conflicts_without_overwriting_ledger_row(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalWaveControlService()
            workflow = service.plan_workflow({
                "workflowId": "daily_reconciliation_run",
                "fromDate": "2026-06-28",
                "toDate": "2026-06-28",
            })
            service.record_workflow_report_snapshots(ledger, workflow)
            LocalBankTransactionImportService(ledger).import_transactions([{
                "id": "wave-stable-transaction",
                "date": "2026-06-28",
                "amount": -42.5,
                "description": "Office Shop",
            }], account_identifier="wave-checking")
            existing = ledger.list_bank_transactions(account_identifier="wave-checking")[0]

            capture = service.record_report_result(ledger, {
                "workflowId": "daily_reconciliation_run",
                "reportType": "account-transactions",
                "actionId": "report_table_read",
                "accountIdentifier": "wave-checking",
                "importTransactions": True,
                "rows": [{
                    "id": "wave-stable-transaction",
                    "date": "2026-06-28",
                    "amount": -99.0,
                    "description": "Office Shop",
                }],
            })
            preserved = ledger.get_bank_transaction(existing["id"])
            import_evidence = capture["waveReportSnapshot"]["metadata"]["resultCapture"]["bankTransactionImport"]

            self.assertTrue(capture["success"])
            self.assertEqual(import_evidence["status"], "needs_review")
            self.assertEqual(import_evidence["identityConflicts"], 1)
            self.assertEqual(import_evidence["identityConflictRows"][0]["transactionId"], "wave-stable-transaction")
            self.assertEqual(preserved["amount"], -42.5)

    def test_report_result_rejects_oversized_row_sets_before_processing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalWaveControlService()
            workflow = service.plan_workflow({"workflowId": "daily_reconciliation_run"})
            service.record_workflow_report_snapshots(ledger, workflow)

            capture = service.record_report_result(ledger, {
                "workflowId": "daily_reconciliation_run",
                "reportType": "account-transactions",
                "actionId": "report_table_read",
                "rows": [{}] * 10_001,
            })

            self.assertFalse(capture["success"])
            self.assertEqual(capture["status"], "invalid_payload")
            self.assertIn("10000-row", capture["error"])

    def test_report_reconciliation_batch_overflow_is_rejected_before_import(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalWaveControlService()
            workflow = service.plan_workflow({"workflowId": "daily_reconciliation_run"})
            service.record_workflow_report_snapshots(ledger, workflow)

            with patch("src.operations.local_wave_control.LocalBankTransactionImportService.import_transactions") as importer:
                capture = service.record_report_result(ledger, {
                    "workflowId": "daily_reconciliation_run",
                    "reportType": "account-transactions",
                    "actionId": "report_table_read",
                    "rows": [{"amount": 1, "date": "2026-06-28"}] * 501,
                    "importTransactions": True,
                    "runReconciliation": True,
                })

            self.assertFalse(capture["success"])
            self.assertEqual(capture["status"], "invalid_payload")
            self.assertIn("500-row", capture["error"])
            importer.assert_not_called()

    def test_report_result_capture_requires_matching_snapshot_and_result_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalWaveControlService()

            missing = service.record_report_result(ledger, {
                "workflowId": "daily_reconciliation_run",
                "reportType": "account-transactions",
                "result": {"rowCount": 1},
            })

            self.assertFalse(missing["success"])
            self.assertEqual(missing["status"], "not_found")

    def test_workflow_plan_records_generic_operation_snapshots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalWaveControlService()

            result = service.plan_workflow({
                "workflowId": "daily_reconciliation_run",
                "fromDate": "2026-06-28",
                "toDate": "2026-06-28",
                "accountOption": "-1",
                "contactOption": "0",
            })
            summary = service.record_workflow_operation_snapshots(ledger, result)

            self.assertEqual(summary["workflowId"], "daily_reconciliation_run")
            self.assertEqual(summary["snapshotCount"], result["operationCount"])
            self.assertEqual(
                len(ledger.list_wave_operation_snapshots(workflow_id="daily_reconciliation_run")),
                result["operationCount"],
            )

if __name__ == "__main__":
    unittest.main()
