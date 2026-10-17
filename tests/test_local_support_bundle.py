import json
import os
import tempfile
import unittest
import zipfile

from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_readiness import LocalReadinessService
from src.operations.local_support_bundle import LocalSupportBundleService


class TestLocalSupportBundleService(unittest.TestCase):
    def test_bundle_contains_only_sanitized_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = os.path.join(temp_dir, "fab.sqlite3")
            output_dir = os.path.join(temp_dir, "support")
            secret = "wave-secret-never-export"
            sensitive_filename = "medical-invoice-private.pdf"
            ledger = LocalOperationsLedger(ledger_path)
            ledger.register_document({
                "source": "test",
                "sourceDocumentId": "private-source-id",
                "originalFilename": sensitive_filename,
                "storagePath": os.path.join(temp_dir, sensitive_filename),
                "mimeType": "application/pdf",
                "contentSha256": "a" * 64,
                "processingStatus": "failed",
            })
            ledger.register_document({
                "source": "test",
                "sourceDocumentId": "second-private-source-id",
                "originalFilename": "second-private-file.pdf",
                "mimeType": "application/pdf",
                "contentSha256": "b" * 64,
                "processingStatus": "failed",
            })
            config = {
                "fab_local_ledger_path": ledger_path,
                "fab_support_bundle_dir": output_dir,
                "fab_support_health_issue_limit": 1,
                "fab_local_api_base_url": "https://sensitive-tenant.example.invalid",
                "waveapps_business_access_token": secret,
                "waveapps_business_id": "private-business-id",
            }
            readiness = LocalReadinessService(config, ledger_path=ledger_path)
            result = LocalSupportBundleService(ledger, config, readiness).create(
                actor="test-operator",
                note="Contains no private source data.",
            )

            self.assertTrue(result["success"])
            self.assertEqual(len(result["sha256"]), 64)
            with zipfile.ZipFile(result["bundlePath"]) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {"manifest.json", "doctor.json", "audit-index.json", "README.txt"},
                )
                rendered = "\n".join(
                    archive.read(name).decode("utf-8")
                    for name in archive.namelist()
                )
                doctor = json.loads(archive.read("doctor.json"))

            self.assertNotIn(secret, rendered)
            self.assertNotIn("private-business-id", rendered)
            self.assertNotIn("sensitive-tenant.example.invalid", rendered)
            self.assertNotIn(sensitive_filename, rendered)
            self.assertNotIn("private-source-id", rendered)
            self.assertFalse(doctor["privacy"]["containsFinancialDocuments"])
            self.assertFalse(doctor["privacy"]["containsCredentials"])
            self.assertEqual(doctor["health"]["issueCount"], 2)
            self.assertEqual(doctor["health"]["issuesReturned"], 1)
            self.assertTrue(doctor["health"]["issuesTruncated"])
            self.assertEqual(doctor["health"]["issueTypeCounts"]["failed_document"], 2)
            self.assertIn(
                "support_bundle.created",
                {event["action"] for event in ledger.list_audit_events(limit=10)},
            )


if __name__ == "__main__":
    unittest.main()
