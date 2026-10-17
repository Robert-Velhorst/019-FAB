import os
import tempfile
import threading
import time
import unittest

from src.operations.local_connector_intake import (
    CONNECTOR_INTAKE_LEASE_NAME,
    LocalConnectorIntakeService,
)
from src.operations.local_bookkeeping_records import LocalBookkeepingRecordService
from src.operations.local_ledger import LocalOperationsLedger


class _Fetcher:
    def __init__(self, documents, error=None):
        self.documents = documents
        self.last_error = error
        self.auth_error = None
        self.last_run = {
            "status": "partial" if error and documents else "failed" if error else "completed",
            "fetched": len(documents),
            "skipped": 0,
            "pages": 2,
        }

    def fetch_documents(self):
        return list(self.documents)


class TestLocalConnectorIntake(unittest.TestCase):
    def _service(self, temp_dir, documents, error=None):
        credentials_path = os.path.join(temp_dir, "gmail-credentials.json")
        token_path = os.path.join(temp_dir, "gmail-token.json")
        for path in (credentials_path, token_path):
            with open(path, "wb") as handle:
                handle.write(b"configured")
        ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
        config = {
            "gmail_enabled": True,
            "gmail_credentials_file": credentials_path,
            "gmail_token_file": token_path,
            "gmail_attachment_download_dir": temp_dir,
            "google_drive_enabled": False,
            "freshdesk_enabled": False,
            "google_photos_enabled": False,
        }
        service = LocalConnectorIntakeService(
            ledger,
            config,
            fetcher_factories={"gmail": lambda _config: _Fetcher(documents, error=error)},
        )
        return ledger, service

    def test_sync_registers_source_provenance_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            document_path = os.path.join(temp_dir, "receipt.pdf")
            with open(document_path, "wb") as handle:
                handle.write(b"receipt-v1")
            documents = [{
                "id": "gmail-message-1_attachment-1",
                "source": "gmail",
                "original_filename": "receipt.pdf",
                "mime_type": "application/pdf",
                "local_path": document_path,
                "metadata": {"subject": "Invoice", "access_token": "must-not-persist"},
            }]
            ledger, service = self._service(temp_dir, documents)

            first = service.sync(["gmail"], actor="test")
            second = service.sync(["gmail"], actor="test")

            self.assertTrue(first["success"])
            self.assertEqual(first["status"], "completed")
            self.assertEqual(first["summary"]["registered"], 1)
            self.assertEqual(second["summary"]["alreadyRegistered"], 1)
            self.assertEqual(len(ledger.list_documents()), 1)
            source = ledger.list_source_accounts(source_type="gmail")[0]
            self.assertEqual(source["status"], "ready")
            self.assertEqual(source["documents_seen"], 2)
            self.assertEqual(source["documents_imported"], 1)
            document = ledger.list_documents()[0]
            self.assertEqual(document["source_account_id"], source["id"])
            self.assertEqual(document["source_document_id"], "gmail-message-1_attachment-1")
            self.assertEqual(document["metadata"]["providerMetadata"]["access_token"], "<redacted>")
            runs = ledger.list_workflow_runs(limit=10)
            self.assertEqual([run["status"] for run in runs[:2]], ["completed", "completed"])
            self.assertEqual(runs[0]["metadata"]["summary"]["alreadyRegistered"], 1)
            steps = ledger.list_workflow_steps(workflow_run_id=runs[0]["id"])
            self.assertEqual(len(steps), 1)
            self.assertEqual(steps[0]["step_key"], "source:gmail")
            self.assertEqual(steps[0]["status"], "completed")
            self.assertEqual(steps[0]["metadata"]["result"]["alreadyRegistered"], 1)
            self.assertEqual(first["externalSubmission"], "not_executed")
            self.assertTrue(first["runtimeLease"]["released"])

    def test_connector_target_backfills_ambiguous_existing_record_without_overriding_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first_path = os.path.join(temp_dir, "business.pdf")
            second_path = os.path.join(temp_dir, "personal.pdf")
            for path, content in ((first_path, b"business"), (second_path, b"personal")):
                with open(path, "wb") as handle:
                    handle.write(content)
            documents = [{
                "id": "gmail-business-1",
                "original_filename": "business.pdf",
                "local_path": first_path,
            }, {
                "id": "gmail-routed-personal-1",
                "original_filename": "personal.pdf",
                "local_path": second_path,
                "metadata": {"routing": {"targetSystem": "waveapps_personal"}},
            }]
            ledger, service = self._service(temp_dir, documents)

            service.sync(["gmail"], actor="test")
            personal_metadata = dict(ledger.get_document(2)["metadata"])
            personal_metadata.pop("targetSystem", None)
            personal_metadata["routing"] = {"targetSystem": "waveapps_personal"}
            ledger.update_document(2, {"metadata": personal_metadata})
            ledger.update_document(1, {
                "processingStatus": "reviewed",
                "vendorName": "Example Vendor",
                "category": "Office",
                "transactionDate": "2026-07-01",
                "totalAmount": 12.5,
                "confidenceScore": 1.0,
            })
            LocalBookkeepingRecordService(ledger).upsert_from_document(1)
            self.assertEqual(
                ledger.get_bookkeeping_record_by_document(1)["target_system"],
                "waveapps",
            )
            LocalBookkeepingRecordService(ledger).upsert_from_document(2)
            self.assertEqual(
                ledger.get_bookkeeping_record_by_document(2)["target_system"],
                "waveapps_personal",
            )

            service.config["gmail_target_system"] = "waveapps_business"
            documents.clear()
            second = service.sync(["gmail"], actor="test")

            self.assertEqual(second["summary"]["alreadyRegistered"], 0)
            self.assertEqual(second["summary"]["targetBackfills"], 1)
            self.assertEqual(
                ledger.get_document(1)["metadata"]["targetSystem"],
                "waveapps_business",
            )
            self.assertEqual(
                ledger.get_bookkeeping_record_by_document(1)["target_system"],
                "waveapps_business",
            )
            routed_personal = ledger.get_document(2)
            self.assertNotIn("targetSystem", routed_personal["metadata"])
            self.assertEqual(
                routed_personal["metadata"]["routing"]["targetSystem"],
                "waveapps_personal",
            )
            self.assertEqual(
                ledger.get_bookkeeping_record_by_document(2)["target_system"],
                "waveapps_personal",
            )
            source = ledger.list_source_accounts(source_type="gmail")[0]
            self.assertEqual(source["metadata"]["targetSystem"], "waveapps_business")
            self.assertIn(
                "local_connector_intake.target_system_backfilled",
                [event["action"] for event in ledger.list_audit_events(limit=20)],
            )

            documents.append({
                "id": "gmail-personal-1",
                "original_filename": "personal.pdf",
                "local_path": second_path,
                "metadata": {"targetSystem": "waveapps_personal"},
            })
            third = service.sync(["gmail"], actor="test")
            personal = next(
                document for document in ledger.list_documents()
                if document["source_document_id"] == "gmail-personal-1"
            )
            self.assertEqual(personal["metadata"]["targetSystem"], "waveapps_personal")
            self.assertEqual(third["summary"]["targetBackfills"], 0)

    def test_sync_does_not_overlap_an_active_connector_cycle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger, service = self._service(temp_dir, [])
            lease = ledger.acquire_runtime_lease(
                CONNECTOR_INTAKE_LEASE_NAME,
                "other-owner",
                ttl_seconds=60,
            )

            result = service.sync(["gmail"], actor="test")

            self.assertTrue(lease["acquired"])
            self.assertFalse(result["success"])
            self.assertEqual(result["status"], "already_running")
            self.assertEqual(result["runtimeLease"]["leaseName"], CONNECTOR_INTAKE_LEASE_NAME)
            self.assertEqual(ledger.list_workflow_runs(limit=10), [])
            self.assertIn(
                "local_connector_intake.sync_skipped_already_running",
                [event["action"] for event in ledger.list_audit_events(limit=10)],
            )
            self.assertTrue(
                ledger.release_runtime_lease(CONNECTOR_INTAKE_LEASE_NAME, "other-owner")
            )

    def test_sync_renews_short_lease_while_connector_fetch_is_running(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger, service = self._service(temp_dir, [])
            service.config["fab_connector_intake_lease_seconds"] = 1
            fetch_started = threading.Event()
            finish_fetch = threading.Event()
            outcome = {}
            errors = []

            def slow_sync(**_kwargs):
                fetch_started.set()
                if not finish_fetch.wait(timeout=5):
                    raise TimeoutError("test did not release connector fetch")
                return {
                    "success": True,
                    "status": "completed",
                    "summary": {},
                    "results": [],
                }

            service._sync_once = slow_sync

            def sync():
                try:
                    outcome["result"] = service.sync(["gmail"], actor="test")
                except Exception as exc:
                    errors.append(exc)

            worker = threading.Thread(target=sync)
            worker.start()
            try:
                self.assertTrue(fetch_started.wait(timeout=5))
                time.sleep(1.25)
                competitor = ledger.acquire_runtime_lease(
                    CONNECTOR_INTAKE_LEASE_NAME,
                    "competing-sync",
                    ttl_seconds=60,
                )
                self.assertFalse(competitor["acquired"])
            finally:
                finish_fetch.set()
                worker.join(timeout=10)

            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(outcome["result"]["success"])
            self.assertIsNone(ledger.get_runtime_lease(CONNECTOR_INTAKE_LEASE_NAME))

    def test_changed_provider_document_creates_reviewable_revision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first_path = os.path.join(temp_dir, "receipt-v1.pdf")
            second_path = os.path.join(temp_dir, "receipt-v2.pdf")
            with open(first_path, "wb") as handle:
                handle.write(b"receipt-v1")
            with open(second_path, "wb") as handle:
                handle.write(b"receipt-v2")
            documents = [{
                "id": "drive-file-1",
                "original_filename": "receipt.pdf",
                "local_path": first_path,
            }]
            ledger, service = self._service(temp_dir, documents)
            service.sync(["gmail"], actor="test")

            documents[0]["local_path"] = second_path
            refreshed = service.sync(["gmail"], actor="test")

            self.assertEqual(refreshed["summary"]["revisions"], 1)
            records = sorted(ledger.list_documents(), key=lambda item: item["id"])
            self.assertEqual(len(records), 2)
            self.assertNotEqual(records[0]["storage_path"], records[1]["storage_path"])
            self.assertIn(":revision:", records[1]["source_document_id"])
            reviews = ledger.list_review_items(status="pending", limit=10)
            self.assertEqual(reviews[0]["reason"], "source_revision_detected")
            self.assertEqual(reviews[0]["document_id"], records[1]["id"])
            repair = ledger.repair_false_source_revisions(actor="test")
            self.assertEqual(repair["repaired"], 0)
            self.assertEqual(repair["skipped"][0]["reason"], "content_differs")
            self.assertEqual(len(ledger.list_documents()), 2)

    def test_partial_provider_failure_keeps_downloaded_evidence_and_redacts_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            document_path = os.path.join(temp_dir, "receipt.pdf")
            with open(document_path, "wb") as handle:
                handle.write(b"partial-receipt")
            documents = [{
                "id": "gmail-partial-1",
                "original_filename": "receipt.pdf",
                "local_path": document_path,
            }]
            ledger, service = self._service(
                temp_dir,
                documents,
                error=RuntimeError(
                    "provider failed?access_token=top-secret; Authorization: Bearer header-secret; X-Api-Key: key-secret"
                ),
            )

            result = service.sync(["gmail"], actor="test")

            self.assertFalse(result["success"])
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["summary"]["registered"], 1)
            self.assertEqual(result["results"][0]["status"], "partial")
            self.assertNotIn("top-secret", result["results"][0]["error"])
            self.assertNotIn("header-secret", result["results"][0]["error"])
            self.assertNotIn("key-secret", result["results"][0]["error"])
            self.assertIn("[REDACTED]", result["results"][0]["error"])
            self.assertEqual(ledger.list_source_accounts(source_type="gmail")[0]["status"], "partial")
            self.assertEqual(len(ledger.list_documents()), 1)
            workflow_run_id = result["workflowRunId"]
            step = ledger.list_workflow_steps(workflow_run_id=workflow_run_id)[0]
            self.assertEqual(step["status"], "failed")
            self.assertNotIn("top-secret", step["error_message"])

    def test_revoked_google_token_requires_reauthorization_without_retry_loop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger, service = self._service(
                temp_dir,
                [],
                error=RuntimeError(
                    "invalid_grant: Token has been expired or revoked."
                ),
            )
            token_path = service.config["gmail_token_file"]

            result = service.sync(["gmail"], actor="test")
            gmail = next(
                item for item in service.plan()["sources"]
                if item["source"] == "gmail"
            )
            source = ledger.list_source_accounts(source_type="gmail")[0]

            self.assertTrue(result["success"])
            self.assertEqual(result["status"], "attention_required")
            self.assertEqual(result["results"][0]["status"], "needs_authorization")
            self.assertTrue(os.path.isfile(f"{token_path}.reauthorize"))
            self.assertEqual(gmail["status"], "needs_authorization")
            self.assertFalse(gmail["canSync"])
            self.assertIn("OAuth consent flow", gmail["nextAction"])
            self.assertEqual(source["status"], "needs_authorization")
            self.assertEqual(source["metadata"]["nextAction"], gmail["nextAction"])

    def test_download_outside_configured_root_fails_source_completeness(self):
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as outside_dir:
            document_path = os.path.join(outside_dir, "unexpected.pdf")
            with open(document_path, "wb") as handle:
                handle.write(b"unexpected")
            ledger, service = self._service(temp_dir, [{
                "id": "gmail-outside-root",
                "original_filename": "unexpected.pdf",
                "local_path": document_path,
            }])

            result = service.sync(["gmail"], actor="test")

            self.assertFalse(result["success"])
            self.assertEqual(result["results"][0]["status"], "failed")
            self.assertEqual(result["summary"]["skipped"], 1)
            self.assertIn("path_outside_source_root", result["results"][0]["error"])
            self.assertEqual(ledger.list_documents(), [])
            self.assertEqual(ledger.list_source_accounts(source_type="gmail")[0]["status"], "failed")

    def test_unconfigured_download_root_does_not_trust_connector_document_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger, service = self._service(temp_dir, [])
            service.config.pop("gmail_attachment_download_dir")

            root = service._download_root("gmail", {
                "local_path": os.path.join(temp_dir, "arbitrary.pdf"),
            })

            self.assertEqual(
                root,
                os.path.abspath(os.path.join("data", "source_downloads", "gmail")),
            )

    def test_configured_connector_stays_disabled_without_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials_path = os.path.join(temp_dir, "gmail-credentials.json")
            token_path = os.path.join(temp_dir, "gmail-token.json")
            for path in (credentials_path, token_path):
                with open(path, "wb") as handle:
                    handle.write(b"configured")
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalConnectorIntakeService(
                ledger,
                {
                    "gmail_credentials_file": credentials_path,
                    "gmail_token_file": token_path,
                    "gmail_attachment_download_dir": temp_dir,
                },
                fetcher_factories={"gmail": lambda _config: self.fail("disabled connector was invoked")},
            )

            gmail = next(item for item in service.plan()["sources"] if item["source"] == "gmail")
            result = service.sync(actor="test")

            self.assertTrue(gmail["configured"])
            self.assertFalse(gmail["enabled"])
            self.assertEqual(gmail["status"], "disabled")
            self.assertEqual(result["status"], "no_sources_enabled")

    def test_gmail_scanner_plan_exposes_strict_profile_and_reauthorization_gate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials_path = os.path.join(temp_dir, "gmail-credentials.json")
            token_path = os.path.join(temp_dir, "gmail-token.json")
            for path in (credentials_path, token_path):
                with open(path, "wb") as handle:
                    handle.write(b"configured")
            with open(f"{token_path}.reauthorize", "wb") as handle:
                handle.write(b"rotation pending")
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalConnectorIntakeService(ledger, {
                "gmail_enabled": True,
                "gmail_credentials_file": credentials_path,
                "gmail_token_file": token_path,
                "gmail_attachment_download_dir": temp_dir,
                "gmail_scanner_mode": True,
                "gmail_trusted_senders": "eprintcenter@hp8.us",
                "gmail_query": "label:all from:eprintcenter@hp8.us has:attachment filename:pdf",
            })

            gmail = next(item for item in service.plan()["sources"] if item["source"] == "gmail")

            self.assertEqual(gmail["label"], "Gmail scanner inbox")
            self.assertEqual(gmail["mode"], "scanner_mailbox_read_only")
            self.assertEqual(gmail["status"], "needs_authorization")
            self.assertFalse(gmail["canSync"])
            self.assertEqual(gmail["scannerProfile"]["trustedSenders"], ["eprintcenter@hp8.us"])
            self.assertEqual(gmail["scannerProfile"]["documentPolicy"], "pdf_only_magic_verified")
            self.assertEqual(gmail["scannerProfile"]["profileId"], "hp_eprint_v1")
            self.assertEqual(
                gmail["scannerProfile"]["deliveryPath"],
                "gmail_to_fab_direct",
            )
            self.assertEqual(
                gmail["scannerProfile"]["sourceProvenance"]["auditedCommit"],
                "e3078d92c214aa3b17d98a8687f16e73f52f71ba",
            )

    def test_freshdesk_plan_exposes_consolidated_read_only_financial_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = LocalConnectorIntakeService(
                LocalOperationsLedger(os.path.join(temp_dir, "operations.db")),
                {
                    "freshdesk_enabled": True,
                    "freshdesk_api_key": "configured-secret",
                    "freshdesk_domain": "example",
                    "freshdesk_download_dir": temp_dir,
                    "freshdesk_financial_filter_enabled": True,
                    "freshdesk_financial_keywords": (
                        "rekening,ontvangstbewijs,facturering,uw bestelling,invoice"
                    ),
                    "freshdesk_ticket_statuses": "2,3",
                    "freshdesk_include_ticket_description": True,
                    "freshdesk_pdf_only": True,
                },
            )

            freshdesk = next(
                item
                for item in service.plan()["sources"]
                if item["source"] == "freshdesk"
            )
            profile = freshdesk["connectorProfile"]

            self.assertTrue(freshdesk["canSync"])
            self.assertEqual(freshdesk["mode"], "financial_ticket_read_only")
            self.assertEqual(profile["profileId"], "scan_to_folder_v1")
            self.assertEqual(profile["ticketStatuses"], ["2", "3"])
            self.assertEqual(
                profile["descriptionPolicy"],
                "non_posting_supporting_evidence",
            )
            self.assertEqual(
                profile["attachmentPolicy"],
                "pdf_only_magic_verified",
            )
            self.assertEqual(profile["ticketMutation"], "not_executed")
            self.assertEqual(profile["driveCopy"], "not_executed")
            self.assertEqual(
                profile["sourceProvenance"]["repository"],
                "Noodzakelijk-Online/025-Scan-to-folder-automation",
            )
            self.assertEqual(
                profile["sourceProvenance"]["auditedCommit"],
                "e3078d92c214aa3b17d98a8687f16e73f52f71ba",
            )

    def test_gmail_scanner_plan_requires_a_trusted_sender_policy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials_path = os.path.join(temp_dir, "gmail-credentials.json")
            token_path = os.path.join(temp_dir, "gmail-token.json")
            for path in (credentials_path, token_path):
                with open(path, "wb") as handle:
                    handle.write(b"configured")

            service = LocalConnectorIntakeService(
                LocalOperationsLedger(os.path.join(temp_dir, "operations.db")),
                {
                    "gmail_enabled": True,
                    "gmail_credentials_file": credentials_path,
                    "gmail_token_file": token_path,
                    "gmail_attachment_download_dir": temp_dir,
                    "gmail_scanner_mode": True,
                    "gmail_trusted_senders": "",
                },
                fetcher_factories={"gmail": lambda _config: self.fail("unsafe connector was invoked")},
            )

            gmail = next(item for item in service.plan()["sources"] if item["source"] == "gmail")

            self.assertEqual(gmail["status"], "needs_configuration")
            self.assertFalse(gmail["canSync"])
            self.assertEqual(gmail["scannerProfile"]["trustedSenders"], [])

    def test_gmail_advances_incremental_checkpoint_only_after_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials_path = os.path.join(temp_dir, "gmail-credentials.json")
            token_path = os.path.join(temp_dir, "gmail-token.json")
            for path in (credentials_path, token_path):
                with open(path, "wb") as handle:
                    handle.write(b"configured")
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            observed_configs = []

            def factory(config):
                observed_configs.append(dict(config))
                return _Fetcher([])

            service = LocalConnectorIntakeService(
                ledger,
                {
                    "gmail_enabled": True,
                    "gmail_credentials_file": credentials_path,
                    "gmail_token_file": token_path,
                    "gmail_attachment_download_dir": temp_dir,
                    "gmail_incremental_overlap_seconds": 3600,
                },
                fetcher_factories={"gmail": factory},
            )

            first = service.sync(["gmail"], actor="test")
            second = service.sync(["gmail"], actor="test")

            self.assertTrue(first["success"])
            self.assertTrue(second["success"])
            self.assertNotIn("gmail_incremental_after_epoch", observed_configs[0])
            self.assertGreater(observed_configs[1]["gmail_incremental_after_epoch"], 0)
            source = ledger.list_source_accounts(source_type="gmail")[0]
            successful_checkpoint = source["metadata"]["lastSuccessfulSyncAt"]
            self.assertTrue(successful_checkpoint)

            def fail_factory(_config):
                raise RuntimeError("provider unavailable")

            service.fetcher_factories["gmail"] = fail_factory
            failed = service.sync(["gmail"], actor="test")
            source_after_failure = ledger.list_source_accounts(source_type="gmail")[0]

            self.assertFalse(failed["success"])
            self.assertEqual(
                source_after_failure["metadata"]["lastSuccessfulSyncAt"],
                successful_checkpoint,
            )

    def test_drive_requires_an_approved_folder_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials_path = os.path.join(temp_dir, "drive-credentials.json")
            token_path = os.path.join(temp_dir, "drive-token.json")
            for path in (credentials_path, token_path):
                with open(path, "wb") as handle:
                    handle.write(b"configured")
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalConnectorIntakeService(ledger, {
                "google_drive_enabled": True,
                "google_drive_credentials_file": credentials_path,
                "google_drive_token_file": token_path,
                "google_drive_download_dir": temp_dir,
            })

            drive = next(item for item in service.plan()["sources"] if item["source"] == "google_drive")
            result = service.sync(["google_drive"], actor="test")

            self.assertFalse(drive["configured"])
            self.assertEqual(drive["status"], "needs_configuration")
            self.assertEqual(result["status"], "attention_required")
            self.assertIn("folder_id", drive["nextAction"])
            self.assertIn("operator dashboard", drive["nextAction"])

    def test_drive_sync_stays_blocked_while_rotated_credentials_need_fresh_consent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials_path = os.path.join(temp_dir, "drive-credentials.json")
            token_path = os.path.join(temp_dir, "drive-token.json")
            for path in (credentials_path, token_path, f"{token_path}.reauthorize"):
                with open(path, "wb") as handle:
                    handle.write(b"configured")
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalConnectorIntakeService(
                ledger,
                {
                    "google_drive_enabled": True,
                    "google_drive_credentials_file": credentials_path,
                    "google_drive_token_file": token_path,
                    "google_drive_folder_id": "approved-source-folder",
                    "google_drive_download_dir": temp_dir,
                },
                fetcher_factories={
                    "google_drive": lambda _config: self.fail(
                        "Drive fetcher was invoked before fresh OAuth consent"
                    )
                },
            )

            drive = next(item for item in service.plan()["sources"] if item["source"] == "google_drive")
            result = service.sync(["google_drive"], actor="test")

            self.assertTrue(drive["configured"])
            self.assertFalse(drive["canSync"])
            self.assertEqual(drive["status"], "needs_authorization")
            self.assertEqual(result["status"], "attention_required")
            self.assertEqual(result["results"][0]["status"], "needs_authorization")

    def test_google_photos_requires_supervised_picker_instead_of_background_scan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = os.path.join(temp_dir, "photos-picker-token.json")
            credentials_path = os.path.join(temp_dir, "photos-picker-credentials.json")
            for path in (token_path, credentials_path):
                with open(path, "wb") as handle:
                    handle.write(b"configured")
            ledger = LocalOperationsLedger(os.path.join(temp_dir, "fab.sqlite3"))
            service = LocalConnectorIntakeService(ledger, {
                "google_photos_enabled": True,
                "google_photos_credentials_file": credentials_path,
                "google_photos_picker_token_file": token_path,
            })

            plan = service.plan()
            photos = next(item for item in plan["sources"] if item["source"] == "google_photos")
            result = service.sync(["google_photos"], actor="test")

            self.assertEqual(photos["status"], "supervision_required")
            self.assertFalse(photos["canSync"])
            self.assertEqual(result["status"], "attention_required")
            self.assertEqual(result["results"][0]["status"], "supervision_required")
            source = ledger.list_source_accounts(source_type="google_photos")[0]
            self.assertEqual(source["status"], "supervision_required")
            self.assertEqual(result["externalSubmission"], "not_executed")


if __name__ == "__main__":
    unittest.main()
