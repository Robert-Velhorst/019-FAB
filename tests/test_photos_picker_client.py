import os
import tempfile
import unittest
from unittest.mock import patch

from src.document_fetchers.photos_picker_client import GooglePhotosPickerClient
from src.document_fetchers.photos_picker_client import PICKER_SCOPE


class _Credentials:
    token = "picker-access-token"
    valid = True
    expired = False
    refresh_token = None

    @staticmethod
    def has_scopes(scopes):
        return bool(scopes)


class _Response:
    def __init__(self, payload=None, content=b"", headers=None):
        self.payload = payload
        self.content = content
        self.headers = headers or {}
        self.closed = False

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload

    def iter_content(self, chunk_size=1024):
        for index in range(0, len(self.content), chunk_size):
            yield self.content[index:index + chunk_size]

    def close(self):
        self.closed = True


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class TestGooglePhotosPickerClient(unittest.TestCase):
    def test_session_pagination_and_bounded_photo_download(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media_response = _Response(
                content=b"receipt-image-bytes",
                headers={"Content-Length": "19"},
            )
            http = _Http([
                _Response({
                    "id": "session-1",
                    "pickerUri": "https://photos.google.com/picker/session-1",
                }),
                _Response({
                    "mediaItems": [{
                        "id": "photo-1",
                        "type": "PHOTO",
                        "mediaFile": {"filename": "receipt.jpg"},
                    }],
                    "nextPageToken": "page-2",
                }),
                _Response({
                    "mediaItems": [{
                        "id": "photo-2",
                        "type": "PHOTO",
                        "mediaFile": {"filename": "invoice.png"},
                    }],
                }),
                media_response,
            ])
            client = GooglePhotosPickerClient(
                {
                    "google_photos_download_dir": temp_dir,
                    "google_photos_max_pages": 5,
                    "google_photos_max_items": 10,
                    "google_photos_max_media_bytes": 100,
                },
                http=http,
                credentials=_Credentials(),
            )

            session = client.create_session()
            listed = client.list_media_items(session["id"])
            downloaded = client.download_media_item({
                "id": "photo-1",
                "type": "PHOTO",
                "createTime": "2026-07-13T08:00:00Z",
                "mediaFile": {
                    "baseUrl": "https://lh3.googleusercontent.com/photo-1",
                    "mimeType": "image/jpeg",
                    "filename": "../receipt?.jpg",
                    "mediaFileMetadata": {"width": 1200, "height": 800},
                },
            }, session["id"])

            self.assertEqual(session["id"], "session-1")
            self.assertEqual([item["id"] for item in listed["items"]], ["photo-1", "photo-2"])
            self.assertEqual(listed["pages"], 2)
            self.assertFalse(listed["truncated"])
            self.assertTrue(os.path.isfile(downloaded["local_path"]))
            self.assertEqual(os.path.basename(downloaded["local_path"]).split("-", 1)[1], "receipt_.jpg")
            self.assertEqual(downloaded["metadata"]["size_bytes"], 19)
            self.assertTrue(media_response.closed)
            self.assertEqual(http.calls[-1][1], "https://lh3.googleusercontent.com/photo-1=d")
            for _, _, kwargs in http.calls:
                self.assertEqual(kwargs["headers"]["Authorization"], "Bearer picker-access-token")

    def test_media_listing_reports_truncation_instead_of_claiming_completeness(self):
        http = _Http([
            _Response({
                "mediaItems": [{"id": "photo-1"}],
                "nextPageToken": "still-more",
            }),
        ])
        client = GooglePhotosPickerClient(
            {"google_photos_max_pages": 1, "google_photos_max_items": 100},
            http=http,
            credentials=_Credentials(),
        )

        result = client.list_media_items("session-1")

        self.assertEqual(result["pages"], 1)
        self.assertTrue(result["truncated"])

    def test_pickle_token_paths_are_rejected(self):
        client = GooglePhotosPickerClient({
            "google_photos_picker_token_file": "tokens/unsafe-token.pickle",
        })

        with self.assertRaisesRegex(RuntimeError, "must use JSON"):
            client._load_credentials()

    @patch("src.document_fetchers.photos_picker_client.GoogleOAuthTokenStore")
    @patch("src.document_fetchers.photos_picker_client.Credentials", new=object)
    def test_token_load_and_save_use_shared_oauth_store(self, token_store):
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = os.path.join(temp_dir, "photos-picker.json")
            client = GooglePhotosPickerClient({
                "google_photos_picker_token_file": token_path,
            })
            with open(token_path, "w", encoding="utf-8") as handle:
                handle.write("{}")
            store = token_store.return_value
            store.load.return_value = "loaded-credentials"

            self.assertEqual(client._load_credentials(), "loaded-credentials")
            client._save_credentials("refreshed-credentials")

            token_store.assert_called_with(
                token_path,
                [PICKER_SCOPE],
                credentials_type=object,
            )
            self.assertEqual(token_store.call_count, 2)
            store.load.assert_called_once_with()
            store.save.assert_called_once_with("refreshed-credentials")

    def test_untrusted_media_url_is_rejected_before_bearer_token_is_sent(self):
        http = _Http([])
        client = GooglePhotosPickerClient(http=http, credentials=_Credentials())

        with self.assertRaisesRegex(ValueError, "untrusted media baseUrl"):
            client.download_media_item({
                "id": "photo-1",
                "type": "PHOTO",
                "mediaFile": {
                    "baseUrl": "https://example.test/steal-token",
                    "mimeType": "image/jpeg",
                    "filename": "receipt.jpg",
                },
            }, "session-1")

        self.assertEqual(http.calls, [])


if __name__ == "__main__":
    unittest.main()
