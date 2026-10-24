from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Protocol

from src.operations.local_ledger import LocalOperationsLedger


STATE_VERSION = "fab-wave-receipt-executor-session-v1"
REQUIRED_CAPABILITIES = (
    "transaction_locate",
    "receipt_upload",
    "receipt_download",
    "transaction_review",
    "observed_fields",
)
ALLOWED_CAPABILITIES = set(REQUIRED_CAPABILITIES) | {
    "transaction_create",
    "transaction_update",
}
ALLOWED_SESSION_STATUSES = {
    "authentication_required",
    "busy",
    "error",
    "ready",
    "stopped",
}
CLAIMABLE_STAGES = {
    "upload_and_verify_attachment",
    "refresh_wave_readback",
}
ALLOWED_SESSION_FIELDS = {
    "browser",
    "businessId",
    "capabilities",
    "currentDocumentId",
    "executorId",
    "message",
    "sessionId",
    "status",
    "version",
}
SENSITIVE_FIELD_FRAGMENTS = {
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "storage_state",
    "storagestate",
    "token",
}
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class WorkOrderProvider(Protocol):
    def list_work_orders(self, limit: int = 100) -> Dict[str, Any]: ...


class LocalWaveReceiptExecutorService:
    """Coordinate a user-owned Wave browser without storing its login state.

    The coordinator is deliberately not a browser implementation. A supervised
    HAI or browser process owns the authenticated session and advertises only
    non-secret capabilities. Until FAB has a trusted provider attachment reader,
    the coordinator must not claim a work order can be completed or archived.
    """

    def __init__(self, ledger: LocalOperationsLedger, config: Optional[Dict[str, Any]] = None):
        self.ledger = ledger
        self.config = config or {}

    def status(self) -> Dict[str, Any]:
        enabled = _bool_config(
            self.config.get("wave_receipt_executor_enabled"),
            default=True,
        )
        state, state_error = self._load_state()
        configured_business_id = self._business_id()
        base = {
            "version": STATE_VERSION,
            "enabled": enabled,
            "mode": "supervised_user_owned_browser",
            "ready": False,
            "configuredBusinessId": configured_business_id or None,
            "configuredBusinessUuid": wave_business_uuid(configured_business_id),
            "requiredCapabilities": list(REQUIRED_CAPABILITIES),
            "missingCapabilities": list(REQUIRED_CAPABILITIES),
            "heartbeatTtlSeconds": self._heartbeat_ttl_seconds(),
            "credentialFieldsAccepted": False,
            "credentialPolicy": "browser_session_owned_by_user_never_stored_in_fab",
            "trustedProviderReadbackAvailable": False,
            "readbackUnavailableReason": "trusted_provider_readback_unavailable",
            "workOrderClaimPath": "/api/wave/receipt-executor/claim",
            "sessionPath": "/api/wave/receipt-executor/session",
            "releasePath": "/api/wave/receipt-executor/release",
            "haiManifestPath": "/api/hai/manifest",
            "pairing": {
                "session": {
                    "method": "POST",
                    "path": "/api/wave/receipt-executor/session",
                },
                "claim": {
                    "method": "POST",
                    "path": "/api/wave/receipt-executor/claim",
                },
                "release": {
                    "method": "POST",
                    "path": "/api/wave/receipt-executor/release",
                },
                "attachmentReadback": {
                    "available": False,
                    "reason": "trusted_provider_readback_unavailable",
                },
            },
            "externalSubmission": "policy_gated_browser_execution",
        }
        if not enabled:
            return {
                **base,
                "status": "prepared_disabled",
                "nextAction": "Enable the supervised Wave receipt executor coordinator.",
            }
        if not configured_business_id:
            return {
                **base,
                "status": "needs_wave_setup",
                "nextAction": "Configure and validate the Wave business before connecting a receipt executor.",
            }
        if state_error:
            return {
                **base,
                "status": "invalid_state",
                "nextAction": "Disconnect and register the Wave receipt executor again.",
                "error": state_error,
            }
        if not state:
            return {
                **base,
                "status": "not_connected",
                "nextAction": "FAB lacks trusted Wave attachment retrieval; connecting an executor cannot complete or archive receipts yet.",
            }

        heartbeat_at = _parse_datetime(state.get("heartbeatAt"))
        now = datetime.now(timezone.utc)
        age_seconds = None
        fresh = False
        if heartbeat_at is not None:
            age_seconds = max(0, int((now - heartbeat_at).total_seconds()))
            fresh = age_seconds <= self._heartbeat_ttl_seconds()
        capabilities = sorted(set(_string_list(state.get("capabilities"))))
        missing_capabilities = sorted(set(REQUIRED_CAPABILITIES) - set(capabilities))
        session_business_id = str(state.get("businessId") or "").strip()
        business_matches = bool(configured_business_id) and session_business_id == configured_business_id
        session_status = str(state.get("status") or "stopped").strip().lower()

        if not fresh:
            resolved_status = "stale"
            next_action = "Reconnect the Wave receipt executor; its heartbeat expired."
        elif not business_matches:
            resolved_status = "wrong_business"
            next_action = "Open the configured Wave business in the supervised browser session."
        elif missing_capabilities:
            resolved_status = "incompatible"
            next_action = "Connect an executor that supports upload, download, review, and observed-field readback."
        elif session_status == "authentication_required":
            resolved_status = "authentication_required"
            next_action = "Sign in to Wave in the user-owned supervised browser session."
        elif session_status in {"ready", "busy"}:
            resolved_status = "readback_unavailable"
            next_action = (
                "Do not claim or archive Wave receipt work. FAB needs a trusted provider attachment retrieval path."
            )
        elif session_status == "error":
            resolved_status = "error"
            next_action = "Inspect the executor message, correct the browser session, and reconnect."
        else:
            resolved_status = "stopped"
            next_action = "Start the supervised Wave receipt executor."

        ready = resolved_status in {"ready", "busy"}
        return {
            **base,
            "status": resolved_status,
            "ready": ready,
            "nextAction": next_action,
            "executorId": state.get("executorId"),
            "sessionId": state.get("sessionId"),
            "sessionStatus": session_status,
            "businessId": session_business_id or None,
            "businessMatches": business_matches,
            "capabilities": capabilities,
            "missingCapabilities": missing_capabilities,
            "browser": state.get("browser"),
            "executorVersion": state.get("version"),
            "message": state.get("message"),
            "currentDocumentId": state.get("currentDocumentId"),
            "registeredAt": state.get("registeredAt"),
            "lastSeenAt": state.get("heartbeatAt"),
            "heartbeatAgeSeconds": age_seconds,
        }

    def register(self, payload: Dict[str, Any], *, actor: str = "wave-receipt-executor") -> Dict[str, Any]:
        if not _bool_config(self.config.get("wave_receipt_executor_enabled"), default=True):
            return {
                "success": False,
                "status": "prepared_disabled",
                "error": "The Wave receipt executor coordinator is disabled.",
                "externalSubmission": "not_executed",
            }
        try:
            normalized = self._normalize_session_payload(payload)
        except ValueError as exc:
            return {
                "success": False,
                "status": "invalid",
                "error": str(exc),
                "externalSubmission": "not_executed",
            }

        configured_business_id = self._business_id()
        if not configured_business_id:
            return {
                "success": False,
                "status": "needs_wave_setup",
                "error": "Configure the Wave business before registering a receipt executor.",
                "externalSubmission": "not_executed",
            }
        existing, _ = self._load_state()
        same_session = bool(
            existing
            and existing.get("executorId") == normalized["executorId"]
            and existing.get("sessionId") == normalized["sessionId"]
        )
        existing_heartbeat = _parse_datetime((existing or {}).get("heartbeatAt"))
        existing_fresh = bool(
            existing_heartbeat
            and (datetime.now(timezone.utc) - existing_heartbeat).total_seconds()
            <= self._heartbeat_ttl_seconds()
        )
        if (
            existing
            and not same_session
            and existing_fresh
            and existing.get("status") not in {"error", "stopped"}
        ):
            return {
                "success": False,
                "status": "session_conflict",
                "error": "A fresh Wave receipt executor session is already registered.",
                "executor": self.status(),
                "externalSubmission": "not_executed",
            }
        if same_session and existing.get("status") == "busy":
            if normalized.get("status") != "busy":
                return {
                    "success": False,
                    "status": "active_claim",
                    "error": "Release the active receipt work order before changing the session state.",
                    "executor": self.status(),
                    "externalSubmission": "not_executed",
                }
            if existing.get("currentDocumentId") != normalized.get("currentDocumentId"):
                return {
                    "success": False,
                    "status": "active_claim",
                    "error": "A busy heartbeat must retain the claimed currentDocumentId.",
                    "executor": self.status(),
                    "externalSubmission": "not_executed",
                }
        if normalized.get("status") == "busy" and not (
            same_session and existing.get("status") == "busy"
        ):
            return {
                "success": False,
                "status": "active_claim_required",
                "error": "A session becomes busy only after FAB returns a claimed work order.",
                "executor": self.status(),
                "externalSubmission": "not_executed",
            }
        now = datetime.now(timezone.utc).isoformat()
        state = {
            **normalized,
            "stateVersion": STATE_VERSION,
            "registeredAt": existing.get("registeredAt") if same_session else now,
            "heartbeatAt": now,
        }
        if state.get("status") == "busy" and state.get("currentDocumentId"):
            lease = self._acquire_document_lease(
                int(state["currentDocumentId"]),
                state,
            )
            if lease.get("acquired") is not True:
                return {
                    "success": False,
                    "status": "lease_conflict",
                    "error": "The active receipt work-order lease is owned by another executor.",
                    "lease": lease.get("lease"),
                    "externalSubmission": "not_executed",
                }
        self._write_state(state)
        self.ledger.record_audit_event({
            "action": "wave_receipt_executor.session_updated",
            "entityType": "wave_receipt_executor_session",
            "entityId": str(state["sessionId"]),
            "details": {
                "actor": str(actor or "wave-receipt-executor")[:120],
                "executorId": state["executorId"],
                "businessMatches": state["businessId"] == configured_business_id,
                "sessionStatus": state["status"],
                "capabilities": state["capabilities"],
                "externalSubmission": "not_executed",
            },
        })
        return {"success": True, **self.status()}

    def disconnect(self, payload: Dict[str, Any], *, actor: str = "local_api") -> Dict[str, Any]:
        state, state_error = self._load_state()
        if state_error:
            self._remove_state()
            return {"success": True, **self.status()}
        if not state:
            return {"success": True, **self.status()}
        try:
            self._require_session_identity(payload, state)
        except ValueError as exc:
            return {
                "success": False,
                "status": "invalid_session",
                "error": str(exc),
                "externalSubmission": "not_executed",
            }
        current_document_id = _optional_positive_int(state.get("currentDocumentId"))
        if current_document_id:
            self.ledger.release_runtime_lease(
                self._lease_name(current_document_id),
                self._owner_token(state),
            )
        self._remove_state()
        self.ledger.record_audit_event({
            "action": "wave_receipt_executor.session_disconnected",
            "entityType": "wave_receipt_executor_session",
            "entityId": str(state["sessionId"]),
            "details": {
                "actor": str(actor or "local_api")[:120],
                "executorId": state["executorId"],
                "externalSubmission": "not_executed",
            },
        })
        return {"success": True, **self.status()}

    def claim_next(
        self,
        work_order_provider: WorkOrderProvider,
        payload: Dict[str, Any],
        *,
        actor: str = "wave-receipt-executor",
    ) -> Dict[str, Any]:
        state, error = self._ready_session(payload)
        if error:
            return error
        limit = _bounded_int(payload.get("limit"), default=100, minimum=1, maximum=500)
        work_orders_payload = work_order_provider.list_work_orders(limit=limit)
        work_orders = work_orders_payload.get("workOrders") or []
        held = 0
        for work_order in work_orders:
            if not isinstance(work_order, dict) or work_order.get("stage") not in CLAIMABLE_STAGES:
                continue
            document_id = _optional_positive_int(work_order.get("documentId"))
            source = work_order.get("source") if isinstance(work_order.get("source"), dict) else {}
            wave = work_order.get("wave") if isinstance(work_order.get("wave"), dict) else {}
            if not document_id or source.get("localAvailable") is not True:
                continue
            if not str(wave.get("externalTransactionId") or "").strip():
                continue
            lease = self._acquire_document_lease(document_id, state)
            if lease.get("acquired") is not True:
                held += 1
                continue
            now = datetime.now(timezone.utc).isoformat()
            updated_state = {
                **state,
                "status": "busy",
                "currentDocumentId": document_id,
                "heartbeatAt": now,
                "message": f"Processing Wave receipt work order for document {document_id}.",
            }
            self._write_state(updated_state)
            self.ledger.record_audit_event({
                "action": "wave_receipt_executor.work_order_claimed",
                "entityType": "bookkeeping_document",
                "entityId": str(document_id),
                "details": {
                    "actor": str(actor or "wave-receipt-executor")[:120],
                    "executorId": state["executorId"],
                    "sessionId": state["sessionId"],
                    "stage": work_order.get("stage"),
                    "externalSubmission": "not_executed",
                },
            })
            return {
                "success": True,
                "status": "claimed",
                "documentId": document_id,
                "workOrder": work_order,
                "lease": lease.get("lease"),
                "externalSubmission": "policy_gated_browser_execution",
            }
        return {
            "success": True,
            "status": "no_work",
            "eligibleStages": sorted(CLAIMABLE_STAGES),
            "heldByAnotherExecutor": held,
            "externalSubmission": "not_executed",
        }

    def release(
        self,
        document_id: int,
        payload: Dict[str, Any],
        *,
        actor: str = "wave-receipt-executor",
    ) -> Dict[str, Any]:
        state, state_error = self._load_state()
        if state_error or not state:
            return {
                "success": False,
                "status": "invalid_session",
                "error": state_error or "No Wave receipt executor session is registered.",
                "externalSubmission": "not_executed",
            }
        try:
            self._require_session_identity(payload, state)
        except ValueError as exc:
            return {
                "success": False,
                "status": "invalid_session",
                "error": str(exc),
                "externalSubmission": "not_executed",
            }
        normalized_document_id = _optional_positive_int(document_id)
        if not normalized_document_id:
            return {
                "success": False,
                "status": "invalid",
                "error": "documentId must be a positive integer.",
                "externalSubmission": "not_executed",
            }
        current_document_id = _optional_positive_int(state.get("currentDocumentId"))
        if state.get("status") != "busy" or current_document_id != normalized_document_id:
            return {
                "success": False,
                "status": "invalid_claim",
                "error": "The active executor session does not own this document claim.",
                "externalSubmission": "not_executed",
            }
        outcome = str(payload.get("outcome") or "released").strip().lower()
        if outcome not in {"blocked", "failed", "released", "verified"}:
            return {
                "success": False,
                "status": "invalid",
                "error": "outcome must be blocked, failed, released, or verified.",
                "externalSubmission": "not_executed",
            }
        released = self.ledger.release_runtime_lease(
            self._lease_name(normalized_document_id),
            self._owner_token(state),
        )
        now = datetime.now(timezone.utc).isoformat()
        updated_state = {
            **state,
            "status": "error" if outcome == "failed" else "ready",
            "currentDocumentId": None,
            "heartbeatAt": now,
            "message": _safe_message(payload.get("message")) or f"Work order {outcome}.",
        }
        self._write_state(updated_state)
        self.ledger.record_audit_event({
            "action": "wave_receipt_executor.work_order_released",
            "entityType": "bookkeeping_document",
            "entityId": str(normalized_document_id),
            "details": {
                "actor": str(actor or "wave-receipt-executor")[:120],
                "executorId": state["executorId"],
                "sessionId": state["sessionId"],
                "outcome": outcome,
                "leaseReleased": released,
                "externalSubmission": "not_executed",
            },
        })
        return {
            "success": True,
            "status": "released",
            "documentId": normalized_document_id,
            "outcome": outcome,
            "leaseReleased": released,
            "executor": self.status(),
            "externalSubmission": "not_executed",
        }

    def _ready_session(self, payload: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        state, state_error = self._load_state()
        if state_error or not state:
            return None, {
                "success": False,
                "status": "executor_not_ready",
                "error": state_error or "No Wave receipt executor session is registered.",
                "executor": self.status(),
                "externalSubmission": "not_executed",
            }
        try:
            self._require_session_identity(payload, state)
        except ValueError as exc:
            return None, {
                "success": False,
                "status": "invalid_session",
                "error": str(exc),
                "externalSubmission": "not_executed",
            }
        executor_status = self.status()
        if executor_status.get("ready") is not True or state.get("status") != "ready":
            return None, {
                "success": False,
                "status": "executor_not_ready",
                "error": str(executor_status.get("nextAction") or "Wave receipt executor is not ready."),
                "executor": executor_status,
                "externalSubmission": "not_executed",
            }
        return state, None

    def _normalize_session_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Session payload must be a JSON object.")
        keys = {str(key) for key in payload}
        sensitive = sorted(
            key for key in keys
            if any(fragment in key.lower() for fragment in SENSITIVE_FIELD_FRAGMENTS)
        )
        if sensitive:
            raise ValueError(f"Sensitive browser data is forbidden: {', '.join(sensitive)}")
        unknown = sorted(keys - ALLOWED_SESSION_FIELDS)
        if unknown:
            raise ValueError(f"Unsupported session fields: {', '.join(unknown)}")
        executor_id = _safe_identifier(payload.get("executorId"), "executorId")
        session_id = _safe_identifier(payload.get("sessionId"), "sessionId")
        business_id = _safe_identifier(payload.get("businessId"), "businessId")
        session_status = str(payload.get("status") or "").strip().lower()
        if session_status not in ALLOWED_SESSION_STATUSES:
            raise ValueError(
                "status must be authentication_required, busy, error, ready, or stopped."
            )
        capabilities = sorted(set(_string_list(payload.get("capabilities"))))
        unsupported_capabilities = sorted(set(capabilities) - ALLOWED_CAPABILITIES)
        if unsupported_capabilities:
            raise ValueError(
                f"Unsupported capabilities: {', '.join(unsupported_capabilities)}"
            )
        current_document_id = _optional_positive_int(payload.get("currentDocumentId"))
        if payload.get("currentDocumentId") not in (None, "") and not current_document_id:
            raise ValueError("currentDocumentId must be a positive integer.")
        if session_status == "busy" and not current_document_id:
            raise ValueError("A busy session must include currentDocumentId.")
        if session_status != "busy" and current_document_id:
            raise ValueError("Only a busy session may include currentDocumentId.")
        return {
            "executorId": executor_id,
            "sessionId": session_id,
            "businessId": business_id,
            "status": session_status,
            "capabilities": capabilities,
            "browser": _safe_optional_identifier(payload.get("browser"), "browser"),
            "version": _safe_optional_identifier(payload.get("version"), "version"),
            "message": _safe_message(payload.get("message")),
            "currentDocumentId": current_document_id,
        }

    def _require_session_identity(self, payload: Dict[str, Any], state: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ValueError("Session identity must be a JSON object.")
        executor_id = _safe_identifier(payload.get("executorId"), "executorId")
        session_id = _safe_identifier(payload.get("sessionId"), "sessionId")
        if executor_id != state.get("executorId") or session_id != state.get("sessionId"):
            raise ValueError("The executorId and sessionId do not own the active session.")

    def _acquire_document_lease(self, document_id: int, state: Dict[str, Any]) -> Dict[str, Any]:
        return self.ledger.acquire_runtime_lease(
            self._lease_name(document_id),
            self._owner_token(state),
            ttl_seconds=self._claim_lease_seconds(),
            metadata={
                "executorId": state.get("executorId"),
                "sessionId": state.get("sessionId"),
                "documentId": document_id,
                "purpose": "wave_receipt_upload_and_readback",
                "externalSubmission": "policy_gated_browser_execution",
            },
        )

    def _load_state(self) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        path = self._state_path()
        if not os.path.isfile(path):
            return None, None
        try:
            if os.path.getsize(path) > 64 * 1024:
                return None, "Wave receipt executor state exceeds the allowed size."
            with open(path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
            if not isinstance(state, dict) or state.get("stateVersion") != STATE_VERSION:
                return None, "Wave receipt executor state has an unsupported format."
            if any(
                any(fragment in str(key).lower() for fragment in SENSITIVE_FIELD_FRAGMENTS)
                for key in state
            ):
                return None, "Wave receipt executor state contains forbidden sensitive fields."
            return state, None
        except (OSError, ValueError, TypeError) as exc:
            return None, f"Wave receipt executor state could not be read: {exc}"

    def _write_state(self, state: Dict[str, Any]) -> None:
        path = self._state_path()
        directory = os.path.dirname(path) or os.getcwd()
        os.makedirs(directory, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=".wave-receipt-executor-",
            suffix=".tmp",
            dir=directory,
            text=True,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)

    def _remove_state(self) -> None:
        try:
            os.unlink(self._state_path())
        except FileNotFoundError:
            pass

    def _state_path(self) -> str:
        configured = str(
            self.config.get("wave_receipt_executor_state_file")
            or "data/wave-receipt-executor.json"
        )
        return os.path.abspath(os.path.expanduser(configured))

    def _business_id(self) -> str:
        return str(
            self.config.get("waveapps_business_id")
            or self.config.get("wave_business_id")
            or ""
        ).strip()

    def _heartbeat_ttl_seconds(self) -> int:
        return _bounded_int(
            self.config.get("wave_receipt_executor_heartbeat_ttl_seconds"),
            default=90,
            minimum=15,
            maximum=900,
        )

    def _claim_lease_seconds(self) -> int:
        return _bounded_int(
            self.config.get("wave_receipt_executor_claim_lease_seconds"),
            default=300,
            minimum=30,
            maximum=3600,
        )

    @staticmethod
    def _lease_name(document_id: int) -> str:
        return f"wave-receipt-execution:{int(document_id)}"

    @staticmethod
    def _owner_token(state: Dict[str, Any]) -> str:
        identity = f"{state.get('executorId')}|{state.get('sessionId')}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _safe_identifier(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if not SAFE_IDENTIFIER.fullmatch(normalized):
        raise ValueError(f"{field} must contain 1-128 safe identifier characters.")
    return normalized


def wave_business_uuid(value: Any) -> Optional[str]:
    normalized = str(value or "").strip()
    candidates = [normalized]
    if normalized:
        try:
            padded = normalized + "=" * (-len(normalized) % 4)
            decoded = base64.b64decode(padded, validate=True).decode("ascii")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            decoded = ""
        if decoded.startswith("Business:"):
            candidates.insert(0, decoded.partition(":")[2])
    for candidate in candidates:
        try:
            parsed = uuid.UUID(candidate)
        except (AttributeError, TypeError, ValueError):
            continue
        if str(parsed) == candidate.lower():
            return str(parsed)
    return None


def _safe_optional_identifier(value: Any, field: str) -> Optional[str]:
    if value in (None, ""):
        return None
    return _safe_identifier(value, field)


def _safe_message(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    normalized = " ".join(str(value).split())
    return normalized[:500] or None


def _string_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, (list, tuple, set)):
        raise ValueError("capabilities must be an array of strings.")
    normalized = []
    for item in value:
        capability = str(item or "").strip().lower()
        if not capability or not SAFE_IDENTIFIER.fullmatch(capability):
            raise ValueError("capabilities must contain safe non-empty identifiers.")
        normalized.append(capability)
    return normalized


def _parse_datetime(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _optional_positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(maximum, normalized))


def _bool_config(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}
