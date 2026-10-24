from __future__ import annotations

import hashlib
import json
import os
import platform
import sqlite3
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from src.operations.local_autonomy_control import LocalAutonomyControlService
from src.operations.local_health import LocalOperationsHealth
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_readiness import LocalReadinessService


SUPPORT_BUNDLE_SCHEMA_VERSION = 1


class LocalSupportBundleService:
    """Create a deliberately data-minimal diagnostic archive for support."""

    def __init__(
        self,
        ledger: LocalOperationsLedger,
        config: Optional[Dict[str, Any]] = None,
        readiness: Optional[LocalReadinessService] = None,
    ):
        self.ledger = ledger
        self.config = config or {}
        self.readiness = readiness or LocalReadinessService(self.config)

    def doctor(self) -> Dict[str, Any]:
        readiness = self.readiness.summarize()
        health_issue_limit = _bounded_positive_int(
            self.config.get("fab_support_health_issue_limit")
            or self.config.get("operations_support_health_issue_limit")
            or self.config.get("support_health_issue_limit"),
            default=100,
            maximum=500,
        )
        health = LocalOperationsHealth(self.ledger, self.config).summarize(
            issue_limit=health_issue_limit,
        )
        dependencies = [
            {
                key: dependency.get(key)
                for key in ("id", "label", "status", "configured", "required", "version")
                if key in dependency
            }
            for dependency in readiness.get("dependencies") or []
        ]
        sources = [
            {
                key: source.get(key)
                for key in ("id", "label", "status", "configured", "ready")
                if key in source
            }
            for source in readiness.get("sources") or []
        ]
        readiness_issues = [
            {
                key: issue.get(key)
                for key in ("type", "severity", "sourceId", "dependencyId", "pathId")
                if issue.get(key) is not None
            }
            for issue in readiness.get("issues") or []
        ]
        health_issues = [
            {
                key: issue.get(key)
                for key in ("type", "severity", "entityType", "ageHours")
                if issue.get(key) is not None
            }
            for issue in health.get("issues") or []
        ]
        autonomy = LocalAutonomyControlService(self.ledger).status()
        readiness_compact = {
            "status": readiness.get("status"),
            "issueCount": len(readiness.get("issues") or []),
            "blockedIssues": sum(
                1 for issue in readiness.get("issues") or []
                if issue.get("severity") == "blocked"
            ),
            "attentionIssues": sum(
                1 for issue in readiness.get("issues") or []
                if issue.get("severity") == "attention"
            ),
            "readySources": sum(
                1 for source in readiness.get("sources") or []
                if source.get("status") == "ready"
            ),
            "dashboardUrl": (readiness.get("localAccess") or {}).get("dashboardUrl"),
            "apiBaseUrl": (readiness.get("localAccess") or {}).get("apiBaseUrl"),
            "authMode": (readiness.get("localAccess") or {}).get("authMode"),
            "remoteExposureSafe": (readiness.get("security") or {}).get("remoteExposureSafe"),
        }
        return {
            "schemaVersion": SUPPORT_BUNDLE_SCHEMA_VERSION,
            "generatedAt": _utc_timestamp(),
            "runtime": {
                "python": platform.python_version(),
                "platform": platform.system(),
                "platformRelease": platform.release(),
                "sqlite": sqlite3.sqlite_version,
                "ledgerSchema": self.ledger.schema_status(),
            },
            "readiness": {
                **readiness_compact,
                "dependencies": dependencies,
                "sources": sources,
                "issues": readiness_issues,
            },
            "health": {
                "status": health.get("status"),
                "generatedAt": health.get("generatedAt"),
                "metrics": health.get("metrics") or {},
                "severityCounts": health.get("severityCounts") or {},
                "issueCount": health.get("issueCount", len(health_issues)),
                "issueTypeCounts": health.get("issueTypeCounts") or {},
                "issueLimit": health.get("issueLimit"),
                "issuesReturned": len(health_issues),
                "issuesTruncated": bool(health.get("issuesTruncated")),
                "issues": health_issues,
            },
            "autonomy": {
                "status": autonomy.get("status"),
                "active": bool(autonomy.get("active")),
                "updatedAt": autonomy.get("updatedAt"),
                "cycleActive": bool(autonomy.get("cycleActive")),
                "externalSubmission": "not_executed",
            },
            "privacy": {
                "containsFinancialDocuments": False,
                "containsOcrText": False,
                "containsLedgerRows": False,
                "containsFilenames": False,
                "containsAmounts": False,
                "containsConfigurationValues": False,
                "containsCredentials": False,
            },
        }
    def create(self, *, actor: str, note: str = "") -> Dict[str, Any]:
        safe_actor = str(actor or "").strip()[:200]
        if not safe_actor:
            raise ValueError("actor is required")
        generated_at = _utc_timestamp()
        stamp = generated_at.replace("-", "").replace(":", "").replace("T", "-").replace("Z", "")
        output_dir = os.path.abspath(os.path.expanduser(os.path.expandvars(str(
            self.config.get("fab_support_bundle_dir")
            or self.config.get("operations_support_bundle_dir")
            or "output/support"
        ))))
        os.makedirs(output_dir, exist_ok=True)
        bundle_path = os.path.join(output_dir, f"fab-support-{stamp}.zip")
        doctor = self.doctor()
        audit_index = [
            {
                "id": event.get("id"),
                "action": event.get("action"),
                "entityType": event.get("entity_type"),
                "createdAt": event.get("created_at"),
            }
            for event in self.ledger.list_audit_events(limit=100)
        ]
        manifest = {
            "schemaVersion": SUPPORT_BUNDLE_SCHEMA_VERSION,
            "generatedAt": generated_at,
            "createdBy": safe_actor,
            "notePresent": bool(str(note or "").strip()),
            "files": ["manifest.json", "doctor.json", "audit-index.json", "README.txt"],
            "privacy": doctor["privacy"],
        }
        readme = (
            "FAB sanitized support bundle\n"
            "============================\n"
            "This archive contains diagnostic status and aggregate counters only.\n"
            "It intentionally excludes financial documents, OCR text, ledger rows, filenames, "
            "amounts, configuration values, and credentials.\n"
        )
        with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", _json_bytes(manifest))
            archive.writestr("doctor.json", _json_bytes(doctor))
            archive.writestr("audit-index.json", _json_bytes(audit_index))
            archive.writestr("README.txt", readme.encode("utf-8"))
        digest = _sha256(bundle_path)
        size_bytes = os.path.getsize(bundle_path)
        self.ledger.record_audit_event({
            "action": "support_bundle.created",
            "entityType": "support_bundle",
            "entityId": os.path.basename(bundle_path),
            "details": {
                "actor": safe_actor,
                "sha256": digest,
                "sizeBytes": size_bytes,
                "privacy": doctor["privacy"],
                "externalSubmission": "not_executed",
            },
        })
        return {
            "success": True,
            "status": "created",
            "bundleFilename": os.path.basename(bundle_path),
            "bundlePath": bundle_path,
            "sha256": digest,
            "sizeBytes": size_bytes,
            "privacy": doctor["privacy"],
            "externalSubmission": "not_executed",
        }


def _bounded_positive_int(value: Any, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True).encode("utf-8")


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
