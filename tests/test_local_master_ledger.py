import csv
import io
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from src.operations.local_exports import (
    EXPORT_APPROVAL_PHRASE,
    EXPORT_RESULT_CONFIRMATION_PHRASE,
    LocalExportAttemptService,
)
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_master_ledger import LocalMasterLedgerService
from src.operations.local_routing import LocalRoutingService


class TestLocalMasterLedgerService(unittest.TestCase):
    def test_export_attempt_lookup_uses_one_query_for_the_selected_record_batch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            records = []
            for index in range(25):
                record_id = ledger.upsert_bookkeeping_record({
                    "sourceType": "document",
                    "status": "validated",
                    "targetSystem": "waveapps_business",
                    "vendorName": f"Vendor {index}",
                    "recordDate": "2026-08-15",
                    "amount": index + 1,
                })
                records.append({"id": record_id, "document_id": None})

            connection = sqlite3.connect(ledger.path)
            try:
                for record in records:
                    record_id = record["id"]
                    for attempt_index in range(2):
                        stamp = f"2026-01-01T00:00:00.00000{attempt_index}Z"
                        connection.execute(
                            """
                            INSERT INTO export_attempts (
                                bookkeeping_record_id, target_system, status, safety,
                                approval_required, external_submission, created_at, updated_at
                            ) VALUES (?, 'waveapps_business', ?, 'requires_confirmation', 1,
                                      'not_executed', ?, ?)
                            """,
                            (record_id, f"attempt_{attempt_index}", stamp, stamp),
                        )
                connection.commit()
            finally:
                connection.close()

            statements = []
            real_connect = ledger._connect

            def connect_with_trace():
                traced = real_connect()
                traced.set_trace_callback(statements.append)
                return traced

            with mock.patch.object(ledger, "_connect", side_effect=connect_with_trace):
                attempts = ledger.latest_export_attempts_for_records(
                    records,
                    target_system="waveapps_business",
                )

            lookup_statements = [
                statement for statement in statements
                if "FROM selected_records" in statement
            ]
            self.assertEqual(len(lookup_statements), 1)
            self.assertEqual(len(attempts), len(records))
            self.assertTrue(all(item["status"] == "attempt_1" for item in attempts.values()))

    def test_export_attempt_lookup_respects_legacy_sqlite_parameter_limits(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            records = [{"id": index, "document_id": None} for index in range(1, 501)]
            real_connect = ledger._connect

            def connect_with_legacy_variable_limit():
                connection = real_connect()
                connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
                return connection

            with mock.patch.object(ledger, "_connect", side_effect=connect_with_legacy_variable_limit):
                attempts = ledger.latest_export_attempts_for_records(
                    records,
                    target_system="waveapps_business",
                )

            self.assertEqual(attempts, {})

    def test_projection_finds_latest_attempt_outside_global_500_attempt_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            selected_record_id = ledger.upsert_bookkeeping_record({
                "sourceType": "document",
                "status": "validated",
                "targetSystem": "waveapps",
                "vendorName": "Selected Vendor",
                "recordDate": "2026-08-15",
                "amount": 12.5,
            })
            other_record_id = ledger.upsert_bookkeeping_record({
                "sourceType": "document",
                "status": "validated",
                "targetSystem": "waveapps",
                "vendorName": "Other Vendor",
                "recordDate": "2026-08-16",
                "amount": 20.0,
            })

            connection = sqlite3.connect(ledger.path)
            try:
                connection.execute(
                    """
                    INSERT INTO export_attempts (
                        bookkeeping_record_id, target_system, status, safety,
                        approval_required, external_submission, created_at, updated_at
                    ) VALUES (?, 'waveapps', 'queued', 'requires_confirmation', 1,
                              'submitted', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
                    """,
                    (selected_record_id,),
                )
                selected_attempt_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
                connection.executemany(
                    """
                    INSERT INTO export_attempts (
                        bookkeeping_record_id, target_system, status, safety,
                        approval_required, external_submission, created_at, updated_at
                    ) VALUES (?, 'waveapps', 'approval_required', 'requires_confirmation', 1,
                              'not_executed', ?, ?)
                    """,
                    [
                        (
                            other_record_id,
                            f"2026-01-02T00:00:00.{index:06d}Z",
                            f"2026-01-02T00:00:00.{index:06d}Z",
                        )
                        for index in range(500)
                    ],
                )
                connection.commit()
            finally:
                connection.close()

            projection = LocalMasterLedgerService(ledger).project()
            row = next(
                item for item in projection["rows"]
                if item["recordId"] == selected_record_id
            )

            self.assertEqual(row["exportAttemptId"], selected_attempt_id)
            self.assertEqual(row["downstreamStatus"], "submitted")

    def test_csv_neutralizes_formula_text_without_changing_ledger_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            ledger.upsert_bookkeeping_record({
                "sourceType": "document",
                "status": "validated",
                "targetSystem": "waveapps_business",
                "vendorName": "=HYPERLINK(\"https://invalid.example\")",
                "description": "\tcommand",
                "category": "@SUM(A1:A2)",
                "recordDate": "2026-08-15",
                "amount": 12.5,
            })
            service = LocalMasterLedgerService(ledger)

            projection = service.project()
            artifact = service.csv_artifact()
            row = next(csv.DictReader(io.StringIO(artifact["content"])))

            self.assertEqual(
                projection["rows"][0]["vendorName"],
                "=HYPERLINK(\"https://invalid.example\")",
            )
            self.assertEqual(row["vendorName"], "'=HYPERLINK(\"https://invalid.example\")")
            self.assertEqual(row["description"], "'\tcommand")
            self.assertEqual(row["category"], "'@SUM(A1:A2)")

    def _register_mijngeldzaken_document(self, ledger):
        return ledger.register_document({
            "source": "scanner",
            "sourceDocumentId": "scan-master-stale-mgz",
            "originalFilename": "groceries.txt",
            "documentType": "receipt",
            "processingStatus": "reviewed",
            "vendorName": "Local Supermarket",
            "category": "Personal",
            "transactionDate": "2026-06-28",
            "totalAmount": 42.5,
            "extractedData": {
                "vendor_name": "Local Supermarket",
                "transaction_date": "2026-06-28",
                "total_amount": 42.5,
                "description": "Weekly groceries",
            },
            "metadata": {"targetSystem": "mijngeldzaken"},
        })

    def _change_mijngeldzaken_document_amount(self, ledger, document_id, amount=99.99):
        ledger.update_document(document_id, {
            "source": "scanner",
            "sourceDocumentId": "scan-master-stale-mgz",
            "originalFilename": "groceries.txt",
            "documentType": "receipt",
            "processingStatus": "export_draft_prepared",
            "vendorName": "Local Supermarket",
            "category": "Personal",
            "transactionDate": "2026-06-28",
            "totalAmount": amount,
            "extractedData": {
                "vendor_name": "Local Supermarket",
                "transaction_date": "2026-06-28",
                "total_amount": amount,
                "description": "Weekly groceries",
            },
            "metadata": {"targetSystem": "mijngeldzaken"},
        })

    def test_projection_tracks_wave_and_mijngeldzaken_downstream_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            wave_document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-master-wave",
                "originalFilename": "office.txt",
                "documentType": "receipt",
                "processingStatus": "reviewed",
                "vendorName": "Office Shop",
                "category": "Office Supplies",
                "transactionDate": "2026-06-28",
                "totalAmount": 42.5,
                "vatAmount": 7.38,
                "extractedData": {
                    "vendor_name": "Office Shop",
                    "transaction_date": "2026-06-28",
                    "total_amount": 42.5,
                    "description": "Printer paper",
                },
            })
            mgz_document_id = ledger.register_document({
                "source": "scanner",
                "sourceDocumentId": "scan-master-mgz",
                "originalFilename": "groceries.txt",
                "documentType": "receipt",
                "processingStatus": "reviewed",
                "vendorName": "Local Supermarket",
                "category": "Personal",
                "transactionDate": "2026-06-29",
                "totalAmount": 31.25,
                "extractedData": {
                    "vendor_name": "Local Supermarket",
                    "transaction_date": "2026-06-29",
                    "total_amount": 31.25,
                    "description": "Weekly groceries",
                },
                "metadata": {"targetSystem": "mijngeldzaken"},
            })
            config = {"mijngeldzaken_category_mapping": {"Personal": "Huishouden"}}
            routing = LocalRoutingService(ledger, config)
            exports = LocalExportAttemptService(ledger, config)

            wave_route = routing.prepare_document_route(wave_document_id)
            wave_export = exports.prepare_from_routing_attempt(wave_route["routingAttemptId"])
            mgz_route = routing.prepare_document_route(mgz_document_id)
            mgz_export = exports.prepare_from_routing_attempt(mgz_route["routingAttemptId"])
            mgz_approved = exports.approve_attempt(
                mgz_export["exportAttemptId"],
                actor="test",
                confirmation=EXPORT_APPROVAL_PHRASE,
            )
            mgz_supervised = exports.execute_attempt(mgz_export["exportAttemptId"], actor="test")
            self.assertTrue(mgz_supervised["success"])
            rejected_queued_result = exports.record_result(
                mgz_export["exportAttemptId"],
                status="queued",
                external_id="mgz-import-queued",
                actor="test",
                confirmation=EXPORT_RESULT_CONFIRMATION_PHRASE,
            )
            self.assertEqual(rejected_queued_result["status"], "invalid_supervised_result")
            mgz_artifact_sha256 = mgz_supervised["artifact"]["sha256"]
            mgz_recorded = exports.record_result(
                mgz_export["exportAttemptId"],
                status="submitted",
                external_id="mgz-import-submitted",
                result={"artifactSha256": mgz_artifact_sha256},
                actor="test",
                confirmation=EXPORT_RESULT_CONFIRMATION_PHRASE,
            )
            self.assertTrue(mgz_recorded["success"])

            service = LocalMasterLedgerService(ledger, config)
            projection = service.project()
            second_projection = service.project()
            csv_artifact = service.csv_artifact()
            service.record_projection_audit(projection, actor="test")

            self.assertTrue(projection["success"])
            self.assertEqual(projection["projectionVersion"], "fab-master-ledger-v1")
            self.assertEqual(projection["summary"]["totalRows"], 2)
            self.assertEqual(len(projection["ledgerChecksum"]), 64)
            self.assertEqual(projection["ledgerChecksum"], second_projection["ledgerChecksum"])
            self.assertEqual(projection["summary"]["ledgerChecksum"], projection["ledgerChecksum"])
            rows_by_target = {row["targetSystem"]: row for row in projection["rows"]}
            self.assertEqual(rows_by_target["waveapps"]["downstreamStatus"], "awaiting_approval")
            self.assertTrue(rows_by_target["waveapps"]["readyForApproval"])
            self.assertEqual(rows_by_target["waveapps"]["exportAttemptId"], wave_export["exportAttemptId"])
            self.assertEqual(rows_by_target["mijngeldzaken"]["downstreamStatus"], "submitted")
            self.assertFalse(rows_by_target["mijngeldzaken"]["readyForExternalExecution"])
            self.assertEqual(rows_by_target["mijngeldzaken"]["externalSubmission"], "submitted")
            self.assertEqual(
                rows_by_target["mijngeldzaken"]["masterLedgerChecksum"],
                mgz_approved["exportAttempt"]["metadata"]["masterLedgerChecksum"],
            )
            self.assertEqual(
                rows_by_target["mijngeldzaken"]["masterLedgerChecksum"],
                mgz_recorded["exportAttempt"]["result"]["masterLedgerChecksum"],
            )
            self.assertEqual(projection["summary"]["byTargetSystem"]["waveapps"]["statuses"]["awaiting_approval"], 1)
            self.assertEqual(projection["summary"]["byTargetSystem"]["mijngeldzaken"]["statuses"]["submitted"], 1)
            self.assertEqual(projection["summary"]["readyForApproval"], 1)
            self.assertEqual(projection["summary"]["blockedRows"], 0)
            self.assertTrue(csv_artifact["success"])
            self.assertEqual(csv_artifact["ledgerChecksum"], projection["ledgerChecksum"])
            self.assertIn("recordId,sourceType,recordType", csv_artifact["content"])
            self.assertIn("mijngeldzaken", csv_artifact["content"])
            audit_actions = [event["action"] for event in ledger.list_audit_events(limit=30)]
            self.assertIn("local_master_ledger.projection_prepared", audit_actions)

    def test_projection_blocks_stale_mijngeldzaken_master_ledger_draft(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            config = {"mijngeldzaken_category_mapping": {"Personal": "Huishouden"}}
            document_id = self._register_mijngeldzaken_document(ledger)
            route = LocalRoutingService(ledger, config).prepare_document_route(document_id)
            prepared = LocalExportAttemptService(ledger, config).prepare_from_routing_attempt(route["routingAttemptId"])
            original_checksum = prepared["exportAttempt"]["metadata"]["masterLedgerChecksum"]

            self._change_mijngeldzaken_document_amount(ledger, document_id)
            projection = LocalMasterLedgerService(ledger, config).project()
            row = projection["rows"][0]

            self.assertEqual(row["downstreamStatus"], "stale_master_ledger_draft")
            self.assertIn("stale_master_ledger_draft", row["blockers"])
            self.assertFalse(row["readyForApproval"])
            self.assertFalse(row["readyForExternalExecution"])
            self.assertEqual(row["draftFreshness"]["status"], "checksum_mismatch")
            self.assertEqual(row["draftFreshness"]["storedChecksum"], original_checksum)
            self.assertNotEqual(row["draftFreshness"]["currentChecksum"], original_checksum)
            self.assertEqual(row["downstreamProof"]["draftFreshness"]["status"], "checksum_mismatch")
            self.assertEqual(projection["summary"]["blockedRows"], 1)
            self.assertEqual(projection["summary"]["blockers"]["stale_master_ledger_draft"], 1)
            self.assertEqual(projection["summary"]["downstreamStatuses"]["stale_master_ledger_draft"], 1)


if __name__ == "__main__":
    unittest.main()
