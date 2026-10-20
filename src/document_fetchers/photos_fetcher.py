try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    Credentials = None
    InstalledAppFlow = None
    Request = None
    build = None
    HttpError = Exception
import os
from typing import List, Dict, Any
import requests

from src.document_fetchers.base import BaseFetcher
from src.security.google_oauth_store import (
    GoogleOAuthTokenStore,
    LegacyGoogleOAuthTokenError,
)

class PhotosFetcher(BaseFetcher):
    """Fetches documents (images) from Google Photos."""

    # The Photos API does not have a direct way to search for 'documents' or 'receipts'.
    # This fetcher will iterate through media items and download them. Further processing
    # (like OCR) will determine if they are financial documents.
    SCOPES = ["https://www.googleapis.com/auth/photoslibrary.readonly"]

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.creds = None
        self.service = None
        self.auth_error = None
        try:
            self._authenticate()
        except Exception as exc:
            self.auth_error = exc

    def _authenticate(self):
        if build is None or InstalledAppFlow is None or Request is None:
            raise ImportError("Google API dependencies are required for Photos fetching.")

        configured_token_path = self.config.get("google_photos_token_file") or self.config.get(
            "photos_token_path", "tokens/photos_token.json"
        )
        credentials_path = self.config.get("google_photos_credentials_file") or self.config.get("photos_credentials_path", "credentials_photos.json")
        token_store = GoogleOAuthTokenStore(
            configured_token_path,
            self.SCOPES,
            credentials_type=Credentials,
        )

        load_error = None
        try:
            self.creds = token_store.load()
        except (FileNotFoundError, LegacyGoogleOAuthTokenError, ValueError) as exc:
            load_error = exc

        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                self.creds.refresh(Request())
            else:
                if not self._interactive_auth_enabled("google_photos"):
                    if isinstance(load_error, LegacyGoogleOAuthTokenError):
                        raise load_error
                    raise RuntimeError(
                        "Google Photos OAuth requires a valid token; interactive authorization is disabled for autonomous runs."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    credentials_path, self.SCOPES
                )
                self.creds = flow.run_local_server(port=0)
            token_store.save(self.creds)

        self.service = build("photoslibrary", "v1", credentials=self.creds, static_discovery=False)

    def fetch_documents(self) -> List[Dict[str, Any]]:
        self._start_run()
        if self.auth_error:
            self._fail_run(self.auth_error)
            return []

        attachments_dir = self._download_directory(
            "google_photos", "google_photos_download_dir"
        )
        os.makedirs(attachments_dir, exist_ok=True)

        documents = []
        page_token = None
        pages = 0
        skipped = 0
        try:
            while True:
                album_name = self.config.get("google_photos_album_name")
                if album_name:
                    albums = self.service.albums().list(pageSize=50).execute().get("albums", [])
                    album = next((item for item in albums if item.get("title") == album_name), None)
                    if not album:
                        self._finish_run(0, skipped=0, pages=pages)
                        return []
                    results = self.service.mediaItems().search(body={"albumId": album["id"], "pageSize": 100, "pageToken": page_token}).execute()
                else:
                    results = self.service.mediaItems().list(pageSize=100, pageToken=page_token).execute()
                items = results.get("mediaItems", [])
                pages += 1

                if not items:
                    break

                for item in items:
                    # The Google Photos API provides a 'baseUrl' for downloading the image.
                    # We can append '=d' to download the original size.
                    download_url = item["baseUrl"] + "=d"
                    file_name = item["filename"]
                    
                    try:
                        response = requests.get(
                            download_url,
                            stream=True,
                            timeout=self._request_timeout(),
                        )
                        response.raise_for_status()

                        local_path = self._download_path(attachments_dir, file_name, item["id"])
                        with open(local_path, "wb") as f:
                            for chunk in response.iter_content(chunk_size=8192):
                                f.write(chunk)

                        documents.append({
                            "id": item["id"],
                            "source": "google_photos",
                            "original_filename": file_name,
                            "local_path": local_path,
                            "timestamp": item.get("creationTime", ""),
                            "metadata": {
                                "mime_type": item.get("mimeType"),
                                "product_url": item.get("productUrl")
                            }
                        })
                    except requests.exceptions.RequestException:
                        skipped += 1

                page_token = results.get("nextPageToken")
                if not page_token:
                    break
            self._finish_run(len(documents), skipped=skipped, pages=pages)
        except Exception as error:
            self._fail_run(error, fetched=len(documents), skipped=skipped, pages=pages)
        return documents


