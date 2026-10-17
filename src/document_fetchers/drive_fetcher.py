try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaIoBaseDownload
except ImportError:
    Credentials = None
    InstalledAppFlow = None
    Request = None
    build = None
    HttpError = Exception
    MediaIoBaseDownload = None
import os
import io
from typing import List, Dict, Any

from src.document_fetchers.base import BaseFetcher
from src.security.google_oauth_store import (
    GoogleOAuthTokenStore,
    LegacyGoogleOAuthTokenError,
)

class DriveFetcher(BaseFetcher):
    """Fetches documents from Google Drive based on specified folder ID."""

    SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

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
        if build is None or InstalledAppFlow is None or Request is None or MediaIoBaseDownload is None:
            raise ImportError("Google API dependencies are required for Drive fetching.")

        configured_token_path = self.config.get("google_drive_token_file") or self.config.get(
            "drive_token_path", "tokens/drive_token.json"
        )
        credentials_path = self.config.get("google_drive_credentials_file") or self.config.get("drive_credentials_path", "credentials_drive.json")
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
                if not self._interactive_auth_enabled("google_drive"):
                    if isinstance(load_error, LegacyGoogleOAuthTokenError):
                        raise load_error
                    raise RuntimeError(
                        "Google Drive OAuth requires a valid token; interactive authorization is disabled for autonomous runs."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    credentials_path, self.SCOPES
                )
                self.creds = flow.run_local_server(port=0)
            token_store.save(self.creds)

        self.service = build("drive", "v3", credentials=self.creds)

    def fetch_documents(self) -> List[Dict[str, Any]]:
        self._start_run()
        if self.auth_error:
            self._fail_run(self.auth_error)
            return []

        folder_id = self.config.get("google_drive_folder_id") or self.config.get("drive_folder_id")
        if not str(folder_id or "").strip():
            self._fail_run(
                ValueError("Google Drive folder_id is required; whole-Drive background scans are disabled.")
            )
            return []
        folder_query = f"'{folder_id}' in parents and trashed = false"

        attachments_dir = self._download_directory(
            "google_drive", "google_drive_download_dir", "drive_download_dir"
        )
        os.makedirs(attachments_dir, exist_ok=True)
        max_pages = _bounded_int(self.config.get("google_drive_max_pages"), 50, 1, 500)
        max_files = _bounded_int(self.config.get("google_drive_max_files"), 5000, 1, 50000)
        max_file_bytes = _bounded_int(
            self.config.get("google_drive_max_file_bytes"),
            25 * 1024 * 1024,
            1024,
            1024 * 1024 * 1024,
        )
        max_run_bytes = _bounded_int(
            self.config.get("google_drive_max_run_bytes"),
            250 * 1024 * 1024,
            max_file_bytes,
            100 * 1024 * 1024 * 1024,
        )

        documents = []
        skipped = 0
        oversized = 0
        downloaded_bytes = 0
        pages = 0
        try:
            page_token = None
            items = []
            while pages < max_pages and len(items) < max_files:
                result = self.service.files().list(
                    q=folder_query,
                    fields=(
                        "nextPageToken, files(id, name, mimeType, createdTime, modifiedTime, "
                        "size, md5Checksum, webViewLink)"
                    ),
                    spaces="drive",
                    pageSize=min(1000, max_files - len(items)),
                    **({"pageToken": page_token} if page_token else {}),
                ).execute()
                pages += 1
                items.extend(result.get("files", []))
                page_token = result.get("nextPageToken")
                if not page_token:
                    break

            for item in items[:max_files]:
                file_id = item["id"]
                file_name = item["name"]
                mime_type = item["mimeType"]
                created_time = item.get("createdTime", "")
                if mime_type == "application/vnd.google-apps.folder":
                    skipped += 1
                    continue
                declared_size = _optional_nonnegative_int(item.get("size"))
                if (
                    declared_size is not None
                    and (
                        declared_size > max_file_bytes
                        or downloaded_bytes + declared_size > max_run_bytes
                    )
                ):
                    skipped += 1
                    oversized += 1
                    continue

                if mime_type.startswith("application/vnd.google-apps."):
                    exportable = {
                        "application/vnd.google-apps.document",
                        "application/vnd.google-apps.presentation",
                        "application/vnd.google-apps.spreadsheet",
                        "application/vnd.google-apps.drawing",
                    }
                    if mime_type not in exportable:
                        skipped += 1
                        continue
                    request = self.service.files().export_media(
                        fileId=file_id,
                        mimeType="application/pdf",
                    )
                    if not file_name.lower().endswith(".pdf"):
                        file_name = f"{file_name}.pdf"
                    download_mime_type = "application/pdf"
                else:
                    request = self.service.files().get_media(fileId=file_id)
                    download_mime_type = mime_type
                remaining_run_bytes = max_run_bytes - downloaded_bytes
                if remaining_run_bytes <= 0:
                    skipped += 1
                    oversized += 1
                    continue
                fh = _BoundedBytesIO(min(max_file_bytes, remaining_run_bytes))
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                try:
                    while done is False:
                        status, done = downloader.next_chunk()
                except _DownloadLimitExceeded:
                    skipped += 1
                    oversized += 1
                    continue

                file_content = fh.getvalue()
                downloaded_bytes += len(file_content)
                local_path = self._store_content(
                    attachments_dir,
                    file_name,
                    file_id,
                    file_content,
                )

                documents.append(
                    {
                        "id": file_id,
                        "source": "google_drive",
                        "original_filename": file_name,
                        "mime_type": download_mime_type,
                        "local_path": local_path,
                        "timestamp": created_time,
                        "metadata": {
                            "mime_type": download_mime_type,
                            "provider_mime_type": mime_type,
                            "modified_time": item.get("modifiedTime"),
                            "size": item.get("size"),
                            "md5_checksum": item.get("md5Checksum"),
                            "web_view_link": item.get("webViewLink"),
                            "folder_id": folder_id,
                        },
                    }
                )
            self._finish_run(len(documents), skipped=skipped, pages=pages)
            self.last_run.update({
                "oversized": oversized,
                "downloadedBytes": downloaded_bytes,
                "maxFileBytes": max_file_bytes,
                "maxRunBytes": max_run_bytes,
            })
        except Exception as error:
            self._fail_run(error, fetched=len(documents), skipped=skipped, pages=pages)
            self.last_run.update({
                "oversized": oversized,
                "downloadedBytes": downloaded_bytes,
                "maxFileBytes": max_file_bytes,
                "maxRunBytes": max_run_bytes,
            })
        return documents


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _optional_nonnegative_int(value: Any):
    if value in (None, ""):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


class _DownloadLimitExceeded(RuntimeError):
    pass


class _BoundedBytesIO(io.BytesIO):
    def __init__(self, limit: int):
        super().__init__()
        self.limit = int(limit)

    def write(self, data):
        if self.tell() + len(data) > self.limit:
            raise _DownloadLimitExceeded("Google Drive file exceeded its download byte limit")
        return super().write(data)


