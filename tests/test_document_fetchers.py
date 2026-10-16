import unittest
from unittest.mock import MagicMock, patch
import base64
import os
import shutil
import tempfile

from src.document_fetchers.base import BaseFetcher
from src.document_fetchers.gmail_fetcher import GmailFetcher
from src.document_fetchers.drive_fetcher import DriveFetcher
from src.document_fetchers.freshdesk_fetcher import FreshdeskFetcher
from src.document_fetchers.photos_fetcher import PhotosFetcher


class _ConcreteFetcher(BaseFetcher):
    def fetch_documents(self):
        return []

    def _authenticate(self):
        return None


class TestDocumentFetchers(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config = {
            "gmail_credentials_file": os.path.join(self.temp_dir.name, "gmail_credentials.json"),
            "gmail_token_file": os.path.join(self.temp_dir.name, "gmail_token.json"),
            "gmail_attachment_download_dir": os.path.join(self.temp_dir.name, "gmail_downloads"),
            "google_drive_credentials_file": os.path.join(self.temp_dir.name, "drive_credentials.json"),
            "google_drive_token_file": os.path.join(self.temp_dir.name, "drive_token.json"),
            "google_drive_download_dir": os.path.join(self.temp_dir.name, "drive_downloads"),
            "google_drive_folder_id": "sort-out-folder",
            "freshdesk_api_key": "test_api_key",
            "freshdesk_domain": "test_domain",
            "freshdesk_attachment_allowed_hosts": ["example.com"],
            "freshdesk_download_dir": os.path.join(self.temp_dir.name, "freshdesk_downloads"),
            "google_photos_credentials_file": os.path.join(self.temp_dir.name, "photos_credentials.json"),
            "google_photos_token_file": os.path.join(self.temp_dir.name, "photos_token.json"),
            "google_photos_album_name": "Test Album",
            "google_photos_download_dir": os.path.join(self.temp_dir.name, "photos_downloads")
        }
        # Create dummy credential and token files for all fetchers
        for key in ["gmail", "google_drive", "google_photos"]:
            with open(self.config[f"{key}_credentials_file"], "w") as f:
                f.write("{}")
            with open(self.config[f"{key}_token_file"], "w") as f:
                f.write("{}")
        
        # Create download directories
        for key in [
            "gmail_attachment_download_dir",
            "google_drive_download_dir",
            "freshdesk_download_dir",
            "google_photos_download_dir",
        ]:
            os.makedirs(self.config[key], exist_ok=True)

    def test_content_store_reuses_only_verified_immutable_evidence(self):
        fetcher = _ConcreteFetcher({})
        content = b"%PDF-1.7\nverified scanner evidence"
        local_path = fetcher._store_content(
            self.temp_dir.name,
            "scan.pdf",
            "gmail-message-1:attachment-1",
            content,
        )

        with patch("src.document_fetchers.base.os.replace") as replace:
            reused_path = fetcher._store_content(
                self.temp_dir.name,
                "scan.pdf",
                "gmail-message-1:attachment-1",
                content,
            )

        self.assertEqual(reused_path, local_path)
        replace.assert_not_called()
        with open(local_path, "rb") as handle:
            self.assertEqual(handle.read(), content)

    def test_content_store_refuses_to_overwrite_conflicting_evidence(self):
        fetcher = _ConcreteFetcher({})
        content = b"%PDF-1.7\nexpected scanner evidence"
        local_path = fetcher._content_download_path(
            self.temp_dir.name,
            "scan.pdf",
            "gmail-message-2:attachment-1",
            content,
        )
        with open(local_path, "wb") as handle:
            handle.write(b"corrupted evidence")

        with self.assertRaisesRegex(OSError, "size does not match"):
            fetcher._store_content(
                self.temp_dir.name,
                "scan.pdf",
                "gmail-message-2:attachment-1",
                content,
            )

        with open(local_path, "rb") as handle:
            self.assertEqual(handle.read(), b"corrupted evidence")

    def test_content_store_removes_staged_file_when_publish_fails(self):
        fetcher = _ConcreteFetcher({})
        with patch(
            "src.document_fetchers.base.os.replace",
            side_effect=OSError("publish failed"),
        ):
            with self.assertRaisesRegex(OSError, "publish failed"):
                fetcher._store_content(
                    self.temp_dir.name,
                    "scan.pdf",
                    "gmail-message-3:attachment-1",
                    b"%PDF-1.7\nscanner evidence",
                )

        self.assertFalse(any(
            name.startswith(".fab-evidence-")
            for name in os.listdir(self.temp_dir.name)
        ))

    @patch("src.document_fetchers.gmail_fetcher.build")
    @patch("src.document_fetchers.gmail_fetcher.InstalledAppFlow")
    @patch("src.document_fetchers.gmail_fetcher.Request")
    @patch("src.document_fetchers.gmail_fetcher.os.path.exists")
    @patch("src.document_fetchers.gmail_fetcher.GoogleOAuthTokenStore")
    def test_gmail_fetcher(self, mock_pickle, mock_exists, mock_Request, mock_InstalledAppFlow, mock_build):
        mock_exists.return_value = True
        mock_pickle.return_value.load.return_value = MagicMock()
        
        mock_service = MagicMock()
        mock_build.return_value = mock_service
        
        # Mock Gmail API calls
        mock_service.users.return_value.messages.return_value.list.return_value.execute.return_value = {"messages": [{"id": "msg1"}]}
        mock_service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
            "internalDate": "1735689600000",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Receipt"},
                    {"name": "From", "value": "vendor@example.com"},
                ],
                "parts": [
                    {"filename": "test.pdf", "body": {"attachmentId": "att1"}, "mimeType": "application/pdf"}
                ]
            }
        }
        mock_service.users.return_value.messages.return_value.attachments.return_value.get.return_value.execute.return_value = {"data": "JVBERi0xLjQK"}

        fetcher = GmailFetcher(self.config)
        documents = fetcher.fetch_documents()
        self.assertEqual(len(documents), 1)
        self.assertTrue(documents[0]["original_filename"].endswith(".pdf"))
        self.assertTrue(os.path.exists(documents[0]["local_path"]))
        os.remove(documents[0]["local_path"])

    @patch("src.document_fetchers.gmail_fetcher.build")
    @patch("src.document_fetchers.gmail_fetcher.InstalledAppFlow")
    @patch("src.document_fetchers.gmail_fetcher.Request")
    @patch("src.document_fetchers.gmail_fetcher.os.path.exists")
    @patch("src.document_fetchers.gmail_fetcher.GoogleOAuthTokenStore")
    def test_gmail_fetcher_follows_next_page_token(
        self,
        mock_pickle,
        mock_exists,
        mock_Request,
        mock_InstalledAppFlow,
        mock_build,
    ):
        mock_exists.return_value = True
        mock_pickle.return_value.load.return_value = MagicMock()
        service = MagicMock()
        mock_build.return_value = service
        service.users.return_value.messages.return_value.list.return_value.execute.side_effect = [
            {"messages": [{"id": "msg1"}], "nextPageToken": "page-2"},
            {"messages": [{"id": "msg2"}]},
        ]
        service.users.return_value.messages.return_value.get.return_value.execute.side_effect = [
            _gmail_message("att1", "one.pdf"),
            _gmail_message("att2", "two.pdf"),
        ]
        service.users.return_value.messages.return_value.attachments.return_value.get.return_value.execute.side_effect = [
            {"data": "JVBERi0xLjQK"},
            {"data": "JVBERi0xLjUK"},
        ]

        fetcher = GmailFetcher(self.config)
        documents = fetcher.fetch_documents()

        self.assertEqual(len(documents), 2)
        self.assertEqual(fetcher.last_run["pages"], 2)
        self.assertEqual(service.users.return_value.messages.return_value.list.call_count, 2)
        for document in documents:
            os.remove(document["local_path"])

    @patch("src.document_fetchers.gmail_fetcher.build")
    @patch("src.document_fetchers.gmail_fetcher.InstalledAppFlow")
    @patch("src.document_fetchers.gmail_fetcher.Request")
    @patch("src.document_fetchers.gmail_fetcher.os.path.exists")
    @patch("src.document_fetchers.gmail_fetcher.GoogleOAuthTokenStore")
    def test_gmail_run_byte_budget_stops_and_marks_import_partial(
        self,
        mock_pickle,
        mock_exists,
        mock_Request,
        mock_InstalledAppFlow,
        mock_build,
    ):
        mock_exists.return_value = True
        mock_pickle.return_value.load.return_value = MagicMock()
        service = MagicMock()
        mock_build.return_value = service
        service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
            "messages": [{"id": "msg-budget"}],
        }
        service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
            "internalDate": "1735689600000",
            "payload": {
                "headers": [{"name": "From", "value": "vendor@example.com"}],
                "parts": [
                    {
                        "filename": "first.pdf",
                        "body": {"data": base64.urlsafe_b64encode(b"A" * 600).decode("ascii")},
                        "mimeType": "application/pdf",
                    },
                    {
                        "filename": "second.pdf",
                        "body": {"data": base64.urlsafe_b64encode(b"B" * 600).decode("ascii")},
                        "mimeType": "application/pdf",
                    },
                ],
            },
        }
        config = {
            **self.config,
            "gmail_max_attachment_bytes": 1024,
            "gmail_max_run_bytes": 1024,
        }

        fetcher = GmailFetcher(config)
        documents = fetcher.fetch_documents()

        self.assertEqual(len(documents), 1)
        self.assertEqual(fetcher.last_run["status"], "partial")
        self.assertEqual(fetcher.last_run["downloadedBytes"], 600)
        self.assertEqual(fetcher.last_run["maxRunBytes"], 1024)
        with open(documents[0]["local_path"], "rb") as handle:
            self.assertEqual(len(handle.read()), 600)
        os.remove(documents[0]["local_path"])

    @patch("src.document_fetchers.gmail_fetcher.build")
    @patch("src.document_fetchers.gmail_fetcher.InstalledAppFlow")
    @patch("src.document_fetchers.gmail_fetcher.Request")
    @patch("src.document_fetchers.gmail_fetcher.os.path.exists")
    @patch("src.document_fetchers.gmail_fetcher.GoogleOAuthTokenStore")
    def test_gmail_scanner_profile_rejects_untrusted_and_invalid_pdf_attachments(
        self,
        mock_pickle,
        mock_exists,
        mock_Request,
        mock_InstalledAppFlow,
        mock_build,
    ):
        mock_exists.return_value = True
        mock_pickle.return_value.load.return_value = MagicMock()
        service = MagicMock()
        mock_build.return_value = service
        service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
            "messages": [{"id": "valid"}, {"id": "spoofed"}, {"id": "fake-pdf"}],
        }
        service.users.return_value.messages.return_value.get.return_value.execute.side_effect = [
            _gmail_message("att-valid", "scan.pdf", sender="HP ePrint <eprintcenter@hp8.us>"),
            _gmail_message("att-spoofed", "scan.pdf", sender="Attacker <other@example.com>"),
            _gmail_message("att-fake", "scan.pdf", sender="eprintcenter@hp8.us"),
        ]
        service.users.return_value.messages.return_value.attachments.return_value.get.return_value.execute.side_effect = [
            {"data": base64.urlsafe_b64encode(b"%PDF-1.7\nscanner").decode("ascii")},
            {"data": base64.urlsafe_b64encode(b"not actually a pdf").decode("ascii")},
        ]
        config = {
            **self.config,
            "gmail_scanner_mode": True,
            "gmail_trusted_senders": "eprintcenter@hp8.us",
            "gmail_query": "label:all from:eprintcenter@hp8.us has:attachment filename:pdf",
        }

        fetcher = GmailFetcher(config)
        documents = fetcher.fetch_documents()

        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["metadata"]["sender_address"], "eprintcenter@hp8.us")
        self.assertTrue(documents[0]["metadata"]["scanner_policy_verified"])
        self.assertEqual(documents[0]["metadata"]["scanner_profile"], "hp_eprint_v1")
        self.assertEqual(
            documents[0]["metadata"]["delivery_path"],
            "gmail_to_fab_direct",
        )
        self.assertEqual(documents[0]["mime_type"], "application/pdf")
        self.assertEqual(fetcher.last_run["rejected"]["untrusted_sender"], 1)
        self.assertEqual(fetcher.last_run["rejected"]["invalid_pdf"], 1)
        list_kwargs = service.users.return_value.messages.return_value.list.call_args.kwargs
        self.assertEqual(list_kwargs["q"], config["gmail_query"])
        os.remove(documents[0]["local_path"])

    @patch("src.document_fetchers.gmail_fetcher.build")
    @patch("src.document_fetchers.gmail_fetcher.InstalledAppFlow")
    @patch("src.document_fetchers.gmail_fetcher.Request")
    @patch("src.document_fetchers.gmail_fetcher.os.path.exists")
    @patch("src.document_fetchers.gmail_fetcher.GoogleOAuthTokenStore")
    def test_gmail_fetcher_marks_capped_history_partial_and_uses_incremental_checkpoint(
        self,
        mock_pickle,
        mock_exists,
        mock_Request,
        mock_InstalledAppFlow,
        mock_build,
    ):
        mock_exists.return_value = True
        mock_pickle.return_value.load.return_value = MagicMock()
        service = MagicMock()
        mock_build.return_value = service
        service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
            "messages": [{"id": "msg1"}],
            "nextPageToken": "more-history",
        }
        service.users.return_value.messages.return_value.get.return_value.execute.return_value = _gmail_message(
            "att1", "scan.pdf"
        )
        service.users.return_value.messages.return_value.attachments.return_value.get.return_value.execute.return_value = {
            "data": base64.urlsafe_b64encode(b"%PDF-1.7\nscanner").decode("ascii"),
        }
        config = {
            **self.config,
            "gmail_max_messages": 1,
            "gmail_incremental_after_epoch": 1_700_000_000,
        }

        fetcher = GmailFetcher(config)
        documents = fetcher.fetch_documents()

        self.assertEqual(len(documents), 1)
        self.assertEqual(fetcher.last_run["status"], "partial")
        self.assertTrue(fetcher.last_run["truncated"])
        query = service.users.return_value.messages.return_value.list.call_args.kwargs["q"]
        self.assertEqual(query, "has:attachment after:1700000000")
        os.remove(documents[0]["local_path"])

    @patch("src.document_fetchers.drive_fetcher.build")
    @patch("src.document_fetchers.drive_fetcher.InstalledAppFlow")
    @patch("src.document_fetchers.drive_fetcher.MediaIoBaseDownload")
    @patch("src.document_fetchers.drive_fetcher.Request")
    @patch("src.document_fetchers.drive_fetcher.os.path.exists")
    @patch("src.document_fetchers.drive_fetcher.GoogleOAuthTokenStore")
    def test_drive_fetcher(self, mock_pickle, mock_exists, mock_Request, mock_MediaIoBaseDownload, mock_InstalledAppFlow, mock_build):
        mock_exists.return_value = True
        mock_pickle.return_value.load.return_value = MagicMock()

        mock_service = MagicMock()
        mock_build.return_value = mock_service

        # Mock Drive API calls
        mock_service.files.return_value.list.return_value.execute.return_value = {
            "files": [
                {"id": "file1", "name": "document.jpg", "mimeType": "image/jpeg"}
            ]
        }
        mock_service.files.return_value.get_media.return_value.execute.return_value = b"dummy_image_content"
        downloader = MagicMock()
        downloader.next_chunk.return_value = (None, True)
        mock_MediaIoBaseDownload.return_value = downloader

        fetcher = DriveFetcher(self.config)
        documents = fetcher.fetch_documents()
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["original_filename"], "document.jpg")
        self.assertTrue(os.path.exists(documents[0]["local_path"]))
        os.remove(documents[0]["local_path"])

    @patch("src.document_fetchers.drive_fetcher.build")
    @patch("src.document_fetchers.drive_fetcher.InstalledAppFlow")
    @patch("src.document_fetchers.drive_fetcher.MediaIoBaseDownload")
    @patch("src.document_fetchers.drive_fetcher.Request")
    @patch("src.document_fetchers.drive_fetcher.os.path.exists")
    @patch("src.document_fetchers.drive_fetcher.GoogleOAuthTokenStore")
    def test_drive_fetcher_follows_next_page_token(
        self,
        mock_pickle,
        mock_exists,
        mock_Request,
        mock_MediaIoBaseDownload,
        mock_InstalledAppFlow,
        mock_build,
    ):
        mock_exists.return_value = True
        mock_pickle.return_value.load.return_value = MagicMock()
        service = MagicMock()
        mock_build.return_value = service
        service.files.return_value.list.return_value.execute.side_effect = [
            {
                "files": [{"id": "file1", "name": "one.pdf", "mimeType": "application/pdf"}],
                "nextPageToken": "page-2",
            },
            {"files": [{"id": "file2", "name": "two.pdf", "mimeType": "application/pdf"}]},
        ]
        downloader = MagicMock()
        downloader.next_chunk.return_value = (None, True)
        mock_MediaIoBaseDownload.return_value = downloader

        fetcher = DriveFetcher(self.config)
        documents = fetcher.fetch_documents()

        self.assertEqual(len(documents), 2)
        self.assertEqual(fetcher.last_run["pages"], 2)
        self.assertEqual(service.files.return_value.list.call_count, 2)
        for document in documents:
            os.remove(document["local_path"])

    @patch("src.document_fetchers.freshdesk_fetcher.requests.get")
    def test_freshdesk_fetcher(self, mock_requests_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "tickets": [
                {"id": 1, "attachments": [{"id": 101, "name": "report.pdf", "attachment_url": "https://example.com/report.pdf"}]}
            ]
        }
        conversations_response = MagicMock()
        conversations_response.json.return_value = []
        attachment_response = MagicMock()
        attachment_response.headers = {}
        attachment_response.iter_content.return_value = [b"dummy_pdf_content"]
        mock_requests_get.side_effect = [mock_response, conversations_response, attachment_response]

        fetcher = FreshdeskFetcher(self.config)
        documents = fetcher.fetch_documents()
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["original_filename"], "report.pdf")
        self.assertEqual(documents[0]["metadata"]["source_provenance"], {})
        self.assertIsNone(fetcher.last_run["sourceProvenance"])
        self.assertTrue(os.path.exists(documents[0]["local_path"]))
        os.remove(documents[0]["local_path"])

    @patch("src.document_fetchers.freshdesk_fetcher.requests.get")
    def test_freshdesk_api_url_compatibility_is_normalized(self, mock_requests_get):
        listing_response = MagicMock()
        listing_response.json.return_value = []
        mock_requests_get.return_value = listing_response

        fetcher = FreshdeskFetcher({
            **self.config,
            "freshdesk_domain": "",
            "freshdesk_api_url": "https://example.freshdesk.com/api",
        })
        documents = fetcher.fetch_documents()

        self.assertEqual(documents, [])
        self.assertEqual(
            mock_requests_get.call_args.args[0],
            "https://example.freshdesk.com/api/v2/tickets",
        )

    @patch("src.document_fetchers.freshdesk_fetcher.requests.get")
    def test_freshdesk_attachment_fetch_rejects_untrusted_host_and_redirect(self, mock_requests_get):
        fetcher = FreshdeskFetcher(self.config)
        attachment = {
            "id": 1,
            "name": "invoice.pdf",
            "content_type": "application/pdf",
            "attachment_url": "https://127.0.0.1/private.pdf",
        }
        kwargs = {
            "ticket": {"id": 1},
            "conversation": {},
            "attachment": attachment,
            "document_id": "freshdesk_1_1",
            "match": None,
            "attachments_dir": self.config["freshdesk_download_dir"],
            "timeout": 10,
            "max_attachment_bytes": 1024,
            "pdf_only": True,
        }

        kwargs["attachment"] = {**attachment, "attachment_url": "https://attacker.example/private.pdf"}
        blocked = fetcher._attachment_document(**kwargs)
        self.assertEqual(blocked["reason"], "unsafe_url")
        mock_requests_get.assert_not_called()

        kwargs["attachment"] = attachment
        blocked = fetcher._attachment_document(**kwargs)
        self.assertEqual(blocked["reason"], "unsafe_url")
        mock_requests_get.assert_not_called()

        redirect_response = MagicMock()
        redirect_response.status_code = 302
        mock_requests_get.return_value = redirect_response
        kwargs["attachment"] = {
            **attachment,
            "attachment_url": "https://example.com/redirect.pdf",
        }
        redirected = fetcher._attachment_document(**kwargs)

        self.assertEqual(redirected["reason"], "unsafe_url")
        self.assertFalse(mock_requests_get.call_args.kwargs["allow_redirects"])
        redirect_response.close.assert_called_once()

    @patch("src.document_fetchers.freshdesk_fetcher.requests.put")
    @patch("src.document_fetchers.freshdesk_fetcher.requests.get")
    def test_freshdesk_financial_profile_retains_description_and_verified_pdf(
        self,
        mock_requests_get,
        mock_requests_put,
    ):
        listing_response = MagicMock()
        listing_response.json.return_value = {"results": [{"id": 42}]}
        detail_response = MagicMock()
        detail_response.json.return_value = {
            "id": 42,
            "subject": "Uw factuur voor juni",
            "description": "<p>Invoice 2026-42 voor onderhoud.</p>",
            "description_text": "Invoice 2026-42 voor onderhoud.",
            "status": 2,
            "created_at": "2026-07-20T09:00:00Z",
            "updated_at": "2026-07-21T10:00:00Z",
            "tags": ["bookkeeping"],
            "attachments": [{
                "id": 701,
                "name": "invoice-42.pdf",
                "content_type": "application/pdf",
                "size": len(b"%PDF-1.7\ninvoice evidence"),
                "attachment_url": "https://example.com/invoice-42.pdf",
                "created_at": "2026-07-20T09:05:00Z",
            }],
        }
        conversations_response = MagicMock()
        conversations_response.json.return_value = []
        attachment_response = MagicMock()
        attachment_response.headers = {}
        attachment_response.iter_content.return_value = [
            b"%PDF-1.7\ninvoice evidence"
        ]
        mock_requests_get.side_effect = [
            listing_response,
            detail_response,
            conversations_response,
            attachment_response,
        ]
        config = {
            **self.config,
            "freshdesk_financial_filter_enabled": True,
            "freshdesk_financial_keywords": "factuur,invoice",
            "freshdesk_include_ticket_description": True,
            "freshdesk_pdf_only": True,
        }

        fetcher = FreshdeskFetcher(config)
        documents = fetcher.fetch_documents()

        self.assertEqual(len(documents), 2)
        by_role = {
            document["metadata"]["evidence_role"]: document
            for document in documents
        }
        description = by_role["ticket_description"]
        attachment = by_role["ticket_attachment"]
        self.assertFalse(description["metadata"]["posting_eligible"])
        self.assertEqual(description["mime_type"], "text/plain")
        with open(description["local_path"], "r", encoding="utf-8") as handle:
            retained_text = handle.read()
        self.assertIn("Invoice 2026-42", retained_text)
        self.assertEqual(
            description["metadata"]["source_provenance"]["repository"],
            "Noodzakelijk-Online/025-Scan-to-folder-automation",
        )
        self.assertEqual(
            attachment["metadata"]["attachment_policy"],
            "pdf_only_magic_verified",
        )
        self.assertEqual(fetcher.last_run["matchedTickets"], 1)
        self.assertEqual(fetcher.last_run["descriptionDocuments"], 1)
        self.assertEqual(fetcher.last_run["attachmentDocuments"], 1)
        self.assertEqual(fetcher.last_run["ticketMutation"], "not_executed")
        first_call = mock_requests_get.call_args_list[0]
        self.assertTrue(first_call.args[0].endswith("/search/tickets"))
        self.assertEqual(
            first_call.kwargs["params"]["query"],
            '"status:2 OR status:3"',
        )
        self.assertIsNone(mock_requests_get.call_args_list[3].kwargs["auth"])
        self.assertTrue(mock_requests_get.call_args_list[3].kwargs["stream"])
        mock_requests_put.assert_not_called()
        for document in documents:
            os.remove(document["local_path"])

    @patch("src.document_fetchers.drive_fetcher.build")
    @patch("src.document_fetchers.drive_fetcher.InstalledAppFlow")
    @patch("src.document_fetchers.drive_fetcher.MediaIoBaseDownload")
    @patch("src.document_fetchers.drive_fetcher.Request")
    @patch("src.document_fetchers.drive_fetcher.os.path.exists")
    @patch("src.document_fetchers.drive_fetcher.GoogleOAuthTokenStore")
    def test_drive_fetcher_rejects_oversized_metadata_before_download(
        self,
        mock_pickle,
        mock_exists,
        mock_Request,
        mock_MediaIoBaseDownload,
        mock_InstalledAppFlow,
        mock_build,
    ):
        mock_exists.return_value = True
        mock_pickle.return_value.load.return_value = MagicMock()
        service = MagicMock()
        mock_build.return_value = service
        service.files.return_value.list.return_value.execute.return_value = {
            "files": [{
                "id": "large-file",
                "name": "large.pdf",
                "mimeType": "application/pdf",
                "size": "2048",
            }],
        }
        fetcher = DriveFetcher({
            **self.config,
            "google_drive_max_file_bytes": 1024,
        })

        documents = fetcher.fetch_documents()

        self.assertEqual(documents, [])
        self.assertEqual(fetcher.last_run["skipped"], 1)
        self.assertEqual(fetcher.last_run["oversized"], 1)
        mock_MediaIoBaseDownload.assert_not_called()

    @patch("src.document_fetchers.freshdesk_fetcher.requests.get")
    def test_freshdesk_financial_profile_stops_oversized_stream(
        self,
        mock_requests_get,
    ):
        listing_response = MagicMock()
        listing_response.json.return_value = {"results": [{"id": 43}]}
        detail_response = MagicMock()
        detail_response.json.return_value = {
            "id": 43,
            "subject": "Invoice attachment",
            "description_text": "",
            "status": 2,
            "attachments": [{
                "id": 702,
                "name": "invoice-43.pdf",
                "content_type": "application/pdf",
                "attachment_url": "https://example.com/invoice-43.pdf",
            }],
        }
        conversations_response = MagicMock()
        conversations_response.json.return_value = []
        attachment_response = MagicMock()
        attachment_response.headers = {}
        attachment_response.iter_content.return_value = [
            b"%PDF-1.7\n",
            b"x" * 2048,
        ]
        mock_requests_get.side_effect = [
            listing_response,
            detail_response,
            conversations_response,
            attachment_response,
        ]

        fetcher = FreshdeskFetcher({
            **self.config,
            "freshdesk_financial_filter_enabled": True,
            "freshdesk_financial_keywords": "invoice",
            "freshdesk_include_ticket_description": False,
            "freshdesk_pdf_only": True,
            "freshdesk_max_attachment_bytes": 1024,
        })
        documents = fetcher.fetch_documents()

        self.assertEqual(documents, [])
        self.assertEqual(fetcher.last_run["rejectedAttachments"]["oversized"], 1)
        attachment_response.close.assert_called_once()

    @patch("src.document_fetchers.freshdesk_fetcher.requests.get")
    def test_freshdesk_financial_profile_filters_unmatched_tickets(
        self,
        mock_requests_get,
    ):
        listing_response = MagicMock()
        listing_response.json.return_value = {"results": [{"id": 99}]}
        detail_response = MagicMock()
        detail_response.json.return_value = {
            "id": 99,
            "subject": "General support question",
            "description_text": "Please help with account access.",
            "status": 2,
            "attachments": [{
                "id": 1,
                "name": "unrelated.pdf",
                "content_type": "application/pdf",
                "attachment_url": "https://example.com/unrelated.pdf",
            }],
        }
        mock_requests_get.side_effect = [listing_response, detail_response]

        fetcher = FreshdeskFetcher({
            **self.config,
            "freshdesk_financial_filter_enabled": True,
            "freshdesk_financial_keywords": "factuur,invoice",
            "freshdesk_include_ticket_description": True,
            "freshdesk_pdf_only": True,
        })
        documents = fetcher.fetch_documents()

        self.assertEqual(documents, [])
        self.assertEqual(fetcher.last_run["matchedTickets"], 0)
        self.assertEqual(fetcher.last_run["filteredTickets"], 1)
        self.assertEqual(mock_requests_get.call_count, 2)

    @patch("src.document_fetchers.photos_fetcher.build")
    @patch("src.document_fetchers.photos_fetcher.InstalledAppFlow")
    @patch("src.document_fetchers.photos_fetcher.Request")
    @patch("src.document_fetchers.photos_fetcher.os.path.exists")
    @patch("src.document_fetchers.photos_fetcher.GoogleOAuthTokenStore")
    @patch("src.document_fetchers.photos_fetcher.requests.get")
    def test_photos_fetcher(self, mock_requests_get, mock_pickle, mock_exists, mock_Request, mock_InstalledAppFlow, mock_build):
        mock_exists.return_value = True
        mock_pickle.return_value.load.return_value = MagicMock()
        
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.albums.return_value.list.return_value.execute.return_value = {
            "albums": [{
                "id": "album123",
                "title": "Test Album"
            }]
        }

        mock_service.mediaItems.return_value.search.return_value.execute.return_value = {
            "mediaItems": [
                {"id": "photo1", "filename": "receipt.jpg", "mimeType": "image/jpeg", "baseUrl": "http://example.com/receipt.jpg"}
            ]
        }

        mock_response = MagicMock()
        mock_response.content = b"dummy_image_content"
        mock_response.raise_for_status.return_value = None
        mock_response.iter_content.return_value = [b"dummy_image_content"]
        mock_requests_get.return_value = mock_response

        fetcher = PhotosFetcher(self.config)
        documents = fetcher.fetch_documents()
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["original_filename"], "receipt.jpg")
        self.assertTrue(os.path.exists(documents[0]["local_path"]))
        os.remove(documents[0]["local_path"])

    def tearDown(self):
        # Clean up dummy files and directories
        for key in ["gmail", "google_drive", "google_photos"]:
            if os.path.exists(self.config[f"{key}_credentials_file"]):
                os.remove(self.config[f"{key}_credentials_file"])
            if os.path.exists(self.config[f"{key}_token_file"]):
                os.remove(self.config[f"{key}_token_file"])
        
        for key in [
            "gmail_attachment_download_dir",
            "google_drive_download_dir",
            "freshdesk_download_dir",
            "google_photos_download_dir",
        ]:
            if os.path.exists(self.config[key]):
                shutil.rmtree(self.config[key])

def _gmail_message(attachment_id, filename, sender="vendor@example.com"):
    return {
        "internalDate": "1735689600000",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Scanner document"},
                {"name": "From", "value": sender},
            ],
            "parts": [{
                "filename": filename,
                "body": {"attachmentId": attachment_id},
                "mimeType": "application/pdf",
            }],
        },
    }


if __name__ == "__main__":
    unittest.main()


