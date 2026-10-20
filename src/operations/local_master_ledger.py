import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from src.operations.local_exports import master_ledger_freshness
from src.operations.local_ledger import LocalOperationsLedger
from src.utils.csv_safety import neutralize_csv_row


MASTER_LEDGER_PROJECTION_VERSION = "fab-master-ledger-v1"


class LocalMasterLedgerService:
    """Project FAB's normalized records into a downstream source-of-truth ledger."""

    def __init__(self, ledger: LocalOperationsLedger, config: Optional[Dict[str, Any]] = None):
        self.ledger = ledger
        self.config = config or {}

    def project(
        self,
        target_system: Optional[str] = None,
        limit: int = 500,
    ) -> Dict[str, Any]:
        records = self.ledger.list_bookkeeping_records(target_system=target_system, limit=limit)
        attempts_by_record = self.ledger.latest_export_attempts_for_records(
            records,
            target_system=target_system,
        )

        rows = []
        for record in records:
            attempt = attempts_by_record.get(int(record["id"]))
            row = self._project_record(record, attempt)
            row["rowChecksum"] = _checksum({
                key: value for key, value in row.items()
                if key != "rowChecksum"
            })
            rows.append(row)

        rows = sorted(rows, key=lambda row: (str(row.get("recordDate") or ""), int(row.get("recordId") or 0)))
        projection = {
            "success": True,
            "projectionVersion": MASTER_LEDGER_PROJECTION_VERSION,
            "generatedAt": _now(),
            "externalSubmission": "not_executed",
            "targetSystem": target_system,
            "summary": _summary(rows),
            "rows": rows,
        }
        projection["ledgerChecksum"] = _checksum({
            "projectionVersion": projection["projectionVersion"],
            "targetSystem": target_system,
            "rowChecksums": [row["rowChecksum"] for row in rows],
        })
        projection["summary"]["ledgerChecksum"] = projection["ledgerChecksum"]
        return projection

    def csv_artifact(
        self,
        target_system: Optional[str] = None,
        limit: int = 500,
    ) -> Dict[str, Any]:
        projection = self.project(target_system=target_system, limit=limit)
        columns = [
            "recordId",
            "sourceType",
            "recordType",
            "recordDate",
            "vendorName",
            "description",
            "category",
            "amount",
            "vatAmount",
            "currency",
            "targetSystem",
            "targetAccount",
            "recordStatus",
            "exportStatus",
            "reconciliationStatus",
            "downstreamStatus",
            "externalSubmission",
            "exportAttemptId",
            "routingAttemptId",
            "masterLedgerChecksum",
            "rowChecksum",
        ]
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in projection["rows"]:
            writer.writerow(neutralize_csv_row({column: row.get(column) for column in columns}))
        return {
            "success": True,
            "status": "prepared",
            "format": "csv",
            "contentType": "text/csv",
            "filename": "fab-master-ledger.csv" if not target_system else f"fab-master-ledger-{target_system}.csv",
            "content": buffer.getvalue(),
            "ledgerChecksum": projection["ledgerChecksum"],
            "rowCount": len(projection["rows"]),
            "externalSubmission": "not_executed",
        }

    def record_projection_audit(
        self,
        projection: Dict[str, Any],
        actor: str = "fab_local_master_ledger",
    ) -> int:
        return self.ledger.record_audit_event({
            "action": "local_master_ledger.projection_prepared",
            "entityType": "master_ledger",
            "details": {
                "actor": actor,
                "projectionVersion": projection.get("projectionVersion"),
                "ledgerChecksum": projection.get("ledgerChecksum"),
                "rowCount": projection.get("summary", {}).get("totalRows"),
                "targetSystem": projection.get("targetSystem"),
                "externalSubmission": "not_executed",
            },
        })

    def _project_record(
        self,
        record: Dict[str, Any],
        attempt: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        latest_export = metadata.get("latestExport") if isinstance(metadata.get("latestExport"), dict) else {}
        export_details = latest_export.get("details") if isinstance(latest_export.get("details"), dict) else {}
        attempt_metadata = attempt.get("metadata") if isinstance((attempt or {}).get("metadata"), dict) else {}
        attempt_result = attempt.get("result") if isinstance((attempt or {}).get("result"), dict) else {}
        master_ledger_checksum = _first_present(
            export_details.get("masterLedgerChecksum"),
            attempt_metadata.get("masterLedgerChecksum"),
            (attempt_metadata.get("masterLedgerDraft") or {}).get("checksum")
            if isinstance(attempt_metadata.get("masterLedgerDraft"), dict)
            else None,
            attempt_result.get("masterLedgerChecksum"),
        )
        freshness = self._attempt_freshness(attempt)
        downstream_status = _downstream_status(record, attempt, freshness)
        blockers = _blockers(record, attempt, downstream_status, freshness)
        return {
            "recordId": record.get("id"),
            "documentId": record.get("document_id"),
            "bankTransactionId": record.get("bank_transaction_id"),
            "sourceType": record.get("source_type"),
            "recordType": record.get("record_type"),
            "recordDate": record.get("record_date"),
            "vendorName": record.get("vendor_name"),
            "description": record.get("description"),
            "category": record.get("category"),
            "amount": record.get("amount"),
            "vatAmount": record.get("vat_amount"),
            "currency": record.get("currency") or "EUR",
            "targetSystem": record.get("target_system"),
            "targetAccount": record.get("target_account"),
            "recordStatus": record.get("status"),
            "exportStatus": record.get("export_status"),
            "reconciliationStatus": record.get("reconciliation_status"),
            "reviewRequired": bool(record.get("review_required")),
            "lineItemCount": record.get("line_item_count") or len(record.get("line_items") or []),
            "downstreamStatus": downstream_status,
            "externalSubmission": (attempt or {}).get("external_submission") or export_details.get("externalSubmission") or "not_executed",
            "exportAttemptId": (attempt or {}).get("id") or export_details.get("exportAttemptId"),
            "routingAttemptId": (attempt or {}).get("routing_attempt_id") or latest_export.get("routingAttemptId"),
            "actionId": (attempt or {}).get("action_id"),
            "surface": (attempt or {}).get("surface"),
            "operationId": (attempt or {}).get("operation_id"),
            "safety": (attempt or {}).get("safety"),
            "externalId": (attempt or {}).get("external_id") or export_details.get("externalId"),
            "masterLedgerChecksum": master_ledger_checksum,
            "sourceProof": {
                "recordId": record.get("id"),
                "documentId": record.get("document_id"),
                "bankTransactionId": record.get("bank_transaction_id"),
                "sourceType": record.get("source_type"),
            },
            "downstreamProof": {
                "targetSystem": record.get("target_system"),
                "targetAccount": record.get("target_account"),
                "exportAttemptId": (attempt or {}).get("id") or export_details.get("exportAttemptId"),
                "routingAttemptId": (attempt or {}).get("routing_attempt_id") or latest_export.get("routingAttemptId"),
                "externalSubmission": (attempt or {}).get("external_submission") or export_details.get("externalSubmission") or "not_executed",
                "masterLedgerChecksum": master_ledger_checksum,
                "draftFreshness": freshness,
            },
            "blockers": blockers,
            "draftFreshness": freshness,
            "readyForDraft": not blockers and not attempt,
            "readyForApproval": bool(attempt and attempt.get("approval_required") and not blockers),
            "readyForExternalExecution": bool(attempt and attempt.get("status") == "approved" and not blockers),
        }

    def _attempt_freshness(self, attempt: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not attempt:
            return {"status": "not_applicable"}
        freshness = master_ledger_freshness(self.ledger, self.config, attempt)
        return {
            key: value for key, value in freshness.items()
            if key != "currentDraft"
        }


def _downstream_status(
    record: Dict[str, Any],
    attempt: Optional[Dict[str, Any]],
    freshness: Optional[Dict[str, Any]] = None,
) -> str:
    if attempt:
        if (freshness or {}).get("status") in {"checksum_mismatch", "source_missing"}:
            return "stale_master_ledger_draft"
        external_submission = str(attempt.get("external_submission") or "not_executed")
        status = str(attempt.get("status") or "unknown")
        if external_submission in {"executed", "submitted", "queued"}:
            return external_submission
        if status == "approved":
            return "approved_not_executed"
        if bool(attempt.get("approval_required")):
            return "awaiting_approval"
        if status.startswith("blocked"):
            return status
        return status
    export_status = str(record.get("export_status") or "not_started")
    if export_status in {"not_started", "ready"} and str(record.get("status")) in {"ready_to_route", "reviewed", "validated"}:
        return "ready_for_draft"
    return export_status


def _blockers(
    record: Dict[str, Any],
    attempt: Optional[Dict[str, Any]],
    downstream_status: str,
    freshness: Optional[Dict[str, Any]] = None,
) -> list[str]:
    blockers = []
    if bool(record.get("review_required")):
        blockers.append("review_required")
    if str(record.get("status") or "") in {"needs_review", "failed", "duplicate"}:
        blockers.append(f"record_{record.get('status')}")
    reconciliation_status = str(record.get("reconciliation_status") or "")
    if reconciliation_status in {"missing_receipt", "unmatched", "needs_review"}:
        blockers.append(f"reconciliation_{reconciliation_status}")
    export_status = str(record.get("export_status") or "")
    if export_status.startswith("blocked"):
        blockers.append(export_status)
    if attempt and str(attempt.get("status") or "").startswith("blocked"):
        blockers.append(str(attempt.get("status")))
    if downstream_status in {"failed"}:
        blockers.append("downstream_failed")
    if (freshness or {}).get("status") in {"checksum_mismatch", "source_missing"}:
        blockers.append("stale_master_ledger_draft")
    return sorted(set(blockers))


def _summary(rows: list[Dict[str, Any]]) -> Dict[str, Any]:
    by_target: Dict[str, Dict[str, Any]] = {}
    status_counts: Dict[str, int] = {}
    totals_by_currency: Dict[str, float] = {}
    blockers: Dict[str, int] = {}
    for row in rows:
        target = str(row.get("targetSystem") or "unknown")
        status = str(row.get("downstreamStatus") or "unknown")
        currency = str(row.get("currency") or "EUR")
        amount = _float(row.get("amount")) or 0.0
        by_target.setdefault(target, {"rows": 0, "statuses": {}, "amountByCurrency": {}})
        by_target[target]["rows"] += 1
        by_target[target]["statuses"][status] = by_target[target]["statuses"].get(status, 0) + 1
        by_target[target]["amountByCurrency"][currency] = round(
            by_target[target]["amountByCurrency"].get(currency, 0.0) + amount,
            2,
        )
        status_counts[status] = status_counts.get(status, 0) + 1
        totals_by_currency[currency] = round(totals_by_currency.get(currency, 0.0) + amount, 2)
        for blocker in row.get("blockers") or []:
            blockers[blocker] = blockers.get(blocker, 0) + 1
    return {
        "totalRows": len(rows),
        "byTargetSystem": by_target,
        "downstreamStatuses": status_counts,
        "amountByCurrency": totals_by_currency,
        "blockedRows": sum(1 for row in rows if row.get("blockers")),
        "readyForDraft": sum(1 for row in rows if row.get("readyForDraft")),
        "readyForApproval": sum(1 for row in rows if row.get("readyForApproval")),
        "readyForExternalExecution": sum(1 for row in rows if row.get("readyForExternalExecution")),
        "blockers": blockers,
    }


def _checksum(value: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: Dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
