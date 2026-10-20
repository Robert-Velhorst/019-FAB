import hashlib
import json
import math
import sqlite3
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
    REFERENCE_RESOURCES = {
        "documentId": "documents",
        "duplicateOfDocumentId": "documents",
        "workflowRunId": "workflow_runs",
        "routingAttemptId": "routing_attempts",
        "bookkeepingRecordId": "bookkeeping_records",
    }
    AUDIT_RESOURCES = {
        "bookkeeping_document": "documents", "document": "documents",
        "workflow_run": "workflow_runs", "review_item": "review_items",
        "routing_attempt": "routing_attempts", "export_attempt": "export_attempts",
        "reconciliation_match": "reconciliation_matches", "bookkeeping_record": "bookkeeping_records",
    }

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
        local_enabled = self.config.get("fab_local_ledger_enabled", self.config.get("operations_local_ledger_enabled"))
        self.local_required = (
            bool(self.config.get("fab_local_ledger_path") or self.config.get("operations_ledger_path"))
            if local_enabled is None else self._as_bool(local_enabled)
        )
        self.destination_key = hashlib.sha256(json.dumps(
            {"baseUrl": self.base_url, "endpoints": self.endpoints,
             "instanceScope": str(self.config.get("fab_operations_identity_scope") or self.config.get("operations_identity_scope") or "")}, sort_keys=True,
        ).encode("utf-8")).hexdigest()

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

        try:
            payload = dict(payload)
            if self.local_required and not self.local_ledger:
                raise ValueError("The authoritative local ledger is unavailable")
            if self.local_ledger:
                if resource_id is not None:
                    resource_id = self._remote_identity(endpoint_key, resource_id)
                for field, resource in self.REFERENCE_RESOURCES.items():
                    if payload.get(field) is not None:
                        payload[field] = self._remote_identity(resource, payload[field])
                if endpoint_key == "audit_events":
                    resource = self.AUDIT_RESOURCES.get(str(payload.get("entityType")))
                    if resource and payload.get("entityId") is not None:
                        payload["entityId"] = str(self._remote_identity(resource, payload["entityId"]))
                    payload["details"] = {"localEvidence": payload.get("details") or {}, "localReferenceScope": self.destination_key}
        except (ValueError, sqlite3.Error):
            return {"enabled": True, "status": "deferred", "errorCode": "remote_identity_unavailable",
                    "error": "Remote telemetry requires a verified identity link; local evidence is retained."}

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

    def _remote_identity(self, resource: str, local_id: Any) -> int:
        normalized_id = self._result_id({"data": {"id": local_id}})
        if normalized_id is None:
            raise ValueError("Invalid local identity")
        remote_id = self.local_ledger.get_remote_identity(self.destination_key, resource, normalized_id)
        if remote_id is None:
            raise ValueError("Remote identity is not established")
        return remote_id

    def _create_and_mirror(
        self,
        endpoint_key: str,
        method_name: str,
        payload: Dict[str, Any],
    ) -> Optional[int]:
        if not self.local_ledger:
            if self.local_required:
                self._log_warning("FAB authoritative local ledger is unavailable; telemetry was not submitted")
                return None
            return self._result_id(self._request("POST", endpoint_key, payload))
        if any(payload.get(field) is not None and self._result_id({"data": {"id": payload[field]}}) is None
               for field in self.REFERENCE_RESOURCES):
            self._log_warning("FAB local telemetry reference is invalid; no record was created")
            return None
        try:
            local_id = getattr(self.local_ledger, method_name)(payload)
        except Exception as exc:
            self._log_warning(f"FAB local operations ledger create failed for {method_name}: {exc}")
            return None
        if not self.enabled:
            return local_id
        try:
            link = self.local_ledger.get_remote_identity_link(self.destination_key, endpoint_key, local_id)
            if link and link["status"] != "verified":
                self._record_mirror_outcome(endpoint_key, local_id, {"status": "deferred", "errorCode": "remote_identity_conflict"})
                return local_id
            if link and endpoint_key != "documents":
                return local_id
            result = self._request("POST", endpoint_key, payload)
            remote_id = self._result_id(result)
            if remote_id is not None:
                self.local_ledger.bind_remote_identity(self.destination_key, endpoint_key, local_id, remote_id)
            elif result.get("status") == "success":
                result = {"status": "failed", "errorCode": "remote_identity_missing_from_response"}
            self._record_mirror_outcome(endpoint_key, local_id, result)
        except (ValueError, sqlite3.Error):
            self._log_warning("FAB remote identity could not be retained; local evidence remains authoritative")
            self._record_mirror_outcome(endpoint_key, local_id, {"status": "failed", "errorCode": "remote_identity_conflict"})
        return local_id

    def _record_mirror_outcome(self, resource: str, local_id: int, result: Dict[str, Any]) -> None:
        if not self.local_ledger or result.get("status") not in {"failed", "deferred"}:
            return
        try:
            self.local_ledger.record_audit_event({
                "action": "operations.remote." + result["status"],
                "entityType": next((name for name, value in self.AUDIT_RESOURCES.items() if value == resource), resource),
                "entityId": str(local_id),
                "details": {"destinationKey": self.destination_key, "errorCode": result.get("errorCode", "remote_request_failed"),
                            "externalSubmission": "not_executed" if result["status"] == "deferred" else "unknown"},
            })
        except (ValueError, sqlite3.Error):
            self._log_warning("FAB remote telemetry failure audit could not be retained")

    def _update_locally(self, method_name: str, resource_id: int, payload: Dict[str, Any]) -> bool:
        if not self.local_ledger:
            return False
        try:
            getter = "get_document_core" if method_name == "update_document" else "get_workflow_run"
            if not getattr(self.local_ledger, getter)(resource_id):
                return False
            getattr(self.local_ledger, method_name)(resource_id, payload)
            return True
        except Exception as exc:
            self._log_warning(f"FAB local operations ledger update failed for {method_name}: {exc}")
            return False

    def _update_and_mirror(self, resource: str, method: str, local_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        if (self._result_id({"data": {"id": local_id}}) is None
                or any(payload.get(field) is not None and self._result_id({"data": {"id": payload[field]}}) is None
                       for field in self.REFERENCE_RESOURCES)):
            return {"enabled": self.enabled, "status": "failed", "errorCode": "invalid_local_identity"}
        local_persisted = self._update_locally(method, local_id, payload)
        if (self.local_required or self.local_ledger is not None) and not local_persisted:
            return {"enabled": self.enabled, "status": "failed", "localLedger": False,
                    "errorCode": "local_persistence_failed", "error": "Authoritative local update could not be retained."}
        result = self._request("PATCH", resource, payload, local_id)
        self._record_mirror_outcome(resource, local_id, result)
        return self._local_result(local_persisted, result)

    @staticmethod
    def _local_result(local_persisted: bool, remote_result: Dict[str, Any]) -> Dict[str, Any]:
        if local_persisted and remote_result.get("status") == "skipped":
            return {"enabled": False, "status": "persisted_local", "localLedger": True}
        if local_persisted and remote_result.get("status") in {"failed", "deferred", "success"}:
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
        return self._create_and_mirror(
            "workflow_runs", "create_workflow_run",
            {
                "status": "running",
                "triggerSource": trigger_source or "manual",
                "metadata": metadata or {},
            },
        )

    def update_workflow_run(self, workflow_run_id: Optional[int], **fields: Any) -> Dict[str, Any]:
        if workflow_run_id is None:
            return self._disabled()
        payload = {key: value for key, value in fields.items() if value is not None}
        return self._update_and_mirror("workflow_runs", "update_workflow_run", workflow_run_id, payload)

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
            "contentSha256": document.get("content_sha256") or document.get("contentSha256"),
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
        return self._create_and_mirror("documents", "register_document", payload)

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
        return self._update_and_mirror("documents", "update_document", document_id, payload)

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
        return self._create_and_mirror("review_items", "create_review_item", payload)

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
        return self._create_and_mirror("routing_attempts", "create_routing_attempt", payload)

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
        return self._create_and_mirror("export_attempts", "upsert_export_attempt", payload)

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
        return self._create_and_mirror("reconciliation_matches", "create_reconciliation_match", payload)

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
        return self._create_and_mirror("audit_events", "record_audit_event", payload)
