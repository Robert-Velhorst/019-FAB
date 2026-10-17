import os
import unittest

from src.document_fetchers.base import BaseFetcher


class MinimalFetcher(BaseFetcher):
    def fetch_documents(self):
        return []

    def _authenticate(self):
        return None


class TestFetcherDownloadDirectories(unittest.TestCase):
    def test_provider_defaults_use_persistent_application_data_paths(self):
        fetcher = MinimalFetcher({})

        for source in ("gmail", "google_drive", "freshdesk", "google_photos"):
            with self.subTest(source=source):
                self.assertEqual(
                    fetcher._download_directory(source),
                    os.path.join("data", "source_downloads", source),
                )

        self.assertEqual(
            fetcher._download_directory("source"),
            os.path.join("data", "source_downloads"),
        )

    def test_provider_section_download_directory_is_used(self):
        fetcher = MinimalFetcher({"google_drive": {"download_dir": "custom/drive"}})

        self.assertEqual(
            fetcher._download_directory("google_drive", "google_drive_download_dir"),
            os.path.join("custom", "drive"),
        )

    def test_flat_config_override_precedes_shared_override(self):
        fetcher = MinimalFetcher(
            {
                "gmail_attachment_download_dir": "custom/gmail",
                "attachments_save_dir": "shared/attachments",
            }
        )

        self.assertEqual(
            fetcher._download_directory("gmail", "gmail_attachment_download_dir"),
            os.path.join("custom", "gmail"),
        )

    def test_shared_override_is_used_before_provider_default(self):
        fetcher = MinimalFetcher({"attachments_save_dir": "shared/attachments"})

        self.assertEqual(
            fetcher._download_directory("freshdesk", "freshdesk_download_dir"),
            os.path.join("shared", "attachments"),
        )


if __name__ == "__main__":
    unittest.main()
