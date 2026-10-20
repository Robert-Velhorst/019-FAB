from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

from src.authorize_gmail import authorize_gmail
from src.operations.local_ledger import LocalOperationsLedger
from src.security.google_oauth_store import GoogleOAuthTokenStore


MAX_GOOGLE_OAUTH_CREDENTIAL_BYTES = 64 * 1024


class LocalGmailAuthorizationCoordinator:
    """Coordinate one user-owned, read-only Gmail OAuth flow for FAB."""

    def __init__(
        self,
        ledger: LocalOperationsLedger,
        config: Optional[Dict[str, Any]] = None,
        authorize: Callable[[Optional[Dict[str, Any]]], Dict[str, Any]] = authorize_gmail,
    ):
        self.ledger = ledger
        self.config = dict(config or {})
        self.authorize = authorize
        self.credentials_path = _absolute_path(
            _config_value(
                self.config,
                "gmail_credentials_file",
                "gmail_credentials_path",
                default="credentials/gmail_credentials.json",
            )
        )
        self.token_store = GoogleOAuthTokenStore(
            _config_value(
                self.config,
                "gmail_token_file",
                "gmail_token_path",
                default="tokens/gmail_token.json",
            ),
            ["https://www.googleapis.com/auth/gmail.readonly"],
        )
        self.token_path = self.token_store.token_path
        self.reauthorization_marker_path = self.token_store.marker_path
        self.scanner_mode = _as_bool(self.config.get("gmail_scanner_mode"))
        self.trusted_senders = _string_list(self.config.get("gmail_trusted_senders"))
        self.query = str(
            self.config.get("gmail_query")
            or self.config.get("gmail_search_query")
            or "has:attachment"
        ).strip()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._session: Dict[str, Any] = {
            "state": "idle",
            "startedAt": None,
            "finishedAt": None,
            "error": None,
            "emailAddress": None,
        }

    def status(self) -> Dict[str, Any]:
        with self._lock:
            session = dict(self._session)
            running = bool(self._thread and self._thread.is_alive())
        credentials_present = os.path.isfile(self.credentials_path)
        token_status = self.token_store.status()
        token_present = token_status["tokenPresent"]
        reauthorization_required = token_status["reauthorizationRequired"]
        if running:
            state = "authorization_in_progress"
        elif reauthorization_required:
            state = "reauthorization_required"
        elif session.get("state") not in {None, "idle"}:
            state = str(session["state"])
        elif token_present:
            state = "token_present"
        elif credentials_present:
            state = "ready_to_authorize"
        else:
            state = "credentials_required"
        policy_ready = not self.scanner_mode or bool(self.trusted_senders)
        return {
            "status": state,
            "credentialsPresent": credentials_present,
            "tokenPresent": token_present,
            "tokenPath": self.token_path,
            "legacyTokenPresent": token_status["legacyTokenPresent"],
            "reauthorizationRequired": reauthorization_required,
            "reauthorizationReason": token_status["reauthorizationReason"],
            "authorizationInProgress": running,
            "canInstallCredentials": not running,
            "canStartAuthorization": credentials_present and policy_ready and not running,
            "startedAt": session.get("startedAt"),
            "finishedAt": session.get("finishedAt"),
            "error": session.get("error"),
            "emailAddress": session.get("emailAddress"),
            "scope": "gmail_readonly",
            "scannerMode": self.scanner_mode,
            "scannerPolicyReady": policy_ready,
            "trustedSenders": list(self.trusted_senders),
            "query": self.query,
            "sourceRetentionPolicy": "original_email_unchanged_local_copy_retained",
            "externalSubmission": "user_owned_oauth_consent",
        }

    def install_credentials(
        self,
        content: bytes,
        *,
        filename: str,
        replace: bool = False,
        actor: str = "local_operator",
    ) -> Dict[str, Any]:
        if not str(filename or "").lower().endswith(".json"):
            raise ValueError("Google OAuth desktop credentials must be a JSON file.")
        if not content or len(content) > MAX_GOOGLE_OAUTH_CREDENTIAL_BYTES:
            raise ValueError(
                f"Google OAuth credential JSON must be between 1 and {MAX_GOOGLE_OAUTH_CREDENTIAL_BYTES} bytes."
            )
        _validate_google_desktop_credentials(content)
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("Gmail authorization is already in progress.")
            existed_before = os.path.exists(self.credentials_path)
            if existed_before and not replace:
                raise FileExistsError(
                    "Gmail credentials already exist. Confirm replacement explicitly to rotate them."
                )
            _atomic_private_write(self.credentials_path, content)
            token_status = self.token_store.status()
            reauthorization_required = bool(
                token_status["tokenPresent"] or token_status["legacyTokenPresent"]
            )
            if reauthorization_required:
                _atomic_private_write(
                    self.reauthorization_marker_path,
                    json.dumps({
                        "requiredAt": _now(),
                        "credentialSha256": hashlib.sha256(content).hexdigest(),
                        "reason": "oauth_client_credentials_changed",
                    }).encode("utf-8"),
                )
            self._session = {
                "state": "reauthorization_required" if reauthorization_required else "ready_to_authorize",
                "startedAt": None,
                "finishedAt": None,
                "error": None,
                "emailAddress": None,
            }
        digest = hashlib.sha256(content).hexdigest()
        self.ledger.record_audit_event({
            "action": "gmail.oauth_credentials.installed",
            "entityType": "connector",
            "entityId": "gmail",
            "details": {
                "actor": str(actor or "local_operator")[:200],
                "sha256": digest,
                "replacedExisting": existed_before,
                "credentialType": "installed_desktop_application",
                "secretValuesPersisted": True,
            },
        })
        return {
            "success": True,
            "status": "reauthorization_required" if reauthorization_required else "ready_to_authorize",
            "credentialsPresent": True,
            "sha256": digest,
            "externalSubmission": "not_executed",
        }

    def start(self, *, actor: str = "local_operator") -> Dict[str, Any]:
        actor = str(actor or "local_operator")[:200]
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {
                    "success": True,
                    "status": "authorization_in_progress",
                    "startedAt": self._session.get("startedAt"),
                    "externalSubmission": "user_owned_oauth_consent",
                }
            if not os.path.isfile(self.credentials_path):
                return {
                    "success": False,
                    "status": "credentials_required",
                    "error": "Install Google OAuth desktop credentials before starting Gmail authorization.",
                    "externalSubmission": "not_executed",
                }
            if self.scanner_mode and not self.trusted_senders:
                return {
                    "success": False,
                    "status": "scanner_policy_required",
                    "error": "Configure at least one trusted scanner sender before authorizing Gmail.",
                    "externalSubmission": "not_executed",
                }
            started_at = _now()
            self._session = {
                "state": "authorization_in_progress",
                "startedAt": started_at,
                "finishedAt": None,
                "error": None,
                "emailAddress": None,
                "actor": actor,
            }
            self._thread = threading.Thread(
                target=self._run_authorization,
                args=(actor,),
                name="fab-gmail-oauth",
                daemon=True,
            )
            self.ledger.record_audit_event({
                "action": "gmail.authorization.started",
                "entityType": "connector",
                "entityId": "gmail",
                "details": {
                    "actor": actor,
                    "scope": "gmail_readonly",
                    "scannerMode": self.scanner_mode,
                    "trustedSenderCount": len(self.trusted_senders),
                    "externalSubmission": "user_owned_oauth_consent",
                },
            })
            self._thread.start()
        return {
            "success": True,
            "status": "authorization_started",
            "startedAt": started_at,
            "externalSubmission": "user_owned_oauth_consent",
        }

    def _run_authorization(self, actor: str) -> None:
        settings = dict(self.config)
        settings["gmail_credentials_file"] = self.credentials_path
        settings["gmail_token_file"] = self.token_path
        settings["gmail_force_reauthorization"] = self.token_store.status()[
            "reauthorizationRequired"
        ]
        try:
            result = self.authorize(settings)
        except Exception as exc:
            result = {
                "success": False,
                "status": "authorization_failed",
                "error": _safe_error(exc),
            }
        success = bool(result.get("success"))
        if success:
            try:
                self.token_store.clear_reauthorization()
            except OSError as exc:
                result = {
                    "success": False,
                    "status": "authorization_failed",
                    "error": f"Authorization succeeded but consent-state cleanup failed: {_safe_error(exc)}",
                }
                success = False
        state = "authorized" if success else str(result.get("status") or "authorization_failed")
        error = None if success else _safe_message(result.get("error"))
        email_address = str(result.get("emailAddress") or "") or None
        finished_at = _now()
        with self._lock:
            self._session = {
                "state": state,
                "startedAt": self._session.get("startedAt"),
                "finishedAt": finished_at,
                "error": error,
                "emailAddress": email_address,
                "actor": actor,
            }
        self.ledger.record_audit_event({
            "action": "gmail.authorization.completed" if success else "gmail.authorization.failed",
            "entityType": "connector",
            "entityId": "gmail",
            "details": {
                "actor": actor,
                "status": state,
                "mailboxVerified": bool(result.get("mailboxVerified")),
                "tokenPresent": self.token_store.status()["tokenPresent"],
                "scannerMode": self.scanner_mode,
                "error": error,
                "externalSubmission": "user_owned_oauth_consent",
            },
        })


