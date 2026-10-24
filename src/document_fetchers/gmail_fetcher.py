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
import base64
from email.utils import parseaddr
import re
from typing import List, Dict, Any

from src.document_fetchers.base import BaseFetcher
from src.document_fetchers.profiles import (
    CUSTOM_SCANNER_PROFILE_ID,
    HP_EPRINT_PROFILE_ID,
    HP_EPRINT_SENDER,
)
from src.security.google_oauth_store import (
    GoogleOAuthTokenStore,
    LegacyGoogleOAuthTokenError,
)


class GmailFetcher(BaseFetcher):
    """Fetches documents (attachments) from Gmail based on specified criteria."""

    SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

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
            raise ImportError("Google API dependencies are required for Gmail fetching.")

        configured_token_path = self.config.get("gmail_token_file") or self.config.get(
            "gmail_token_path", "tokens/gmail_token.json"
        )
        credentials_path = self.config.get("gmail_credentials_file") or self.config.get('gmail_credentials_path', 'credentials.json')
        token_store = GoogleOAuthTokenStore(
            configured_token_path,
            self.SCOPES,
            credentials_type=Credentials,
        )

        force_reauthorization = _as_bool(self.config.get("gmail_force_reauthorization"))
        load_error = None
        if not force_reauthorization:
            try:
                self.creds = token_store.load()
            except (FileNotFoundError, LegacyGoogleOAuthTokenError, ValueError) as exc:
                load_error = exc

        scopes_valid = bool(self.creds) and (
            not hasattr(self.creds, "has_scopes") or self.creds.has_scopes(self.SCOPES)
        )
        if not self.creds or not self.creds.valid or not scopes_valid:
            if self.creds and self.creds.expired and self.creds.refresh_token and scopes_valid:
                self.creds.refresh(Request())
            else:
                if not self._interactive_auth_enabled("gmail"):
                    if isinstance(load_error, LegacyGoogleOAuthTokenError):
                        raise load_error
                    raise RuntimeError(
                        "Gmail OAuth requires a valid token; interactive authorization is disabled for autonomous runs."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    credentials_path, self.SCOPES)
                self.creds = flow.run_local_server(port=0)
            token_store.save(self.creds)

        self.service = build('gmail', 'v1', credentials=self.creds)

    def fetch_documents(self) -> List[Dict[str, Any]]:
        self._start_run()
        if self.auth_error:
            self._fail_run(self.auth_error)
            return []

        scanner_mode = _as_bool(self.config.get("gmail_scanner_mode"))
        trusted_senders = _string_list(self.config.get("gmail_trusted_senders"))
        scanner_profile_id = _scanner_profile_id(trusted_senders)
        if scanner_mode and not trusted_senders:
            error = ValueError("Gmail scanner mode requires at least one trusted sender.")
            self._fail_run(error)
            return []
        query = self.config.get("gmail_query") or self.config.get("gmail_search_query")
        if not query:
            query = _scanner_query(trusted_senders) if scanner_mode else "has:attachment"
        incremental_after = _optional_epoch(self.config.get("gmail_incremental_after_epoch"))
        effective_query = str(query).strip()
        if incremental_after and not re.search(r"(?:^|\s)(?:after:|newer_than:)", effective_query, re.IGNORECASE):
            effective_query = f"{effective_query} after:{incremental_after}"
        attachments_dir = self._download_directory(
            "gmail", "gmail_attachment_download_dir", "gmail_download_dir"
        )
        os.makedirs(attachments_dir, exist_ok=True)
        max_pages = _bounded_int(self.config.get("gmail_max_pages"), 50, 1, 500)
        max_messages = _bounded_int(self.config.get("gmail_max_messages"), 5000, 1, 50000)
        max_attachment_bytes = _bounded_int(
            self.config.get("gmail_max_attachment_bytes"),
            25 * 1024 * 1024,
            1024,
            50 * 1024 * 1024,
        )
        max_run_attachment_bytes = _bounded_int(
            self.config.get("gmail_max_run_attachment_bytes"),
            250 * 1024 * 1024,
            1024 * 1024,
            1024 * 1024 * 1024,
        )
        max_run_attachments = _bounded_int(
            self.config.get("gmail_max_run_attachments"),
            100,
            1,
            5000,
        )

        documents = []
        downloaded_bytes = 0
        skipped = 0
        pages = 0
        resource_limit_reached = False
        rejected = {
            "untrusted_sender": 0,
            "not_pdf": 0,
            "invalid_pdf": 0,
            "oversized": 0,
            "missing_attachment": 0,
            "run_budget": 0,
        }
        first_error = None
        try:
            page_token = None
            messages = []
            while pages < max_pages and len(messages) < max_messages:
                request = self.service.users().messages().list(
                    userId="me",
                    q=effective_query,
                    maxResults=min(500, max_messages - len(messages)),
                    **({"pageToken": page_token} if page_token else {}),
                )
                results = request.execute()
                pages += 1
                messages.extend(results.get("messages", []))
                page_token = results.get("nextPageToken")
                if not page_token:
                    break

            truncated = bool(page_token)
            if truncated:
                first_error = RuntimeError(
                    "Gmail scan reached its configured page or message cap; increase the cap before advancing the source checkpoint."
                )

            for message in messages[:max_messages]:
                try:
                    msg = self.service.users().messages().get(
                        userId='me', id=message['id'], format='full'
                    ).execute()
                except Exception as error:
                    skipped += 1
                    first_error = first_error or error
                    continue
                payload = msg.get("payload") or {}
                headers = _headers(payload)
                sender_address = parseaddr(headers.get("from", ""))[1].strip().lower()
                if scanner_mode and sender_address not in trusted_senders:
                    rejected["untrusted_sender"] += 1
                    skipped += 1
                    continue
                for part in _attachment_parts(payload):
                    if len(documents) >= max_run_attachments:
                        rejected["run_budget"] += 1
                        resource_limit_reached = True
                        break
                    file_name = str(part.get('filename') or '').strip()
                    body = part.get('body') if isinstance(part.get('body'), dict) else {}
                    attachment_id = str(body.get('attachmentId') or '').strip()
                    encoded = str(body.get("data") or "")
                    if not encoded and attachment_id:
                        try:
                            attachment = self.service.users().messages().attachments().get(
                                userId='me', messageId=message['id'], id=attachment_id
                            ).execute()
                            encoded = str(attachment.get("data") or "")
                        except Exception as error:
                            skipped += 1
                            first_error = first_error or error
                            continue
                    if not file_name or not encoded:
                        rejected["missing_attachment"] += 1
                        skipped += 1
                        continue
                    if scanner_mode and not _pdf_candidate(file_name, part.get("mimeType")):
                        rejected["not_pdf"] += 1
                        skipped += 1
                        continue
                    max_encoded_bytes = 4 * ((max_attachment_bytes + 2) // 3)
                    if len(encoded) > max_encoded_bytes:
                        rejected["oversized"] += 1
                        skipped += 1
                        first_error = first_error or RuntimeError(
                            "Gmail scan skipped an attachment above its configured per-attachment byte limit; "
                            "increase the limit before advancing the source checkpoint."
                        )
                        continue
                    try:
                        encoded += "=" * (-len(encoded) % 4)
                        file_data = base64.urlsafe_b64decode(encoded)
                    except Exception:
                        rejected["missing_attachment"] += 1
                        skipped += 1
                        continue
                    if len(file_data) > max_attachment_bytes:
                        rejected["oversized"] += 1
                        skipped += 1
                        first_error = first_error or RuntimeError(
                            "Gmail scan skipped an attachment above its configured per-attachment byte limit; "
                            "increase the limit before advancing the source checkpoint."
                        )
                        continue
                    if downloaded_bytes + len(file_data) > max_run_attachment_bytes:
                        rejected["run_budget"] += 1
                        skipped += 1
                        resource_limit_reached = True
                        break
                    if scanner_mode and not _is_pdf(file_data):
                        rejected["invalid_pdf"] += 1
                        skipped += 1
                        continue
                    stable_attachment_id = attachment_id or _inline_attachment_id(part, file_data)
                    document_id = message['id'] + '_' + stable_attachment_id
                    local_path = self._store_content(
                        attachments_dir,
                        file_name,
                        document_id,
                        file_data,
                    )

                    documents.append({
                        'id': document_id,
                        'source': 'gmail',
                        'original_filename': file_name,
                        'mime_type': 'application/pdf' if scanner_mode else part.get("mimeType"),
                        'local_path': local_path,
                        'timestamp': msg.get('internalDate'),
                        'metadata': {
                            'subject': headers.get('subject', ''),
                            'from': headers.get('from', ''),
                            'sender_address': sender_address,
                            'message_id': message['id'],
                            'thread_id': msg.get('threadId'),
                            'attachment_id': stable_attachment_id,
                            'label_ids': list(msg.get('labelIds') or []),
                            'scanner_profile': scanner_profile_id if scanner_mode else None,
                            'scanner_policy_verified': scanner_mode,
                            'delivery_path': 'gmail_to_fab_direct' if scanner_mode else None,
                        }
                    })
                    downloaded_bytes += len(file_data)
                if resource_limit_reached:
                    first_error = first_error or RuntimeError(
                        "Gmail scan reached its configured attachment byte or file cap; increase the cap before advancing the source checkpoint."
                    )
                    break
            if first_error:
                self._fail_run(first_error, fetched=len(documents), skipped=skipped, pages=pages)
            else:
                self._finish_run(len(documents), skipped=skipped, pages=pages)
            self.last_run.update({
                "query": effective_query,
                "scannerMode": scanner_mode,
                "trustedSenderCount": len(trusted_senders),
                "incrementalAfter": incremental_after,
                "truncated": truncated,
                "rejected": rejected,
                "attachmentBytesDownloaded": downloaded_bytes,
                "attachmentBytesLimit": max_run_attachment_bytes,
                "attachmentCountLimit": max_run_attachments,
            })
        except Exception as error:
            self._fail_run(error, fetched=len(documents), skipped=skipped, pages=pages)
            self.last_run.update({
                "query": effective_query,
                "scannerMode": scanner_mode,
                "trustedSenderCount": len(trusted_senders),
                "incrementalAfter": incremental_after,
                "truncated": bool(page_token),
                "rejected": rejected,
                "attachmentBytesDownloaded": downloaded_bytes,
                "attachmentBytesLimit": max_run_attachment_bytes,
                "attachmentCountLimit": max_run_attachments,
            })
        return documents


def _attachment_parts(payload: Dict[str, Any]):
    for part in payload.get("parts") or []:
        if part.get("parts"):
            yield from _attachment_parts(part)
        if part.get("filename") and isinstance(part.get("body"), dict):
            yield part


def _headers(payload: Dict[str, Any]) -> Dict[str, str]:
    return {
        str(header.get("name") or "").strip().lower(): str(header.get("value") or "").strip()
        for header in payload.get("headers") or []
        if isinstance(header, dict) and str(header.get("name") or "").strip()
    }


def _string_list(value: Any) -> List[str]:
    if isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = str(value or "").replace(";", ",").split(",")
    return list(dict.fromkeys(
        str(item or "").strip().lower()
        for item in values
        if str(item or "").strip()
    ))


def _scanner_query(trusted_senders: List[str]) -> str:
    sender_query = " OR ".join(f"from:{sender}" for sender in trusted_senders)
    if len(trusted_senders) > 1:
        sender_query = f"{{{sender_query}}}"
    return f"label:all {sender_query} has:attachment filename:pdf"


def _scanner_profile_id(trusted_senders: List[str]) -> str:
    return (
        HP_EPRINT_PROFILE_ID
        if trusted_senders == [HP_EPRINT_SENDER]
        else CUSTOM_SCANNER_PROFILE_ID
    )


def _pdf_candidate(filename: str, mime_type: Any) -> bool:
    return filename.lower().endswith(".pdf") and str(mime_type or "").lower() in {
        "application/pdf",
        "application/octet-stream",
    }


def _is_pdf(content: bytes) -> bool:
    return content[:1024].lstrip(b"\x00\t\r\n ").startswith(b"%PDF-")


def _inline_attachment_id(part: Dict[str, Any], content: bytes) -> str:
    import hashlib

    identity = f"{part.get('partId') or 'inline'}:{hashlib.sha256(content).hexdigest()}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _optional_epoch(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


