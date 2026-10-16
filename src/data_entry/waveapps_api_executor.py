from __future__ import annotations

from typing import Any, Dict, Optional

from src.data_entry.waveapps_business_handler import WaveappsBusinessHandler
from src.data_entry.waveapps_personal_handler import WaveappsPersonalHandler


SUPPORTED_WAVE_API_ACTIONS = {"transaction_add"}
WAVE_TARGET_ALIASES = {
    "business": "waveapps_business",
    "wave_business": "waveapps_business",
    "waveapps_business": "waveapps_business",
    "personal": "waveapps_personal",
    "wave_personal": "waveapps_personal",
    "waveapps_personal": "waveapps_personal",
}


class WaveappsApiExecutor:
    """Execute approved FAB Wave operations through the supported public API.

    This adapter deliberately supports only actions backed by a verified API
    handler. A modeled Wave UI capability is not treated as externally queued
    unless an executor actually accepted it.
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        handlers: Optional[Dict[str, Any]] = None,
    ):
        self.config = config or {}
        self.handlers = handlers or {}

    def execute(
        self,
        *,
        target_system: Any,
        action_id: Any,
        payload: Optional[Dict[str, Any]],
        idempotency_key: Any,
        document_id: Any = None,
        bookkeeping_record_id: Any = None,
    ) -> Dict[str, Any]:
        target = self._resolve_target(target_system)
        if not target:
            return {
                "status": "needs_review",
                "message": (
                    "Wave execution needs an explicit waveapps_business or "
                    "waveapps_personal target. Configure waveapps_default_target "
                    "only when a safe default is intentional."
                ),
                "requires_manual_review": True,
                "review_reason": "wave_target_ambiguous",
            }

        normalized_action = str(action_id or "").strip()
        if normalized_action not in SUPPORTED_WAVE_API_ACTIONS:
            return {
                "status": "needs_review",
                "message": f"Wave action {normalized_action!r} has no verified FAB API executor.",
                "requires_manual_review": True,
                "review_reason": "wave_executor_unavailable",
                "target_system": target,
                "action_id": normalized_action,
            }

        missing_credentials = self._missing_credentials(target)
        if missing_credentials:
            return {
                "status": "blocked_requires_credentials",
                "message": "Wave API execution is not configured for this target.",
                "requires_manual_review": True,
                "review_reason": "wave_credentials_missing",
                "missing_configuration": missing_credentials,
                "target_system": target,
                "action_id": normalized_action,
            }

        categorized_data = _categorized_data(
            payload or {},
            target_system=target,
            idempotency_key=idempotency_key,
            document_id=document_id,
            bookkeeping_record_id=bookkeeping_record_id,
        )
        handler = self.handlers.get(target) or self._build_handler(target)
        try:
            result = handler.enter_data(categorized_data)
        except Exception:
            return {
                "status": "outcome_unknown",
                "message": "Wave did not return a verifiable result. Check Wave for this transaction before taking further action.",
                "requires_manual_review": True,
                "review_reason": "wave_api_outcome_unknown",
                "target_system": target,
                "action_id": normalized_action,
                "provider_external_id": _fab_external_id(
                    categorized_data.get("idempotency_key") or categorized_data.get("document_id")
                ),
            }

        public_result = dict(result or {})
        public_result.update({
            "target_system": target,
            "action_id": normalized_action,
            "idempotency_key": str(idempotency_key or ""),
        })
        if public_result.get("status") == "outcome_unknown":
            public_result.setdefault(
                "provider_external_id",
                _fab_external_id(categorized_data.get("idempotency_key") or categorized_data.get("document_id")),
            )
        if public_result.get("status") == "csv_generated":
            public_result.update({
                "status": "needs_review",
                "requires_manual_review": True,
                "review_reason": "wave_credentials_missing",
                "message": "Wave API execution was not configured; no external submission was made.",
            })
        return public_result

    def _resolve_target(self, target_system: Any) -> Optional[str]:
        normalized = _normalize_target(target_system)
        if normalized in WAVE_TARGET_ALIASES:
            return WAVE_TARGET_ALIASES[normalized]
        if normalized in {"wave", "waveapps", ""}:
            configured = _first_config_value(
                self.config,
                "waveapps_default_target",
                "operations_waveapps_default_target",
                "fab_waveapps_default_target",
            )
            return WAVE_TARGET_ALIASES.get(_normalize_target(configured))
        return None

    def _missing_credentials(self, target: str) -> list[str]:
        prefix = target
        required = (
            (f"{prefix}_access_token", ("access_token",)),
            (f"{prefix}_id", ("id", "business_id")),
        )
        nested = self.config.get(prefix) if isinstance(self.config.get(prefix), dict) else {}
        missing = []
        for flat_key, nested_keys in required:
            nested_value = next((nested.get(key) for key in nested_keys if nested.get(key) not in (None, "")), None)
            if self.config.get(flat_key) in (None, "") and nested_value in (None, ""):
                missing.append(flat_key)
        return missing

    def _build_handler(self, target: str) -> Any:
        handler_config = dict(self.config)
        nested = self.config.get(target) if isinstance(self.config.get(target), dict) else {}
        for key, value in nested.items():
            handler_config.setdefault(f"{target}_{key}", value)
        if target == "waveapps_business":
            return WaveappsBusinessHandler(handler_config)
        return WaveappsPersonalHandler(handler_config)


def _categorized_data(
    payload: Dict[str, Any],
    *,
    target_system: str,
    idempotency_key: Any,
    document_id: Any,
    bookkeeping_record_id: Any,
) -> Dict[str, Any]:
    line_items = payload.get("lineItems") if isinstance(payload.get("lineItems"), list) else []
    return {
        "document_id": document_id or bookkeeping_record_id or idempotency_key,
        "bookkeeping_record_id": bookkeeping_record_id,
        "document_type": payload.get("documentType"),
        "transaction_direction": payload.get("transactionDirection"),
        "target_system": target_system,
        "target_account": payload.get("account"),
        "category": payload.get("category"),
        "idempotency_key": str(idempotency_key or ""),
        "extracted_data": {
            "description": payload.get("description"),
            "document_type": payload.get("documentType"),
            "transaction_direction": payload.get("transactionDirection"),
            "vendor_name": payload.get("vendor"),
            "transaction_date": payload.get("date"),
            "total_amount": payload.get("amount"),
            "line_items": line_items,
        },
    }


def _normalize_target(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _first_config_value(config: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = config.get(key)
        if value not in (None, ""):
            return value
    return None


def _fab_external_id(idempotency_key: Any) -> str:
    value = str(idempotency_key or "").strip()
    return f"fab:{value}" if value else "fab:unidentified"
