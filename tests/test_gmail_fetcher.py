import base64
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src.document_fetchers.gmail_fetcher import GmailFetcher


class TestGmailFetcherResourceLimits(unittest.TestCase):
    def _fetcher(self, config, attachments):
        fetcher = GmailFetcher.__new__(GmailFetcher)
        fetcher.config = config
        fetcher.last_error = None
        fetcher.last_run = {"status": "idle", "fetched": 0, "skipped": 0, "pages": 0}
        fetcher.creds = None
        fetcher.auth_error = None
        fetcher.service = MagicMock()
        messages = fetcher.service.users.return_value.messages.return_value
        messages.list.return_value.execute.return_value = {"messages": [{"id": "message-1"}]}
        messages.get.return_value.execute.return_value = {
            "payload": {
                "headers": [{"name": "From", "value": "scanner@example.test"}],
                "parts": attachments,
            },
            "internalDate": "1783641600000",
        }
        return fetcher

    def test_oversized_base64_is_rejected_before_decode(self):
        oversized = "A" * 1369
        fetcher = self._fetcher({
            "gmail_max_attachment_bytes": 1024,
            "gmail_attachment_download_dir": tempfile.gettempdir(),
        }, [{
            "filename": "large.bin",
            "mimeType": "application/octet-stream",
            "body": {"data": oversized},
        }])

        with patch("src.document_fetchers.gmail_fetcher.base64.urlsafe_b64decode") as decode:
            documents = fetcher.fetch_documents()

        self.assertEqual(documents, [])
        decode.assert_not_called()
        self.assertEqual(fetcher.last_run["status"], "failed")
        self.assertEqual(fetcher.last_run["rejected"]["oversized"], 1)
        self.assertIn("before advancing the source checkpoint", str(fetcher.last_error))

    def test_attachment_count_budget_marks_scan_partial(self):
        encoded = base64.urlsafe_b64encode(b"small document").decode("ascii")
        attachments = [{
            "filename": f"receipt-{index}.bin",
            "mimeType": "application/octet-stream",
            "body": {"data": encoded},
        } for index in range(2)]
        with tempfile.TemporaryDirectory() as directory:
            fetcher = self._fetcher({
                "gmail_max_run_attachments": 1,
                "gmail_attachment_download_dir": directory,
            }, attachments)

            documents = fetcher.fetch_documents()

        self.assertEqual(len(documents), 1)
        self.assertEqual(fetcher.last_run["status"], "partial")
        self.assertEqual(fetcher.last_run["rejected"]["run_budget"], 1)
        self.assertEqual(fetcher.last_run["attachmentCountLimit"], 1)
        self.assertIn("before advancing the source checkpoint", str(fetcher.last_error))

    def test_aggregate_attachment_byte_budget_marks_scan_partial(self):
        content = b"x" * (600 * 1024)
        encoded = base64.urlsafe_b64encode(content).decode("ascii")
        attachments = [{
            "filename": f"receipt-{index}.bin",
            "mimeType": "application/octet-stream",
            "body": {"data": encoded},
        } for index in range(2)]
        with tempfile.TemporaryDirectory() as directory:
            fetcher = self._fetcher({
                "gmail_max_run_attachment_bytes": 1024 * 1024,
                "gmail_attachment_download_dir": directory,
            }, attachments)

            documents = fetcher.fetch_documents()

        self.assertEqual(len(documents), 1)
        self.assertEqual(fetcher.last_run["status"], "partial")
        self.assertEqual(fetcher.last_run["rejected"]["run_budget"], 1)
        self.assertEqual(fetcher.last_run["attachmentBytesDownloaded"], len(content))
        self.assertEqual(fetcher.last_run["attachmentBytesLimit"], 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
