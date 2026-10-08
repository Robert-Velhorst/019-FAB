from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import tempfile
from typing import Any, Dict, Optional
from urllib.parse import quote, urlparse

import requests
from src.security.google_oauth_store import GoogleOAuthTokenStore

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    Request = None
    Credentials = None
    InstalledAppFlow = None


PICKER_SCOPE = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"
PICKER_API_BASE = "https://photospicker.googleapis.com/v1"


class UnsupportedPickerMedia(ValueError):
    pass


class GooglePhotosPickerClient:
    """Thin authenticated client for user-owned Google Photos Picker sessions."""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        http: Optional[Any] = None,
        credentials: Optional[Any] = None,
    ):
        self.config = config or {}
        self.http = http or requests.Session()
        self._credentials = credentials
        self.timeout = _bounded_float(
            _config_value(
                self.config,
                "google_photos_request_timeout_seconds",
                "source_request_timeout_seconds",
                "request_timeout_seconds",
                default=30,
            ),
            30,
            1,
            300,
        )
        self.max_pages = _bounded_int(
            _config_value(self.config, "google_photos_max_pages", default=50),
            50,
            1,
            500,
        )
        self.max_items = _bounded_int(
            _config_value(self.config, "google_photos_max_items", default=1000),
            1000,
            1,
            10000,
        )
        self.max_media_bytes = _bounded_int(
            _config_value(
                self.config,
                "google_photos_max_media_bytes",
                default=25 * 1024 * 1024,
            ),
            25 * 1024 * 1024,
            1024,
            1024 * 1024 * 1024,
        )

    @property
    def download_dir(self) -> str:
        value = _config_value(
            self.config,
            "google_photos_download_dir",
            "google_photos_picker_download_dir",
            default="data/source_downloads/google-photos-picker",
        )
        return os.path.abspath(os.path.expandvars(os.path.expanduser(str(value))))

    @property
    def token_path(self) -> str:
        value = _config_value(
            self.config,
            "google_photos_picker_token_file",
            default="tokens/photos_picker_token.json",
        )
        return os.path.abspath(os.path.expandvars(os.path.expanduser(str(value))))

    @property
    def credentials_path(self) -> str:
        value = _config_value(
            self.config,
            "google_photos_credentials_file",
            default="credentials/photos_picker_credentials.json",
        )
        return os.path.abspath(os.path.expandvars(os.path.expanduser(str(value))))

    def authorize_interactively(self) -> Dict[str, Any]:
        if InstalledAppFlow is None:
            raise ImportError("google-auth-oauthlib is required for Google Photos Picker authorization.")
        if not os.path.isfile(self.credentials_path):
            raise FileNotFoundError(f"Google Photos Picker credentials file not found: {self.credentials_path}")
        flow = InstalledAppFlow.from_client_secrets_file(self.credentials_path, [PICKER_SCOPE])
        credentials = flow.run_local_server(port=0)
        self._credentials = credentials
        self._save_credentials(credentials)
        return {
            "success": True,
            "status": "authorized",
            "tokenFile": self.token_path,
            "scope": PICKER_SCOPE,
        }

    def create_session(self) -> Dict[str, Any]:
        payload = self._json_request("POST", "/sessions", json={})
        if not payload.get("id") or not payload.get("pickerUri"):
            raise RuntimeError("Google Photos Picker returned an incomplete session payload.")
        return payload

    def get_session(self, session_id: str) -> Dict[str, Any]:
        return self._json_request("GET", f"/sessions/{quote(str(session_id), safe='')}")

    def delete_session(self, session_id: str) -> None:
        self._request("DELETE", f"/sessions/{quote(str(session_id), safe='')}")

    def list_media_items(self, session_id: str) -> Dict[str, Any]:
        items = []
        seen_ids = set()
        page_token = None
        pages = 0
        truncated = False
        while pages < self.max_pages and len(items) < self.max_items:
            params = {
                "sessionId": session_id,
                "pageSize": min(100, self.max_items - len(items)),
            }
            if page_token:
                params["pageToken"] = page_token
            payload = self._json_request("GET", "/mediaItems", params=params)
            pages += 1
            for item in payload.get("mediaItems") or []:
                if not isinstance(item, dict):
                    continue
                provider_id = str(item.get("id") or "").strip()
                if not provider_id or provider_id in seen_ids:
                    continue
                seen_ids.add(provider_id)
                items.append(item)
                if len(items) >= self.max_items:
                    break
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        if page_token:
            truncated = True
        return {"items": items, "pages": pages, "truncated": truncated}

    def download_media_item(self, item: Dict[str, Any], session_id: str) -> Dict[str, Any]:
        if str(item.get("type") or "").upper() != "PHOTO":
            raise UnsupportedPickerMedia("Only photos can enter FAB's receipt document pipeline.")
        provider_id = str(item.get("id") or "").strip()
        media_file = item.get("mediaFile") if isinstance(item.get("mediaFile"), dict) else {}
        base_url = str(media_file.get("baseUrl") or "").strip()
        if not provider_id or not base_url:
            raise ValueError("Selected Google Photos item is missing its stable id or baseUrl.")
        mime_type = str(media_file.get("mimeType") or "application/octet-stream")
        if not mime_type.lower().startswith("image/"):
            raise UnsupportedPickerMedia("Selected photo does not expose an image MIME type.")
        filename = _safe_filename(media_file.get("filename"), mime_type)
        os.makedirs(self.download_dir, exist_ok=True)
        response = self._request(
            "GET",
            _validated_media_url(base_url),
            absolute=True,
            stream=True,
            allow_redirects=False,
        )
        try:
            content_length = response.headers.get("Content-Length") if hasattr(response, "headers") else None
            try:
                declared_length = int(content_length) if content_length else None
            except (TypeError, ValueError):
                declared_length = None
            if declared_length and declared_length > self.max_media_bytes:
                raise ValueError(f"Selected media exceeds the configured {self.max_media_bytes}-byte limit.")

            file_handle, temporary_path = tempfile.mkstemp(
                prefix=".fab-photos-picker-",
                suffix=".part",
                dir=self.download_dir,
            )
            total = 0
            digest = hashlib.sha256()
            try:
                with os.fdopen(file_handle, "wb") as handle:
                    iterator = response.iter_content(chunk_size=1024 * 1024)
                    for chunk in iterator:
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > self.max_media_bytes:
                            raise ValueError(
                                f"Selected media exceeds the configured {self.max_media_bytes}-byte limit."
                            )
                        digest.update(chunk)
                        handle.write(chunk)
                if total == 0:
                    raise ValueError("Selected Google Photos item downloaded as an empty file.")
                content_hash = digest.hexdigest()
                prefix = hashlib.sha256(f"{provider_id}:{content_hash}".encode("utf-8")).hexdigest()[:12]
                final_path = os.path.join(self.download_dir, f"{prefix}-{filename}")
                if os.path.exists(final_path):
                    os.remove(temporary_path)
                else:
                    os.replace(temporary_path, final_path)
            except Exception:
                if os.path.exists(temporary_path):
                    os.remove(temporary_path)
                raise
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

        return {
            "id": provider_id,
            "source": "google_photos",
            "original_filename": filename,
            "mime_type": mime_type,
            "local_path": final_path,
            "timestamp": item.get("createTime"),
            "metadata": {
                "picker_session_id": session_id,
                "media_type": item.get("type"),
                "media_file_metadata": media_file.get("mediaFileMetadata") or {},
                "content_sha256": content_hash,
                "size_bytes": total,
            },
        }

    def _json_request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        response = self._request(method, path, **kwargs)
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Google Photos Picker returned a non-object JSON payload.")
        return payload

    def _request(self, method: str, path: str, absolute: bool = False, **kwargs: Any) -> Any:
        credentials = self._credentials_for_request()
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {credentials.token}"
        headers.setdefault("Accept", "application/json")
        url = path if absolute else f"{PICKER_API_BASE}{path}"
        response = self.http.request(
            method,
            url,
            headers=headers,
            timeout=self.timeout,
            **kwargs,
        )
        status_code = int(getattr(response, "status_code", 200) or 200)
        if 300 <= status_code < 400:
            raise RuntimeError("Google Photos Picker returned an unexpected redirect.")
        response.raise_for_status()
        return response

    def _credentials_for_request(self) -> Any:
        credentials = self._credentials or self._load_credentials()
        if getattr(credentials, "expired", False) and getattr(credentials, "refresh_token", None):
            if Request is None:
                raise ImportError("google-auth is required to refresh the Google Photos Picker token.")
            credentials.refresh(Request())
            self._save_credentials(credentials)
        if not getattr(credentials, "valid", bool(getattr(credentials, "token", None))):
            raise RuntimeError(
                "Google Photos Picker token is invalid; run the supervised authorization command."
            )
        has_scopes = getattr(credentials, "has_scopes", None)
        if callable(has_scopes) and not has_scopes([PICKER_SCOPE]):
            raise RuntimeError("Google Photos Picker token does not include the required read-only scope.")
        self._credentials = credentials
        return credentials

    def _load_credentials(self) -> Any:
        if Credentials is None:
            raise ImportError("google-auth is required for Google Photos Picker access.")
        if not self.token_path.lower().endswith(".json"):
            raise RuntimeError("Google Photos Picker tokens must use JSON; unsafe pickle tokens are not loaded.")
        if not os.path.isfile(self.token_path):
            raise FileNotFoundError(
                "Google Photos Picker token is missing; run python -m src.run_photos_picker_auth."
            )
        return GoogleOAuthTokenStore(
            self.token_path,
            [PICKER_SCOPE],
            credentials_type=Credentials,
        ).load()

    def _save_credentials(self, credentials: Any) -> None:
        if not self.token_path.lower().endswith(".json"):
            raise RuntimeError("Google Photos Picker token output must use a .json path.")
        GoogleOAuthTokenStore(
            self.token_path,
            [PICKER_SCOPE],
            credentials_type=Credentials,
        ).save(credentials)


def _safe_filename(value: Any, mime_type: str) -> str:
    filename = os.path.basename(str(value or "selected-photo"))
    filename = re.sub(r"[^A-Za-z0-9._ -]+", "_", filename).strip(" .") or "selected-photo"
    if not os.path.splitext(filename)[1]:
        extension = mimetypes.guess_extension(mime_type) or ""
        filename = f"{filename}{extension}"
    if len(filename) > 180:
        stem, extension = os.path.splitext(filename)
        extension = extension[:20]
        filename = f"{stem[:max(1, 180 - len(extension))]}{extension}"
    return filename


def _validated_media_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    hostname = str(parsed.hostname or "").lower()
    if (
        parsed.scheme.lower() != "https"
        or not hostname.endswith(".googleusercontent.com")
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Google Photos Picker returned an untrusted media baseUrl.")
    return f"{base_url}=d"


def _config_value(config: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = config.get(key)
        if value not in (None, ""):
            return value
    return default


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))
