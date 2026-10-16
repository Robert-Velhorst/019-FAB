import ipaddress
import os
import re
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

from src.document_fetchers.base import BaseFetcher
from src.document_fetchers.profiles import (
    LEGACY_025_COMMIT,
    LEGACY_025_FINANCIAL_KEYWORDS,
    LEGACY_025_PROFILE_ID,
    LEGACY_025_REPOSITORY,
)


class FreshdeskFetcher(BaseFetcher):
    """Fetch immutable financial evidence from Freshdesk without mutating tickets."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.api_key = self.config.get("freshdesk_api_key")
        self.domain = str(self.config.get("freshdesk_domain") or "").strip()
        configured_url = str(self.config.get("freshdesk_api_url") or "").strip()
        self.configured_api_url = configured_url
        if configured_url:
            self.base_url = _normalized_freshdesk_api_url(configured_url)
        else:
            normalized_domain = re.sub(r"^https?://", "", self.domain, flags=re.IGNORECASE)
            normalized_domain = normalized_domain.rstrip("/")
            if normalized_domain and "." not in normalized_domain:
                normalized_domain = f"{normalized_domain}.freshdesk.com"
            self.base_url = f"https://{normalized_domain}/api/v2/"
        self._authenticate()

    def _authenticate(self):
        if not self.api_key or not (self.domain or self.configured_api_url):
            raise ValueError("Freshdesk API key and domain or API URL are not configured.")
        self.auth = (self.api_key, "X")

    def fetch_documents(self) -> List[Dict[str, Any]]:
        self._start_run()
        configured_params = self.config.get("freshdesk_query_params", {})
        query_params = dict(configured_params) if isinstance(configured_params, dict) else {}
        attachments_dir = self._download_directory("freshdesk", "freshdesk_download_dir")
        os.makedirs(attachments_dir, exist_ok=True)
        max_pages = _bounded_int(self.config.get("freshdesk_max_pages"), 50, 1, 500)
        per_page = _bounded_int(self.config.get("freshdesk_page_size"), 100, 1, 100)
        max_attachment_bytes = _bounded_int(
            self.config.get("freshdesk_max_attachment_bytes"),
            25 * 1024 * 1024,
            1024,
            100 * 1024 * 1024,
        )
        timeout = self._request_timeout()
        financial_filter_enabled = _as_bool(
            self.config.get("freshdesk_financial_filter_enabled")
        )
        keywords = _financial_keywords(self.config, financial_filter_enabled)
        include_ticket_description = _as_bool(
            self.config.get(
                "freshdesk_include_ticket_description",
                financial_filter_enabled,
            )
        )
        pdf_only = _as_bool(
            self.config.get("freshdesk_pdf_only", financial_filter_enabled)
        )

        documents: List[Dict[str, Any]] = []
        skipped = 0
        pages = 0
        matched_tickets = 0
        filtered_tickets = 0
        description_documents = 0
        attachment_documents = 0
        rejected_attachments = {
            "missing_metadata": 0,
            "not_pdf": 0,
            "invalid_pdf": 0,
            "oversized": 0,
            "size_mismatch": 0,
            "unsafe_url": 0,
        }
        first_error: Optional[Exception] = None
        tickets: List[Dict[str, Any]] = []

        try:
            tickets_url, listing_params, search_mode = self._ticket_listing(
                financial_filter_enabled,
                query_params,
            )
            for page in range(1, max_pages + 1):
                request_params = {**listing_params, "page": page}
                if not search_mode:
                    request_params["per_page"] = per_page
                response = requests.get(
                    tickets_url,
                    auth=self.auth,
                    params=request_params,
                    timeout=timeout,
                )
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict):
                    page_tickets = payload.get("results")
                    if page_tickets is None:
                        page_tickets = payload.get("tickets")
                else:
                    page_tickets = payload
                page_tickets = page_tickets if isinstance(page_tickets, list) else []
                pages += 1
                tickets.extend(
                    ticket for ticket in page_tickets if isinstance(ticket, dict)
                )
                if len(page_tickets) < (30 if search_mode else per_page):
                    break

            for ticket in tickets:
                try:
                    ticket_detail = self._ticket_detail(
                        ticket,
                        financial_filter_enabled,
                        timeout,
                    )
                    match = (
                        _financial_match(keywords, ticket_detail)
                        if financial_filter_enabled
                        else None
                    )
                    if financial_filter_enabled and not match:
                        filtered_tickets += 1
                        skipped += 1
                        continue
                    matched_tickets += 1

                    if include_ticket_description:
                        description = _ticket_description(ticket_detail)
                        if description:
                            try:
                                description_document = self._description_document(
                                    ticket_detail,
                                    description,
                                    match,
                                    attachments_dir,
                                )
                                documents.append(description_document)
                                description_documents += 1
                            except Exception as error:
                                skipped += 1
                                first_error = first_error or error

                    conversations = [ticket_detail] if ticket_detail.get("attachments") else []
                    try:
                        conversations.extend(
                            self._ticket_conversations(ticket_detail["id"], timeout)
                        )
                    except Exception as error:
                        first_error = first_error or error

                    seen_attachment_ids = set()
                    for conversation in conversations:
                        if not isinstance(conversation, dict):
                            continue
                        for attachment in conversation.get("attachments") or []:
                            if not isinstance(attachment, dict):
                                skipped += 1
                                rejected_attachments["missing_metadata"] += 1
                                continue
                            attachment_id = str(attachment.get("id") or "").strip()
                            document_id = (
                                f"freshdesk_{ticket_detail['id']}_{attachment_id}"
                                if attachment_id
                                else ""
                            )
                            if not document_id or document_id in seen_attachment_ids:
                                skipped += 1
                                rejected_attachments["missing_metadata"] += 1
                                continue
                            seen_attachment_ids.add(document_id)
                            try:
                                result = self._attachment_document(
                                    ticket_detail,
                                    conversation,
                                    attachment,
                                    document_id,
                                    match,
                                    attachments_dir,
                                    timeout,
                                    max_attachment_bytes,
                                    pdf_only,
                                )
                            except Exception as error:
                                skipped += 1
                                first_error = first_error or error
                                continue
                            if result.get("document"):
                                documents.append(result["document"])
                                attachment_documents += 1
                            else:
                                skipped += 1
                                reason = str(result.get("reason") or "missing_metadata")
                                rejected_attachments[reason] = (
                                    rejected_attachments.get(reason, 0) + 1
                                )
                except Exception as error:
                    skipped += 1
                    first_error = first_error or error

            if first_error:
                self._fail_run(
                    first_error,
                    fetched=len(documents),
                    skipped=skipped,
                    pages=pages,
                )
            else:
                self._finish_run(len(documents), skipped=skipped, pages=pages)
        except Exception as error:
            self._fail_run(
                error,
                fetched=len(documents),
                skipped=skipped,
                pages=pages,
            )

        self.last_run.update({
            "profileId": (
                LEGACY_025_PROFILE_ID if financial_filter_enabled else "generic_read_only"
            ),
            "financialFilterEnabled": financial_filter_enabled,
            "financialKeywords": keywords,
            "ticketsSeen": len(tickets),
            "matchedTickets": matched_tickets,
            "filteredTickets": filtered_tickets,
            "descriptionDocuments": description_documents,
            "attachmentDocuments": attachment_documents,
            "rejectedAttachments": rejected_attachments,
            "ticketMutation": "not_executed",
            "driveCopy": "not_executed",
            "deliveryPath": "freshdesk_to_fab_direct",
            "sourceProvenance": (
                _source_provenance() if financial_filter_enabled else None
            ),
        })
        return documents

    def _ticket_listing(
        self,
        financial_filter_enabled: bool,
        query_params: Dict[str, Any],
    ) -> tuple[str, Dict[str, Any], bool]:
        configured_search = str(
            self.config.get("freshdesk_search_query") or ""
        ).strip()
        if configured_search or financial_filter_enabled:
            statuses = _string_list(
                self.config.get("freshdesk_ticket_statuses") or "2,3"
            )
            status_query = " OR ".join(
                f"status:{status}" for status in statuses if status
            )
            search_query = configured_search or status_query
            return (
                f"{self.base_url}search/tickets",
                {**query_params, "query": f'"{search_query}"'},
                True,
            )
        return f"{self.base_url}tickets", query_params, False

    def _ticket_detail(
        self,
        ticket: Dict[str, Any],
        financial_filter_enabled: bool,
        timeout: float,
    ) -> Dict[str, Any]:
        if not financial_filter_enabled:
            return ticket
        ticket_id = ticket.get("id")
        response = requests.get(
            f"{self.base_url}tickets/{ticket_id}",
            auth=self.auth,
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError(f"Freshdesk ticket {ticket_id} returned invalid details.")
        return payload

    def _ticket_conversations(
        self,
        ticket_id: Any,
        timeout: float,
    ) -> List[Dict[str, Any]]:
        response = requests.get(
            f"{self.base_url}tickets/{ticket_id}/conversations",
            auth=self.auth,
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else []

    def _description_document(
        self,
        ticket: Dict[str, Any],
        description: str,
        match: Optional[Dict[str, str]],
        attachments_dir: str,
    ) -> Dict[str, Any]:
        ticket_id = str(ticket["id"])
        subject = _normalized_text(ticket.get("subject"))
        content = (
            f"Freshdesk ticket #{ticket_id}\n"
            f"Subject: {subject or 'Untitled'}\n\n"
            f"{description}\n"
        ).encode("utf-8")
        filename = f"freshdesk-{ticket_id}-description.txt"
        document_id = f"freshdesk_{ticket_id}_description"
        local_path = self._store_content(
            attachments_dir,
            filename,
            document_id,
            content,
        )
        return {
            "id": document_id,
            "source": "freshdesk",
            "original_filename": filename,
            "mime_type": "text/plain",
            "local_path": local_path,
            "timestamp": ticket.get("updated_at") or ticket.get("created_at") or "",
            "metadata": {
                **_ticket_metadata(ticket, match),
                "evidence_role": "ticket_description",
                "posting_eligible": False,
                "source_content_format": "normalized_utf8_text",
            },
        }

    def _attachment_document(
        self,
        ticket: Dict[str, Any],
        conversation: Dict[str, Any],
        attachment: Dict[str, Any],
        document_id: str,
        match: Optional[Dict[str, str]],
        attachments_dir: str,
        timeout: float,
        max_attachment_bytes: int,
        pdf_only: bool,
    ) -> Dict[str, Any]:
        attachment_url = str(attachment.get("attachment_url") or "").strip()
        file_name = os.path.basename(str(attachment.get("name") or "").strip())
        mime_type = str(attachment.get("content_type") or "").strip().lower()
        provider_size = _optional_int(attachment.get("size"))
        if not attachment_url or not file_name:
            return {"reason": "missing_metadata"}
        if provider_size is not None and provider_size > max_attachment_bytes:
            return {"reason": "oversized"}
        if pdf_only and not _pdf_candidate(file_name, mime_type):
            return {"reason": "not_pdf"}
        parsed_attachment_url = urlparse(attachment_url)
        attachment_host = (parsed_attachment_url.hostname or "").lower().rstrip(".")
        api_host = (urlparse(self.base_url).hostname or "").lower().rstrip(".")
        allowed_hosts = {api_host} if api_host else set()
        allowed_hosts.update(_string_list(self.config.get("freshdesk_attachment_allowed_hosts")))
        try:
            ipaddress.ip_address(attachment_host)
            is_ip_literal = True
        except ValueError:
            is_ip_literal = False
        try:
            attachment_port = parsed_attachment_url.port
        except ValueError:
            attachment_port = -1
        if (
            parsed_attachment_url.scheme.lower() != "https"
            or not attachment_host
            or parsed_attachment_url.username is not None
            or parsed_attachment_url.password is not None
            or is_ip_literal
            or attachment_port not in (None, 443)
            or attachment_host not in allowed_hosts
        ):
            return {"reason": "unsafe_url"}

        response = requests.get(
            attachment_url,
            auth=self._attachment_auth(parsed_attachment_url),
            timeout=timeout,
            stream=True,
            allow_redirects=False,
        )
        try:
            status_code = getattr(response, "status_code", None)
            if isinstance(status_code, int) and 300 <= status_code < 400:
                return {"reason": "unsafe_url"}
            response.raise_for_status()
            content, bounded_reason = _bounded_response_content(
                response,
                max_attachment_bytes,
            )
        finally:
            response.close()
        if bounded_reason:
            return {"reason": bounded_reason}
        if provider_size is not None and provider_size != len(content):
            return {"reason": "size_mismatch"}
        if pdf_only and not _is_pdf(content):
            return {"reason": "invalid_pdf"}

        local_path = self._store_content(
            attachments_dir,
            file_name,
            document_id,
            content,
        )
        return {
            "document": {
                "id": document_id,
                "source": "freshdesk",
                "original_filename": file_name,
                "mime_type": mime_type or (
                    "application/pdf" if file_name.lower().endswith(".pdf") else None
                ),
                "local_path": local_path,
                "timestamp": (
                    attachment.get("updated_at")
                    or attachment.get("created_at")
                    or ticket.get("updated_at")
                    or ""
                ),
                "metadata": {
                    **_ticket_metadata(ticket, match),
                    "conversation_id": conversation.get("id"),
                    "attachment_id": attachment.get("id"),
                    "attachment_size": len(content),
                    "provider_attachment_size": provider_size,
                    "evidence_role": "ticket_attachment",
                    "posting_eligible": True,
                    "attachment_policy": (
                        "pdf_only_magic_verified"
                        if pdf_only
                        else "bounded_immutable_attachment"
                    ),
                },
            },
        }

    def _attachment_auth(self, attachment_url: Any):
        api_host = (urlparse(self.base_url).hostname or "").lower()
        attachment_host = str(getattr(attachment_url, "hostname", "") or "").lower()
        return self.auth if api_host and attachment_host == api_host else None


def _ticket_metadata(
    ticket: Dict[str, Any],
    match: Optional[Dict[str, str]],
) -> Dict[str, Any]:
    return {
        "ticket_id": ticket.get("id"),
        "ticket_subject": _normalized_text(ticket.get("subject"))[:500],
        "ticket_status": ticket.get("status"),
        "ticket_created_at": ticket.get("created_at"),
        "ticket_updated_at": ticket.get("updated_at"),
        "ticket_tags": list(ticket.get("tags") or []),
        "financial_filter_match": dict(match or {}),
        "profile_id": LEGACY_025_PROFILE_ID if match else "generic_read_only",
        "delivery_path": "freshdesk_to_fab_direct",
        "ticket_mutation": "not_executed",
        "drive_copy": "not_executed",
        "credential_forwarding": "same_origin_only",
        "source_provenance": _source_provenance() if match else {},
        "external_submission": "not_executed",
    }


def _normalized_freshdesk_api_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    path = urlparse(base_url).path.rstrip("/").lower()
    if path.endswith("/api/v2") or path.endswith("/v2"):
        return f"{base_url}/"
    if path.endswith("/api"):
        return f"{base_url}/v2/"
    return f"{base_url}/api/v2/"


def _source_provenance() -> Dict[str, str]:
    return {
        "repository": LEGACY_025_REPOSITORY,
        "auditedCommit": LEGACY_025_COMMIT,
        "legacyTransport": "freshdesk_to_google_drive",
        "consolidatedTransport": "freshdesk_to_fab_direct",
    }


def _financial_keywords(
    config: Dict[str, Any],
    financial_filter_enabled: bool,
) -> List[str]:
    configured = _string_list(config.get("freshdesk_financial_keywords"))
    if configured:
        return configured
    return list(LEGACY_025_FINANCIAL_KEYWORDS) if financial_filter_enabled else []


def _financial_match(
    keywords: List[str],
    ticket: Dict[str, Any],
) -> Optional[Dict[str, str]]:
    fields = {
        "subject": _normalized_text(ticket.get("subject")),
        "description": _ticket_description(ticket),
    }
    for keyword in keywords:
        normalized_keyword = _normalized_text(keyword).lower()
        if not normalized_keyword:
            continue
        for field_name, value in fields.items():
            if normalized_keyword in value.lower():
                return {"keyword": keyword, "field": field_name}
    return None


def _ticket_description(ticket: Dict[str, Any]) -> str:
    plain = _normalized_text(ticket.get("description_text"))
    if plain:
        return plain
    return _html_to_text(str(ticket.get("description") or ""))


def _html_to_text(value: str) -> str:
    parser = _PlainTextParser()
    parser.feed(value)
    parser.close()
    return _normalized_text(" ".join(parser.parts))


class _PlainTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data)


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _string_list(value: Any) -> List[str]:
    if isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = re.split(r"[,;\r\n]+", str(value or ""))
    return list(dict.fromkeys(
        _normalized_text(item).lower()
        for item in values
        if _normalized_text(item)
    ))


def _pdf_candidate(filename: str, mime_type: str) -> bool:
    return filename.lower().endswith(".pdf") and mime_type in {
        "",
        "application/octet-stream",
        "application/pdf",
    }


def _is_pdf(content: bytes) -> bool:
    return content[:1024].lstrip(b"\x00\t\r\n ").startswith(b"%PDF-")


def _bounded_response_content(
    response: Any,
    max_bytes: int,
) -> tuple[bytes, Optional[str]]:
    headers = response.headers if isinstance(response.headers, Mapping) else {}
    content_length = _optional_int(
        headers.get("Content-Length") or headers.get("content-length")
    )
    if content_length is not None and content_length > max_bytes:
        return b"", "oversized"

    chunks: List[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        if not isinstance(chunk, bytes):
            raise TypeError("Freshdesk attachment response contained non-byte content.")
        total += len(chunk)
        if total > max_bytes:
            return b"", "oversized"
        chunks.append(chunk)
    return b"".join(chunks), None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {
        "1",
        "enabled",
        "on",
        "true",
        "yes",
    }


def _optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, parsed)


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))
