import json
import math
from typing import Any, Dict, Optional

import requests

from src.document_handling.source_identity import source_document_id
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_runtime import build_local_operations_ledger


def _finite_json_number(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("FAB operations response numbers must be finite")
    return parsed


class OperationsClient:
    """Best-effort client for the FAB operations service API.

    The autonomous workflow must keep processing even when telemetry is not
    configured or the web service is temporarily unavailable.
    """

    DEFAULT_ENDPOINTS = {
        "workflow_runs": "/api/fab/operations/workflow-runs",
        "documents": "/api/fab/operations/documents",
        "review_items": "/api/fab/operations/review-items",
        "routing_attempts": "/api/fab/operations/routing-attempts",
        "export_attempts": "/api/fab/operations/export-attempts",
        "reconciliation_matches": "/api/fab/operations/reconciliation-matches",
        "audit_events": "/api/fab/operations/audit-events",
    }
    MAX_RESPONSE_BYTES = 1024 * 1024

    def __init__(self, config: Dict[str, Any], logger=None):
        self.config = config or {}
        self.logger = logger
        self.base_url = str(
            self.config.get("fab_operations_api_url")
            or self.config.get("operations_api_url")
            or ""
        ).rstrip("/")
        configured_enabled = self.config.get(
            "fab_operations_enabled",
            self.config.get("operations_enabled"),
        )
        self.enabled = (
            bool(self.base_url)
            if configured_enabled is None
            else self._as_bool(configured_enabled) and bool(self.base_url)
        )
        self.token = str(
            self.config.get("fab_operations_api_token")
            or self.config.get("operations_api_token")
            or ""
        )
        self.timeout = self._positive_float(
            self.config.get(
                "fab_operations_timeout_seconds",
                self.config.get("operations_timeout_seconds", 5),
            ),
            5.0,
        )
        self.endpoints = dict(self.DEFAULT_ENDPOINTS)
        configured_endpoints = self.config.get("fab_operations_endpoints", {})
        if isinstance(configured_endpoints, dict):
            self.endpoints.update(configured_endpoints)
        self.session = requests.Session()
        self.local_ledger = self._build_local_ledger()

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in {"", "0", "false", "no", "off"}
        return bool(value)

    @staticmethod
    def _positive_float(value: Any, default: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return default
        return min(parsed, 120.0) if math.isfinite(parsed) and parsed > 0 else default

    def _build_local_ledger(self) -> Optional[LocalOperationsLedger]:
        return build_local_operations_ledger(self.config, on_error=self._log_warning)

    def _disabled(self) -> Dict[str, Any]:
        return {"enabled": False, "status": "skipped"}

    def _log_warning(self, message: str):
        if self.logger:
            self.logger.warning(message)

    def _request(
        self,
        method: str,
        endpoint_key: str,
        payload: Dict[str, Any],
        resource_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not self.enabled:
            return self._disabled()

        path = self.endpoints[endpoint_key]
        if resource_id is not None:
            path = f"{path}/{resource_id}"

        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        response = None
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                json=payload,
                headers=headers,
                timeout=self.timeout,
                allow_redirects=False,
                stream=True,
            )
            if 300 <= response.status_code < 400:
                raise ValueError("FAB operations redirects are not allowed")
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if len(content) + len(chunk) > self.MAX_RESPONSE_BYTES:
                    raise ValueError("FAB operations response exceeds the 1 MiB limit")
                content.extend(chunk)
            data = json.loads(content, parse_float=_finite_json_number, parse_constant=_finite_json_number) if content else {"success": True}
            if not isinstance(data, dict):
                raise ValueError("FAB operations response must be a JSON object")
            if "success" in data:
                if not isinstance(data["success"], bool):
                    raise ValueError("FAB operations response success must be a boolean")
                if not data["success"]:
                    raise ValueError("FAB operations service reported an unsuccessful operation")
            if "id" in data and self._result_id({"data": data}) is None:
                raise ValueError("FAB operations response id must be a positive signed 64-bit integer")
            return {"enabled": True, "status": "success", "data": data}
        except requests.RequestException as exc:
            self._log_warning(f"FAB operations request failed for {endpoint_key}: {exc}")
            return {"enabled": True, "status": "failed", "error": str(exc)}
        except (ValueError, RecursionError) as exc:
            self._log_warning(f"FAB operations response was invalid for {endpoint_key}: {exc}")
            return {"enabled": True, "status": "failed", "error": str(exc)}
        except (TypeError, OverflowError) as exc:
            self._log_warning(f"FAB operations payload was not JSON serializable for {endpoint_key}: {exc}")
            return {"enabled": True, "status": "failed", "error": str(exc)}
        finally:
            if response is not None:
                response.close()

    def _create_locally(
        self,
        method_name: str,
        payload: Dict[str, Any],
        preferred_id: Optional[int],
    ) -> Optional[int]:
        if not self.local_ledger:
            return None
        try:
            return getattr(self.local_ledger, method_name)(payload, preferred_id=preferred_id)
        except Exception as exc:
            self._log_warning(f"FAB local operations ledger create failed for {method_name}: {exc}")
            return None

    def _update_locally(self, method_name: str, resource_id: int, payload: Dict[str, Any]) -> bool:
        if not self.local_ledger:
            return False
        try:
            getattr(self.local_ledger, method_name)(resource_id, payload)
            return True
        except Exception as exc:
            self._log_warning(f"FAB local operations ledger update failed for {method_name}: {exc}")
            return False

    @staticmethod
    def _local_result(local_persisted: bool, remote_result: Dict[str, Any]) -> Dict[str, Any]:
        if local_persisted and remote_result.get("status") == "skipped":
            return {"enabled": False, "status": "persisted_local", "localLedger": True}
        if local_persisted and remote_result.get("status") == "failed":
            return {**remote_result, "localLedger": True}
        return remote_result

    @staticmethod
    def _result_id(result: Dict[str, Any]) -> Optional[int]:
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, dict):
            return None
        value = data.get("id")
        if isinstance(value, str):
            value = value.strip()
            if not 1 <= len(value) <= 19 or not value.isascii() or not value.isdecimal():
                return None
            value = int(value)
        if type(value) is not int or not 0 < value <= 2 ** 63 - 1:
            return None
        return value

    def create_workflow_run(self, trigger_source: str, metadata: Optional[Dict[str, Any]] = None) -> Optional[int]:
        result = self._request(
            "POST",
            "workflow_runs",
            {
                "status": "running",
                "triggerSource": trigger_source or "manual",
                "metadata": metadata or {},
            },
        )
        remote_id = self._result_id(result)
        local_id = self._create_locally(
            "create_workflow_run",
            {
                "status": "running",
                "triggerSource": trigger_source or "manual",
                "metadata": metadata or {},
            },
            remote_id,
        )
        return local_id or remote_id

    def update_workflow_run(self, workflow_run_id: Optional[int], **fields: Any) -> Dict[str, Any]:
        if workflow_run_id is None:
            return self._disabled()
        payload = {key: value for key, value in fields.items() if value is not None}
        result = self._request("PATCH", "workflow_runs", payload, workflow_run_id)
        local_persisted = self._update_locally("update_workflow_run", workflow_run_id, payload)
        return self._local_result(local_persisted, result)

    def register_document(
        self,
        source: str,
        document: Dict[str, Any],
        processed_data: Optional[Dict[str, Any]] = None,
        processing_status: str = "imported",
    ) -> Optional[int]:
        extracted = (processed_data or {}).get("extracted_data", {})
        payload = {
            "source": source or "unknown",
            "sourceDocumentId": source_document_id(document),
            "originalFilename": document.get("original_filename") or document.get("filename") or "unknown",
            "mimeType": document.get("mime_type"),
            "storagePath": document.get("local_path"),
            "documentType": document.get("document_type", "unknown"),
            "processingStatus": processing_status,
            "duplicateFingerprint": (processed_data or {}).get("duplicate_fingerprint") or document.get("duplicate_fingerprint"),
            "duplicateOfDocumentId": (processed_data or {}).get("duplicate_of_document_id") or document.get("duplicate_of_document_id"),
            "vendorName": extracted.get("vendor_name") or (processed_data or {}).get("vendor_name"),
            "category": (processed_data or {}).get("category") or extracted.get("category"),
            "transactionDate": extracted.get("transaction_date"),
            "totalAmount": extracted.get("total_amount"),
            "vatAmount": extracted.get("vat_amount"),
            "confidenceScore": (processed_data or {}).get("confidence_score"),
            "ocrText": (processed_data or {}).get("ocr_text"),
            "extractedData": extracted or None,
            "metadata": {
                "source_document": {
                    key: value
                    for key, value in document.items()
                    if key not in {"content"}
                },
            },
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        result = self._request("POST", "documents", payload)
        remote_id = self._result_id(result)
        local_id = self._create_locally("register_document", payload, remote_id)
        return local_id or remote_id

    def update_document(
        self,
        document_id: Optional[int],
        processed_data: Optional[Dict[str, Any]] = None,
        processing_status: Optional[str] = None,
        duplicate_fingerprint: Optional[str] = None,
        duplicate_of_document_id: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if document_id is None:
            return self._disabled()

        extracted = (processed_data or {}).get("extracted_data", {})
        payload = {
            "processingStatus": processing_status,
            "duplicateFingerprint": duplicate_fingerprint or (processed_data or {}).get("duplicate_fingerprint"),
            "duplicateOfDocumentId": duplicate_of_document_id or (processed_data or {}).get("duplicate_of_document_id"),
            "vendorName": extracted.get("vendor_name") or (processed_data or {}).get("vendor_name"),
            "category": (processed_data or {}).get("category") or extracted.get("category"),
            "transactionDate": extracted.get("transaction_date"),
            "totalAmount": extracted.get("total_amount"),
            "vatAmount": extracted.get("vat_amount"),
            "confidenceScore": (processed_data or {}).get("confidence_score"),
            "ocrText": (processed_data or {}).get("ocr_text"),
            "extractedData": extracted or None,
            "metadata": metadata,
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        result = self._request("PATCH", "documents", payload, document_id)
        local_persisted = self._update_locally("update_document", document_id, payload)
        return self._local_result(local_persisted, result)

    def create_review_item(
        self,
        document_id: Optional[int],
        reason: str,
        details: str = "",
        status: str = "pending",
    ) -> Optional[int]:
        payload = {
            "documentId": document_id,
            "reason": reason,
            "details": details,
            "status": status,
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        result = self._request("POST", "review_items", payload)
        remote_id = self._result_id(result)
        local_id = self._create_locally("create_review_item", payload, remote_id)
        return local_id or remote_id

    def create_routing_attempt(
        self,
        document_id: Optional[int],
        target: str,
        status: str,
        workflow_run_id: Optional[int] = None,
        external_id: Optional[str] = None,
        message: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        bookkeeping_record_id: Optional[int] = None,
    ) -> Optional[int]:
        if document_id is None and bookkeeping_record_id is None:
            return None
        payload = {
            "documentId": document_id,
            "bookkeepingRecordId": bookkeeping_record_id,
            "workflowRunId": workflow_run_id,
            "target": target,
            "status": status,
            "externalId": external_id,
            "message": message,
            "metadata": metadata or {},
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        result = self._request("POST", "routing_attempts", payload)
        remote_id = self._result_id(result)
        local_id = self._create_locally("create_routing_attempt", payload, remote_id)
        return local_id or remote_id

    def upsert_export_attempt(
        self,
        document_id: Optional[int],
        status: str,
        routing_attempt_id: Optional[int] = None,
        bookkeeping_record_id: Optional[int] = None,
        workflow_run_id: Optional[int] = None,
        target_system: str = "waveapps",
        target_account: Optional[str] = None,
        action_id: Optional[str] = None,
        surface: Optional[str] = None,
        operation_id: Optional[str] = None,
        safety: str = "requires_confirmation",
        approval_required: bool = True,
        external_submission: str = "not_executed",
        message: Optional[str] = None,
        payload_data: Optional[Dict[str, Any]] = None,
        result: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        payload = {
            "bookkeepingRecordId": bookkeeping_record_id,
            "documentId": document_id,
            "routingAttemptId": routing_attempt_id,
            "workflowRunId": workflow_run_id,
            "targetSystem": target_system,
            "targetAccount": target_account,
            "actionId": action_id,
            "surface": surface,
            "operationId": operation_id,
            "status": status,
            "safety": safety,
            "approvalRequired": approval_required,
            "externalSubmission": external_submission,
            "message": message,
            "payload": payload_data,
            "result": result,
            "metadata": metadata or {},
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        result_payload = self._request("POST", "export_attempts", payload)
        remote_id = self._result_id(result_payload)
        local_id = self._create_locally("upsert_export_attempt", payload, remote_id)
        return local_id or remote_id

    def create_reconciliation_match(
        self,
        bank_transaction_id: str,
        status: str,
        document_id: Optional[int] = None,
        confidence_score: Optional[float] = None,
        amount_difference: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        payload = {
            "documentId": document_id,
            "bankTransactionId": bank_transaction_id,
            "status": status,
            "confidenceScore": confidence_score,
            "amountDifference": amount_difference,
            "metadata": metadata or {},
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        result = self._request("POST", "reconciliation_matches", payload)
        remote_id = self._result_id(result)
        local_id = self._create_locally("create_reconciliation_match", payload, remote_id)
        return local_id or remote_id

    def record_audit_event(
        self,
        action: str,
        entity_type: str,
        entity_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        payload = {
            "action": action,
            "entityType": entity_type,
            "entityId": entity_id,
            "details": details or {},
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        result = self._request("POST", "audit_events", payload)
        remote_id = self._result_id(result)
        local_id = self._create_locally("record_audit_event", payload, remote_id)
        return local_id or remote_id
