from __future__ import annotations

import hashlib
from typing import Any, Dict, Optional

from src.document_processors.document_resource_limits import MAX_SOURCE_DOCUMENT_BYTES
from src.security.google_oauth_store import (
    GoogleOAuthTokenStore,
    LegacyGoogleOAuthTokenError,
)

DRIVE_HASH_CHUNK_BYTES = 8 * 1024 * 1024

try:
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
except ImportError:
    Request = None
    InstalledAppFlow = None
    build = None
    MediaIoBaseDownload = None


class DriveArchiveClient:
    """Move a verified Drive source while preserving its provider file ID."""

    SCOPES = ["https://www.googleapis.com/auth/drive"]
    FILE_FIELDS = "id,name,mimeType,parents,size,md5Checksum,modifiedTime,trashed,webViewLink"

    def __init__(self, config: Optional[Dict[str, Any]] = None, service: Any = None):
        self.config = config or {}
        self.service = service or self._authenticate()

    def inspect_file(self, file_id: str) -> Dict[str, Any]:
        return self.service.files().get(
            fileId=str(file_id),
            fields=self.FILE_FIELDS,
            supportsAllDrives=True,
        ).execute()

    def download_sha256(self, file_id: str) -> str:
        if MediaIoBaseDownload is None:
            raise RuntimeError("Google Drive download support is unavailable.")
        request = self.service.files().get_media(fileId=str(file_id), supportsAllDrives=True)
        digest = _LimitedHashSink(MAX_SOURCE_DOCUMENT_BYTES)
        downloader = MediaIoBaseDownload(digest, request, chunksize=DRIVE_HASH_CHUNK_BYTES)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return digest.hexdigest()

    def move_file(self, file_id: str, source_folder_id: str, archive_folder_id: str) -> Dict[str, Any]:
        if str(source_folder_id) == str(archive_folder_id):
            raise RuntimeError("Drive intake and archive folders must be different.")
        before = self.inspect_file(file_id)
        parents = set(before.get("parents") or [])
        if archive_folder_id in parents and source_folder_id not in parents:
            return {"status": "already_archived", "before": before, "after": before}
        if source_folder_id not in parents:
            raise RuntimeError("Drive source file is no longer in the configured intake folder.")

        self.service.files().update(
            fileId=str(file_id),
            addParents=str(archive_folder_id),
            removeParents=str(source_folder_id),
            fields=self.FILE_FIELDS,
            supportsAllDrives=True,
        ).execute()
        after = self.inspect_file(file_id)
        after_parents = set(after.get("parents") or [])
        if archive_folder_id not in after_parents or source_folder_id in after_parents:
            try:
                self.service.files().update(
                    fileId=str(file_id),
                    addParents=str(source_folder_id),
                    removeParents=str(archive_folder_id),
                    fields=self.FILE_FIELDS,
                    supportsAllDrives=True,
                ).execute()
            except Exception:
                pass
            raise RuntimeError("Drive archive postcondition failed; rollback was attempted.")
        return {"status": "archived", "before": before, "after": after}

    def restore_file(self, file_id: str, source_folder_id: str, archive_folder_id: str) -> Dict[str, Any]:
        """Restore a failed archive move to intake without deleting or replacing the file."""
        if str(source_folder_id) == str(archive_folder_id):
            raise RuntimeError("Drive intake and archive folders must be different.")
        before = self.inspect_file(file_id)
        parents = set(before.get("parents") or [])
        if source_folder_id in parents and archive_folder_id not in parents:
            return {"status": "already_restored", "before": before, "after": before}
        if archive_folder_id not in parents:
            raise RuntimeError("Drive archive rollback cannot find the file in the archive folder.")

        self.service.files().update(
            fileId=str(file_id),
            addParents=str(source_folder_id),
            removeParents=str(archive_folder_id),
            fields=self.FILE_FIELDS,
            supportsAllDrives=True,
        ).execute()
        after = self.inspect_file(file_id)
        after_parents = set(after.get("parents") or [])
        if source_folder_id not in after_parents or archive_folder_id in after_parents:
            raise RuntimeError("Drive archive rollback postcondition failed.")
        return {"status": "restored", "before": before, "after": after}

    def _authenticate(self) -> Any:
        if build is None or Request is None or InstalledAppFlow is None:
            raise RuntimeError("Google Drive API dependencies are required for verified archiving.")
        configured_token_path = str(
            self.config.get("google_drive_token_file")
            or self.config.get("drive_token_path")
            or "tokens/drive_token.json"
        )
        credentials_path = str(
            self.config.get("google_drive_credentials_file")
            or self.config.get("drive_credentials_path")
            or "credentials/drive_credentials.json"
        )
        credentials = None
        credentials_changed = False
        token_store = GoogleOAuthTokenStore(
            configured_token_path,
            self.SCOPES,
        )
        force_reauthorization = _as_bool(
            self.config.get("google_drive_force_reauthorization"),
            False,
        )
        load_error = None
        if not force_reauthorization:
            try:
                credentials = token_store.load()
            except (FileNotFoundError, LegacyGoogleOAuthTokenError, ValueError) as exc:
                load_error = exc

        scopes_valid = bool(credentials) and (
            not hasattr(credentials, "has_scopes") or credentials.has_scopes(self.SCOPES)
        )
        if credentials and credentials.expired and credentials.refresh_token and scopes_valid:
            credentials.refresh(Request())
            credentials_changed = True
        if not credentials or not credentials.valid or not scopes_valid:
            if not _as_bool(self.config.get("google_drive_interactive_auth"), False):
                if isinstance(load_error, LegacyGoogleOAuthTokenError):
                    raise load_error
                raise RuntimeError(
                    "Google Drive archiving needs a valid full-Drive OAuth token; "
                    "run supervised authorization with google_drive.interactive_auth enabled."
                )
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, self.SCOPES)
            credentials = flow.run_local_server(port=0)
            credentials_changed = True
        if credentials_changed:
            token_store.save(credentials)
        return build("drive", "v3", credentials=credentials)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


class _LimitedHashSink:
    def __init__(self, max_bytes: int):
        self.max_bytes = max(1, int(max_bytes))
        self.bytes_written = 0
        self._digest = hashlib.sha256()

    def write(self, content: bytes) -> int:
        next_size = self.bytes_written + len(content)
        if next_size > self.max_bytes:
            raise ValueError("drive_archive_document_exceeds_limit")
        self._digest.update(content)
        self.bytes_written = next_size
        return len(content)

    def hexdigest(self) -> str:
        return self._digest.hexdigest()
