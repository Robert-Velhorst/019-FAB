from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from src.operations.local_close_readiness import LocalCloseReadinessService
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_master_ledger import LocalMasterLedgerService


CLOSE_PACK_FORMAT = "fab-period-close-pack-v1"


class LocalClosePackService:
    """Create durable period-close evidence packs from local FAB state."""

    def __init__(self, ledger: LocalOperationsLedger, config: Optional[Dict[str, Any]] = None):
        self.ledger = ledger
        self.config = config or {}
        self.ledger_path = os.path.abspath(ledger.path)
        self.close_pack_dir = self._close_pack_dir()
        os.makedirs(self.close_pack_dir, exist_ok=True)

    def prepare(
        self,
        workflow_id: str = "daily_reconciliation_run",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        actor: str = "fab_close_pack",
        require_ready: bool = True,
    ) -> Dict[str, Any]:
        close_readiness = LocalCloseReadinessService(self.ledger, self.config).assess(
            workflow_id=workflow_id,
            from_date=from_date,
            to_date=to_date,
        )
        if require_ready and not close_readiness.get("canClose"):
            return {
                "success": False,
                "status": "blocked_not_ready",
                "externalSubmission": "not_executed",
                "closeReadiness": close_readiness,
                "message": "Close readiness gates must be ready before preparing a period close pack.",
            }

        payload = self._payload(close_readiness=close_readiness, actor=actor)
        timestamp = _timestamp()
        filename = (
            f"fab-period-close-pack_"
            f"{_safe_name(payload['fromDate'])}_{_safe_name(payload['toDate'])}_"
            f"{_safe_name(payload['workflowId'])}_{timestamp}.json"
        )
        path = os.path.join(self.close_pack_dir, filename)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2, default=str)
            handle.write("\n")
        digest = _sha256_file(path)
        size_bytes = os.path.getsize(path)

        self.ledger.record_audit_event({
            "action": "local_close_pack.prepared",
            "entityType": "period_close_pack",
            "entityId": filename,
            "details": {
                "actor": actor,
                "closePackPath": path,
                "closePackFilename": filename,
                "sha256": digest,
                "sizeBytes": size_bytes,
                "workflowId": payload["workflowId"],
                "fromDate": payload["fromDate"],
                "toDate": payload["toDate"],
                "externalSubmission": "not_executed",
                "closeReadiness": {
                    "status": close_readiness.get("status"),
                    "blockingCount": close_readiness.get("blockingCount"),
                    "attentionCount": close_readiness.get("attentionCount"),
                },
            },
        })
        return {
            "success": True,
            "status": "prepared",
            "externalSubmission": "not_executed",
            "closePackPath": path,
            "closePackFilename": filename,
            "sha256": digest,
            "sizeBytes": size_bytes,
            "closeReadiness": close_readiness,
            "manifest": _manifest(payload, digest, size_bytes),
        }

    def list_packs(self, limit: int = 25) -> Dict[str, Any]:
        packs = []
        for name in os.listdir(self.close_pack_dir) if os.path.isdir(self.close_pack_dir) else []:
            if not name.lower().endswith(".json"):
                continue
            path = os.path.join(self.close_pack_dir, name)
            try:
                inspected = self.inspect_pack(path, include_payload=False)
                packs.append({
                    "closePackFilename": name,
                    "closePackPath": path,
                    "status": inspected["status"],
                    "createdAt": inspected.get("manifest", {}).get("createdAt"),
                    "workflowId": inspected.get("manifest", {}).get("workflowId"),
                    "fromDate": inspected.get("manifest", {}).get("fromDate"),
                    "toDate": inspected.get("manifest", {}).get("toDate"),
                    "sha256": inspected.get("sha256"),
                    "sizeBytes": inspected.get("sizeBytes"),
                })
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                packs.append({
                    "closePackFilename": name,
                    "closePackPath": path,
                    "status": "invalid",
                    "error": str(exc),
                    "sizeBytes": os.path.getsize(path) if os.path.exists(path) else 0,
                })
        packs.sort(key=lambda item: item.get("createdAt") or "", reverse=True)
        return {
            "closePackDir": self.close_pack_dir,
            "packs": packs[: _bounded_limit(limit)],
        }

    def inspect_pack(self, close_pack_path: str, include_payload: bool = True) -> Dict[str, Any]:
        resolved_path = self._resolve_pack_path(close_pack_path)
        if not os.path.exists(resolved_path):
            raise ValueError(f"Close pack not found: {resolved_path}")
        if not resolved_path.lower().endswith(".json"):
            raise ValueError("Only .json FAB period close packs are supported")
        with open(resolved_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if payload.get("format") != CLOSE_PACK_FORMAT:
            raise ValueError("Unsupported FAB period close pack format")
        digest = _sha256_file(resolved_path)
        size_bytes = os.path.getsize(resolved_path)
        result = {
            "success": True,
            "status": "valid",
            "closePackPath": resolved_path,
            "closePackFilename": os.path.basename(resolved_path),
            "sha256": digest,
            "sizeBytes": size_bytes,
            "manifest": _manifest(payload, digest, size_bytes),
        }
        if include_payload:
            result["payload"] = payload
        return result

    def _payload(self, close_readiness: Dict[str, Any], actor: str) -> Dict[str, Any]:
        workflow_id = str(close_readiness.get("workflowId") or "daily_reconciliation_run")
        master_ledger = LocalMasterLedgerService(self.ledger, self.config).project(limit=500)
        return {
            "format": CLOSE_PACK_FORMAT,
            "createdAt": _now(),
            "actor": actor,
            "workflowId": workflow_id,
            "fromDate": close_readiness.get("fromDate"),
            "toDate": close_readiness.get("toDate"),
            "externalSubmission": "not_executed",
            "sourceLedgerBasename": os.path.basename(self.ledger_path),
            "closeReadiness": close_readiness,
            "evidence": {
                "metrics": self.ledger.dashboard_metrics(),
                "masterLedger": master_ledger,
                "waveReportSnapshots": self.ledger.list_wave_report_snapshots(workflow_id=workflow_id, limit=500),
                "waveOperationSnapshots": self.ledger.list_wave_operation_snapshots(workflow_id=workflow_id, limit=500),
                "bankStatementImports": self.ledger.list_bank_statement_imports(limit=500),
                "bankTransactions": self.ledger.list_bank_transactions(limit=500),
                "bookkeepingRecords": self.ledger.list_bookkeeping_records(limit=500),
                "reconciliationMatches": self.ledger.list_reconciliation_matches(limit=500),
                "exportAttempts": self.ledger.list_export_attempts(limit=500),
                "reviewItems": self.ledger.list_review_items(limit=500),
                "vendorCategoryRules": self.ledger.list_vendor_category_rules(limit=500),
                "auditEvents": self.ledger.list_audit_events(limit=500),
            },
            "safety": {
                "containsSecrets": False,
                "externalSubmission": "not_executed",
                "requiresReadyCloseGates": True,
            },
        }

    def _close_pack_dir(self) -> str:
        value = _config_value(self.config, "fab_local_close_pack_dir", "operations_close_pack_dir")
        if not value:
            value = os.path.join(os.path.dirname(self.ledger_path), "close_packs")
        return os.path.realpath(os.path.abspath(os.path.expanduser(str(value))))

    def _resolve_pack_path(self, close_pack_path: str) -> str:
        if not close_pack_path:
            raise ValueError("closePackPath is required")
        candidate = os.path.expanduser(str(close_pack_path))
        if not os.path.isabs(candidate):
            candidate = os.path.join(self.close_pack_dir, candidate)
        candidate = os.path.realpath(os.path.abspath(candidate))
        close_pack_dir = os.path.normcase(os.path.realpath(os.path.abspath(self.close_pack_dir)))
        try:
            common_path = os.path.commonpath([os.path.normcase(candidate), close_pack_dir])
        except ValueError as exc:
            raise ValueError("Close pack path must be inside the configured FAB close pack directory") from exc
        if common_path != close_pack_dir:
            raise ValueError("Close pack path must be inside the configured FAB close pack directory")
        return candidate


def _manifest(payload: Dict[str, Any], digest: str, size_bytes: int) -> Dict[str, Any]:
    evidence = payload.get("evidence") or {}
    master_ledger = evidence.get("masterLedger") if isinstance(evidence.get("masterLedger"), dict) else {}
    master_ledger_summary = master_ledger.get("summary") if isinstance(master_ledger.get("summary"), dict) else {}
    return {
        "format": payload.get("format"),
        "createdAt": payload.get("createdAt"),
        "workflowId": payload.get("workflowId"),
        "fromDate": payload.get("fromDate"),
        "toDate": payload.get("toDate"),
        "externalSubmission": payload.get("externalSubmission"),
        "sha256": digest,
        "sizeBytes": size_bytes,
        "masterLedger": {
            "ledgerChecksum": master_ledger.get("ledgerChecksum"),
            "totalRows": master_ledger_summary.get("totalRows", 0),
            "blockedRows": master_ledger_summary.get("blockedRows", 0),
            "externalSubmission": master_ledger.get("externalSubmission"),
        },
        "evidenceCounts": {
            key: len(value) if isinstance(value, list) else None
            for key, value in evidence.items()
            if isinstance(value, list)
        },
    }


def _config_value(config: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = config.get(key)
        if value not in (None, ""):
            return value
    return None


def _bounded_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 25
    return max(1, min(parsed, 100))


def _safe_name(value: Any) -> str:
    text = str(value or "none").strip().lower()
    text = re.sub(r"[^a-z0-9_.-]+", "-", text)
    return text.strip("-") or "none"


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