def _validate_google_desktop_credentials(content: bytes) -> None:
    try:
        payload = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Google OAuth credentials are not valid UTF-8 JSON.") from exc
    installed = payload.get("installed") if isinstance(payload, dict) else None
    if not isinstance(installed, dict):
        raise ValueError("Use an OAuth client JSON for an installed desktop application.")
    client_id = str(installed.get("client_id") or "").strip()
    client_secret = str(installed.get("client_secret") or "").strip()
    if not client_id.endswith(".apps.googleusercontent.com") or not client_secret:
        raise ValueError("The OAuth desktop client ID or client secret is missing or invalid.")
    if not _trusted_google_url(installed.get("auth_uri"), {"accounts.google.com"}):
        raise ValueError("The OAuth authorization endpoint is not an approved Google endpoint.")
    if not _trusted_google_url(installed.get("token_uri"), {"oauth2.googleapis.com"}):
        raise ValueError("The OAuth token endpoint is not an approved Google endpoint.")
    redirect_uris = installed.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not any(_local_redirect_uri(item) for item in redirect_uris):
        raise ValueError("The OAuth desktop client must allow a localhost callback URI.")


def _trusted_google_url(value: Any, hosts: set[str]) -> bool:
    parsed = urlsplit(str(value or ""))
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in hosts


