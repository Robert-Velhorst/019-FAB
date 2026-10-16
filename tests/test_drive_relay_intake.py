import hashlib
import io
import json
import os
import tempfile
import unittest

from src.operations.drive_relay_intake import DriveRelayIntakeService
from src.operations.local_api import create_app
from src.operations.local_ledger import LocalOperationsLedger


SOURCE_FOLDER = "1alDzsiSCziPOAq-ynSWTdEgaXy80-vav"
PROVIDER_FILE_ID = "1hFzi6KuTcvU_OapTcVuSEpf0PBUfMk7Z"


class TestDriveRelayIntakeService(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ledger_path = os.path.join(self.temp_dir.name, "fab.sqlite3")
        self.download_dir = os.path.join(self.temp_dir.name, "drive")
        self.ledger = LocalOperationsLedger(self.ledger_path)
        self.config = {
            "fab_local_ledger_path": self.ledger_path,
            "google_drive_folder_id": SOURCE_FOLDER,
            "google_drive_download_dir": self.download_dir,
            "google_drive_relay_max_bytes": 1024,
        }
        self.content = b"%PDF-1.4\nverified drive relay\n"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _ingest(self, content=None, **updates):
        payload = {
            "provider_file_id": PROVIDER_FILE_ID,
            "source_folder_id": SOURCE_FOLDER,
            "filename": "Invoice 2026.pdf",
            "mime_type": "application/pdf",
            "provider_size": len(content or self.content),
            "expected_sha256": hashlib.sha256(content or self.content).hexdigest(),
            "modified_time": "2026-07-22T12:00:00Z",
        }
        payload.update(updates)
        return DriveRelayIntakeService(self.ledger, self.config).ingest(
            content or self.content,
            **payload,
        )

    def test_exact_provider_file_is_registered_idempotently(self):
        first = self._ingest()
        second = self._ingest()

        self.assertTrue(first["success"])
        self.assertEqual(first["status"], "registered")
        self.assertEqual(second["status"], "already_registered")
        self.assertEqual(first["document"]["id"], second["document"]["id"])
        document = self.ledger.get_document(first["document"]["id"])
        self.assertEqual(document["source"], "google_drive")
        self.assertEqual(document["source_document_id"], PROVIDER_FILE_ID)
        self.assertEqual(
            document["metadata"]["providerMetadata"]["folder_id"],
            SOURCE_FOLDER,
        )
        self.assertEqual(
            document["metadata"]["contentSha256"],
            hashlib.sha256(self.content).hexdigest(),
        )
        self.assertTrue(os.path.isfile(document["storage_path"]))

    def test_untrusted_traversal_filename_is_stored_only_under_download_root(self):
        result = self._ingest(filename="../../outside\\receipt.pdf")

        self.assertTrue(result["success"])
        stored_path = os.path.realpath(result["document"]["path"])
        root = os.path.realpath(self.download_dir)
        self.assertTrue(stored_path.startswith(root + os.sep))
        self.assertEqual(
            os.path.basename(stored_path),
            f"{result['sourceSha256'][:12]}-receipt.pdf",
        )

    def test_folder_size_and_hash_mismatches_are_rejected_without_writing(self):
        service = DriveRelayIntakeService(self.ledger, self.config)
        folder_result = service.ingest(
            self.content,
            provider_file_id=PROVIDER_FILE_ID,
            source_folder_id="another-folder",
            filename="invoice.pdf",
        )
        size_result = self._ingest(provider_size=len(self.content) + 1)
        hash_result = self._ingest(expected_sha256="0" * 64)

        self.assertIn("drive_source_folder_mismatch", folder_result["reasons"])
        self.assertIn("drive_provider_size_mismatch", size_result["reasons"])
        self.assertIn("drive_source_sha256_mismatch", hash_result["reasons"])
        self.assertEqual(self.ledger.list_documents(limit=10), [])

    def test_changed_provider_bytes_become_a_reviewable_revision(self):
        first = self._ingest()
        changed = b"%PDF-1.4\nchanged provider bytes\n"
        revision = self._ingest(content=changed)

        self.assertEqual(first["status"], "registered")
        self.assertEqual(revision["status"], "revision")
        revised = self.ledger.get_document(revision["document"]["id"])
        self.assertEqual(revised["processing_status"], "needs_review")
        self.assertEqual(revised["review_items"][0]["reason"], "source_revision_detected")

    def test_multipart_api_preserves_drive_identity(self):
        client = create_app(self.config).test_client()
        metadata = {
            "providerFileId": PROVIDER_FILE_ID,
            "sourceFolderId": SOURCE_FOLDER,
            "filename": "Invoice 2026.pdf",
            "mimeType": "application/pdf",
            "sizeBytes": len(self.content),
            "sha256": hashlib.sha256(self.content).hexdigest(),
        }

        response = client.post(
            "/api/connectors/google-drive/relay",
            data={
                "metadata": json.dumps(metadata),
                "file": (io.BytesIO(self.content), "Invoice 2026.pdf"),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 201)
        result = response.get_json()
        self.assertEqual(result["providerFileId"], PROVIDER_FILE_ID)
        self.assertEqual(result["sourceSha256"], metadata["sha256"])
        self.assertEqual(result["document"]["status"], "imported")


if __name__ == "__main__":
    unittest.main()
