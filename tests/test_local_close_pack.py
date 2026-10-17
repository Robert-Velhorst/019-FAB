import json
import os
import subprocess
import sys
import tempfile
import unittest

from src.operations.local_close_pack import CLOSE_PACK_FORMAT, LocalClosePackService
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_wave_control import LocalWaveControlService


class TestLocalClosePackService(unittest.TestCase):
    def test_close_pack_blocks_when_close_readiness_is_not_ready(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalClosePackService(ledger, {"fab_local_close_pack_dir": os.path.join(temp_dir, "packs")})

            result = service.prepare(from_date="2026-06-28", to_date="2026-06-28")

            self.assertFalse(result["success"])
            self.assertEqual(result["status"], "blocked_not_ready")
            self.assertEqual(result["externalSubmission"], "not_executed")
            self.assertEqual(result["closeReadiness"]["status"], "blocked")
            self.assertEqual(service.list_packs()["packs"], [])

    def test_close_pack_writes_auditable_json_evidence_pack(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            _capture_zero_activity_wave_result(ledger)
            record_id = ledger.upsert_bookkeeping_record({
                "sourceType": "manual",
                "recordType": "expense",
                "status": "routed",
                "targetSystem": "waveapps",
                "targetAccount": "Office Supplies",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "recordDate": "2026-06-28",
                "amount": 42.5,
                "vatAmount": 7.38,
                "currency": "EUR",
                "description": "Closed period expense",
                "exportStatus": "executed",
                "reconciliationStatus": "approved",
            })
            service = LocalClosePackService(ledger, {"fab_local_close_pack_dir": os.path.join(temp_dir, "packs")})

            result = service.prepare(
                workflow_id="daily_reconciliation_run",
                from_date="2026-06-28",
                to_date="2026-06-28",
                actor="test",
            )
            listed = service.list_packs()
            inspected = service.inspect_pack(result["closePackFilename"])

            self.assertTrue(result["success"])
            self.assertEqual(result["status"], "prepared")
            self.assertTrue(os.path.exists(result["closePackPath"]))
            self.assertEqual(result["manifest"]["format"], CLOSE_PACK_FORMAT)
            self.assertEqual(result["manifest"]["externalSubmission"], "not_executed")
            self.assertEqual(result["manifest"]["masterLedger"]["totalRows"], 1)
            self.assertEqual(result["manifest"]["masterLedger"]["blockedRows"], 0)
            self.assertEqual(len(result["manifest"]["masterLedger"]["ledgerChecksum"]), 64)
            self.assertGreater(result["manifest"]["evidenceCounts"]["waveReportSnapshots"], 0)
            self.assertEqual(len(listed["packs"]), 1)
            self.assertEqual(listed["packs"][0]["sha256"], result["sha256"])
            self.assertEqual(inspected["sha256"], result["sha256"])
            self.assertEqual(inspected["payload"]["closeReadiness"]["status"], "ready")
            self.assertEqual(inspected["payload"]["safety"]["containsSecrets"], False)
            with open(result["closePackPath"], "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(payload["format"], CLOSE_PACK_FORMAT)
            self.assertEqual(payload["evidence"]["masterLedger"]["summary"]["totalRows"], 1)
            self.assertEqual(payload["evidence"]["masterLedger"]["rows"][0]["recordId"], record_id)
            self.assertEqual(
                payload["evidence"]["masterLedger"]["ledgerChecksum"],
                result["manifest"]["masterLedger"]["ledgerChecksum"],
            )
            self.assertEqual(payload["evidence"]["waveReportSnapshots"][0]["external_submission"], "not_executed")
            self.assertEqual(ledger.list_audit_events()[0]["action"], "local_close_pack.prepared")

    def test_inspect_pack_rejects_symlink_that_resolves_outside_pack_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            packs_dir = os.path.join(temp_dir, "packs")
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalClosePackService(ledger, {"fab_local_close_pack_dir": packs_dir})
            outside_dir = os.path.join(temp_dir, "outside")
            link_dir = os.path.join(packs_dir, "linked")
            os.makedirs(outside_dir)
            outside_path = os.path.join(outside_dir, "pack.json")
            with open(outside_path, "w", encoding="utf-8") as handle:
                json.dump({"format": CLOSE_PACK_FORMAT}, handle)
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

            with self.assertRaisesRegex(ValueError, "inside the configured"):
                service.inspect_pack(os.path.join("linked", "pack.json"))


def _capture_zero_activity_wave_result(ledger: LocalOperationsLedger):
    service = LocalWaveControlService()
    plan = service.plan_workflow({
        "workflowId": "daily_reconciliation_run",
        "fromDate": "2026-06-28",
        "toDate": "2026-06-28",
        "accountOption": "-1",
        "contactOption": "0",
    })
    service.record_workflow_report_snapshots(ledger, plan)
    return service.record_report_result(ledger, {
        "workflowId": "daily_reconciliation_run",
        "reportType": "account-transactions",
        "actionId": "report_table_read",
        "result": {
            "rowCount": 0,
            "totalDebits": 0,
            "totalCredits": 0,
        },
    })


if __name__ == "__main__":
    unittest.main()