def _local_redirect_uri(value: Any) -> bool:
    parsed = urlsplit(str(value or ""))
    return parsed.scheme in {"http", "https"} and (parsed.hostname or "").lower() in {
        "localhost",
        "127.0.0.1",
        "::1",
    }


def _atomic_private_write(path: str, content: bytes) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(prefix=".fab-gmail-oauth-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary_path, 0o600)
        except OSError:
            pass
        os.replace(temporary_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def _config_value(config: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = config.get(key)
        if value not in (None, ""):
            return value
    return default


def _absolute_path(value: Any) -> str:
    return os.path.abspath(os.path.expanduser(str(value or "")))


def _string_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = str(value or "").replace(";", ",").split(",")
    return list(dict.fromkeys(
        str(item or "").strip().lower()
        for item in values
        if str(item or "").strip()
    ))


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_error(error: Exception) -> str:
    return _safe_message(f"{type(error).__name__}: {error}")


def _safe_message(value: Any) -> str:
    message = " ".join(str(value or "Gmail authorization failed.").split())
    lowered = message.lower()
    if "http://" in lowered or "https://" in lowered or any(
        marker in lowered
        for marker in ("client_secret", "access_token", "refresh_token", "authorization:")
    ):
        return "Gmail authorization failed; sensitive provider details were omitted."
    return message[:500]
