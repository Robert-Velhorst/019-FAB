import base64
import binascii
from collections import OrderedDict
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import stat
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlsplit

from flask import Flask, Response, g, jsonify, redirect, render_template_string, request, session, url_for
from werkzeug.exceptions import BadRequest, HTTPException
from werkzeug.utils import secure_filename

from src.config_loader import ConfigLoader
from src.security.deployment_secrets import MAX_API_TOKEN_BYTES
from src.operations.deployment_preflight import require_deployment_ready
from src.operations.parent_operator_session import parent_session_active, requires_parent_session
from src.data_entry.waveapps_account_discovery import WaveappsAccountDiscoveryService
from src.data_entry.waveapps_entity_sync import WaveappsEntitySyncService
from src.document_handling.duplicate_detector import DuplicateDetector
from src.document_processors.document_type_classifier import is_non_posting_document_type
from src.operations.local_autonomy import LocalAutonomousService
from src.operations.local_autonomy_control import LocalAutonomyControlService
from src.operations.local_backup import (
    BACKUP_FORMAT_V2,
    FULL_RESTORE_CONFIRMATION_PHRASE,
    RESTORE_CONFIRMATION_PHRASE,
    RESTORE_MODE_FULL,
    RESTORE_MODE_LEDGER_ONLY,
    LocalBackupService,
)
from src.operations.local_bank_transactions import (
    BANK_STATEMENT_FILE_EXTENSIONS,
    MAX_BANK_STATEMENT_BYTES,
    SUPPORTED_BANK_STATEMENT_FORMATS,
    LocalBankTransactionImportService,
)
from src.operations.local_bookkeeping_records import (
    BOOKKEEPING_RECORD_RESOLUTION_STATUSES,
    LocalBookkeepingRecordService,
)
from src.operations.local_close_readiness import LocalCloseReadinessService
from src.operations.local_close_pack import LocalClosePackService
from src.operations.local_cloud_access import LocalCloudAccessService
from src.operations.local_categories import fab_category_intents, fab_category_options
from src.operations.local_category_suggestions import suggest_category_intent
from src.operations.local_compliance import LocalComplianceService, OPEN_FINDING_STATUSES
from src.operations.local_connector_intake import LocalConnectorIntakeService
from src.operations.drive_relay_intake import (
    MAX_DRIVE_RELAY_MAX_BYTES,
    DriveRelayIntakeService,
)
from src.operations.drive_wave_delivery import (
    DriveWaveDeliveryService,
    WAVE_RECEIPT_MAX_BYTES,
)
from src.operations.local_exceptions import LocalExceptionQueueService
from src.operations.local_exports import (
    EXPORT_APPROVAL_PHRASE,
    EXPORT_REJECTION_PHRASE,
    EXPORT_RESULT_CONFIRMATION_PHRASE,
    LocalExportAttemptService,
)
from src.operations.local_health import LocalOperationsHealth
from src.operations.local_grouping import LocalDocumentGroupingService
from src.operations.local_google_drive_auth import LocalGoogleDriveAuthorizationCoordinator
from src.operations.local_gmail_auth import LocalGmailAuthorizationCoordinator
from src.operations.local_hai_connector import LocalHaiConnector, MAX_HAI_PAYLOAD_BYTES
from src.operations.local_intake import DEFAULT_ALLOWED_EXTENSIONS, LocalFolderIntake
from src.operations.local_ledger import (
    VENDOR_CATEGORY_RULE_STATUSES,
    LocalOperationsLedger,
    default_ledger_path,
)
from src.operations.local_master_ledger import LocalMasterLedgerService
from src.operations.local_mijngeldzaken_control import LocalMijngeldzakenControlService
from src.operations.local_notifications import ACTIVE_NOTIFICATION_STATUSES, LocalNotificationService
from src.operations.local_photos_picker import LocalGooglePhotosPickerService
from src.operations.local_processing import LocalDocumentProcessor
from src.operations.local_readiness import LocalReadinessService
from src.operations.local_support_bundle import LocalSupportBundleService
from src.operations.local_reconciliation import LocalReconciliationService, MAX_RECONCILIATION_JSON_BYTES
from src.operations.local_reporting import LocalFinancialReportingService, LocalScheduledReportService
from src.operations.local_review import LocalReviewService
from src.operations.local_routing import LocalRoutingService
from src.operations.local_wave_control import LocalWaveControlService, MAX_WAVE_REPORT_RESULT_BYTES
from src.operations.local_wave_receipt_executor import LocalWaveReceiptExecutorService
from src.operations.local_wave_setup import LocalWaveSetupService
from src.operations.local_workflow_recovery import (
    LocalWorkflowRecoveryScheduler,
    LocalWorkflowRecoveryService,
)
from src.utils.runtime_identity import local_instance_id


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_LOCAL_UPLOAD_MAX_BYTES = 6 * 1024 * 1024
DEFAULT_LOCAL_API_MAX_REQUEST_BYTES = 101 * 1024 * 1024
MAX_RECONCILIATION_REQUEST_BYTES = MAX_RECONCILIATION_JSON_BYTES + 1024 * 1024
MAX_LOCAL_API_MAX_REQUEST_BYTES = 128 * 1024 * 1024
DEFAULT_LOCAL_JSON_MUTATION_REQUEST_BYTES = 2 * 1024 * 1024
LOCAL_JSON_REQUEST_OVERHEAD_BYTES = 128 * 1024
LOCAL_MULTIPART_REQUEST_OVERHEAD_BYTES = 1024 * 1024
MAX_LOCAL_INTAKE_REQUEST_BYTES = (
    4 * ((DEFAULT_LOCAL_UPLOAD_MAX_BYTES + 2) // 3)
    + LOCAL_JSON_REQUEST_OVERHEAD_BYTES
)
MAX_LOCAL_BANK_IMPORT_REQUEST_BYTES = (
    4 * ((MAX_BANK_STATEMENT_BYTES + 2) // 3)
    + LOCAL_JSON_REQUEST_OVERHEAD_BYTES
)
MAX_LOCAL_OAUTH_CREDENTIAL_REQUEST_BYTES = 90_000 + 16 * 1024
MAX_LOCAL_WAVE_READBACK_REQUEST_BYTES = (
    WAVE_RECEIPT_MAX_BYTES + LOCAL_MULTIPART_REQUEST_OVERHEAD_BYTES
)
MAX_LOCAL_WAVE_REPORT_RESULT_REQUEST_BYTES = (
    MAX_WAVE_REPORT_RESULT_BYTES + LOCAL_JSON_REQUEST_OVERHEAD_BYTES
)
MAX_LOCAL_DRIVE_RELAY_REQUEST_BYTES = (
    MAX_DRIVE_RELAY_MAX_BYTES + LOCAL_MULTIPART_REQUEST_OVERHEAD_BYTES
)
LOCAL_API_ROUTE_BODY_LIMITS = {
    "upload_intake_document": MAX_LOCAL_INTAKE_REQUEST_BYTES,
    "import_bank_transactions": MAX_LOCAL_BANK_IMPORT_REQUEST_BYTES,
    "import_bank_transactions_form": MAX_LOCAL_BANK_IMPORT_REQUEST_BYTES,
    "install_gmail_credentials_api": MAX_LOCAL_OAUTH_CREDENTIAL_REQUEST_BYTES,
    "install_google_drive_credentials_api": MAX_LOCAL_OAUTH_CREDENTIAL_REQUEST_BYTES,
    "drive_wave_attachment_readback_api": MAX_LOCAL_WAVE_READBACK_REQUEST_BYTES,
    "record_wave_report_result": MAX_LOCAL_WAVE_REPORT_RESULT_REQUEST_BYTES,
    "google_drive_relay_intake_api": MAX_LOCAL_DRIVE_RELAY_REQUEST_BYTES,
    "run_autonomy": MAX_RECONCILIATION_REQUEST_BYTES,
    "run_reconciliation": MAX_RECONCILIATION_REQUEST_BYTES,
    "run_reconciliation_form": MAX_RECONCILIATION_REQUEST_BYTES,
    "hai_command_plan_api": MAX_HAI_PAYLOAD_BYTES + 1024 * 1024,
    "hai_command_execute_api": MAX_HAI_PAYLOAD_BYTES + 1024 * 1024,
}
LOCAL_SOURCE_PREVIEW_MAX_BYTES = 25 * 1024 * 1024
LOCAL_SOURCE_PREVIEW_MIME_TYPES = {
    "application/json",
    "application/pdf",
    "application/xml",
    "image/bmp",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/webp",
    "text/csv",
    "text/plain",
    "text/xml",
}
LOCAL_FORM_SESSION_KEY = "fab_local_form_session"
REVIEW_RESOLUTION_STATUSES = {"approved", "rejected", "resolved", "ignored"}
RECONCILIATION_RESOLUTION_STATUSES = {"approved", "reconciled", "rejected", "resolved", "ignored", "needs_review"}
RULE_RESOLUTION_STATUSES = VENDOR_CATEGORY_RULE_STATUSES - {"learned"}
API_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,64}$")
HAI_ROUTE_RULES = (
    ("GET", re.compile(r"^/api/hai/(?:manifest|status)$")),
    ("POST", re.compile(r"^/api/hai/commands/(?:plan|execute)$")),
    ("GET", re.compile(r"^/api/backups$")),
    ("POST", re.compile(r"^/api/connectors/google-drive/relay$")),
    ("GET", re.compile(r"^/api/drive-wave/work-orders$")),
    ("POST", re.compile(r"^/api/drive-wave/documents/[1-9][0-9]*/attachment-readback$")),
    ("GET", re.compile(r"^/api/wave/receipt-executor/status$")),
    ("POST", re.compile(r"^/api/wave/receipt-executor/(?:session|claim|release)$")),
)
CONTROL_CENTER_BATCH_PATHS = {
    "autonomy": "/api/autonomy/plan?limit=25",
    "reviewQueue": "/api/review?status=open&limit=25&offset=0&view=summary&includeCategoryOptions=false",
    "exceptions": "/api/exceptions?limit=25&includeEntities=true",
    "health": "/api/health",
    "closeReadiness": "/api/close-readiness",
    "driveWaveStatus": "/api/drive-wave/status",
    "driveWaveWorkOrders": "/api/drive-wave/work-orders?limit=200&itemsLimit=25&view=summary",
    "reportRuns": "/api/report-runs?limit=5",
    "compliance": "/api/compliance/assessments?limit=5",
    "recovery": "/api/workflows/recovery?limit=10",
    "liveness": "/api/live",
    "waveSetup": "/api/wave/setup",
    "workflows": "/api/workflows?limit=10",
    "masterLedger": "/api/master-ledger?limit=250&summaryOnly=true",
    "metrics": "/api/dashboard",
    "notifications": "/api/notifications?limit=10",
    "settings": "/api/settings",
    "bankTransactions": "/api/bank-transactions?limit=250",
    "reconciliation": "/api/reconciliation?limit=10",
    "activity": "/api/audit?limit=12",
    "sources": "/api/sources?limit=50",
    "waveReceiptExecutor": "/api/wave/receipt-executor/status",
    "sourceReadiness": "/api/sources/readiness",
    "driveAuthorization": "/api/connectors/google-drive/authorization",
    "haiStatus": "/api/hai/status",
    "haiManifest": "/api/hai/manifest",
    "gmailAuthorization": "/api/connectors/gmail/authorization",
}


def _hai_request_allowed(method: str, path: str) -> bool:
    normalized_method = str(method or "").upper()
    normalized_path = str(path or "")
    return any(
        normalized_method == allowed_method and pattern.fullmatch(normalized_path)
        for allowed_method, pattern in HAI_ROUTE_RULES
    )


def _hai_backup_status_projection(payload: Dict[str, Any]) -> Dict[str, Any]:
    backups = payload.get("backups") if isinstance(payload, dict) else []
    schedule = payload.get("schedule") if isinstance(payload, dict) else {}
    restore_policy = payload.get("restorePolicy") if isinstance(payload, dict) else {}
    backup_fields = (
        "status",
        "createdAt",
        "ledgerBytes",
        "sizeBytes",
        "format",
        "sourceEvidenceStatus",
        "sourceEvidenceDocuments",
        "sourceEvidenceFiles",
        "sourceEvidenceBytes",
        "sourceEvidenceGaps",
    )
    schedule_fields = (
        "status",
        "due",
        "intervalHours",
        "requireCompleteSourceEvidence",
        "lastSuccessfulAt",
        "nextDueAt",
        "invalidBackupCount",
        "reason",
        "sourceEvidenceStatus",
        "sourceEvidenceDocuments",
        "sourceEvidenceFiles",
        "sourceEvidenceBytes",
        "sourceEvidenceGaps",
        "integrityVerification",
    )
    policy_fields = (
        "status",
        "maintenanceMode",
        "ledgerRestoreSupported",
        "sourceEvidenceRestoreSupported",
        "workerMustBeStopped",
        "externalSubmission",
    )
    safe_backups = [
        {key: backup[key] for key in backup_fields if key in backup}
        for backup in backups or []
        if isinstance(backup, dict)
    ]
    return {
        "backups": safe_backups,
        "backupCount": len(safe_backups),
        "schedule": {
            key: schedule[key] for key in schedule_fields
            if isinstance(schedule, dict) and key in schedule
        },
        "restorePolicy": {
            key: restore_policy[key] for key in policy_fields
            if isinstance(restore_policy, dict) and key in restore_policy
        },
        "verificationMode": "manifest_only",
    }


OPERATOR_SESSION_AUDIENCE = "fab-local-operator-session"
OPERATOR_SESSION_MAX_TTL_SECONDS = 60
OPERATOR_SESSION_MAX_TICKET_LENGTH = 8_192
OPERATOR_SESSION_NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,96}$")
OPERATOR_SESSION_ACTOR_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _normalize_operator_session_target(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value or len(value) > 2_048:
        return None
    if not value.startswith("/") or value.startswith("//"):
        return None
    if "\\" in value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None

    decoded = value
    try:
        for _ in range(2):
            next_decoded = unquote(decoded)
            if next_decoded == decoded:
                break
            decoded = next_decoded
    except (TypeError, ValueError):
        return None
    if not decoded.startswith("/") or decoded.startswith("//") or "\\" in decoded:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in decoded):
        return None

    parsed = urlsplit(value)
    decoded_parsed = urlsplit(decoded)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return None
    decoded_path = decoded_parsed.path.lower()
    if decoded_path == "/operator/session/bootstrap" or decoded_path.startswith(
        "/operator/session/bootstrap/"
    ):
        return None
    return value


def _decode_operator_session_part(value: str) -> bytes:
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Invalid base64url value")
    decoded = base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if not hmac.compare_digest(canonical, value):
        raise ValueError("Non-canonical base64url value")
    return decoded


def _api_error_code(status_code: int) -> str:
    return {
        400: "invalid_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        413: "payload_too_large",
        421: "misdirected_request",
        429: "rate_limited",
        502: "upstream_error",
        503: "service_unavailable",
    }.get(status_code, "internal_error" if status_code >= 500 else "request_failed")


def _file_matches_content(path: str, content: bytes) -> bool:
    """Compare a bounded upload with an existing regular file without following links."""
    try:
        before = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_size != len(content):
            return False
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_size != len(content)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                return False
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return hmac.compare_digest(digest.digest(), hashlib.sha256(content).digest())
    except OSError:
        return False


DASHBOARD_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FAB Operations</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f8fb;
      --panel: #ffffff;
      --panel-soft: #f0f5f8;
      --text: #15202b;
      --muted: #5d6b78;
      --line: #d9e2ea;
      --accent: #0f766e;
      --accent-dark: #115e59;
      --danger: #b42318;
      --warning: #a15c07;
      --ok: #166534;
      --shadow: 0 14px 36px rgba(21, 32, 43, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
      line-height: 1.45;
    }
    a { color: var(--accent-dark); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .shell { max-width: 1320px; margin: 0 auto; padding: 24px; }
    header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 24px;
      padding: 10px 0 22px;
    }
    h1 { margin: 0; font-size: 30px; line-height: 1.1; letter-spacing: 0; }
    h2 { margin: 0 0 14px; font-size: 18px; line-height: 1.2; letter-spacing: 0; }
    h3 { margin: 0 0 8px; font-size: 15px; line-height: 1.2; letter-spacing: 0; }
    .subtitle { margin: 8px 0 0; color: var(--muted); max-width: 720px; }
    .top-status {
      display: grid;
      gap: 8px;
      min-width: 260px;
      padding: 12px 14px;
      background: var(--panel);
      border: 1px solid var(--line);
      box-shadow: var(--shadow);
    }
    .status-row { display: flex; justify-content: space-between; gap: 14px; color: var(--muted); }
    .status-row strong { color: var(--text); font-weight: 650; text-align: right; }
    nav {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 0 0 18px;
    }
    nav a {
      display: inline-flex;
      align-items: center;
      min-height: 34px;
      padding: 7px 11px;
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--text);
      font-size: 13px;
      font-weight: 650;
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .metric {
      background: var(--panel);
      border: 1px solid var(--line);
      padding: 16px;
      box-shadow: var(--shadow);
      min-height: 96px;
    }
    .metric span { display: block; color: var(--muted); font-size: 12px; font-weight: 650; text-transform: uppercase; }
    .metric strong { display: block; margin-top: 8px; font-size: 30px; line-height: 1; }
    section {
      margin: 18px 0;
      background: var(--panel);
      border: 1px solid var(--line);
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .section-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 14px;
      padding: 16px 18px;
      border-bottom: 1px solid var(--line);
      background: #fbfcfd;
    }
    .section-head p { margin: 4px 0 0; color: var(--muted); }
    .table-wrap { max-width: 100%; overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; min-width: 880px; }
    th, td {
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      background: var(--panel-soft);
      white-space: nowrap;
    }
    tbody tr:hover { background: #f9fbfc; }
    .muted { color: var(--muted); }
    .notice { margin: 14px 0; border: 1px solid var(--line); padding: 12px 14px; background: #fff; }
    .notice.error { border-color: #f1aaa4; background: #fff5f4; color: #7a271a; }
    .notice p { margin: 5px 0 10px; }
    .mono { font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; font-size: 12px; overflow-wrap: anywhere; }
    .badge {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 3px 8px;
      border: 1px solid var(--line);
      background: #edf7f4;
      color: var(--accent-dark);
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }
    .badge.failed, .badge.error, .badge.rejected { background: #fff1f0; color: var(--danger); }
    .badge.blocked, .badge.blocked_routing, .badge.blocked_by_review, .badge.blocked_invalid_plan, .badge.blocked_unapproved, .badge.high { background: #fff1f0; color: var(--danger); }
    .badge.pending, .badge.review, .badge.needs_review, .badge.in_review, .badge.candidate, .badge.suggested, .badge.missing_receipt, .badge.unmatched_document { background: #fff7e6; color: var(--warning); }
    .badge.attention, .badge.medium, .badge.low { background: #fff7e6; color: var(--warning); }
    .badge.needs_attention, .badge.needs_auth { background: #fff7e6; color: var(--warning); }
    .badge.safe_auto, .badge.safe_draft, .badge.read_only { background: #ecfdf3; color: var(--ok); }
    .badge.review_required, .badge.approval_required, .badge.attention_required, .badge.completed_with_errors, .badge.deferred, .badge.awaiting_approval, .badge.approved_not_submitted, .badge.approved_not_executed, .badge.supervision_required, .badge.needs_configuration, .badge.partial, .badge.syncing, .badge.running { background: #fff7e6; color: var(--warning); }
    .badge.completed, .badge.approved, .badge.executed, .badge.submitted, .badge.resolved, .badge.validated, .badge.routed, .badge.reconciled, .badge.ok { background: #ecfdf3; color: var(--ok); }
    .badge.ready { background: #ecfdf3; color: var(--ok); }
    .badge.not_configured, .badge.disabled, .badge.skipped, .badge.not_run { background: #f1f5f9; color: var(--muted); }
    .review-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 12px;
      padding: 14px;
    }
    .review-item {
      border: 1px solid var(--line);
      background: #fff;
      padding: 14px;
      min-height: 170px;
    }
    .review-item p { margin: 7px 0; color: var(--muted); }
    form.review-actions {
      display: grid;
      grid-template-columns: 1fr;
      gap: 8px;
      margin-top: 12px;
    }
    .correction-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
      gap: 8px;
    }
    .button-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .inline-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: flex-end;
      align-items: center;
    }
    form.table-actions {
      display: inline-flex;
      margin: 0;
    }
    .button-link {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 30px;
      border: 1px solid var(--accent-dark);
      padding: 4px 8px;
      background: #fff;
      color: var(--accent-dark);
      font-size: 12px;
      font-weight: 750;
      line-height: 1.2;
    }
    .button-link:hover { background: var(--panel-soft); text-decoration: none; }
    button.danger { background: var(--danger); }
    .export-table { min-width: 0; table-layout: fixed; }
    .export-table td { min-width: 0; overflow-wrap: anywhere; }
    .export-actions > .button-link { margin-bottom: 8px; }
    .export-actions form.table-actions {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 6px;
      width: 100%;
      margin: 0 0 7px;
    }
    .export-actions form.table-actions input { min-width: 0; }
    input[type="text"], textarea, select {
      width: 100%;
      min-height: 36px;
      border: 1px solid var(--line);
      padding: 7px 9px;
      font: inherit;
      background: #fff;
      color: var(--text);
    }
    textarea { min-height: 82px; resize: vertical; }
    button {
      min-height: 36px;
      border: 1px solid var(--accent-dark);
      background: var(--accent);
      color: #fff;
      padding: 7px 11px;
      font: inherit;
      font-size: 13px;
      font-weight: 750;
      cursor: pointer;
    }
    button.secondary {
      background: #fff;
      color: var(--accent-dark);
    }
    button.compact {
      min-height: 30px;
      padding: 4px 8px;
      font-size: 12px;
    }
    button:disabled {
      cursor: not-allowed;
      opacity: 0.55;
    }
    .summary-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 10px;
      padding: 14px 18px 0;
    }
    .summary-item {
      border: 1px solid var(--line);
      background: #fff;
      padding: 10px;
    }
    .summary-item span { display: block; color: var(--muted); font-size: 12px; font-weight: 700; text-transform: uppercase; }
    .summary-item strong { display: block; margin-top: 5px; font-size: 20px; }
    details {
      padding: 13px 14px;
      border-top: 1px solid var(--line);
      background: #fbfcfd;
    }
    summary { cursor: pointer; font-weight: 700; }
    pre {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      background: #0f172a;
      color: #e2e8f0;
      padding: 12px;
      margin: 10px 0 0;
      max-height: 260px;
      overflow: auto;
      font-size: 12px;
    }
    .empty { padding: 22px 18px; color: var(--muted); }
    .settings-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 12px;
      padding: 14px 18px 18px;
    }
    .setting {
      border: 1px solid var(--line);
      padding: 12px;
      background: #fff;
    }
    .setting span { display: block; color: var(--muted); font-size: 12px; text-transform: uppercase; font-weight: 700; }
    .setting strong { display: block; margin-top: 6px; overflow-wrap: anywhere; }
    @media (max-width: 900px) {
      .shell { padding: 16px; }
      header { display: block; }
      .top-status { margin-top: 16px; min-width: 0; }
      .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      form.review-actions { grid-template-columns: 1fr; }
      .inline-actions { justify-content: flex-start; }
    }
    @media (max-width: 1200px) {
      #exports .table-wrap { overflow: visible; padding: 12px; }
      .export-table, .export-table tbody { display: block; min-width: 0; }
      .export-table thead {
        position: absolute;
        width: 1px;
        height: 1px;
        padding: 0;
        margin: -1px;
        overflow: hidden;
        clip: rect(0, 0, 0, 0);
        white-space: nowrap;
        border: 0;
      }
      .export-table tbody { display: grid; gap: 12px; }
      .export-table tr {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        border: 1px solid var(--line);
        background: #fff;
      }
      .export-table td {
        display: grid;
        grid-template-columns: minmax(88px, 0.45fr) minmax(0, 1fr);
        gap: 10px;
        padding: 10px 12px;
      }
      .export-table td::before {
        content: attr(data-label);
        color: var(--muted);
        font-size: 11px;
        font-weight: 700;
        text-transform: uppercase;
      }
      .export-table .export-message,
      .export-table .export-actions { grid-column: 1 / -1; }
      .export-table .export-actions { display: block; background: #fbfcfd; }
      .export-table .export-actions::before { display: block; margin-bottom: 8px; }
      .export-actions form.table-actions { max-width: 520px; }
    }
    @media (max-width: 520px) {
      .metrics { grid-template-columns: 1fr; }
      h1 { font-size: 25px; }
      .section-head { display: block; }
      .export-table tr { grid-template-columns: 1fr; }
      .export-table td,
      .export-table .export-message,
      .export-table .export-actions { grid-column: 1; }
      .export-actions form.table-actions { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <div>
        <h1>FAB Operations</h1>
        <p class="subtitle">Local bookkeeping ledger, review queue, and audit trail for the autonomous workflow.</p>
      </div>
      <div class="top-status">
        <div class="status-row"><span>API</span><strong>{{ health.status }}</strong></div>
        <div class="status-row"><span>Auth</span><strong>{{ "required" if health.auth_required else "local" }}</strong></div>
        <div class="status-row"><span>Ledger</span><strong class="mono">{{ ledger_name }}</strong></div>
      </div>
    </header>

    <nav aria-label="FAB operations sections">
      <a href="#overview">Overview</a>
      <a href="#health">Health</a>
      <a href="#notifications">Notifications</a>
      <a href="#close">Close</a>
      <a href="#sources">Sources</a>
      <a href="#autonomy">Autonomy</a>
      <a href="#workflows">Runs</a>
      <a href="#intake">Intake</a>
      <a href="#ledger">Ledger</a>
      <a href="#groups">Groups</a>
      <a href="#duplicates">Duplicates</a>
      <a href="#records">Records</a>
      <a href="#master-ledger">Master Ledger</a>
      <a href="#reports">Reports</a>
      <a href="#compliance">Compliance</a>
      <a href="#fields">Fields</a>
      <a href="#routing">Routing</a>
      <a href="#exports">Exports</a>
      <a href="#mijngeldzaken">MijnGeldzaken</a>
      <a href="#wave">Wave</a>
      <a href="#bank">Bank</a>
      <a href="#reconciliation">Reconciliation</a>
      <a href="#review">Manual Review</a>
      <a href="#rules">Rules</a>
      <a href="#audit">Audit</a>
      <a href="#backups">Backups</a>
      <a href="#settings">Settings</a>
    </nav>

    <div id="overview" class="metrics">
      {% for metric in metric_cards %}
      <div class="metric">
        <span>{{ metric.label }}</span>
        <strong>{{ metric.value }}</strong>
      </div>
      {% endfor %}
    </div>

    <section id="health">
      <div class="section-head">
        <div>
          <h2>Operations Health</h2>
          <p>Current queue freshness, routing blocks, failed records, and workflow run state.</p>
        </div>
        <span class="badge {{ operations_health.status }}">{{ operations_health.status }}</span>
      </div>
      <div class="summary-grid">
        <div class="summary-item"><span>Open reviews</span><strong>{{ operations_health.metrics.openReviewItems }}</strong></div>
        <div class="summary-item"><span>Stale reviews</span><strong>{{ operations_health.metrics.staleReviewItems }}</strong></div>
        <div class="summary-item"><span>Stuck docs</span><strong>{{ operations_health.metrics.stuckDocuments }}</strong></div>
        <div class="summary-item"><span>Routing blocks</span><strong>{{ operations_health.metrics.routingBlocks }}</strong></div>
        <div class="summary-item"><span>Drafts waiting</span><strong>{{ operations_health.metrics.pendingRoutingDrafts }}</strong></div>
        <div class="summary-item"><span>Failed docs</span><strong>{{ operations_health.metrics.failedDocuments }}</strong></div>
      </div>
      {% if operations_health.issues %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Severity</th>
              <th>Issue</th>
              <th>Entity</th>
              <th>Age</th>
              <th>Message</th>
            </tr>
          </thead>
          <tbody>
          {% for issue in operations_health.issues[:10] %}
            <tr>
              <td><span class="badge {{ issue.severity }}">{{ issue.severity }}</span></td>
              <td>{{ issue.type }}</td>
              <td>{{ issue.entityType }} {{ issue.entityId or "" }}</td>
              <td>{{ issue.ageHours if issue.ageHours is not none else "-" }}h</td>
              <td>{{ issue.message }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      <details>
        <summary>Recommended next actions</summary>
        <pre>{{ pretty_json(operations_health.nextActions) }}</pre>
      </details>
      {% else %}
      <div class="empty">No operational health issues detected.</div>
      {% endif %}
    </section>

    <section id="notifications">
      <div class="section-head">
        <div>
          <h2>Notification Center</h2>
          <p>Preference-controlled local alerts derived from current operating evidence.</p>
        </div>
        <form class="inline-actions" method="post" action="{{ url_for('refresh_notifications_form') }}">
          <button type="submit">Refresh alerts</button>
        </form>
      </div>
      <div class="summary-grid">
        <div class="summary-item"><span>Active</span><strong>{{ notification_summary.active }}</strong></div>
        <div class="summary-item"><span>Unread</span><strong>{{ notification_summary.unread }}</strong></div>
        <div class="summary-item"><span>High</span><strong>{{ notification_summary.high }}</strong></div>
        <div class="summary-item"><span>Medium</span><strong>{{ notification_summary.medium }}</strong></div>
        <div class="summary-item"><span>Acknowledged</span><strong>{{ notification_summary.acknowledged }}</strong></div>
        <div class="summary-item"><span>External delivery</span><strong>{{ notification_summary.externalDelivery }}</strong></div>
      </div>
      {% if notification_refresh_summary %}
      <details open>
        <summary>Last notification refresh</summary>
        <pre>{{ pretty_json(notification_refresh_summary) }}</pre>
      </details>
      {% endif %}
      {% if notifications %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Severity</th>
              <th>Alert</th>
              <th>Entity</th>
              <th>Status</th>
              <th>Last seen</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
          {% for notification in notifications %}
            <tr>
              <td><span class="badge {{ notification.severity }}">{{ notification.severity }}</span></td>
              <td>
                <strong>{{ notification.title }}</strong>
                <div class="muted">{{ notification.message }}</div>
                <a href="{{ notification.payload.dashboardPath or '#health' }}">Open evidence</a>
              </td>
              <td>{{ notification.entity_type or "-" }} {{ notification.entity_id or "" }}</td>
              <td><span class="badge {{ notification.status }}">{{ notification.status }}</span></td>
              <td class="mono">{{ notification.last_seen_at }}</td>
              <td>
                <div class="button-row">
                  {% if notification.status == 'unread' %}
                  <form class="table-actions" method="post" action="{{ url_for('notification_status_form', notification_id=notification.id) }}">
                    <input type="hidden" name="status" value="read">
                    <button class="compact secondary" type="submit">Mark read</button>
                  </form>
                  {% endif %}
                  {% if notification.status != 'acknowledged' %}
                  <form class="table-actions" method="post" action="{{ url_for('notification_status_form', notification_id=notification.id) }}">
                    <input type="hidden" name="status" value="acknowledged">
                    <button class="compact secondary" type="submit">Acknowledge</button>
                  </form>
                  {% endif %}
                  <form class="table-actions" method="post" action="{{ url_for('notification_status_form', notification_id=notification.id) }}">
                    <input type="hidden" name="status" value="resolved">
                    <button class="compact secondary" type="submit">Resolve</button>
                  </form>
                </div>
              </td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No active local notifications. Refresh alerts to compare the inbox with current operating health.</div>
      {% endif %}
      <details>
        <summary>Notification preferences</summary>
        <form method="post" action="{{ url_for('notification_preference_form') }}">
          <div class="form-grid">
            <label>Event type
              <input name="eventType" value="*" required>
            </label>
            <label>Minimum severity
              <select name="minimumSeverity">
                <option value="low">Low</option>
                <option value="medium">Medium</option>
                <option value="high">High</option>
              </select>
            </label>
            <label><input type="checkbox" name="enabled" checked> Enabled</label>
            <label><input type="checkbox" name="inAppEnabled" checked> In-app inbox</label>
          </div>
          <button type="submit">Save preference</button>
        </form>
        <pre>{{ pretty_json(notification_preferences) }}</pre>
      </details>
    </section>

    <section id="exceptions">
      <div class="section-head">
        <div>
          <h2>Exception Queue</h2>
          <p>Actionable failed, stale, blocked, and review-required operating items with safe next steps.</p>
        </div>
        <span class="badge {{ exceptions.status }}">{{ exceptions.status }}</span>
      </div>
      <div class="summary-grid">
        <div class="summary-item"><span>Total</span><strong>{{ exceptions.summary.total }}</strong></div>
        <div class="summary-item"><span>High</span><strong>{{ exceptions.summary.bySeverity.high }}</strong></div>
        <div class="summary-item"><span>Medium</span><strong>{{ exceptions.summary.bySeverity.medium }}</strong></div>
        <div class="summary-item"><span>Low</span><strong>{{ exceptions.summary.bySeverity.low }}</strong></div>
      </div>
      {% if exceptions.exceptions %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Severity</th>
              <th>Type</th>
              <th>Entity</th>
              <th>Next action</th>
              <th>Safe controls</th>
            </tr>
          </thead>
          <tbody>
          {% for item in exceptions.exceptions[:12] %}
            <tr>
              <td><span class="badge {{ item.severity }}">{{ item.severity }}</span></td>
              <td>{{ item.type }}</td>
              <td>
                {{ item.entityType }} {{ item.entityId or "" }}
                {% if item.entity and item.entity.originalFilename %}
                <div class="muted">{{ item.entity.originalFilename }}</div>
                {% endif %}
              </td>
              <td>{{ item.nextAction }}</td>
              <td>
                {% if item.actions %}
                  {% for action in item.actions %}
                  {% if action.method == "GET" %}
                  <a class="button-link secondary" href="{{ action.dashboardPath or action.path }}">{{ action.id }}</a>
                  {% elif action.safety == "safe_auto" and action.dashboardPath %}
                  <form class="table-actions" method="post" action="{{ action.dashboardPath }}">
                    <button class="compact secondary" type="submit">{{ action.id }}</button>
                  </form>
                  {% else %}
                  <span class="badge {{ action.safety }}">{{ action.id }}</span>
                  {% endif %}
                  {% endfor %}
                {% else %}
                  <span class="muted">Inspect manually</span>
                {% endif %}
              </td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      <details>
        <summary>Raw exception queue</summary>
        <pre>{{ pretty_json(exceptions) }}</pre>
      </details>
      {% else %}
      <div class="empty">No operating exceptions are currently queued.</div>
      {% endif %}
    </section>

    <section id="close">
      <div class="section-head">
        <div>
          <h2>Close Readiness</h2>
          <p>Combines Wave report evidence, reconciliation, review, routing, and export gates into one close decision.</p>
        </div>
        <div class="inline-actions">
          <span class="badge {{ close_readiness.status }}">{{ close_readiness.status }}</span>
          <form class="table-actions" method="post" action="{{ url_for('prepare_close_pack_form') }}">
            <button type="submit" {% if not close_readiness.canClose %}disabled{% endif %}>Prepare pack</button>
          </form>
        </div>
      </div>
      <div class="summary-grid">
        <div class="summary-item"><span>Can close</span><strong>{{ "yes" if close_readiness.canClose else "no" }}</strong></div>
        <div class="summary-item"><span>Blocking gates</span><strong>{{ close_readiness.blockingCount }}</strong></div>
        <div class="summary-item"><span>Attention gates</span><strong>{{ close_readiness.attentionCount }}</strong></div>
        <div class="summary-item"><span>Report controls</span><strong>{{ close_readiness.reportControls.status }}</strong></div>
        <div class="summary-item"><span>Unreconciled bank</span><strong>{{ close_readiness.metrics.unreconciledBankTransactions }}</strong></div>
        <div class="summary-item"><span>Manual reviews</span><strong>{{ close_readiness.metrics.pendingReview }}</strong></div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Gate</th>
              <th>Status</th>
              <th>Message</th>
              <th>Evidence</th>
            </tr>
          </thead>
          <tbody>
          {% for gate in close_readiness.gates %}
            <tr>
              <td>{{ gate.label }}</td>
              <td><span class="badge {{ gate.status }}">{{ gate.status }}</span></td>
              <td>{{ gate.message }}</td>
              <td class="mono">{{ compact_json(gate.evidence) }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      <details>
        <summary>Close next actions</summary>
        <pre>{{ pretty_json(close_readiness.nextActions) }}</pre>
      </details>
      {% if close_pack_summary %}
      <details open>
        <summary>Last close pack action</summary>
        <pre>{{ pretty_json(close_pack_summary) }}</pre>
      </details>
      {% endif %}
      {% if close_packs.packs %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Pack</th>
              <th>Status</th>
              <th>Period</th>
              <th>Created</th>
              <th>SHA-256</th>
              <th>Size</th>
            </tr>
          </thead>
          <tbody>
          {% for pack in close_packs.packs %}
            <tr>
              <td class="mono">{{ pack.closePackFilename }}</td>
              <td><span class="badge {{ pack.status }}">{{ pack.status }}</span></td>
              <td>{{ pack.fromDate or "-" }} - {{ pack.toDate or "-" }}</td>
              <td class="mono">{{ pack.createdAt or "-" }}</td>
              <td class="mono">{{ pack.sha256[:12] if pack.sha256 else "-" }}</td>
              <td>{{ pack.sizeBytes or "-" }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No period close packs have been prepared yet.</div>
      {% endif %}
    </section>

    <section id="sources">
      <div class="section-head">
        <div>
          <h2>Sources</h2>
          <p>Configured and observed document sources with scan provenance, counts, and status.</p>
        </div>
        <form class="inline-actions" method="post" action="{{ url_for('sync_sources_form') }}">
          <button type="submit" {% if not connector_plan.canSync %}disabled{% endif %}>Sync configured sources</button>
        </form>
      </div>
      {% if connector_sync_summary %}
      <pre>{{ pretty_json(connector_sync_summary) }}</pre>
      {% endif %}
      <div class="table-wrap">
        <table>
          <thead><tr><th>Connector</th><th>Status</th><th>Mode</th><th>Configured</th><th>Next action</th></tr></thead>
          <tbody>
          {% for connector in connector_plan.sources %}
            <tr>
              <td>{{ connector.label }}</td>
              <td><span class="badge {{ connector.status }}">{{ connector.status }}</span></td>
              <td>{{ connector.mode }}</td>
              <td>{{ "yes" if connector.configured else "no" }}</td>
              <td>{{ connector.nextAction or "Ready for read-only sync." }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      <div class="section-head">
        <div>
          <h3>Google Photos selections</h3>
          <p>User-owned receipt selections waiting for import or cleanup.</p>
        </div>
        <form class="inline-actions" method="post" action="{{ url_for('start_google_photos_picker_form') }}">
          <button type="submit" {% if not photos_picker_plan.canStartSession %}disabled{% endif %}>Start selection</button>
        </form>
      </div>
      {% if photos_picker_action %}
      <pre>{{ pretty_json(photos_picker_action) }}</pre>
      {% endif %}
      {% if photos_picker_sessions %}
      <div class="table-wrap">
        <table>
          <thead><tr><th>Session</th><th>Status</th><th>Selected</th><th>Updated</th><th>Actions</th></tr></thead>
          <tbody>
          {% for picker_session in photos_picker_sessions %}
            <tr>
              <td class="mono">#{{ picker_session.id }}</td>
              <td><span class="badge {{ picker_session.status }}">{{ picker_session.status }}</span></td>
              <td>{{ picker_session.selectedItemCount }}</td>
              <td class="mono">{{ picker_session.updatedAt or "-" }}</td>
              <td>
                <div class="inline-actions">
                  {% if picker_session.pickerUri %}
                  <a href="{{ picker_session.pickerUri }}" target="_blank" rel="noopener noreferrer">Open selection</a>
                  {% endif %}
                  {% if picker_session.status in ["creating", "awaiting_user_selection", "collecting", "partial", "completed_cleanup_required", "failed"] %}
                  {% if picker_session.providerSessionId and not picker_session.providerSessionDeleted %}
                  <form method="post" action="{{ url_for('collect_google_photos_picker_form', workflow_run_id=picker_session.id) }}">
                    <button type="submit">Check &amp; import</button>
                  </form>
                  {% endif %}
                  <form method="post" action="{{ url_for('cancel_google_photos_picker_form', workflow_run_id=picker_session.id) }}">
                    <button type="submit" class="secondary">Cancel</button>
                  </form>
                  {% endif %}
                </div>
              </td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No Google Photos selection sessions recorded.</div>
      {% endif %}
      {% if sources %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Source</th>
              <th>Type</th>
              <th>Status</th>
              <th>Seen</th>
              <th>Imported</th>
              <th>Duplicates</th>
              <th>Last scan</th>
              <th>Identifier</th>
            </tr>
          </thead>
          <tbody>
          {% for source in sources %}
            <tr>
              <td>{{ source.label }}</td>
              <td>{{ source.source_type }}</td>
              <td><span class="badge {{ source.status }}">{{ source.status }}</span></td>
              <td>{{ source.documents_seen }}</td>
              <td>{{ source.documents_imported }}</td>
              <td>{{ source.duplicates_detected }}</td>
              <td class="mono">{{ source.last_scan_at or "-" }}</td>
              <td class="mono">{{ source.source_identifier }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      <details>
        <summary>Raw source registry</summary>
        <pre>{{ pretty_json(sources) }}</pre>
      </details>
      {% else %}
      <div class="empty">No document sources have been observed yet. Run a folder rescan or register a source through the API.</div>
      {% endif %}
    </section>

    <section id="autonomy">
      <div class="section-head">
        <div>
          <h2>Autonomous Cycle</h2>
          <p>Runs local, policy-gated bookkeeping work from the dashboard or recurring worker without overlapping cycles.</p>
        </div>
        <form class="inline-actions" method="post" action="{{ url_for('run_autonomy_form') }}">
          <button type="submit" {% if not autonomy_plan.canRunAutonomously or (autonomy_plan.runtimeLease and autonomy_plan.runtimeLease.active) %}disabled{% endif %}>Run safe cycle</button>
        </form>
      </div>
      {% if autonomy_control.active %}
      <div class="notice error">
        <strong>Automation emergency stop is engaged.</strong>
        <p>{{ autonomy_control.reason }} Updated by {{ autonomy_control.updatedBy }} at {{ autonomy_control.updatedAt }}.</p>
        <form class="table-actions" method="post" action="{{ url_for('clear_autonomy_emergency_stop_form') }}">
          <input type="text" name="reason" placeholder="Why automation may resume" required>
          <input type="text" name="confirmation" placeholder="{{ autonomy_control.resumeConfirmationPhrase }}" required>
          <button class="compact" type="submit" {% if autonomy_control.cycleActive %}disabled{% endif %}>Clear stop</button>
        </form>
      </div>
      {% else %}
      <form class="inline-actions" method="post" action="{{ url_for('engage_autonomy_emergency_stop_form') }}">
        <input type="text" name="reason" value="Operator stopped autonomous processing for inspection." required>
        <button class="compact danger" type="submit">Stop automation</button>
      </form>
      {% endif %}
      <div class="summary-grid">
        <div class="summary-item"><span>Status</span><strong>{{ autonomy_plan.status }}</strong></div>
        <div class="summary-item"><span>Runnable</span><strong>{{ autonomy_plan.runnableActionIds|length }}</strong></div>
        <div class="summary-item"><span>Manual gates</span><strong>{{ autonomy_plan.manualActionIds|length }}</strong></div>
        <div class="summary-item"><span>Cycle lease</span><strong>{{ "active" if autonomy_plan.runtimeLease and autonomy_plan.runtimeLease.active else "free" }}</strong></div>
        <div class="summary-item"><span>Exceptions</span><strong>{{ autonomy_plan.exceptions.total }}</strong></div>
        <div class="summary-item"><span>Imported</span><strong>{{ autonomy_plan.counts.importedDocuments }}</strong></div>
        <div class="summary-item"><span>Routable</span><strong>{{ autonomy_plan.counts.routableDocuments }}</strong></div>
        <div class="summary-item"><span>Bank tx</span><strong>{{ autonomy_plan.counts.unreconciledBankTransactions }}</strong></div>
        <div class="summary-item"><span>External submit</span><strong>{{ autonomy_plan.externalSubmission }}</strong></div>
      </div>
      <div class="empty">{{ autonomy_plan.nextAction }}</div>
      {% if autonomy_plan.exceptions.total %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Operating exception</th>
              <th>Severity</th>
              <th>Entity</th>
              <th>Next action</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
          {% for item in autonomy_plan.exceptions.topExceptions %}
            <tr>
              <td>
                <strong>{{ item.type }}</strong>
                <div class="muted">{{ item.message }}</div>
              </td>
              <td><span class="badge {{ item.severity }}">{{ item.severity }}</span></td>
              <td>{{ item.entityType }} #{{ item.entityId }}</td>
              <td>{{ item.nextAction }}</td>
              <td>
                <div class="button-row">
                {% for action in item.actions %}
                  <a class="button-link secondary" href="{{ action.path }}">{{ action.label }}</a>
                {% endfor %}
                {% if not item.actions %}-{% endif %}
                </div>
              </td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      <div class="empty"><a href="#exceptions">Open full exception queue</a></div>
      {% endif %}
      {% if autonomy_summary %}
      <details open>
        <summary>Last autonomous cycle</summary>
        <pre>{{ pretty_json(autonomy_summary) }}</pre>
      </details>
      {% endif %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Action</th>
              <th>Stage</th>
              <th>Mode</th>
              <th>Risk</th>
              <th>Can run</th>
              <th>Evidence</th>
              <th>Gate</th>
            </tr>
          </thead>
          <tbody>
          {% for action in autonomy_plan.actions %}
            <tr>
              <td>{{ action.label }}</td>
              <td>{{ action.stage }}</td>
              <td><span class="badge {{ action.mode }}">{{ action.mode }}</span></td>
              <td><span class="badge {{ action.risk }}">{{ action.risk }}</span></td>
              <td>{{ "yes" if action.canRun else "no" }}</td>
              <td class="mono">{{ compact_json(action.evidence) }}</td>
              <td>{{ action.blockedReason or "ready" }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      <details>
        <summary>Raw autonomous plan</summary>
        <pre>{{ pretty_json(autonomy_plan) }}</pre>
      </details>
    </section>

    <section id="workflows">
      <div class="section-head">
        <div><h2>Workflow Runs</h2></div>
        <a class="button-link secondary" href="{{ url_for('workflow_runs_api') }}">JSON</a>
      </div>
      <div class="summary-grid">
        <div class="summary-item"><span>Recovery policy</span><strong>{{ workflow_recovery_schedule.status }}</strong></div>
        <div class="summary-item"><span>Due now</span><strong>{{ workflow_recovery_schedule.dueCount }}</strong></div>
        <div class="summary-item"><span>Candidates</span><strong>{{ workflow_recovery_schedule.candidateCount }}</strong></div>
        <div class="summary-item"><span>Retry limit</span><strong>{{ workflow_recovery_schedule.policy.maxRetries }}</strong></div>
      </div>
      <div class="actions">
        <form class="inline-actions" method="post" action="{{ url_for('run_due_workflow_recovery_form') }}">
          <button class="secondary" type="submit" {% if not workflow_recovery_schedule.dueCount %}disabled{% endif %}>Run due safe recovery</button>
        </form>
        <a class="button-link secondary" href="{{ url_for('workflow_recovery_schedule_api') }}">Recovery queue JSON</a>
      </div>
      {% if workflow_recovery_schedule.statusCounts %}
      <div class="empty">Queue: {{ compact_json(workflow_recovery_schedule.statusCounts) }}</div>
      {% endif %}
      {% if workflow_recovery_summary %}
      <details open>
        <summary>Last workflow recovery</summary>
        <pre>{{ pretty_json(workflow_recovery_summary) }}</pre>
      </details>
      {% endif %}
      {% if workflow_runs %}
      {% for run in workflow_runs %}
      <details {% if run.status in ["failed", "error", "completed_with_errors", "running"] or run.step_summary.get("failed", 0) or run.step_summary.get("blocked", 0) %}open{% endif %}>
        <summary>
          <span class="mono">#{{ run.id }}</span>
          {{ run.trigger_source }}
          <span class="badge {{ run.status }}">{{ run.status }}</span>
        </summary>
        <div class="summary-grid">
          <div class="summary-item"><span>Started</span><strong class="mono">{{ run.started_at or "-" }}</strong></div>
          <div class="summary-item"><span>Finished</span><strong class="mono">{{ run.finished_at or "-" }}</strong></div>
          <div class="summary-item"><span>Steps</span><strong>{{ run.step_count }}</strong></div>
          <div class="summary-item"><span>Completed</span><strong>{{ run.step_summary.get("completed", 0) }}</strong></div>
          <div class="summary-item"><span>Skipped</span><strong>{{ run.step_summary.get("skipped", 0) }}</strong></div>
          <div class="summary-item"><span>Failed</span><strong>{{ run.step_summary.get("failed", 0) }}</strong></div>
          <div class="summary-item"><span>Blocked</span><strong>{{ run.step_summary.get("blocked", 0) }}</strong></div>
          <div class="summary-item"><span>Not run</span><strong>{{ run.step_summary.get("not_run", 0) }}</strong></div>
        </div>
        {% if run.error_message %}<div class="empty">{{ run.error_message }}</div>{% endif %}
        {% if run.recovery %}
        <div class="empty">
          <strong>Recovery: {{ run.recovery.status }}</strong>
          <span>{{ run.recovery.nextAction }}</span>
          {% if run.recovery.selectedStepKeys %}
          <span class="mono">Safe path: {{ run.recovery.selectedStepKeys | join(" -> ") }}</span>
          {% endif %}
          <div class="inline-actions">
            {% if run.recovery.canRetry %}
            <form method="post" action="{{ url_for('retry_workflow_form', workflow_run_id=run.id) }}">
              <button class="secondary" type="submit">Resume safe workflow</button>
            </form>
            {% endif %}
            {% if run.recovery.supersededByWorkflowRunId %}
            <a href="{{ url_for('workflow_detail_api', workflow_run_id=run.recovery.supersededByWorkflowRunId) }}">Recovery run #{{ run.recovery.supersededByWorkflowRunId }}</a>
            {% endif %}
          </div>
        </div>
        {% endif %}
        {% if run.steps %}
        <div class="table-wrap">
          <table>
            <thead><tr><th>Order</th><th>Step</th><th>Stage</th><th>Status</th><th>Attempt</th><th>Duration</th><th>Started</th><th>Error</th></tr></thead>
            <tbody>
            {% for step in run.steps %}
              <tr>
                <td>{{ step.step_order }}</td>
                <td class="mono">{{ step.step_key }}</td>
                <td>{{ step.stage or "-" }}</td>
                <td><span class="badge {{ step.status }}">{{ step.status }}</span></td>
                <td>{{ step.attempt }}</td>
                <td>{% if step.duration_ms is not none %}{{ step.duration_ms }} ms{% else %}-{% endif %}</td>
                <td class="mono">{{ step.started_at or "-" }}</td>
                <td>{{ step.error_message or "-" }}</td>
              </tr>
            {% endfor %}
            </tbody>
          </table>
        </div>
        {% else %}
        <div class="empty">No step evidence was recorded for this legacy workflow run.</div>
        {% endif %}
        <div class="inline-actions">
          <a href="{{ url_for('workflow_detail_api', workflow_run_id=run.id) }}">Run JSON</a>
        </div>
      </details>
      {% endfor %}
      {% else %}
      <div class="empty">No workflow runs recorded.</div>
      {% endif %}
    </section>

    <section id="intake">
      <div class="section-head">
        <div>
          <h2>Folder Intake</h2>
          <p>Scan configured folders and process imported documents through OCR, categorization, and review gates.</p>
        </div>
        <div class="inline-actions">
          <form class="inline-actions" method="post" action="{{ url_for('rescan_intake_form') }}">
            <button type="submit" {% if not intake_paths %}disabled{% endif %}>Rescan folders</button>
          </form>
          <form class="inline-actions" method="post" action="{{ url_for('process_imported_form') }}">
            <button class="secondary" type="submit">Process imported</button>
          </form>
          <form class="inline-actions" method="post" action="{{ url_for('retry_failed_processing_form') }}">
            <button class="secondary" type="submit">Retry failed</button>
          </form>
        </div>
      </div>
      {% if intake_summary %}
      <div class="summary-grid">
        <div class="summary-item"><span>Scanned</span><strong>{{ intake_summary.scanned }}</strong></div>
        <div class="summary-item"><span>Registered</span><strong>{{ intake_summary.registered }}</strong></div>
        <div class="summary-item"><span>Duplicates</span><strong>{{ intake_summary.duplicates }}</strong></div>
        <div class="summary-item"><span>Already in ledger</span><strong>{{ intake_summary.alreadyRegistered }}</strong></div>
        <div class="summary-item"><span>Skipped</span><strong>{{ intake_summary.skipped|length }}</strong></div>
      </div>
      <details>
        <summary>Last intake run</summary>
        <pre>{{ pretty_json(intake_summary) }}</pre>
      </details>
      {% endif %}
      {% if processing_summary %}
      <div class="summary-grid">
        <div class="summary-item"><span>Requested</span><strong>{{ processing_summary.requested }}</strong></div>
        <div class="summary-item"><span>Retried</span><strong>{{ processing_summary.retried or 0 }}</strong></div>
        <div class="summary-item"><span>Processed</span><strong>{{ processing_summary.processed }}</strong></div>
        <div class="summary-item"><span>Needs review</span><strong>{{ processing_summary.needsReview }}</strong></div>
        <div class="summary-item"><span>Failed</span><strong>{{ processing_summary.failed }}</strong></div>
        <div class="summary-item"><span>Skipped</span><strong>{{ processing_summary.skipped }}</strong></div>
      </div>
      <details>
        <summary>Last processing run</summary>
        <pre>{{ pretty_json(processing_summary) }}</pre>
      </details>
      {% endif %}
      {% if intake_paths %}
      <div class="settings-grid">
        {% for path in intake_paths %}
        <div class="setting"><span>Folder {{ loop.index }}</span><strong class="mono">{{ path }}</strong></div>
        {% endfor %}
        <div class="setting"><span>Allowed extensions</span><strong class="mono">{{ intake_extensions|join(", ") }}</strong></div>
      </div>
      {% else %}
      <div class="empty">No intake folders configured. Add local_intake_paths under [operations].</div>
      {% endif %}
    </section>

    <section id="ledger">
      <div class="section-head">
        <div>
          <h2>Document Ledger</h2>
          <p>{{ documents|length }} latest documents from the local operating ledger.</p>
        </div>
      </div>
      {% if documents %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Source</th>
              <th>File</th>
              <th>Vendor</th>
              <th>Amount</th>
              <th>Date</th>
              <th>Category</th>
              <th>Confidence</th>
              <th>Status</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
          {% for doc in documents %}
            <tr>
              <td class="mono">#{{ doc.id }}</td>
              <td>{{ doc.source }}</td>
              <td>
                <a href="{{ url_for('document_detail_page', document_id=doc.id) }}">{{ doc.original_filename }}</a>
                <div class="muted mono">{{ doc.source_document_id or "no source id" }}</div>
              </td>
              <td>{{ doc.vendor_name or "Unknown" }}</td>
              <td>{{ format_money(doc.total_amount) }}</td>
              <td>{{ doc.transaction_date or "-" }}</td>
              <td>{{ doc.category or "Unassigned" }}</td>
              <td>{{ format_confidence(doc.confidence_score) }}</td>
              <td><span class="badge {{ doc.processing_status }}">{{ doc.processing_status }}</span></td>
              <td>
                {% if doc.processing_status == "imported" %}
                <form class="table-actions" method="post" action="{{ url_for('process_document_form', document_id=doc.id) }}">
                  <button class="compact secondary" type="submit">Process</button>
                </form>
                {% elif doc.processing_status == "failed" %}
                <form class="table-actions" method="post" action="{{ url_for('retry_document_processing_form', document_id=doc.id) }}">
                  <button class="compact secondary" type="submit">Retry</button>
                </form>
                {% elif doc.processing_status in ["processed", "reviewed", "validated", "ready_to_route"] %}
                <form class="table-actions" method="post" action="{{ url_for('route_document_form', document_id=doc.id) }}">
                  <button class="compact secondary" type="submit">Prepare Wave draft</button>
                </form>
                {% else %}
                <span class="muted">-</span>
                {% endif %}
              </td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No documents have been registered yet.</div>
      {% endif %}
    </section>

    <section id="groups">
      <div class="section-head">
        <div>
          <h2>Document Groups</h2>
          <p>Candidate scanner batches and manual merge/split groups before documents are routed or exported.</p>
        </div>
        <form class="inline-actions" method="post" action="{{ url_for('detect_document_groups_form') }}">
          <button type="submit">Detect scanner groups</button>
        </form>
      </div>
      <div class="summary-grid">
        <div class="summary-item"><span>Groups</span><strong>{{ metrics.document_groups }}</strong></div>
        <div class="summary-item"><span>Need review</span><strong>{{ metrics.open_document_groups }}</strong></div>
      </div>
      {% if grouping_summary %}
      <details open>
        <summary>Last grouping run</summary>
        <pre>{{ pretty_json(grouping_summary) }}</pre>
      </details>
      {% endif %}
      {% if document_groups %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Group</th>
              <th>Type</th>
              <th>Members</th>
              <th>Confidence</th>
              <th>Status</th>
              <th>Reason</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
          {% for group in document_groups %}
            <tr>
              <td>
                <strong>#{{ group.id }} {{ group.title or group.group_key }}</strong>
                <div class="muted mono">{{ group.group_key }}</div>
              </td>
              <td>{{ group.group_type }}</td>
              <td>
                {{ group.member_count }}
                {% for member in group.members[:3] %}
                <div class="muted mono">#{{ member.document_id }} {{ member.document.original_filename if member.document else "" }} {{ "(" ~ member.status ~ ")" if member.status != "active" else "" }}</div>
                {% endfor %}
              </td>
              <td>{{ format_confidence(group.confidence_score) }}</td>
              <td><span class="badge {{ group.status }}">{{ group.status }}</span></td>
              <td>{{ group.reason or "-" }}</td>
              <td class="mono">{{ group.updated_at }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No document groups have been detected yet. Run scanner detection after importing local/scanner folders.</div>
      {% endif %}
    </section>

    <section id="duplicates">
      <div class="section-head">
        <div>
          <h2>Duplicate Candidates</h2>
          <p>Review exact and fuzzy duplicate evidence before FAB blocks, ignores, or routes documents.</p>
        </div>
      </div>
      <div class="summary-grid">
        <div class="summary-item"><span>Candidates</span><strong>{{ metrics.duplicate_candidates }}</strong></div>
        <div class="summary-item"><span>Open</span><strong>{{ metrics.open_duplicate_candidates }}</strong></div>
        <div class="summary-item"><span>Marked duplicate</span><strong>{{ metrics.duplicates }}</strong></div>
      </div>
      {% if duplicate_candidates %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Candidate</th>
              <th>Documents</th>
              <th>Match</th>
              <th>Confidence</th>
              <th>Status</th>
              <th>Evidence</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
          {% for candidate in duplicate_candidates %}
            <tr>
              <td class="mono">#{{ candidate.id }}</td>
              <td class="mono">#{{ candidate.document_id }} -> #{{ candidate.candidate_document_id }}</td>
              <td>{{ candidate.match_type }}</td>
              <td>{{ format_confidence(candidate.confidence_score) }}</td>
              <td><span class="badge {{ candidate.status }}">{{ candidate.status }}</span></td>
              <td class="mono">{{ compact_json(candidate.evidence) }}</td>
              <td class="mono">{{ candidate.updated_at }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No duplicate candidates have been recorded yet.</div>
      {% endif %}
    </section>

    <section id="records">
      <div class="section-head">
        <div>
          <h2>Bookkeeping Records</h2>
          <p>Normalized financial records that connect source evidence, review state, export readiness, and reconciliation.</p>
        </div>
        <div class="inline-actions">
          <form class="table-actions" method="post" action="{{ url_for('refresh_bookkeeping_records_form') }}">
            <input type="hidden" name="sourceType" value="document">
            <button class="compact secondary" type="submit">Refresh document records</button>
          </form>
          <form class="table-actions" method="post" action="{{ url_for('refresh_bookkeeping_records_form') }}">
            <input type="hidden" name="sourceType" value="bank_transaction">
            <button class="compact secondary" type="submit">Refresh bank records</button>
          </form>
          <form class="table-actions" method="post" action="{{ url_for('refresh_bookkeeping_records_form') }}">
            <input type="hidden" name="sourceType" value="all">
            <button class="compact" type="submit">Refresh all records</button>
          </form>
        </div>
      </div>
      {% if record_refresh_summary %}
      <details open>
        <summary>Last record refresh</summary>
        <pre>{{ pretty_json(record_refresh_summary) }}</pre>
      </details>
      {% endif %}
      <div class="summary-grid">
        <div class="summary-item"><span>Records</span><strong>{{ metrics.bookkeeping_records }}</strong></div>
        <div class="summary-item"><span>Line items</span><strong>{{ metrics.bookkeeping_record_line_items }}</strong></div>
        <div class="summary-item"><span>Need review</span><strong>{{ metrics.bookkeeping_records_needing_review }}</strong></div>
        <div class="summary-item"><span>Export ready</span><strong>{{ metrics.export_ready_records }}</strong></div>
      </div>
      {% if bookkeeping_records %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Source</th>
              <th>Vendor</th>
              <th>Amount</th>
              <th>Date</th>
              <th>Lines</th>
              <th>Account / Tax</th>
              <th>Category</th>
              <th>Target</th>
              <th>Status</th>
              <th>Export</th>
              <th>Reconciliation</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
          {% for record in bookkeeping_records %}
            <tr>
              <td class="mono"><a href="{{ url_for('bookkeeping_record_detail_page', record_id=record.id) }}">#{{ record.id }}</a></td>
              <td>
                {{ record.source_type }}
                <div class="muted mono">
                  {% if record.document_id %}doc #{{ record.document_id }}{% elif record.bank_transaction_id %}bank #{{ record.bank_transaction_id }}{% else %}-{% endif %}
                </div>
              </td>
              <td>{{ record.vendor_name or "Unknown" }}</td>
              <td>{{ format_money(record.amount) }} {{ record.currency }}</td>
              <td>{{ record.record_date or "-" }}</td>
              <td>
                {{ record.line_item_count or 0 }}
                {% if record.line_items %}
                <div class="muted">{{ record.line_items[0].description or record.line_items[0].item_name or "Line item" }}</div>
                {% endif %}
              </td>
              <td>
                {% if record.line_items %}
                {{ record.line_items[0].account_name or record.target_account or "Unmapped" }}
                <div class="muted">
                  tax {{ record.line_items[0].tax_code or "unmapped" }}
                  {% if record.line_items[0].tax_rate is not none %} / {{ record.line_items[0].tax_rate }}%{% endif %}
                </div>
                {% else %}
                {{ record.target_account or "Unmapped" }}
                {% endif %}
              </td>
              <td>{{ record.category or "Unassigned" }}</td>
              <td>{{ record.target_system }}</td>
              <td><span class="badge {{ record.status }}">{{ record.status }}</span></td>
              <td><span class="badge {{ record.export_status }}">{{ record.export_status }}</span></td>
              <td><span class="badge {{ record.reconciliation_status }}">{{ record.reconciliation_status }}</span></td>
              <td>
                {% if record.review_required or record.status in ["needs_review", "missing_receipt", "failed", "duplicate"] %}
                <form class="table-actions" method="post" action="{{ url_for('resolve_bookkeeping_record_form', record_id=record.id) }}">
                  <input type="hidden" name="status" value="approved">
                  <input type="hidden" name="resolution" value="Approved normalized bookkeeping record from dashboard.">
                  <button class="compact" type="submit">Approve</button>
                </form>
                <form class="table-actions" method="post" action="{{ url_for('resolve_bookkeeping_record_form', record_id=record.id) }}">
                  <input type="hidden" name="status" value="rejected">
                  <input type="hidden" name="resolution" value="Rejected normalized bookkeeping record from dashboard.">
                  <button class="compact secondary" type="submit">Reject</button>
                </form>
                {% elif record.status not in ["rejected", "ignored"] %}
                <form class="table-actions" method="post" action="{{ url_for('resolve_bookkeeping_record_form', record_id=record.id) }}">
                  <input type="hidden" name="status" value="needs_review">
                  <input type="hidden" name="resolution" value="Reopened normalized bookkeeping record for manual review.">
                  <button class="compact secondary" type="submit">Reopen review</button>
                </form>
                {% endif %}
                {% if record.source_type == "bank_transaction" %}
                <form class="table-actions" method="post" action="{{ url_for('route_bookkeeping_record_form', record_id=record.id) }}">
                  <button class="compact" type="submit">Prepare draft</button>
                </form>
                {% else %}
                <span class="muted">Document route</span>
                {% endif %}
              </td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No normalized bookkeeping records have been created yet. Process documents or import bank transactions to populate this operating layer.</div>
      {% endif %}
    </section>

    <section id="master-ledger">
      <div class="section-head">
        <div>
          <h2>Master Ledger</h2>
          <p>FAB source-of-truth projection across normalized records, Wave/MGZ downstream state, checksums, and blockers.</p>
        </div>
        <div class="inline-actions">
          <a class="button-link secondary" href="{{ url_for('master_ledger_api') }}">JSON</a>
          <a class="button-link secondary" href="{{ url_for('master_ledger_api', format='csv') }}">CSV</a>
        </div>
      </div>
      <div class="summary-grid">
        <div class="summary-item"><span>Rows</span><strong>{{ master_ledger.summary.totalRows }}</strong></div>
        <div class="summary-item"><span>Blocked</span><strong>{{ master_ledger.summary.blockedRows }}</strong></div>
        <div class="summary-item"><span>Ready drafts</span><strong>{{ master_ledger.summary.readyForDraft }}</strong></div>
        <div class="summary-item"><span>Need approval</span><strong>{{ master_ledger.summary.readyForApproval }}</strong></div>
        <div class="summary-item"><span>Executable</span><strong>{{ master_ledger.summary.readyForExternalExecution }}</strong></div>
        <div class="summary-item"><span>Checksum</span><strong class="mono">{{ master_ledger.ledgerChecksum[:12] if master_ledger.ledgerChecksum else "-" }}</strong></div>
      </div>
      {% if master_ledger.rows %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Record</th>
              <th>Date</th>
              <th>Vendor</th>
              <th>Amount</th>
              <th>Target</th>
              <th>Downstream</th>
              <th>Proof</th>
              <th>Blockers</th>
            </tr>
          </thead>
          <tbody>
          {% for row in master_ledger.rows[:25] %}
            <tr>
              <td class="mono">#{{ row.recordId }}<div class="muted">{{ row.sourceType }}</div></td>
              <td>{{ row.recordDate or "-" }}</td>
              <td>{{ row.vendorName or row.description or "Unknown" }}</td>
              <td>{{ format_money(row.amount) }} {{ row.currency }}</td>
              <td>{{ row.targetSystem }}<div class="muted">{{ row.targetAccount or "unmapped" }}</div></td>
              <td>
                <span class="badge {{ row.downstreamStatus }}">{{ row.downstreamStatus }}</span>
                <div class="muted">{{ row.externalSubmission }}</div>
              </td>
              <td>
                <div class="mono">{{ row.rowChecksum[:12] }}</div>
                {% if row.masterLedgerChecksum %}
                <div class="muted mono">draft {{ row.masterLedgerChecksum[:12] }}</div>
                {% endif %}
                {% if row.exportAttemptId %}
                <div class="muted mono">export #{{ row.exportAttemptId }}</div>
                {% endif %}
              </td>
              <td>
                {% if row.blockers %}
                {{ row.blockers|join(", ") }}
                {% else %}
                <span class="muted">clear</span>
                {% endif %}
              </td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No master-ledger rows yet. Refresh records after processing documents or importing bank transactions.</div>
      {% endif %}
      <details>
        <summary>Master ledger summary</summary>
        <pre>{{ pretty_json(master_ledger.summary) }}</pre>
      </details>
    </section>

    <section id="reports">
      <div class="section-head">
        <div>
          <h2>Financial Reports</h2>
          <p>{{ financial_report.period.fromDate }} through {{ financial_report.period.toDate }} / {{ financial_report.basis }} basis / provisional</p>
        </div>
        <div class="inline-actions">
          <a class="button-link secondary" href="{{ url_for('financial_reports_api') }}">JSON</a>
          <a class="button-link secondary" href="{{ url_for('financial_reports_api', format='csv') }}">CSV</a>
        </div>
      </div>
      <div class="summary-grid">
        <div class="summary-item"><span>Included</span><strong>{{ financial_report.summary.includedRecordCount }}</strong></div>
        <div class="summary-item"><span>Excluded</span><strong>{{ financial_report.summary.excludedRecordCount }}</strong></div>
        <div class="summary-item"><span>Undated</span><strong>{{ financial_report.summary.undatedRecordCount }}</strong></div>
        <div class="summary-item"><span>Readiness</span><strong>{{ financial_report.summary.readiness }}</strong></div>
      </div>
      <details open>
        <summary>Scheduled report generation</summary>
        <div class="summary-grid">
          <div class="summary-item"><span>Schedule</span><strong>{{ report_schedule_status.status }}</strong></div>
          <div class="summary-item"><span>Frequency</span><strong>{{ report_schedule_status.schedule.frequency if report_schedule_status.schedule else "-" }}</strong></div>
          <div class="summary-item"><span>Current slot</span><strong class="mono">{{ report_schedule_status.slot.scheduleSlot if report_schedule_status.slot else "-" }}</strong></div>
          <div class="summary-item"><span>Next due</span><strong class="mono">{{ report_schedule_status.slot.nextDueAt if report_schedule_status.slot else "-" }}</strong></div>
        </div>
        <div class="inline-actions">
          <form method="post" action="{{ url_for('run_due_report_schedule_form') }}">
            <button type="submit" {% if not report_schedule_status.enabled %}disabled{% endif %}>Run due schedule</button>
          </form>
        </div>
        {% if report_schedule_status.error %}<div class="empty">{{ report_schedule_status.error }}</div>{% endif %}
        {% if scheduled_report_summary %}<pre>{{ pretty_json(scheduled_report_summary) }}</pre>{% endif %}
        {% if financial_report_runs %}
        <div class="table-wrap">
          <table>
            <thead>
              <tr><th>Run</th><th>Slot</th><th>Period</th><th>Status</th><th>Rows</th><th>Gates</th><th>Attempts</th><th>Artifacts</th></tr>
            </thead>
            <tbody>
            {% for run in financial_report_runs %}
              <tr>
                <td class="mono">#{{ run.id }}</td>
                <td class="mono">{{ run.schedule_slot }}</td>
                <td>{{ run.period_from }} to {{ run.period_to }}</td>
                <td><span class="badge {{ run.status }}">{{ run.status }}</span></td>
                <td>{{ run.row_count }}</td>
                <td>{{ run.blocker_count }}</td>
                <td>{{ run.attempt_count }}</td>
                <td>
                  {% if run.json_path %}<a href="{{ url_for('financial_report_run_artifact_api', report_run_id=run.id, format='json') }}">JSON</a>{% endif %}
                  {% if run.csv_path %}<a href="{{ url_for('financial_report_run_artifact_api', report_run_id=run.id, format='csv') }}">CSV</a>{% endif %}
                </td>
              </tr>
            {% endfor %}
            </tbody>
          </table>
        </div>
        {% endif %}
      </details>
      {% if financial_report.reports.profitAndLoss.byCurrency %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Currency</th>
              <th>Revenue net</th>
              <th>Expenses net</th>
              <th>Net result</th>
              <th>Output VAT</th>
              <th>Input VAT</th>
              <th>VAT payable</th>
              <th>Cash movement</th>
            </tr>
          </thead>
          <tbody>
          {% for pnl in financial_report.reports.profitAndLoss.byCurrency %}
            {% set vat = financial_report.reports.vat.byCurrency | selectattr('currency', 'equalto', pnl.currency) | first %}
            {% set cash = financial_report.reports.cashFlow.byCurrency | selectattr('currency', 'equalto', pnl.currency) | first %}
            <tr>
              <td>{{ pnl.currency }}</td>
              <td>{{ format_money(pnl.revenueNet) }}</td>
              <td>{{ format_money(pnl.expensesNet) }}</td>
              <td>{{ format_money(pnl.netResult) }}</td>
              <td>{{ format_money(vat.outputVat if vat else 0) }}</td>
              <td>{{ format_money(vat.inputVat if vat else 0) }}</td>
              <td>{{ format_money(vat.netVatPayable if vat else 0) }}</td>
              <td>{{ format_money(cash.netMovement if cash else 0) }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No reportable records in the selected period.</div>
      {% endif %}
      <details {% if financial_report.summary.blockers %}open{% endif %}>
        <summary>Report completeness gates</summary>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Gate</th><th>Records</th></tr></thead>
            <tbody>
            {% if financial_report.summary.blockers %}
            {% for blocker in financial_report.summary.blockers %}
              <tr><td>{{ blocker.code }}</td><td>{{ blocker.count }}</td></tr>
            {% endfor %}
            {% else %}
              <tr><td>clear</td><td>0</td></tr>
            {% endif %}
            </tbody>
          </table>
        </div>
      </details>
      {% if financial_report.reports.expenses.byCategory %}
      <details>
        <summary>Expense breakdown</summary>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Category</th><th>Currency</th><th>Records</th><th>Gross</th><th>VAT</th><th>Net</th></tr></thead>
            <tbody>
            {% for item in financial_report.reports.expenses.byCategory[:15] %}
              <tr>
                <td>{{ item.category }}</td>
                <td>{{ item.currency }}</td>
                <td>{{ item.recordCount }}</td>
                <td>{{ format_money(item.grossAmount) }}</td>
                <td>{{ format_money(item.vatAmount) }}</td>
                <td>{{ format_money(item.netAmount) }}</td>
              </tr>
            {% endfor %}
            </tbody>
          </table>
        </div>
      </details>
      {% endif %}
    </section>

    <section id="compliance">
      <div class="section-head">
        <div>
          <h2>VAT & Compliance</h2>
          <p>Provisional Dutch VAT checks, source evidence, and document-retention controls. No tax filing is performed.</p>
        </div>
        <form class="inline-actions" method="post" action="{{ url_for('run_compliance_assessment_form') }}">
          <button type="submit">Assess current quarter</button>
        </form>
      </div>
      <div class="summary-grid">
        <div class="summary-item"><span>Assessments</span><strong>{{ compliance_summary.assessmentCount }}</strong></div>
        <div class="summary-item"><span>Open findings</span><strong>{{ compliance_summary.openFindings }}</strong></div>
        <div class="summary-item"><span>Blocking</span><strong>{{ compliance_summary.blockingFindings }}</strong></div>
        <div class="summary-item"><span>Retention records</span><strong>{{ compliance_summary.retentionRecords }}</strong></div>
        <div class="summary-item"><span>Statutory status</span><strong>{{ compliance_summary.statutoryStatus }}</strong></div>
        <div class="summary-item"><span>External filing</span><strong>{{ compliance_summary.externalFiling }}</strong></div>
      </div>
      {% if last_compliance_summary %}
      <details open>
        <summary>Last assessment action</summary>
        <pre>{{ pretty_json(last_compliance_summary) }}</pre>
      </details>
      {% endif %}
      {% if compliance_assessments %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>Assessment</th><th>Period</th><th>Status</th><th>Records</th><th>Findings</th><th>Blocking</th><th>VAT summary</th><th>Filing</th></tr>
          </thead>
          <tbody>
          {% for assessment in compliance_assessments %}
            <tr>
              <td class="mono">#{{ assessment.id }}<div class="muted">{{ assessment.source_checksum[:12] }}</div></td>
              <td>{{ assessment.period_from }} to {{ assessment.period_to }}</td>
              <td><span class="badge {{ assessment.status }}">{{ assessment.status }}</span></td>
              <td>{{ assessment.record_count }}</td>
              <td>{{ assessment.finding_count }}</td>
              <td>{{ assessment.blocking_count }}</td>
              <td class="mono">{{ compact_json(assessment.vat_summary) }}</td>
              <td>{{ assessment.statutory_status }} / {{ assessment.external_filing }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No compliance assessment yet. Run the current-quarter assessment to create reviewable evidence.</div>
      {% endif %}
      {% if compliance_findings %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>Severity</th><th>Finding</th><th>Record</th><th>Status</th><th>Evidence</th><th>Review action</th></tr>
          </thead>
          <tbody>
          {% for finding in compliance_findings %}
            <tr>
              <td><span class="badge {{ finding.severity }}">{{ finding.severity }}</span></td>
              <td><strong>{{ finding.title }}</strong><div class="muted">{{ finding.message }}</div><div class="mono">{{ finding.code }}</div></td>
              <td>
                {% if finding.bookkeeping_record_id %}<a href="{{ url_for('bookkeeping_record_detail_page', record_id=finding.bookkeeping_record_id) }}">#{{ finding.bookkeeping_record_id }}</a>{% else %}-{% endif %}
                {% if finding.document_id %}<div class="muted">document #{{ finding.document_id }}</div>{% endif %}
              </td>
              <td><span class="badge {{ finding.status }}">{{ finding.status }}</span></td>
              <td class="mono">{{ compact_json(finding.evidence) }}</td>
              <td>
                {% if finding.status == 'open' %}
                <form class="table-actions" method="post" action="{{ url_for('compliance_finding_status_form', finding_id=finding.id) }}">
                  <input type="hidden" name="status" value="acknowledged">
                  <button class="compact secondary" type="submit">Acknowledge</button>
                </form>
                {% endif %}
                <form class="review-actions" method="post" action="{{ url_for('compliance_finding_status_form', finding_id=finding.id) }}">
                  <input type="hidden" name="status" value="resolved">
                  <input name="resolution" placeholder="Correction or reviewed exception evidence" required>
                  <button class="compact secondary" type="submit">Resolve</button>
                </form>
              </td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% elif compliance_assessments %}
      <div class="empty">The latest stored compliance evidence has no open findings.</div>
      {% endif %}
      <details>
        <summary>Document retention records</summary>
        {% if retention_records %}
        <div class="table-wrap">
          <table>
            <thead><tr><th>Document</th><th>Source date</th><th>Retain until</th><th>Status</th><th>Source file</th><th>Deletion</th></tr></thead>
            <tbody>
            {% for retention in retention_records %}
              <tr>
                <td class="mono">#{{ retention.document_id }}</td>
                <td>{{ retention.source_date or "-" }}</td>
                <td>{{ retention.retain_until or "-" }}</td>
                <td><span class="badge {{ retention.status }}">{{ retention.status }}</span></td>
                <td>{{ "present" if retention.source_file_present else "unverified" if retention.source_file_present is none else "missing" }}</td>
                <td>not authorized</td>
              </tr>
            {% endfor %}
            </tbody>
          </table>
        </div>
        {% else %}
        <div class="empty">No document-retention evidence has been assessed yet.</div>
        {% endif %}
      </details>
    </section>

    <section id="fields">
      <div class="section-head">
        <div>
          <h2>Extracted Fields</h2>
          <p>Structured OCR/extraction evidence with confidence and provenance for review and learning.</p>
        </div>
      </div>
      {% if extracted_fields %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Document</th>
              <th>Field</th>
              <th>Value</th>
              <th>Confidence</th>
              <th>Source</th>
              <th>Provenance</th>
            </tr>
          </thead>
          <tbody>
          {% for field in extracted_fields %}
            <tr>
              <td class="mono">#{{ field.document_id }}</td>
              <td>{{ field.field_name }}</td>
              <td class="mono">{{ compact_json(field.field_value) }}</td>
              <td>{{ format_confidence(field.confidence_score) }}</td>
              <td>{{ field.source }}</td>
              <td class="mono">{{ compact_json(field.provenance) }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No extracted field records have been stored yet. Process an imported document to populate this evidence.</div>
      {% endif %}
    </section>

    <section id="routing">
      <div class="section-head">
        <div>
          <h2>Routing & Export Drafts</h2>
          <p>Prepare Wave draft operations from reviewed documents and bank records without submitting data externally.</p>
        </div>
        <form class="inline-actions" method="post" action="{{ url_for('prepare_ready_routes_form') }}">
          <input type="hidden" name="sourceType" value="all">
          <button type="submit">Prepare ready drafts</button>
        </form>
      </div>
      {% if routing_summary %}
      <div class="summary-grid">
        <div class="summary-item"><span>Requested</span><strong>{{ routing_summary.requested }}</strong></div>
        <div class="summary-item"><span>Drafts</span><strong>{{ routing_summary.draftPrepared }}</strong></div>
        <div class="summary-item"><span>Already ready</span><strong>{{ routing_summary.alreadyPrepared }}</strong></div>
        <div class="summary-item"><span>Needs review</span><strong>{{ routing_summary.needsReview }}</strong></div>
        <div class="summary-item"><span>Blocked</span><strong>{{ routing_summary.blocked }}</strong></div>
      </div>
      <details>
        <summary>Last routing run</summary>
        <pre>{{ pretty_json(routing_summary) }}</pre>
      </details>
      {% endif %}
      {% if routing_attempts %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Source</th>
              <th>Target</th>
              <th>Status</th>
              <th>Wave action</th>
              <th>Message</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
          {% for attempt in routing_attempts %}
            {% set operation = attempt.metadata.operation if attempt.metadata and attempt.metadata.operation else None %}
            <tr>
              <td class="mono">#{{ attempt.id }}</td>
              <td class="mono">
                {% if attempt.document_id %}
                doc #{{ attempt.document_id }}
                {% elif attempt.bookkeeping_record_id %}
                record #{{ attempt.bookkeeping_record_id }}
                {% elif attempt.metadata and attempt.metadata.bookkeepingRecordId %}
                record #{{ attempt.metadata.bookkeepingRecordId }}
                {% else %}
                -
                {% endif %}
              </td>
              <td>{{ attempt.target }}</td>
              <td><span class="badge {{ attempt.status }}">{{ attempt.status }}</span></td>
              <td>{{ operation.action_id if operation else "-" }}</td>
              <td>{{ attempt.message or "-" }}</td>
              <td class="mono">{{ attempt.created_at }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No export drafts or routing blocks have been recorded yet.</div>
      {% endif %}
    </section>

    <section id="exports">
      <div class="section-head">
        <div>
          <h2>Export Attempts</h2>
          <p>FAB-owned approval and result ledger for Wave operations; approvals never submit externally by themselves.</p>
        </div>
        <form class="inline-actions" method="post" action="{{ url_for('prepare_ready_export_attempts_form') }}">
          <button type="submit">Prepare export approvals</button>
        </form>
      </div>
      <div class="summary-grid">
        <div class="summary-item"><span>Attempts</span><strong>{{ metrics.export_attempts }}</strong></div>
        <div class="summary-item"><span>Need approval</span><strong>{{ metrics.export_attempts_needing_approval }}</strong></div>
        <div class="summary-item"><span>Approved</span><strong>{{ metrics.approved_export_attempts }}</strong></div>
        <div class="summary-item"><span>Needs attention</span><strong>{{ metrics.attention_export_attempts }}</strong></div>
        <div class="summary-item"><span>Deferred</span><strong>{{ metrics.deferred_export_attempts }}</strong></div>
        <div class="summary-item"><span>Needs supervision</span><strong>{{ metrics.supervised_export_attempts }}</strong></div>
        <div class="summary-item"><span>Executed</span><strong>{{ metrics.executed_export_attempts }}</strong></div>
      </div>
      {% if export_summary %}
      <details>
        <summary>Last export preparation run</summary>
        <pre>{{ pretty_json(export_summary) }}</pre>
      </details>
      {% endif %}
      {% if export_attempts %}
      <div class="table-wrap">
        <table class="export-table">
          <colgroup>
            <col style="width: 4%">
            <col style="width: 9%">
            <col style="width: 10%">
            <col style="width: 15%">
            <col style="width: 10%">
            <col style="width: 10%">
            <col style="width: 8%">
            <col style="width: 14%">
            <col style="width: 20%">
          </colgroup>
          <thead>
            <tr>
              <th>ID</th>
              <th>Source</th>
              <th>Target</th>
              <th>Action</th>
              <th>Status</th>
              <th>External</th>
              <th>Approval</th>
              <th>Message</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
          {% for export in export_attempts %}
            <tr>
              <td class="mono" data-label="ID">#{{ export.id }}</td>
              <td class="mono" data-label="Source">
                {% if export.document_id %}
                <a href="{{ url_for('document_detail_page', document_id=export.document_id) }}">doc #{{ export.document_id }}</a>
                {% elif export.bookkeeping_record_id %}
                <a href="{{ url_for('bookkeeping_record_detail_page', record_id=export.bookkeeping_record_id) }}">record #{{ export.bookkeeping_record_id }}</a>
                {% else %}
                -
                {% endif %}
              </td>
              <td data-label="Target">
                {{ export.target_system }}
                <div class="muted">{{ export.surface or "-" }}</div>
              </td>
              <td data-label="Action">
                {{ export.action_id or "-" }}
                <div class="muted mono">{{ export.operation_id or "-" }}</div>
                {% if export.external_id %}
                <div class="muted">Wave ID</div>
                <div class="muted mono">{{ export.external_id }}</div>
                {% endif %}
                {% if export.metadata and export.metadata.masterLedgerDraft %}
                <div class="muted">Master ledger {{ export.metadata.masterLedgerDraft.draftType }}</div>
                <div class="muted mono">{{ export.metadata.masterLedgerChecksum[:12] if export.metadata.masterLedgerChecksum else export.metadata.masterLedgerDraft.checksum[:12] }}</div>
                <div><a href="{{ url_for('export_attempt_artifact', export_attempt_id=export.id, format='csv' if export.metadata.masterLedgerDraft.draftType == 'transaction_import' else 'json') }}">Artifact</a></div>
                {% endif %}
              </td>
              <td data-label="Status"><span class="badge {{ export.status }}">{{ export.status }}</span></td>
              <td data-label="External"><span class="badge {{ export.external_submission }}">{{ export.external_submission }}</span></td>
              <td data-label="Approval">
                {% if export.approval_required %}
                Required
                {% elif export.approved_at %}
                {{ export.approved_by or "approved" }}<div class="muted mono">{{ export.approved_at }}</div>
                {% else %}
                -
                {% endif %}
              </td>
              <td class="export-message" data-label="Message">{{ export.message or "-" }}</td>
              <td class="export-actions" data-label="Actions">
                {% if export.document_id %}
                <a class="button-link" href="{{ url_for('document_detail_page', document_id=export.document_id) }}">{{ "Open bill" if export.action_id == "bill_create" else "Open document" }}</a>
                {% elif export.bookkeeping_record_id %}
                <a class="button-link" href="{{ url_for('bookkeeping_record_detail_page', record_id=export.bookkeeping_record_id) }}">Open record</a>
                {% endif %}
                {% if export.metadata and export.metadata.masterLedgerDraft and export.status != "supervision_required" and export.external_submission not in ["queued", "submitted", "executed"] %}
                <form class="table-actions" method="post" action="{{ url_for('regenerate_export_attempt_form', export_attempt_id=export.id) }}">
                  <button class="compact secondary" type="submit">Regenerate draft</button>
                </form>
                {% endif %}
                {% if export.status in ["approval_required", "prepared", "attention_required"] %}
                {% if export.status == "attention_required" %}
                <div class="muted">Resolve the linked review/configuration issue, then approve again.</div>
                {% endif %}
                <form class="table-actions" method="post" action="{{ url_for('approve_export_attempt_form', export_attempt_id=export.id) }}">
                  <input type="text" name="confirmation" placeholder="{{ export_approval_phrase }}">
                  <button class="compact" type="submit">Approve</button>
                </form>
                <form class="table-actions" method="post" action="{{ url_for('reject_export_attempt_form', export_attempt_id=export.id) }}">
                  <input type="text" name="confirmation" placeholder="{{ export_rejection_phrase }}">
                  <button class="compact secondary" type="submit">Reject</button>
                </form>
                {% elif export.status == "approved" %}
                <form class="review-actions" method="post" action="{{ url_for('execute_export_attempt_form', export_attempt_id=export.id) }}">
                  <button type="submit">Execute</button>
                </form>
                <form class="review-actions" method="post" action="{{ url_for('reject_export_attempt_form', export_attempt_id=export.id) }}">
                  <input type="text" name="confirmation" placeholder="{{ export_rejection_phrase }}">
                  <button class="compact secondary" type="submit">Cancel approval</button>
                </form>
                <form class="review-actions" method="post" action="{{ url_for('record_export_attempt_result_form', export_attempt_id=export.id) }}">
                  <select name="status" aria-label="Export result">
                    <option value="executed">Executed</option>
                    <option value="queued">Queued</option>
                    <option value="failed">Failed</option>
                  </select>
                  <input type="text" name="externalId" placeholder="External ID">
                  <input type="text" name="confirmation" placeholder="{{ export_result_confirmation_phrase }}">
                  <button class="compact secondary" type="submit">Record result</button>
                </form>
                {% elif export.status == "supervision_required" %}
                <div class="muted">Complete the stored artifact import in your MijnGeldzaken session.</div>
                <form class="review-actions" method="post" action="{{ url_for('record_export_attempt_result_form', export_attempt_id=export.id) }}">
                  <select name="status" aria-label="Supervised export result">
                    <option value="executed">Executed</option>
                    <option value="submitted">Submitted</option>
                    <option value="failed">Failed</option>
                  </select>
                  <input type="text" name="externalId" placeholder="Receipt reference">
                  <input type="text" name="confirmation" placeholder="{{ export_result_confirmation_phrase }}">
                  <button class="compact secondary" type="submit">Record supervised result</button>
                </form>
                {% else %}
                -
                {% endif %}
              </td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No export attempts have been prepared yet. Prepare Wave drafts first, then prepare export approvals.</div>
      {% endif %}
    </section>

    <section id="mijngeldzaken">
      <div class="section-head">
        <div>
          <h2>MijnGeldzaken Control Center</h2>
          <p>Model household-bookkeeping menus, master-ledger sync, document vault, planning, and account-health actions before external execution.</p>
        </div>
        <form class="inline-actions" method="post" action="{{ url_for('plan_mijngeldzaken_workflow_form') }}">
          <select name="workflowId" aria-label="MijnGeldzaken workflow">
            <option value="master_ledger_downstream_sync">Master ledger sync</option>
            <option value="document_vault_sync">Document vault</option>
            <option value="planning_context_read">Planning context</option>
            <option value="connection_health_check">Connection health</option>
          </select>
          <input type="text" name="fromDate" placeholder="From date">
          <input type="text" name="toDate" placeholder="To date">
          <button type="submit">Plan MGZ workflow</button>
        </form>
      </div>
      <div class="summary-grid">
        <div class="summary-item"><span>Surfaces</span><strong>{{ mijngeldzaken_control.summary.surfaces }}</strong></div>
        <div class="summary-item"><span>Actions</span><strong>{{ mijngeldzaken_control.summary.actions }}</strong></div>
        <div class="summary-item"><span>Features</span><strong>{{ mijngeldzaken_control.summary.feature_pages }}</strong></div>
        <div class="summary-item"><span>Read-only</span><strong>{{ mijngeldzaken_control.summary.actions_by_safety.read_only }}</strong></div>
        <div class="summary-item"><span>Safe drafts</span><strong>{{ mijngeldzaken_control.summary.actions_by_safety.safe_draft }}</strong></div>
        <div class="summary-item"><span>Confirmations</span><strong>{{ mijngeldzaken_control.summary.actions_by_safety.requires_confirmation }}</strong></div>
      </div>
      {% if mijngeldzaken_plan_summary %}
      <details open>
        <summary>Last MijnGeldzaken workflow plan</summary>
        <pre>{{ pretty_json(mijngeldzaken_plan_summary) }}</pre>
      </details>
      {% endif %}
      {% if mijngeldzaken_controls %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Master-ledger gate</th>
              <th>Status</th>
              <th>Count</th>
              <th>Purpose</th>
            </tr>
          </thead>
          <tbody>
          {% for gate in mijngeldzaken_controls.gates %}
            <tr>
              <td><strong>{{ gate.label }}</strong></td>
              <td><span class="badge {{ gate.status }}">{{ gate.status }}</span></td>
              <td>{{ gate.count }}</td>
              <td>{{ gate.description }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      <details>
        <summary>MijnGeldzaken next actions</summary>
        <pre>{{ pretty_json(mijngeldzaken_controls.nextActions) }}</pre>
      </details>
      {% endif %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Feature surface</th>
              <th>Mode</th>
              <th>Controls</th>
              <th>Gate</th>
            </tr>
          </thead>
          <tbody>
          {% for feature_id, feature in mijngeldzaken_control.featureInventory.items() %}
            <tr>
              <td>
                <strong>{{ feature.surface }}</strong>
                <div class="muted">{{ feature.module }} / {{ feature_id }}</div>
              </td>
              <td><span class="badge {{ feature.automation_mode }}">{{ feature.automation_mode }}</span></td>
              <td>{{ feature.controls|length }}</td>
              <td>{{ feature.review_gate }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      <details>
        <summary>MijnGeldzaken guardrails and sync contract</summary>
        <pre>{{ pretty_json({"credentials": mijngeldzaken_control.credentials, "safetyPolicy": mijngeldzaken_control.safetyPolicy, "syncContracts": mijngeldzaken_control.syncContracts}) }}</pre>
      </details>
    </section>

    <section id="wave">
      <div class="section-head">
        <div>
          <h2>Wave Control Center</h2>
          <p>Model Wave menus, reports, and actions as FAB-owned operation plans before anything is executed externally.</p>
        </div>
        <form class="inline-actions" method="post" action="{{ url_for('plan_wave_workflow_form') }}">
          <select name="workflowId" aria-label="Wave workflow">
            <option value="daily_reconciliation_run">Daily reconciliation</option>
            <option value="period_close_pack">Period close pack</option>
          </select>
          <input type="text" name="fromDate" placeholder="From date">
          <input type="text" name="toDate" placeholder="To date">
          <button type="submit">Plan Wave workflow</button>
        </form>
      </div>
      <div class="summary-grid">
        <div class="summary-item"><span>Surfaces</span><strong>{{ wave_control.summary.surfaces }}</strong></div>
        <div class="summary-item"><span>Actions</span><strong>{{ wave_control.summary.actions }}</strong></div>
        <div class="summary-item"><span>Reports</span><strong>{{ wave_control.summary.reports }}</strong></div>
        <div class="summary-item"><span>Read-only</span><strong>{{ wave_control.summary.actions_by_safety.read_only }}</strong></div>
        <div class="summary-item"><span>Safe drafts</span><strong>{{ wave_control.summary.actions_by_safety.safe_draft }}</strong></div>
        <div class="summary-item"><span>Confirmations</span><strong>{{ wave_control.summary.actions_by_safety.requires_confirmation }}</strong></div>
      </div>
      <div class="section-head" style="padding: 14px 18px 0;">
        <div>
          <h3>Verified account mappings</h3>
          <p>Read Wave's chart of accounts and confirm the exact anchor and category IDs used by approved exports.</p>
        </div>
        <form class="inline-actions" method="post" action="{{ url_for('discover_wave_accounts_form') }}">
          <select name="targetSystem" aria-label="Wave target account">
            <option value="waveapps_business">Wave Business</option>
            <option value="waveapps_personal">Wave Personal</option>
          </select>
          <button type="submit">Refresh accounts</button>
        </form>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Target</th><th>Configuration</th><th>Anchor account</th><th>Category accounts</th><th>Discovery</th></tr></thead>
          <tbody>
          {% for mapping in wave_control.accountMappings.targets %}
            <tr>
              <td><strong>{{ mapping.targetSystem }}</strong></td>
              <td><span class="badge {{ 'ready' if mapping.configured else 'blocked' }}">{{ 'configured' if mapping.configured else 'missing' }}</span><div class="muted">{{ ", ".join(mapping.requiredMissing) if mapping.requiredMissing else "All required settings present" }}</div></td>
              <td><span class="mono">{{ mapping.anchorAccount.accountId or '-' }}</span><div class="muted">verified {{ mapping.anchorAccount.verified if mapping.anchorAccount.verified is not none else 'not checked' }}</div></td>
              <td>{{ mapping.categoryAccounts|length }} mapped<div class="muted">verified {{ mapping.verified if mapping.accountsDiscovered is not none else 'not checked' }}</div></td>
              <td>{{ mapping.accountsDiscovered if mapping.accountsDiscovered is not none else '-' }} accounts</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      <div class="section-head" style="padding: 14px 18px 0;">
        <div>
          <h3>Wave entity mirror</h3>
          <p>Read customers, products/services, and invoices into FAB so downstream IDs and drift remain inspectable.</p>
        </div>
        <form class="inline-actions" method="post" action="{{ url_for('sync_wave_entities_form') }}">
          <select name="targetSystem" aria-label="Wave mirror target account">
            <option value="waveapps_business">Wave Business</option>
            <option value="waveapps_personal">Wave Personal</option>
          </select>
          <select name="entityTypes" aria-label="Wave entity type">
            <option value="all">Customers, products, invoices</option>
            <option value="customer">Customers</option>
            <option value="product">Products &amp; services</option>
            <option value="invoice">Invoices</option>
          </select>
          <button type="submit">Sync Wave records</button>
        </form>
      </div>
      <div class="summary-grid">
        <div class="summary-item"><span>Mirrored entities</span><strong>{{ metrics.wave_entities }}</strong></div>
        <div class="summary-item"><span>Missing downstream</span><strong>{{ metrics.wave_entities_missing_downstream }}</strong></div>
        <div class="summary-item"><span>Sync runs</span><strong>{{ metrics.wave_sync_runs }}</strong></div>
      </div>
      {% if wave_entity_sync_summary %}
      <details open>
        <summary>Last Wave entity sync</summary>
        <pre>{{ pretty_json(wave_entity_sync_summary) }}</pre>
      </details>
      {% endif %}
      {% if wave_sync_runs %}
      <div class="table-wrap">
        <table>
          <thead><tr><th>Sync target</th><th>Entities</th><th>Pages</th><th>Seen</th><th>Status</th><th>Finished</th></tr></thead>
          <tbody>
          {% for run in wave_sync_runs %}
            <tr>
              <td>{{ run.target_system }}</td>
              <td>{{ ", ".join(run.entity_types) }}</td>
              <td>{{ run.pages_fetched }}</td>
              <td>{{ run.entities_seen }}</td>
              <td><span class="badge {{ run.status }}">{{ run.status }}</span></td>
              <td class="mono">{{ run.finished_at or run.started_at }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% endif %}
      {% if wave_entities %}
      <div class="table-wrap">
        <table>
          <thead><tr><th>Wave record</th><th>Type</th><th>Target</th><th>Status</th><th>Amount</th><th>Presence</th><th>Updated</th></tr></thead>
          <tbody>
          {% for entity in wave_entities %}
            <tr>
              <td><strong>{{ entity.name or entity.external_id }}</strong><div class="muted mono">{{ entity.external_id }}</div>{% if entity.email %}<div class="muted">{{ entity.email }}</div>{% endif %}</td>
              <td>{{ entity.entity_type }}</td>
              <td>{{ entity.target_system }}</td>
              <td><span class="badge {{ entity.status or 'unknown' }}">{{ entity.status or '-' }}</span></td>
              <td>{% if entity.amount is not none %}{{ format_money(entity.amount) }} {{ entity.currency or 'EUR' }}{% else %}-{% endif %}</td>
              <td><span class="badge {{ entity.presence_status }}">{{ entity.presence_status }}</span></td>
              <td class="mono">{{ entity.modified_at or entity.updated_at }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No Wave customers, products, or invoices have been mirrored yet.</div>
      {% endif %}
      {% if wave_account_discovery_summary %}
      <details open>
        <summary>Last Wave account discovery</summary>
        <pre>{{ pretty_json(wave_account_discovery_summary) }}</pre>
      </details>
      {% endif %}
      {% if wave_plan_summary %}
      <details open>
        <summary>Last Wave workflow plan</summary>
        <pre>{{ pretty_json(wave_plan_summary) }}</pre>
      </details>
      {% endif %}
      {% if wave_report_controls %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Report control gate</th>
              <th>Required actions</th>
              <th>Planned actions</th>
              <th>Evidence</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
          {% for gate in wave_report_controls.gates %}
            <tr>
              <td>
                <strong>{{ gate.label }}</strong>
                <div class="muted">{{ gate.section }}</div>
              </td>
              <td>{{ ", ".join(gate.requiredActions) }}</td>
              <td>{{ ", ".join(gate.plannedActions) if gate.plannedActions else "-" }}</td>
              <td>{{ gate.snapshotCount }} snapshots{% if gate.hasResultPayload %} / result payload{% endif %}</td>
              <td><span class="badge {{ gate.status }}">{{ gate.status }}</span></td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      <details>
        <summary>Report control next actions</summary>
        <pre>{{ pretty_json(wave_report_controls.nextActions) }}</pre>
      </details>
      {% endif %}
      {% if wave_report_snapshots %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Report evidence</th>
              <th>Action</th>
              <th>Period</th>
              <th>Scope</th>
              <th>Result</th>
              <th>Status</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
          {% for snapshot in wave_report_snapshots %}
            <tr>
              <td>
                <strong>{{ snapshot.report_type }}</strong>
                <div class="muted">{{ snapshot.report_section or "reports" }} · {{ snapshot.export_format or "screen" }}</div>
              </td>
              <td>{{ snapshot.action_id }}</td>
              <td>{{ snapshot.from_date or snapshot.as_of_date or "-" }}{% if snapshot.to_date %} to {{ snapshot.to_date }}{% endif %}</td>
              <td>{{ snapshot.account_name or snapshot.account_option or "All Accounts" }} / {{ snapshot.contact_name or snapshot.contact_option or "All Contacts" }}</td>
              <td>
                {% if snapshot.row_count is not none %}Rows {{ snapshot.row_count }}{% endif %}
                {% if snapshot.total_amount is not none %}<div>{{ format_money(snapshot.total_amount) }}</div>{% endif %}
                {% if snapshot.total_debits is not none or snapshot.total_credits is not none %}
                  <div class="muted">Dr {{ format_money(snapshot.total_debits) }} / Cr {{ format_money(snapshot.total_credits) }}</div>
                {% endif %}
                {% if snapshot.row_count is none and snapshot.total_amount is none and snapshot.total_debits is none and snapshot.total_credits is none %}-{% endif %}
              </td>
              <td><span class="badge {{ snapshot.status }}">{{ snapshot.status }}</span></td>
              <td class="mono">{{ snapshot.updated_at }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No Wave report evidence snapshots have been planned yet.</div>
      {% endif %}
      {% if wave_operation_snapshots %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Operation evidence</th>
              <th>Action</th>
              <th>Surface</th>
              <th>Safety</th>
              <th>Status</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
          {% for snapshot in wave_operation_snapshots %}
            <tr>
              <td>
                <strong>{{ snapshot.operation_id }}</strong>
                <div class="muted">{{ snapshot.mode or "-" }} &middot; workflow {{ snapshot.workflow_id or "-" }}</div>
              </td>
              <td>{{ snapshot.action_id }}</td>
              <td>{{ snapshot.surface or "-" }}</td>
              <td><span class="badge {{ snapshot.safety }}">{{ snapshot.safety }}</span></td>
              <td><span class="badge {{ snapshot.status }}">{{ snapshot.status }}</span></td>
              <td class="mono">{{ snapshot.updated_at }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No Wave operation evidence snapshots have been planned yet.</div>
      {% endif %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Report section</th>
              <th>Reports</th>
              <th>Purpose</th>
            </tr>
          </thead>
          <tbody>
          {% for section in wave_control.reportSections %}
            <tr>
              <td>{{ section.label }}</td>
              <td>{{ section.report_count }}</td>
              <td>{{ section.description }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      <details>
        <summary>Wave guardrails and report catalog</summary>
        <pre>{{ pretty_json({"credentials": wave_control.credentials, "safetyPolicy": wave_control.safetyPolicy, "reports": wave_control.reports}) }}</pre>
      </details>
    </section>

    <section id="bank">
      <div class="section-head">
        <div>
          <h2>Bank Transactions</h2>
          <p>Persist Wave account-transactions exports, bank statement rows, and imported ledger evidence before reconciliation.</p>
        </div>
      </div>
      <form method="post" action="{{ url_for('import_bank_transactions_form') }}" style="padding: 14px 18px 0;">
        <div class="correction-grid">
          <input type="text" name="accountIdentifier" value="default" placeholder="Account identifier">
          <input type="text" name="source" value="manual_upload" placeholder="Source">
          <input type="text" name="filename" placeholder="Filename or Wave export name">
          <select name="format" aria-label="Statement format">
            <option value="json">JSON</option>
            <option value="csv">CSV</option>
            <option value="camt">CAMT XML</option>
            <option value="mt940">MT940</option>
          </select>
        </div>
        <textarea name="statementText" placeholder='Paste JSON, CSV, CAMT XML, or MT940 statement rows. JSON example: [{"id":"tx-1","date":"2026-06-28","amount":-42.5,"description":"Office Shop"}]'></textarea>
        <div class="button-row" style="margin-top: 8px;">
          <button type="submit">Import bank transactions</button>
        </div>
      </form>
      {% if bank_import_summary %}
      {% if bank_import_summary.error %}
      <div class="empty">{{ bank_import_summary.error }}</div>
      {% else %}
      <div class="summary-grid">
        <div class="summary-item"><span>Rows seen</span><strong>{{ bank_import_summary.rowsSeen }}</strong></div>
        <div class="summary-item"><span>Imported</span><strong>{{ bank_import_summary.rowsImported }}</strong></div>
        <div class="summary-item"><span>Duplicates</span><strong>{{ bank_import_summary.duplicates }}</strong></div>
        <div class="summary-item"><span>Identity conflicts</span><strong>{{ bank_import_summary.identityConflicts }}</strong></div>
        <div class="summary-item"><span>Skipped</span><strong>{{ bank_import_summary.skipped }}</strong></div>
      </div>
      <details open>
        <summary>Last bank import</summary>
        <pre>{{ pretty_json(bank_import_summary) }}</pre>
      </details>
      {% endif %}
      {% endif %}
      <div class="summary-grid">
        <div class="summary-item"><span>Imported tx</span><strong>{{ metrics.bank_transactions }}</strong></div>
        <div class="summary-item"><span>Unreconciled tx</span><strong>{{ metrics.unreconciled_bank_transactions }}</strong></div>
        <div class="summary-item"><span>Imports</span><strong>{{ metrics.bank_statement_imports }}</strong></div>
      </div>
      {% if bank_statement_imports %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Import</th>
              <th>Account</th>
              <th>Format</th>
              <th>Status</th>
              <th>Rows</th>
              <th>Duplicates</th>
              <th>Identity conflicts</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
          {% for import_row in bank_statement_imports %}
            <tr>
              <td class="mono">#{{ import_row.id }} {{ import_row.filename or import_row.source }}</td>
              <td class="mono">{{ import_row.account_identifier }}</td>
              <td>{{ import_row.format }}</td>
              <td><span class="badge {{ import_row.status }}">{{ import_row.status }}</span></td>
              <td>{{ import_row.rows_imported }} / {{ import_row.rows_seen }}</td>
              <td>{{ import_row.duplicates }}</td>
              <td>{{ (import_row.metadata or {}).get('identityConflicts', 0) }}</td>
              <td class="mono">{{ import_row.updated_at }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% endif %}
      {% if bank_transactions %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Date</th>
              <th>Amount</th>
              <th>Counterparty</th>
              <th>Description</th>
              <th>Reconciliation</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody>
          {% for tx in bank_transactions %}
            <tr>
              <td class="mono">{{ tx.transaction_id }}</td>
              <td>{{ tx.transaction_date or "-" }}</td>
              <td>{{ format_money(tx.amount) }} {{ tx.currency }}</td>
              <td>{{ tx.counterparty or "-" }}</td>
              <td>{{ tx.description or "-" }}</td>
              <td><span class="badge {{ tx.reconciliation_status }}">{{ tx.reconciliation_status }}</span></td>
              <td>{{ tx.source }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No bank transactions imported yet. Paste a Wave account-transactions export or bank statement above.</div>
      {% endif %}
    </section>

    <section id="reconciliation">
      <div class="section-head">
        <div>
          <h2>Reconciliation</h2>
          <p>Match processed ledger documents to imported bank transactions and route uncertain evidence to review.</p>
        </div>
      </div>
      <form method="post" action="{{ url_for('run_reconciliation_form') }}" style="padding: 14px 18px 0;">
        <textarea name="bankTransactionsJson" placeholder='Optional override batch. Leave empty to use imported bank transactions. Example: [{"id":"tx-1","date":"2026-06-28","amount":-42.5,"description":"Office Shop"}]'></textarea>
        <div class="button-row" style="margin-top: 8px;">
          <button type="submit">Run matching</button>
        </div>
      </form>
      {% if reconciliation_summary %}
      {% if reconciliation_summary.error %}
      <div class="empty">{{ reconciliation_summary.error }}</div>
      {% else %}
      <div class="summary-grid">
        <div class="summary-item"><span>Transactions</span><strong>{{ reconciliation_summary.requestedTransactions }}</strong></div>
        <div class="summary-item"><span>Candidate docs</span><strong>{{ reconciliation_summary.candidateDocuments }}</strong></div>
        <div class="summary-item"><span>Matches</span><strong>{{ reconciliation_summary.matchedCandidates }}</strong></div>
        <div class="summary-item"><span>Missing receipts</span><strong>{{ reconciliation_summary.missingReceipts }}</strong></div>
        <div class="summary-item"><span>Unmatched docs</span><strong>{{ reconciliation_summary.unmatchedDocuments }}</strong></div>
        <div class="summary-item"><span>Review items</span><strong>{{ reconciliation_summary.reviewItemsCreated }}</strong></div>
      </div>
      <details>
        <summary>Last reconciliation run</summary>
        <pre>{{ pretty_json(reconciliation_summary) }}</pre>
      </details>
      {% endif %}
      {% endif %}
      {% if reconciliation_matches %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Document</th>
              <th>Bank transaction</th>
              <th>Status</th>
              <th>Confidence</th>
              <th>Difference</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
          {% for match in reconciliation_matches %}
            <tr>
              <td class="mono">#{{ match.id }}</td>
              <td class="mono">{{ "#" ~ match.document_id if match.document_id else "-" }}</td>
              <td class="mono">{{ match.bank_transaction_id }}</td>
              <td><span class="badge {{ match.status }}">{{ match.status }}</span></td>
              <td>{{ format_confidence(match.confidence_score) }}</td>
              <td>{{ match.amount_difference if match.amount_difference is not none else "-" }}</td>
              <td>
                {% if match.status in ["candidate", "unmatched_document", "missing_receipt", "needs_review"] %}
                <form class="table-actions" method="post" action="{{ url_for('resolve_reconciliation_form', reconciliation_match_id=match.id) }}">
                  {% if match.status == "candidate" %}
                  <button class="compact" type="submit" name="status" value="approved">Reconcile</button>
                  <button class="compact secondary" type="submit" name="status" value="rejected">Reject</button>
                  {% else %}
                  <button class="compact secondary" type="submit" name="status" value="resolved">Resolve</button>
                  <button class="compact secondary" type="submit" name="status" value="ignored">Ignore</button>
                  {% endif %}
                </form>
                {% else %}
                <span class="muted">-</span>
                {% endif %}
              </td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No reconciliation evidence has been recorded yet.</div>
      {% endif %}
    </section>

    <section id="review">
      <div class="section-head">
        <div>
          <h2>Manual Review</h2>
          <p>{{ review_items|length }} pending or in-progress review items.</p>
        </div>
      </div>
      {% if review_items %}
      <div class="review-grid">
        {% for item in review_items %}
        <article class="review-item">
          {% set doc = review_documents.get(item.document_id) if item.document_id else None %}
          <h3>{{ item.reason }}</h3>
          <span class="badge {{ item.status }}">{{ item.status }}</span>
          <p>{{ item.details or "No detail recorded." }}</p>
          <p class="mono">Document: {{ "#" ~ item.document_id if item.document_id else "not linked" }}</p>
          {% if item.reason in ["reconciliation_candidate", "missing_receipt", "unmatched_document"] %}
            {% set bank_tx = item.corrected_data.bankTransaction if item.corrected_data and item.corrected_data.bankTransaction else None %}
            {% if bank_tx %}
            <div class="empty">
              <strong>{{ bank_tx.id or bank_tx.transaction_id or item.corrected_data.bankTransactionId }}</strong>
              · {{ bank_tx.date or bank_tx.transaction_date or "-" }}
              · {{ format_money(bank_tx.amount) }}
              · {{ bank_tx.counterparty or bank_tx.description or "No description" }}
            </div>
            {% endif %}
          {% endif %}
          <form class="review-actions" method="post" action="{{ url_for('resolve_review_form', review_item_id=item.id) }}">
            <input type="text" name="resolution" value="" placeholder="Resolution note">
            <div class="correction-grid">
              <input type="text" name="vendorName" value="{{ doc.vendor_name if doc and doc.vendor_name else "" }}" placeholder="Vendor">
              <input type="text" name="category" value="{{ doc.category if doc and doc.category else "" }}" placeholder="Category">
              <input type="text" name="transactionDate" value="{{ doc.transaction_date if doc and doc.transaction_date else "" }}" placeholder="Date YYYY-MM-DD">
              <input type="text" name="totalAmount" value="{{ doc.total_amount if doc and doc.total_amount is not none else "" }}" placeholder="Total">
              <input type="text" name="vatAmount" value="{{ doc.vat_amount if doc and doc.vat_amount is not none else "" }}" placeholder="VAT">
            </div>
            <div class="button-row">
              {% if item.reason == "reconciliation_candidate" %}
              <button type="submit" name="status" value="approved">Reconcile</button>
              <button class="secondary" type="submit" name="status" value="rejected">Reject match</button>
              {% elif item.reason in ["missing_receipt", "unmatched_document"] %}
              <button type="submit" name="status" value="resolved">Resolved</button>
              <button class="secondary" type="submit" name="status" value="ignored">No receipt needed</button>
              {% else %}
              <button type="submit" name="status" value="approved">Approve</button>
              <button class="secondary" type="submit" name="status" value="rejected">Reject</button>
              <button class="secondary" type="submit" name="status" value="resolved">Resolve</button>
              {% endif %}
            </div>
          </form>
        </article>
        {% endfor %}
      </div>
      {% else %}
      <div class="empty">No pending review items.</div>
      {% endif %}
    </section>

    <section id="rules">
      <div class="section-head">
        <div>
          <h2>Vendors & Rules</h2>
          <p>{{ vendor_summaries|length }} vendors, {{ category_summaries|length }} categories, and {{ rules|length }} suggested or learned rules from ledger evidence.</p>
        </div>
      </div>
      {% if vendor_summaries %}
      <details open>
        <summary>Vendor Directory</summary>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Vendor</th>
                <th>Records</th>
                <th>Documents</th>
                <th>Categories</th>
                <th>Targets</th>
                <th>Needs attention</th>
                <th>Export ready</th>
                <th>Rules</th>
              </tr>
            </thead>
            <tbody>
            {% for vendor in vendor_summaries %}
              <tr>
                <td>{{ vendor.vendorName }}</td>
                <td>{{ vendor.recordCount }}</td>
                <td>{{ vendor.documentCount }}</td>
                <td>{{ vendor.categories[:3]|map(attribute="value")|join(", ") or "-" }}</td>
                <td>{{ vendor.targetSystems[:3]|map(attribute="value")|join(", ") or "-" }}</td>
                <td><span class="badge {{ 'needs_attention' if vendor.needsAttention else 'ok' }}">{{ vendor.reviewRequiredCount + vendor.failedCount }}</span></td>
                <td>{{ vendor.exportReadyCount }}</td>
                <td>{{ vendor.ruleCount }}{% if vendor.suggestedRuleCount %} ({{ vendor.suggestedRuleCount }} suggested){% endif %}</td>
              </tr>
            {% endfor %}
            </tbody>
          </table>
        </div>
      </details>
      {% endif %}
      {% if category_summaries %}
      <details>
        <summary>Category Directory</summary>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Category</th>
                <th>Records</th>
                <th>Documents</th>
                <th>Vendors</th>
                <th>Targets</th>
                <th>Needs attention</th>
                <th>Export ready</th>
                <th>Rules</th>
              </tr>
            </thead>
            <tbody>
            {% for category in category_summaries %}
              <tr>
                <td>{{ category.category }}</td>
                <td>{{ category.recordCount }}</td>
                <td>{{ category.documentCount }}</td>
                <td>{{ category.vendors[:3]|map(attribute="value")|join(", ") or "-" }}</td>
                <td>{{ category.targetSystems[:3]|map(attribute="value")|join(", ") or "-" }}</td>
                <td><span class="badge {{ 'needs_attention' if category.needsAttention else 'ok' }}">{{ category.reviewRequiredCount + category.failedCount }}</span></td>
                <td>{{ category.exportReadyCount }}</td>
                <td>{{ category.ruleCount }}{% if category.suggestedRuleCount %} ({{ category.suggestedRuleCount }} suggested){% endif %}</td>
              </tr>
            {% endfor %}
            </tbody>
          </table>
        </div>
      </details>
      {% endif %}
      {% if rules %}
      <details open>
        <summary>Rule Review</summary>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Vendor</th>
              <th>Category</th>
              <th>Target</th>
              <th>Status</th>
              <th>Uses</th>
              <th>Updated</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
          {% for rule in rules %}
            <tr>
              <td>{{ rule.vendor_name }}</td>
              <td>{{ rule.category }}</td>
              <td>{{ rule.target_system }}</td>
              <td><span class="badge {{ rule.status }}">{{ rule.status }}</span></td>
              <td>{{ rule.usage_count }}</td>
              <td class="mono">{{ rule.updated_at }}</td>
              <td>
                {% if rule.status == "suggested" %}
                <form class="table-actions" method="post" action="{{ url_for('resolve_rule_form', rule_id=rule.id) }}">
                  <input type="hidden" name="status" value="approved">
                  <input type="hidden" name="resolution" value="Approved for future autonomous suggestions.">
                  <button class="compact" type="submit">Approve</button>
                </form>
                <form class="table-actions" method="post" action="{{ url_for('resolve_rule_form', rule_id=rule.id) }}">
                  <input type="hidden" name="status" value="rejected">
                  <input type="hidden" name="resolution" value="Rejected by operator review.">
                  <button class="compact secondary" type="submit">Reject</button>
                </form>
                {% elif rule.status == "approved" %}
                <form class="table-actions" method="post" action="{{ url_for('resolve_rule_form', rule_id=rule.id) }}">
                  <input type="hidden" name="status" value="disabled">
                  <input type="hidden" name="resolution" value="Disabled by operator review.">
                  <button class="compact secondary" type="submit">Disable</button>
                </form>
                {% elif rule.status in ["rejected", "disabled"] %}
                <form class="table-actions" method="post" action="{{ url_for('resolve_rule_form', rule_id=rule.id) }}">
                  <input type="hidden" name="status" value="suggested">
                  <input type="hidden" name="resolution" value="Returned to rule review queue.">
                  <button class="compact secondary" type="submit">Reopen</button>
                </form>
                {% else %}
                <span class="muted">No action</span>
                {% endif %}
              </td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      </details>
      {% else %}
      <div class="empty">No correction-based vendor rules yet.</div>
      {% endif %}
      <details>
        <summary>Recent correction history</summary>
        <pre>{{ pretty_json(corrections) }}</pre>
      </details>
    </section>

    <section id="audit">
      <div class="section-head">
        <div>
          <h2>Audit Log</h2>
          <p>{{ audit_events|length }} latest audit events.</p>
        </div>
      </div>
      {% if audit_events %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Action</th>
              <th>Entity</th>
              <th>Details</th>
            </tr>
          </thead>
          <tbody>
          {% for event in audit_events %}
            <tr>
              <td class="mono">{{ event.created_at }}</td>
              <td>{{ event.action }}</td>
              <td>{{ event.entity_type }} {{ event.entity_id or "" }}</td>
              <td class="mono">{{ compact_json(event.details) }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No audit events recorded yet.</div>
      {% endif %}
    </section>

    <section id="backups">
      <div class="section-head">
        <div>
          <h2>Backup and recovery</h2>
          <p>Verified recovery packages protect the ledger and its source evidence.</p>
        </div>
        {% if not maintenance_mode %}
        <form class="inline-actions" method="post" action="{{ url_for('create_backup_form') }}">
          <button type="submit">Create backup</button>
        </form>
        {% endif %}
      </div>
      <div class="settings-grid">
        <div class="setting"><span>Backup folder</span><strong class="mono">{{ backups.backupDir }}</strong></div>
        <div class="setting"><span>Runtime mode</span><strong>{{ "Maintenance" if maintenance_mode else "Standard" }}</strong></div>
        <div class="setting"><span>Recovery status</span><strong>{{ backups.restorePolicy.status }}</strong></div>
        <div class="setting"><span>Worker requirement</span><strong>{{ "Stopped and locked" if maintenance_mode else "Stop before recovery" }}</strong></div>
      </div>
      {% if maintenance_mode %}
      <div class="empty">
        Maintenance mode is local-only. Normal changes, HAI commands, cloud access, and the autonomous worker are locked.
      </div>
      {% else %}
      <div class="empty">
        Recovery is locked during standard operation. Restart with <span class="mono">Start-FAB-Maintenance.cmd</span> to inspect and restore a package.
      </div>
      {% endif %}
      {% if backup_summary %}
      {% if backup_summary.error %}
      <div class="empty">{{ backup_summary.error }}</div>
      {% else %}
      <details open>
        <summary>Last backup action</summary>
        <pre>{{ pretty_json(backup_summary) }}</pre>
      </details>
      {% endif %}
      {% endif %}
      {% if backups.backups %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Backup</th>
              <th>Status</th>
              <th>Created</th>
              <th>Ledger bytes</th>
              <th>Source evidence</th>
              <th>Checksum</th>
              <th>Recovery</th>
            </tr>
          </thead>
          <tbody>
          {% for backup in backups.backups %}
            <tr>
              <td class="mono">{{ backup.backupFilename }}</td>
              <td><span class="badge {{ backup.status }}">{{ backup.status }}</span></td>
              <td class="mono">{{ backup.createdAt or "-" }}</td>
              <td>{{ backup.ledgerBytes or "-" }}</td>
              <td>
                <span class="badge {{ backup.sourceEvidenceStatus }}">{{ backup.sourceEvidenceStatus or "unknown" }}</span>
                <div class="muted">{{ backup.sourceEvidenceDocuments or 0 }} documents, {{ backup.sourceEvidenceGaps or 0 }} gaps</div>
              </td>
              <td class="mono">{{ backup.ledgerSha256[:12] if backup.ledgerSha256 else "-" }}</td>
              <td>
                {% if backup.status == "valid" and maintenance_mode %}
                <form method="post" action="{{ url_for('restore_backup_form') }}">
                  <input type="hidden" name="backupPath" value="{{ backup.backupPath }}">
                  <input type="hidden" name="restoreMode" value="{{ ledger_restore_mode }}">
                  <input type="text" name="confirmation" placeholder="{{ backups.restoreConfirmationPhrase }}">
                  <button class="compact secondary" type="submit">Restore ledger</button>
                </form>
                {% if backup.format == recovery_package_format and backup.sourceEvidenceStatus == "complete" and (backup.sourceEvidenceGaps or 0) == 0 %}
                <form method="post" action="{{ url_for('restore_backup_form') }}">
                  <input type="hidden" name="backupPath" value="{{ backup.backupPath }}">
                  <input type="hidden" name="restoreMode" value="{{ full_restore_mode }}">
                  <input type="text" name="confirmation" placeholder="{{ backups.fullRestoreConfirmationPhrase }}">
                  <button class="compact" type="submit">Restore ledger and files</button>
                </form>
                {% endif %}
                {% elif backup.status == "valid" %}
                <span class="muted">Maintenance required</span>
                {% else %}
                <span class="muted">{{ backup.error or "-" }}</span>
                {% endif %}
              </td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No local ledger backups have been created yet.</div>
      {% endif %}
    </section>

    <section id="settings">
      <div class="section-head">
        <div>
          <h2>Settings</h2>
          <p>Operational status without secret values.</p>
        </div>
        <span class="badge {{ readiness.status }}">{{ readiness.status }}</span>
      </div>
      <div class="settings-grid">
        <div class="setting"><span>Ledger path</span><strong class="mono">{{ health.ledger_path }}</strong></div>
        <div class="setting"><span>API host</span><strong class="mono">{{ health.host }}</strong></div>
        <div class="setting"><span>Remote auth</span><strong>{{ "enabled" if readiness.security.apiTokenConfigured else "not required on loopback" }}</strong></div>
        <div class="setting"><span>Remote exposure</span><strong>{{ "safe" if readiness.security.remoteExposureSafe else "blocked until API token is configured" }}</strong></div>
        <div class="setting"><span>Ready sources</span><strong>{{ readiness.sources|selectattr("status", "equalto", "ready")|list|length }}</strong></div>
        <div class="setting"><span>Secret values</span><strong>{{ "redacted" if readiness.security.secretValuesRedacted else "unsafe" }}</strong></div>
        <div class="setting"><span>Dashboard URL</span><strong class="mono">{{ readiness.localAccess.dashboardUrl }}</strong></div>
        <div class="setting"><span>API base URL</span><strong class="mono">{{ readiness.localAccess.apiBaseUrl }}</strong></div>
        <div class="setting"><span>Auth mode</span><strong>{{ readiness.localAccess.authMode }}</strong></div>
        <div class="setting"><span>Ngrok safety</span><strong>{{ readiness.localAccess.ngrokSafety }}</strong></div>
      </div>
      <details>
        <summary>Windows Local Runbook</summary>
        <div class="settings-grid">
          <div class="setting"><span>Start command</span><strong class="mono">{{ readiness.localAccess.windows.startCommand }}</strong></div>
          <div class="setting"><span>Working directory</span><strong>{{ readiness.localAccess.windows.workingDirectory }}</strong></div>
          <div class="setting"><span>Task Scheduler</span><strong>{{ readiness.localAccess.windows.taskScheduler }}</strong></div>
          <div class="setting"><span>Auth header</span><strong class="mono">{{ readiness.localAccess.authHeaderExample or "not required for loopback-only no-token mode" }}</strong></div>
        </div>
        <pre>{{ pretty_json(readiness.localAccess.safeRemoteChecklist) }}</pre>
      </details>
      {% if readiness.issues %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Severity</th>
              <th>Issue</th>
              <th>Message</th>
              <th>Next action</th>
            </tr>
          </thead>
          <tbody>
          {% for issue in readiness.issues[:8] %}
            <tr>
              <td><span class="badge {{ issue.severity }}">{{ issue.severity }}</span></td>
              <td>{{ issue.type }}</td>
              <td>{{ issue.message }}</td>
              <td>{{ issue.nextAction or "-" }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% endif %}
      <details open>
        <summary>Source Status</summary>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Source</th>
                <th>Status</th>
                <th>Configured</th>
                <th>Details</th>
              </tr>
            </thead>
            <tbody>
            {% for source in readiness.sources %}
              <tr>
                <td>{{ source.label }}</td>
                <td><span class="badge {{ source.status }}">{{ source.status }}</span></td>
                <td>{{ "yes" if source.configured else "no" }}</td>
                <td>{{ source.details }}</td>
              </tr>
            {% endfor %}
            </tbody>
          </table>
        </div>
      </details>
      <details>
        <summary>Dependency Status</summary>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Dependency</th>
                <th>Status</th>
                <th>Version / command</th>
                <th>Purpose</th>
              </tr>
            </thead>
            <tbody>
            {% for dependency in readiness.dependencies %}
              <tr>
                <td>{{ dependency.label }}</td>
                <td><span class="badge {{ dependency.status }}">{{ dependency.status }}</span></td>
                <td class="mono">{{ dependency.version or dependency.command or dependency.resolved or "-" }}</td>
                <td>{{ dependency.details }}</td>
              </tr>
            {% endfor %}
            </tbody>
          </table>
        </div>
      </details>
      <details>
        <summary>Credential Status</summary>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Credential</th>
                <th>Configured</th>
                <th>File exists</th>
                <th>Stored value</th>
              </tr>
            </thead>
            <tbody>
            {% for credential in readiness.credentials %}
              <tr>
                <td>{{ credential.label }}</td>
                <td>{{ "yes" if credential.configured else "no" }}</td>
                <td>{{ "yes" if credential.exists else "no" }}</td>
                <td>{{ "redacted" if credential.secret else "not secret" }}</td>
              </tr>
            {% endfor %}
            </tbody>
          </table>
        </div>
      </details>
      <details>
        <summary>Storage Paths</summary>
        <pre>{{ pretty_json(readiness.paths) }}</pre>
      </details>
      <details>
        <summary>Raw readiness payload</summary>
        <pre>{{ pretty_json(readiness) }}</pre>
      </details>
      <details>
        <summary>Raw dashboard payload</summary>
        <pre>{{ raw_payload }}</pre>
      </details>
    </section>
  </div>
</body>
</html>
"""


DOCUMENT_DETAIL_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FAB Document #{{ document.id }}</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f8fb;
      --panel: #ffffff;
      --panel-soft: #f0f5f8;
      --text: #15202b;
      --muted: #5d6b78;
      --line: #d9e2ea;
      --accent: #0f766e;
      --accent-dark: #115e59;
      --danger: #b42318;
      --warning: #a15c07;
      --ok: #166534;
      --shadow: 0 14px 36px rgba(21, 32, 43, 0.08);
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size: 14px; line-height: 1.45; }
    a { color: var(--accent-dark); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .shell { max-width: 1320px; margin: 0 auto; padding: 24px; }
    header, .section-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
    header { padding: 10px 0 18px; }
    h1 { margin: 0; font-size: 28px; line-height: 1.1; letter-spacing: 0; overflow-wrap: anywhere; }
    h2 { margin: 0 0 12px; font-size: 18px; line-height: 1.2; letter-spacing: 0; }
    h3 { margin: 0 0 8px; font-size: 15px; line-height: 1.2; letter-spacing: 0; }
    .subtitle { margin: 7px 0 0; color: var(--muted); overflow-wrap: anywhere; }
    .button-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    button, .button-link {
      min-height: 36px; border: 1px solid var(--accent-dark); background: var(--accent); color: #fff;
      padding: 7px 11px; font: inherit; font-size: 13px; font-weight: 750; cursor: pointer;
      display: inline-flex; align-items: center;
    }
    .button-link.secondary, button.secondary { background: #fff; color: var(--accent-dark); }
    input[type="text"], textarea, select { width: 100%; min-height: 36px; border: 1px solid var(--line); padding: 7px 9px; font: inherit; background: #fff; color: var(--text); }
    textarea { min-height: 82px; resize: vertical; }
    section { margin: 18px 0; background: var(--panel); border: 1px solid var(--line); box-shadow: var(--shadow); overflow: hidden; }
    .section-head { padding: 16px 18px; border-bottom: 1px solid var(--line); background: #fbfcfd; }
    .section-head p { margin: 4px 0 0; color: var(--muted); }
    .summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; padding: 14px 18px 0; }
    .summary-item { border: 1px solid var(--line); background: #fff; padding: 10px; min-width: 0; }
    .summary-item span { display: block; color: var(--muted); font-size: 12px; font-weight: 700; text-transform: uppercase; }
    .summary-item strong { display: block; margin-top: 5px; font-size: 18px; overflow-wrap: anywhere; }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; min-width: 820px; }
    th, td { padding: 11px 13px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { color: var(--muted); font-size: 12px; font-weight: 700; text-transform: uppercase; background: var(--panel-soft); white-space: nowrap; }
    .muted { color: var(--muted); }
    .mono { font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; font-size: 12px; }
    .badge { display: inline-flex; align-items: center; min-height: 24px; padding: 3px 8px; border: 1px solid var(--line); background: #edf7f4; color: var(--accent-dark); font-size: 12px; font-weight: 700; white-space: nowrap; }
    .badge.failed, .badge.rejected, .badge.duplicate, .badge.high { background: #fff1f0; color: var(--danger); }
    .badge.pending, .badge.needs_review, .badge.in_review, .badge.candidate, .badge.needs_review, .badge.approval_required { background: #fff7e6; color: var(--warning); }
    .badge.completed, .badge.approved, .badge.processed, .badge.reviewed, .badge.ready_to_route, .badge.reconciled, .badge.resolved { background: #ecfdf3; color: var(--ok); }
    .evidence-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; padding: 14px 18px 18px; }
    .evidence { border: 1px solid var(--line); background: #fff; padding: 12px; min-width: 0; }
    .review-actions { display: grid; gap: 8px; margin-top: 10px; }
    .correction-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 8px; }
    details { padding: 13px 14px; border-top: 1px solid var(--line); background: #fbfcfd; }
    summary { cursor: pointer; font-weight: 700; }
    pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #0f172a; color: #e2e8f0; padding: 12px; margin: 10px 0 0; max-height: 360px; overflow: auto; font-size: 12px; }
    .empty { padding: 22px 18px; color: var(--muted); }
    @media (max-width: 760px) { .shell { padding: 16px; } header, .section-head { display: block; } .button-row { margin-top: 12px; } h1 { font-size: 24px; } }
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <div>
        <a class="button-link secondary" href="{{ url_for('dashboard_page', _anchor='ledger') }}">Back to dashboard</a>
        <h1>{{ document.original_filename }}</h1>
        <p class="subtitle">Document #{{ document.id }} from {{ document.source }} / {{ document.source_document_id or "no source id" }}</p>
      </div>
      <div class="button-row">
        {% if document.processing_status == "imported" %}
        <form method="post" action="{{ url_for('process_document_form', document_id=document.id) }}">
          <button type="submit">Process</button>
        </form>
        {% endif %}
        {% if document.processing_status in ["processed", "reviewed", "validated", "ready_to_route"] %}
        <form method="post" action="{{ url_for('route_document_form', document_id=document.id) }}">
          <button type="submit">Prepare Wave draft</button>
        </form>
        {% endif %}
      </div>
    </header>

    <section>
      <div class="section-head">
        <div>
          <h2>Review Summary</h2>
          <p>The operational state FAB will use before routing or export.</p>
        </div>
        <span class="badge {{ document.processing_status }}">{{ document.processing_status }}</span>
      </div>
      <div class="summary-grid">
        <div class="summary-item"><span>Vendor</span><strong>{{ document.vendor_name or "Unknown" }}</strong></div>
        <div class="summary-item"><span>Amount</span><strong>{{ format_money(document.total_amount) }}</strong></div>
        <div class="summary-item"><span>Date</span><strong>{{ document.transaction_date or "-" }}</strong></div>
        <div class="summary-item"><span>Category</span><strong>{{ document.category or "Unassigned" }}</strong></div>
        <div class="summary-item"><span>Confidence</span><strong>{{ format_confidence(document.confidence_score) }}</strong></div>
        <div class="summary-item"><span>Reconciliation</span><strong>{{ document.reconciliation_status }}</strong></div>
        <div class="summary-item"><span>Reviews</span><strong>{{ open_review_count }} open / {{ document.review_items|length }} total</strong></div>
        <div class="summary-item"><span>Groups</span><strong>{{ document.document_groups|length }}</strong></div>
      </div>
      <div class="evidence-grid">
        <div class="evidence">
          <h3>Source Provenance</h3>
          <p><strong>Path:</strong> <span class="mono">{{ document.storage_path or "-" }}</span></p>
          <p><strong>MIME:</strong> {{ document.mime_type or "-" }}</p>
          <p><strong>Type:</strong> {{ document.document_type }}</p>
          <p><strong>Created:</strong> <span class="mono">{{ document.created_at }}</span></p>
          <p><strong>Updated:</strong> <span class="mono">{{ document.updated_at }}</span></p>
        </div>
        <div class="evidence">
          <h3>Source Preview</h3>
          <p>{{ source_preview.status }}</p>
          {% if source_preview.text %}
          <pre>{{ source_preview.text }}</pre>
          {% else %}
          <p class="muted mono">{{ source_preview.path or "No local source path recorded." }}</p>
          {% endif %}
        </div>
      </div>
    </section>

    <section>
      <div class="section-head">
        <div>
          <h2>Manual Review</h2>
          <p>Resolve only the specific review items that need a human decision.</p>
        </div>
      </div>
      {% if document.review_items %}
      <div class="evidence-grid">
        {% for item in document.review_items %}
        <article class="evidence">
          <h3>{{ item.reason }}</h3>
          <span class="badge {{ item.status }}">{{ item.status }}</span>
          <p>{{ item.details or "No detail recorded." }}</p>
          <p class="mono">{{ compact_json(item.corrected_data) }}</p>
          {% if item.status in ["pending", "in_review"] %}
          <form class="review-actions" method="post" action="{{ url_for('resolve_review_form', review_item_id=item.id) }}">
            <input type="text" name="resolution" placeholder="Resolution note">
            <div class="correction-grid">
              <input type="text" name="vendorName" value="{{ document.vendor_name or "" }}" placeholder="Vendor">
              <input type="text" name="category" value="{{ document.category or "" }}" placeholder="Category">
              <input type="text" name="transactionDate" value="{{ document.transaction_date or "" }}" placeholder="Date YYYY-MM-DD">
              <input type="text" name="totalAmount" value="{{ document.total_amount if document.total_amount is not none else "" }}" placeholder="Total">
              <input type="text" name="vatAmount" value="{{ document.vat_amount if document.vat_amount is not none else "" }}" placeholder="VAT">
              <input type="text" name="duplicateOfDocumentId" value="{{ document.duplicate_of_document_id if document.duplicate_of_document_id else "" }}" placeholder="Duplicate doc ID">
            </div>
            <div class="button-row">
              <button type="submit" name="status" value="approved">Approve</button>
              <button class="secondary" type="submit" name="status" value="rejected">Reject</button>
              <button class="secondary" type="submit" name="status" value="resolved">Resolve</button>
              <button class="secondary" type="submit" name="status" value="ignored">Ignore</button>
            </div>
          </form>
          {% endif %}
        </article>
        {% endfor %}
      </div>
      {% else %}
      <div class="empty">No review items recorded for this document.</div>
      {% endif %}
    </section>

    <section>
      <div class="section-head">
        <div>
          <h2>Extracted Fields</h2>
          <p>Structured OCR/extraction evidence with confidence and provenance.</p>
        </div>
      </div>
      {% if document.extracted_fields %}
      <div class="table-wrap">
        <table>
          <thead><tr><th>Field</th><th>Value</th><th>Confidence</th><th>Source</th><th>Provenance</th></tr></thead>
          <tbody>
          {% for field in document.extracted_fields %}
            <tr>
              <td>{{ field.field_name }}</td>
              <td class="mono">{{ compact_json(field.field_value) }}</td>
              <td>{{ format_confidence(field.confidence_score) }}</td>
              <td>{{ field.source }}</td>
              <td class="mono">{{ compact_json(field.provenance) }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No extracted fields have been stored yet.</div>
      {% endif %}
      <details {% if document.ocr_text %}open{% endif %}>
        <summary>OCR Text</summary>
        <pre>{{ document.ocr_text or "No OCR text stored." }}</pre>
      </details>
      <details>
        <summary>Extracted Data JSON</summary>
        <pre>{{ pretty_json(document.extracted_data) }}</pre>
      </details>
    </section>

    <section>
      <div class="section-head">
        <div>
          <h2>Duplicate And Group Evidence</h2>
          <p>Non-destructive duplicate and multi-page grouping signals.</p>
        </div>
      </div>
      <div class="evidence-grid">
        <div class="evidence">
          <h3>Duplicate Candidates</h3>
          {% if document.duplicate_candidates %}
          {% for candidate in document.duplicate_candidates %}
            <p><span class="badge {{ candidate.status }}">{{ candidate.status }}</span> #{{ candidate.document_id }} -> #{{ candidate.candidate_document_id }} {{ candidate.match_type }} {{ format_confidence(candidate.confidence_score) }}</p>
            <p class="mono">{{ compact_json(candidate.evidence) }}</p>
          {% endfor %}
          {% else %}
          <p class="muted">No duplicate candidates.</p>
          {% endif %}
        </div>
        <div class="evidence">
          <h3>Document Groups</h3>
          {% if document.document_groups %}
          {% for group in document.document_groups %}
            <p><span class="badge {{ group.status }}">{{ group.status }}</span> #{{ group.id }} {{ group.title or group.group_key }} ({{ group.member_count }} active)</p>
            {% for member in group.members %}
              <p class="mono">#{{ member.document_id }} {{ member.role }} {{ member.status }} {{ member.document.original_filename if member.document else "" }}</p>
            {% endfor %}
          {% endfor %}
          {% else %}
          <p class="muted">No document groups.</p>
          {% endif %}
        </div>
      </div>
    </section>

    <section>
      <div class="section-head">
        <div>
          <h2>Bookkeeping, Routing, Export, Reconciliation</h2>
          <p>Downstream operating evidence tied to this document.</p>
        </div>
      </div>
      <div class="evidence-grid">
        <div class="evidence">
          <h3>Bookkeeping Record</h3>
          {% if document.bookkeeping_record %}
          <p><span class="badge {{ document.bookkeeping_record.status }}">{{ document.bookkeeping_record.status }}</span> {{ document.bookkeeping_record.target_system }} / {{ document.bookkeeping_record.export_status }}</p>
          <p>{{ document.bookkeeping_record.vendor_name or "Unknown" }} {{ format_money(document.bookkeeping_record.amount) }} {{ document.bookkeeping_record.currency }}</p>
          <p class="mono">{{ compact_json(document.bookkeeping_record.metadata) }}</p>
          {% else %}
          <p class="muted">No bookkeeping record yet.</p>
          {% endif %}
        </div>
        <div class="evidence">
          <h3>Reconciliation</h3>
          {% if document.reconciliation_matches %}
          {% for match in document.reconciliation_matches %}
            <p><span class="badge {{ match.status }}">{{ match.status }}</span> {{ match.bank_transaction_id }} {{ format_confidence(match.confidence_score) }}</p>
          {% endfor %}
          {% else %}
          <p class="muted">No reconciliation evidence.</p>
          {% endif %}
        </div>
      </div>
      {% if document.routing_attempts or document.export_attempts %}
      <div class="table-wrap">
        <table>
          <thead><tr><th>Type</th><th>Status</th><th>Target</th><th>Action</th><th>Message</th></tr></thead>
          <tbody>
          {% for attempt in document.routing_attempts %}
            <tr><td>Routing</td><td><span class="badge {{ attempt.status }}">{{ attempt.status }}</span></td><td>{{ attempt.target }}</td><td>{{ attempt.metadata.operation.action_id if attempt.metadata and attempt.metadata.operation else "-" }}</td><td>{{ attempt.message or "-" }}</td></tr>
          {% endfor %}
          {% for export in document.export_attempts %}
            <tr><td>Export</td><td><span class="badge {{ export.status }}">{{ export.status }}</span></td><td>{{ export.target_system }}</td><td>{{ export.action_id or "-" }}</td><td>{{ export.message or "-" }}</td></tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% endif %}
    </section>

    <section>
      <div class="section-head">
        <div>
          <h2>Audit Trail</h2>
          <p>Document-specific sensitive actions and state transitions.</p>
        </div>
      </div>
      {% if document.audit_events %}
      <div class="table-wrap">
        <table>
          <thead><tr><th>Created</th><th>Action</th><th>Entity</th><th>Details</th></tr></thead>
          <tbody>
          {% for event in document.audit_events %}
            <tr>
              <td class="mono">{{ event.created_at }}</td>
              <td>{{ event.action }}</td>
              <td>{{ event.entity_type }} {{ event.entity_id or "" }}</td>
              <td class="mono">{{ compact_json(event.details) }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No document-specific audit events recorded.</div>
      {% endif %}
      <details>
        <summary>Raw document payload</summary>
        <pre>{{ pretty_json(document) }}</pre>
      </details>
    </section>
  </div>
</body>
</html>
"""


BOOKKEEPING_RECORD_DETAIL_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FAB Bookkeeping Record #{{ record.id }}</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f8fb;
      --panel: #ffffff;
      --panel-soft: #f0f5f8;
      --text: #15202b;
      --muted: #5d6b78;
      --line: #d9e2ea;
      --accent: #0f766e;
      --accent-dark: #115e59;
      --danger: #b42318;
      --warning: #a15c07;
      --ok: #166534;
      --shadow: 0 14px 36px rgba(21, 32, 43, 0.08);
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size: 14px; line-height: 1.45; }
    a { color: var(--accent-dark); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .shell { max-width: 1320px; margin: 0 auto; padding: 24px; }
    header, .section-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
    header { padding: 10px 0 18px; }
    h1 { margin: 0; font-size: 28px; line-height: 1.1; letter-spacing: 0; overflow-wrap: anywhere; }
    h2 { margin: 0 0 12px; font-size: 18px; line-height: 1.2; letter-spacing: 0; }
    h3 { margin: 0 0 8px; font-size: 15px; line-height: 1.2; letter-spacing: 0; }
    p { margin: 6px 0; }
    .subtitle { margin: 7px 0 0; color: var(--muted); overflow-wrap: anywhere; }
    .button-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    button, .button-link {
      min-height: 36px; border: 1px solid var(--accent-dark); background: var(--accent); color: #fff;
      padding: 7px 11px; font: inherit; font-size: 13px; font-weight: 750; cursor: pointer;
      display: inline-flex; align-items: center;
    }
    .button-link.secondary, button.secondary { background: #fff; color: var(--accent-dark); }
    input[type="text"], textarea { width: 100%; min-height: 36px; border: 1px solid var(--line); padding: 7px 9px; font: inherit; background: #fff; color: var(--text); }
    textarea { min-height: 82px; resize: vertical; }
    section { margin: 18px 0; background: var(--panel); border: 1px solid var(--line); box-shadow: var(--shadow); overflow: hidden; }
    .section-head { padding: 16px 18px; border-bottom: 1px solid var(--line); background: #fbfcfd; }
    .section-head p { margin: 4px 0 0; color: var(--muted); }
    .summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; padding: 14px 18px 18px; }
    .summary-item { border: 1px solid var(--line); background: #fff; padding: 10px; min-width: 0; }
    .summary-item span { display: block; color: var(--muted); font-size: 12px; font-weight: 700; text-transform: uppercase; }
    .summary-item strong { display: block; margin-top: 5px; font-size: 18px; overflow-wrap: anywhere; }
    .evidence-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; padding: 14px 18px 18px; }
    .evidence { border: 1px solid var(--line); background: #fff; padding: 12px; min-width: 0; }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; min-width: 820px; }
    th, td { padding: 11px 13px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { color: var(--muted); font-size: 12px; font-weight: 700; text-transform: uppercase; background: var(--panel-soft); white-space: nowrap; }
    .muted { color: var(--muted); }
    .mono { font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; font-size: 12px; }
    .badge { display: inline-flex; align-items: center; min-height: 24px; padding: 3px 8px; border: 1px solid var(--line); background: #edf7f4; color: var(--accent-dark); font-size: 12px; font-weight: 700; white-space: nowrap; }
    .badge.failed, .badge.rejected, .badge.duplicate, .badge.high { background: #fff1f0; color: var(--danger); }
    .badge.pending, .badge.needs_review, .badge.in_review, .badge.candidate, .badge.blocked_by_review, .badge.missing_receipt { background: #fff7e6; color: var(--warning); }
    .badge.completed, .badge.approved, .badge.processed, .badge.reviewed, .badge.ready_to_route, .badge.ready, .badge.reconciled, .badge.resolved { background: #ecfdf3; color: var(--ok); }
    .review-actions { display: grid; gap: 8px; margin-top: 10px; }
    .correction-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 8px; }
    details { padding: 13px 14px; border-top: 1px solid var(--line); background: #fbfcfd; }
    summary { cursor: pointer; font-weight: 700; }
    pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #0f172a; color: #e2e8f0; padding: 12px; margin: 10px 0 0; max-height: 360px; overflow: auto; font-size: 12px; }
    .empty { padding: 22px 18px; color: var(--muted); }
    @media (max-width: 760px) { .shell { padding: 16px; } header, .section-head { display: block; } .button-row { margin-top: 12px; } h1 { font-size: 24px; } }
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <div>
        <a class="button-link secondary" href="{{ url_for('dashboard_page', _anchor='records') }}">Back to records</a>
        <h1>Bookkeeping Record #{{ record.id }}</h1>
        <p class="subtitle">{{ record.source_type }} source{% if record.document_id %} / document #{{ record.document_id }}{% elif record.bank_transaction_id %} / bank transaction #{{ record.bank_transaction_id }}{% endif %}</p>
      </div>
      <div class="button-row">
        <a class="button-link secondary" href="{{ url_for('bookkeeping_record_detail', record_id=record.id) }}">JSON</a>
        {% if record.source_type == "bank_transaction" or record.status in ["ready_to_route", "approved", "reviewed"] %}
        <form method="post" action="{{ url_for('route_bookkeeping_record_form', record_id=record.id) }}">
          <button type="submit">Prepare draft</button>
        </form>
        {% endif %}
      </div>
    </header>

    <section>
      <div class="section-head">
        <div>
          <h2>Operating Summary</h2>
          <p>Normalized source-of-truth state before FAB routes anything downstream.</p>
        </div>
        <span class="badge {{ record.status }}">{{ record.status }}</span>
      </div>
      <div class="summary-grid">
        <div class="summary-item"><span>Vendor</span><strong>{{ record.vendor_name or "Unknown" }}</strong></div>
        <div class="summary-item"><span>Amount</span><strong>{{ format_money(record.amount) }}</strong></div>
        <div class="summary-item"><span>Date</span><strong>{{ record.record_date or "-" }}</strong></div>
        <div class="summary-item"><span>Category</span><strong>{{ record.category or "Unassigned" }}</strong></div>
        <div class="summary-item"><span>Target</span><strong>{{ record.target_system or "-" }}</strong></div>
        <div class="summary-item"><span>Account</span><strong>{{ record.target_account or "Unmapped" }}</strong></div>
        <div class="summary-item"><span>Confidence</span><strong>{{ format_confidence(record.confidence_score) }}</strong></div>
        <div class="summary-item"><span>Review</span><strong>{{ "required" if record.review_required else "not required" }}</strong></div>
        <div class="summary-item"><span>Export</span><strong>{{ record.export_status }}</strong></div>
        <div class="summary-item"><span>Reconciliation</span><strong>{{ record.reconciliation_status }}</strong></div>
      </div>
    </section>

    <section>
      <div class="section-head">
        <div>
          <h2>Record Review</h2>
          <p>Approve, reject, or reopen this normalized row with an audited explanation.</p>
        </div>
      </div>
      <div class="evidence-grid">
        <div class="evidence">
          <h3>Decision</h3>
          <form class="review-actions" method="post" action="{{ url_for('resolve_bookkeeping_record_form', record_id=record.id) }}">
            <textarea name="resolution" placeholder="Resolution note">{{ record.metadata.lastResolution.resolution if record.metadata and record.metadata.lastResolution else "" }}</textarea>
            <div class="button-row">
              <button type="submit" name="status" value="approved">Approve</button>
              <button class="secondary" type="submit" name="status" value="rejected">Reject</button>
              <button class="secondary" type="submit" name="status" value="needs_review">Reopen review</button>
              <button class="secondary" type="submit" name="status" value="ignored">Ignore</button>
            </div>
          </form>
        </div>
        <div class="evidence">
          <h3>Corrections</h3>
          <form class="review-actions" method="post" action="{{ url_for('resolve_bookkeeping_record_form', record_id=record.id) }}">
            <input type="hidden" name="status" value="approved">
            <div class="correction-grid">
              <input type="text" name="vendorName" value="{{ record.vendor_name or "" }}" placeholder="Vendor">
              <input type="text" name="category" value="{{ record.category or "" }}" placeholder="Category">
              <input type="text" name="recordDate" value="{{ record.record_date or "" }}" placeholder="Date YYYY-MM-DD">
              <input type="text" name="amount" value="{{ record.amount if record.amount is not none else "" }}" placeholder="Amount">
              <input type="text" name="targetAccount" value="{{ record.target_account or "" }}" placeholder="Target account">
              <input type="text" name="targetSystem" value="{{ record.target_system or "" }}" placeholder="Target system">
            </div>
            <input type="text" name="resolution" value="Approved with dashboard corrections.">
            <button type="submit">Approve corrections</button>
          </form>
        </div>
      </div>
    </section>

    <section>
      <div class="section-head">
        <div>
          <h2>Source Proof</h2>
          <p>Trace the normalized row back to the originating document or bank transaction.</p>
        </div>
      </div>
      <div class="evidence-grid">
        <div class="evidence">
          <h3>Source</h3>
          <p><strong>Type:</strong> {{ record.source_type }}</p>
          <p><strong>Record type:</strong> {{ record.record_type }}</p>
          <p><strong>Description:</strong> {{ record.description or "-" }}</p>
          {% if record.document_id %}
          <p><strong>Document:</strong> <a href="{{ url_for('document_detail_page', document_id=record.document_id) }}">#{{ record.document_id }}</a></p>
          {% endif %}
          {% if record.bank_transaction_id %}
          <p><strong>Bank transaction:</strong> <span class="mono">#{{ record.bank_transaction_id }}</span></p>
          {% endif %}
        </div>
        <div class="evidence">
          <h3>Metadata</h3>
          <pre>{{ pretty_json(record.metadata) }}</pre>
        </div>
      </div>
    </section>

    <section>
      <div class="section-head">
        <div>
          <h2>Line Items</h2>
          <p>Posting lines FAB will use for ledger, Wave, and MGZ downstream mapping.</p>
        </div>
      </div>
      {% if record.line_items %}
      <div class="table-wrap">
        <table>
          <thead><tr><th>#</th><th>Description</th><th>Quantity</th><th>Unit</th><th>Amount</th><th>VAT</th><th>Account</th><th>Tax</th></tr></thead>
          <tbody>
          {% for item in record.line_items %}
            <tr>
              <td class="mono">{{ item.line_index }}</td>
              <td>{{ item.description or item.item_name or "-" }}</td>
              <td>{{ item.quantity if item.quantity is not none else "-" }}</td>
              <td>{{ format_money(item.unit_price if item.unit_price is defined else none) }}</td>
              <td>{{ format_money(item.amount if item.amount is defined else none) }}</td>
              <td>{{ format_money(item.vat_amount if item.vat_amount is defined else none) }}</td>
              <td>{{ item.account_name or "Unmapped" }}</td>
              <td>{{ item.tax_code or "-" }}{% if item.tax_rate is not none %} / {{ item.tax_rate }}%{% endif %}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No line items are attached to this bookkeeping record.</div>
      {% endif %}
    </section>

    <section>
      <div class="section-head">
        <div>
          <h2>Reconciliation Evidence</h2>
          <p>Bank/document matches tied to this record, with approval-gated close controls.</p>
        </div>
      </div>
      <div class="evidence-grid">
        <div class="evidence">
          <h3>Bank Transaction</h3>
          {% if bank_transaction %}
          <p><strong>Account:</strong> {{ bank_transaction.account_identifier }}</p>
          <p><strong>Transaction:</strong> <span class="mono">{{ bank_transaction.transaction_id }}</span></p>
          <p><strong>Date:</strong> {{ bank_transaction.transaction_date or "-" }}</p>
          <p><strong>Amount:</strong> {{ format_money(bank_transaction.amount) }} {{ bank_transaction.currency or "" }}</p>
          <p><strong>Description:</strong> {{ bank_transaction.description or "-" }}</p>
          <p><strong>Status:</strong> <span class="badge {{ bank_transaction.reconciliation_status }}">{{ bank_transaction.reconciliation_status }}</span></p>
          {% else %}
          <p class="muted">No persisted bank transaction is linked to this bookkeeping record.</p>
          {% endif %}
        </div>
        <div class="evidence">
          <h3>Latest Match State</h3>
          {% if reconciliation_matches %}
          {% set latest_match = reconciliation_matches[0] %}
          <p><strong>Match:</strong> #{{ latest_match.id }} <span class="badge {{ latest_match.status }}">{{ latest_match.status }}</span></p>
          <p><strong>Confidence:</strong> {{ format_confidence(latest_match.confidence_score) }}</p>
          <p><strong>Amount difference:</strong> {{ latest_match.amount_difference if latest_match.amount_difference is not none else "-" }}</p>
          <p><strong>Bank transaction:</strong> <span class="mono">{{ latest_match.bank_transaction_id }}</span></p>
          {% else %}
          <p class="muted">No reconciliation match evidence is attached to this record.</p>
          {% endif %}
        </div>
      </div>
      {% if reconciliation_matches %}
      <div class="table-wrap">
        <table>
          <thead><tr><th>Match</th><th>Document</th><th>Bank transaction</th><th>Status</th><th>Confidence</th><th>Amount diff</th><th>Actions</th></tr></thead>
          <tbody>
          {% for match in reconciliation_matches %}
            <tr>
              <td class="mono">#{{ match.id }}</td>
              <td class="mono">{{ "#" ~ match.document_id if match.document_id else "-" }}</td>
              <td class="mono">{{ match.bank_transaction_id }}</td>
              <td><span class="badge {{ match.status }}">{{ match.status }}</span></td>
              <td>{{ format_confidence(match.confidence_score) }}</td>
              <td>{{ match.amount_difference if match.amount_difference is not none else "-" }}</td>
              <td>
                {% if match.status in ["candidate", "unmatched_document", "missing_receipt", "needs_review"] %}
                <form class="review-actions" method="post" action="{{ url_for('resolve_reconciliation_form', reconciliation_match_id=match.id) }}">
                  {% if match.status == "candidate" %}
                  <button type="submit" name="status" value="approved">Reconcile</button>
                  <button class="secondary" type="submit" name="status" value="rejected">Reject match</button>
                  {% else %}
                  <button type="submit" name="status" value="resolved">Resolve</button>
                  <button class="secondary" type="submit" name="status" value="ignored">Ignore</button>
                  {% endif %}
                  <input type="text" name="resolution" value="Resolved from bookkeeping record #{{ record.id }}.">
                </form>
                {% else %}
                <span class="muted">closed</span>
                {% endif %}
              </td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% endif %}
    </section>

    <section>
      <div class="section-head">
        <div>
          <h2>Routing And Export Evidence</h2>
          <p>Prepared downstream operations and approval/submission state.</p>
        </div>
      </div>
      {% if routing_attempts or export_attempts %}
      <div class="table-wrap">
        <table>
          <thead><tr><th>Type</th><th>Status</th><th>Target</th><th>Action</th><th>Submission</th><th>Message</th><th>Updated</th></tr></thead>
          <tbody>
          {% for attempt in routing_attempts %}
            <tr>
              <td>Routing</td>
              <td><span class="badge {{ attempt.status }}">{{ attempt.status }}</span></td>
              <td>{{ attempt.target }}</td>
              <td>{{ attempt.metadata.operation.action_id if attempt.metadata and attempt.metadata.operation else "-" }}</td>
              <td>not_executed</td>
              <td>{{ attempt.message or "-" }}</td>
              <td class="mono">{{ attempt.updated_at or attempt.created_at }}</td>
            </tr>
          {% endfor %}
          {% for export in export_attempts %}
            <tr>
              <td>Export</td>
              <td><span class="badge {{ export.status }}">{{ export.status }}</span></td>
              <td>{{ export.target_system }}</td>
              <td>{{ export.action_id or "-" }}</td>
              <td>{{ export.external_submission }}</td>
              <td>{{ export.message or "-" }}</td>
              <td class="mono">{{ export.updated_at or export.created_at }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No routing or export attempts are attached to this record yet.</div>
      {% endif %}
    </section>

    <section>
      <div class="section-head">
        <div>
          <h2>Audit Trail</h2>
          <p>Record-specific actions FAB has logged locally.</p>
        </div>
      </div>
      {% if audit_events %}
      <div class="table-wrap">
        <table>
          <thead><tr><th>Created</th><th>Action</th><th>Actor</th><th>Details</th></tr></thead>
          <tbody>
          {% for event in audit_events %}
            <tr>
              <td class="mono">{{ event.created_at }}</td>
              <td>{{ event.action }}</td>
              <td>{{ event.actor or "-" }}</td>
              <td class="mono">{{ compact_json(event.details) }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No record-specific audit events recorded.</div>
      {% endif %}
      <details>
        <summary>Raw bookkeeping record payload</summary>
        <pre>{{ pretty_json(record) }}</pre>
      </details>
    </section>
  </div>
</body>
</html>
"""


LOGIN_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FAB Operations Login</title>
  <style>
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f6f8fb; color: #15202b; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    form { width: min(420px, calc(100vw - 32px)); background: #fff; border: 1px solid #d9e2ea; padding: 22px; box-shadow: 0 14px 36px rgba(21, 32, 43, 0.08); }
    h1 { margin: 0 0 8px; font-size: 24px; letter-spacing: 0; }
    p { margin: 0 0 16px; color: #5d6b78; }
    input { width: 100%; min-height: 40px; border: 1px solid #d9e2ea; padding: 8px 10px; font: inherit; }
    button { width: 100%; min-height: 40px; margin-top: 12px; border: 1px solid #115e59; background: #0f766e; color: #fff; font: inherit; font-weight: 750; cursor: pointer; }
    .error { color: #b42318; }
  </style>
</head>
<body>
  <form method="post" action="{{ url_for('login') }}">
    <h1>FAB Operations</h1>
    <p>Enter the local API token.</p>
    {% if error %}<p class="error">{{ error }}</p>{% endif %}
    <input type="password" name="token" autofocus autocomplete="current-password">
    <button type="submit">Open dashboard</button>
  </form>
</body>
</html>
"""


def _ascii_credential_matches(supplied: str, expected: str) -> bool:
    return (len(supplied) <= MAX_API_TOKEN_BYTES + len("Bearer ")
            and supplied.isascii() and hmac.compare_digest(supplied, expected))


def create_app(config: Optional[Dict[str, Any]] = None) -> Flask:
    config = config or {}
    require_deployment_ready(config)
    maintenance_mode = _bool_value(
        config.get("fab_maintenance_mode")
        or config.get("operations_maintenance_mode"),
        default=False,
    )
    maintenance_container_bind = _bool_value(
        config.get("fab_maintenance_container_bind"),
        default=False,
    )
    host = str(
        config.get("fab_local_api_host")
        or config.get("operations_api_host")
        or "127.0.0.1"
    )
    token = str(
        config.get("fab_local_api_token")
        or config.get("fab_operations_api_token")
        or config.get("operations_api_token")
        or ""
    )
    hai_token = str(
        config.get("fab_hai_api_token")
        or config.get("operations_hai_api_token")
        or config.get("hai_api_token")
        or ""
    )
    if hai_token and not token:
        raise ValueError("A dedicated HAI token requires an operator API token.")
    if hai_token and hmac.compare_digest(hai_token, token):
        raise ValueError("The HAI token must be different from the operator API token.")
    configured_base_url = str(
        config.get("fab_local_api_base_url")
        or config.get("operations_api_base_url")
        or ""
    ).strip().rstrip("/")
    if host not in LOOPBACK_HOSTS and not token:
        raise ValueError("Refusing to expose FAB local API beyond loopback without an API token.")
    if (
        maintenance_mode
        and host not in LOOPBACK_HOSTS
        and not (maintenance_container_bind and host == "0.0.0.0")
    ):
        raise ValueError("FAB maintenance mode is local-only and must bind to a loopback host.")

    ledger_path = str(
        config.get("fab_local_ledger_path")
        or config.get("operations_ledger_path")
        or default_ledger_path()
    )
    intake_paths = _list_config(
        config,
        "fab_local_intake_paths",
        "operations_local_intake_paths",
        "operations_intake_paths",
        "operations_scanner_folder",
        "operations_scanner_watch_folder",
        "scanner_folder",
        "scanner_watch_folder",
    )
    intake_extensions = _list_config(
        config,
        "fab_local_intake_extensions",
        "operations_local_intake_extensions",
        "operations_intake_extensions",
    ) or sorted(DEFAULT_ALLOWED_EXTENSIONS)
    ledger = LocalOperationsLedger(ledger_path)
    instance_id = local_instance_id(Path(__file__).resolve().parents[2])
    app = Flask(__name__)
    readiness_service = _readiness_service(
        config,
        ledger_path,
        host,
        bool(token),
        intake_paths,
        intake_extensions,
    )
    support_bundle_service = LocalSupportBundleService(ledger, config, readiness_service)
    app.extensions["fab_readiness_service"] = readiness_service
    app.extensions["fab_support_bundle_service"] = support_bundle_service
    app.config["FAB_GMAIL_AUTH"] = LocalGmailAuthorizationCoordinator(ledger, config)
    app.config["FAB_GOOGLE_DRIVE_AUTH"] = LocalGoogleDriveAuthorizationCoordinator(ledger, config)
    app.config["FAB_LOCAL_LEDGER_PATH"] = ledger_path
    app.config["FAB_LOCAL_API_HOST"] = host
    app.config["FAB_MAINTENANCE_MODE"] = maintenance_mode
    app.config["FAB_LOCAL_INTAKE_PATHS"] = intake_paths
    app.config["FAB_LOCAL_INTAKE_EXTENSIONS"] = intake_extensions
    app.config["MAX_CONTENT_LENGTH"] = _bounded_positive_int(
        config.get("fab_local_api_max_request_bytes")
        or config.get("operations_api_max_request_bytes"),
        default=DEFAULT_LOCAL_API_MAX_REQUEST_BYTES,
        maximum=MAX_LOCAL_API_MAX_REQUEST_BYTES,
    )
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = urlsplit(configured_base_url).scheme == "https"
    app.secret_key = (
        hashlib.sha256(token.encode("utf-8")).hexdigest()
        if token
        else secrets.token_hex(32)
    )
    health_cache_ttl_value = None
    for key in (
        "fab_health_cache_ttl_seconds",
        "operations_health_cache_ttl_seconds",
        "health_cache_ttl_seconds",
    ):
        candidate = config.get(key)
        if candidate not in (None, ""):
            health_cache_ttl_value = candidate
            break
    health_cache_ttl_seconds = _bounded_nonnegative_float(
        health_cache_ttl_value,
        default=2.0,
        maximum=30.0,
    )
    health_cache: OrderedDict[int, tuple[float, Dict[str, Any]]] = OrderedDict()
    health_cache_lock = threading.Lock()
    consumed_operator_session_nonces: Dict[str, int] = {}
    operator_session_nonce_lock = threading.Lock()

    @app.before_request
    def assign_request_id():
        supplied = request.headers.get("X-Request-ID", "").strip()
        g.fab_request_id = (
            supplied
            if API_REQUEST_ID_PATTERN.fullmatch(supplied)
            else secrets.token_hex(12)
        )

    @app.before_request
    def require_token():
        request_hostname = (urlsplit(request.host_url).hostname or "").lower()
        if not token and request_hostname not in LOOPBACK_HOSTS:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Untrusted host for loopback-only service"}), 421
            return "Untrusted host for loopback-only service", 421
        if request.method == "GET" and not request.path.startswith("/api/"):
            if not session.get(LOCAL_FORM_SESSION_KEY):
                session[LOCAL_FORM_SESSION_KEY] = secrets.token_urlsafe(24)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            fetch_site = request.headers.get("Sec-Fetch-Site", "").strip().lower()
            origin = request.headers.get("Origin", "").strip().rstrip("/")
            allowed_origins = {request.host_url.rstrip("/")}
            if configured_base_url:
                allowed_origins.add(configured_base_url)
            trusted_opaque_form = (
                origin == "null"
                and not request.path.startswith("/api/")
                and bool(session.get(LOCAL_FORM_SESSION_KEY))
                and request.mimetype == "application/x-www-form-urlencoded"
            )
            origin_rejected = (
                bool(origin)
                and origin not in allowed_origins
                and not trusted_opaque_form
            )
            originless_cross_site = not origin and fetch_site == "cross-site"
            if origin_rejected or originless_cross_site:
                if request.path.startswith("/api/"):
                    return jsonify({"error": "Cross-origin mutation rejected"}), 403
                return "Cross-origin mutation rejected", 403
        if not token:
            return None
        if request.endpoint in {"login", "operator_session_bootstrap"}:
            return None
        supplied_authorization = request.headers.get("Authorization", "")
        operator_authenticated = _ascii_credential_matches(
            supplied_authorization,
            f"Bearer {token}",
        )
        if operator_authenticated:
            g.fab_api_principal = "operator"
            return None
        hai_authenticated = bool(hai_token) and _ascii_credential_matches(
            supplied_authorization,
            f"Bearer {hai_token}",
        )
        if hai_authenticated:
            g.fab_api_principal = "hai"
            if _hai_request_allowed(request.method, request.path):
                return None
            return jsonify({"error": "HAI credential is not permitted for this route"}), 403
        if session.get("fab_local_api_authenticated"):
            parent = session.get("fab_operator_parent")
            requires_parent = requires_parent_session(config)
            if parent is not None:
                authenticated = parent_session_active(parent, config, token)
            else:
                authenticated = not requires_parent or session.get("fab_local_api_auth_method") == "token"
            if authenticated:
                g.fab_api_principal = "operator"
                return None
            session.clear()
        if not request.path.startswith("/api/"):
            return redirect(url_for("login"))
        return jsonify({"error": "Unauthorized"}), 401

    @app.before_request
    def bound_local_operation_request():
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None
        maximum = LOCAL_API_ROUTE_BODY_LIMITS.get(request.endpoint or "")
        if maximum is None and request.path.startswith("/api/"):
            maximum = DEFAULT_LOCAL_JSON_MUTATION_REQUEST_BYTES
        if maximum is None:
            return None
        # Enforce the stream bound after authentication, before JSON or form decoding.
        request.max_content_length = min(app.config["MAX_CONTENT_LENGTH"], maximum)
        return None

    @app.before_request
    def validate_json_mutation_body():
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"} or not request.is_json:
            return None
        try:
            payload = request.get_json(silent=False)
        except (BadRequest, ValueError, RecursionError):
            return jsonify({"error": "Request body must contain valid JSON"}), 400
        if not isinstance(payload, dict):
            return jsonify({"error": "Request JSON body must be an object"}), 400
        return None

    @app.before_request
    def enforce_maintenance_boundary():
        if not maintenance_mode or request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None
        if request.endpoint == "login" or request.path in {
            "/api/backups/restore",
            "/backups/restore",
        }:
            return None
        message = (
            "FAB is in local maintenance mode. Normal mutations are locked until "
            "the standard runtime is restarted."
        )
        if request.path.startswith("/api/"):
            return jsonify({
                "success": False,
                "status": "maintenance_locked",
                "error": message,
                "externalSubmission": "not_executed",
            }), 423
        return message, 423

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not token:
            return redirect(url_for("dashboard_page"))
        if request.method == "POST":
            submitted = request.form.get("token", "")
            if _ascii_credential_matches(submitted, token):
                session.clear()
                session["fab_local_api_authenticated"] = True
                session["fab_local_api_auth_method"] = "token"
                return redirect(url_for("dashboard_page"))
            return render_template_string(LOGIN_TEMPLATE, error="Invalid token."), 401
        return render_template_string(LOGIN_TEMPLATE, error=None)

    @app.get("/operator/session/bootstrap")
    def operator_session_bootstrap():
        if not token:
            return "The protected FAB ledger is not configured.", 503

        ticket = request.args.get("ticket", "")
        if not ticket or len(ticket) > OPERATOR_SESSION_MAX_TICKET_LENGTH:
            return "Invalid or expired FAB operator session.", 401
        parts = ticket.split(".")
        if len(parts) != 2:
            return "Invalid or expired FAB operator session.", 401
        encoded_payload, encoded_signature = parts

        try:
            supplied_signature = _decode_operator_session_part(encoded_signature)
            expected_signature = hmac.new(
                token.encode("utf-8"),
                encoded_payload.encode("ascii"),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(supplied_signature, expected_signature):
                return "Invalid or expired FAB operator session.", 401
            payload = json.loads(_decode_operator_session_part(encoded_payload).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError, binascii.Error):
            return "Invalid or expired FAB operator session.", 401

        if not isinstance(payload, dict):
            return "Invalid or expired FAB operator session.", 401
        issued_at = payload.get("iat")
        expires_at = payload.get("exp")
        nonce = payload.get("nonce")
        actor = payload.get("actor")
        next_target = _normalize_operator_session_target(payload.get("next"))
        now = int(time.time())
        valid_integer_times = (
            isinstance(issued_at, int)
            and not isinstance(issued_at, bool)
            and isinstance(expires_at, int)
            and not isinstance(expires_at, bool)
        )
        if (
            type(payload.get("v")) is not int
            or payload.get("v") not in {1, 2}
            or payload.get("aud") != OPERATOR_SESSION_AUDIENCE
            or not valid_integer_times
            or issued_at < now - OPERATOR_SESSION_MAX_TTL_SECONDS
            or issued_at > now + 5
            or expires_at <= now
            or expires_at > issued_at + OPERATOR_SESSION_MAX_TTL_SECONDS
            or expires_at <= issued_at
            or not isinstance(nonce, str)
            or not OPERATOR_SESSION_NONCE_PATTERN.fullmatch(nonce)
            or not isinstance(actor, str)
            or not OPERATOR_SESSION_ACTOR_PATTERN.fullmatch(actor)
            or next_target is None
        ):
            return "Invalid or expired FAB operator session.", 401

        parent = payload.get("parent")
        requires_parent = requires_parent_session(config)
        if payload["v"] == 2:
            if not parent_session_active(parent, config, token, nonce=nonce, ticket_expiry=expires_at):
                return "Invalid or expired FAB operator session.", 401
        elif requires_parent or parent is not None:
            return "Invalid or expired FAB operator session.", 401

        with operator_session_nonce_lock:
            for consumed_nonce, consumed_expiry in list(consumed_operator_session_nonces.items()):
                if consumed_expiry <= now:
                    consumed_operator_session_nonces.pop(consumed_nonce, None)
            if nonce in consumed_operator_session_nonces:
                return "This FAB operator session has already been used.", 409
            consumed_operator_session_nonces[nonce] = expires_at

        session.clear()
        session["fab_local_api_authenticated"] = True
        if payload["v"] == 2:
            session["fab_operator_parent"] = parent
        session[LOCAL_FORM_SESSION_KEY] = secrets.token_urlsafe(24)
        ledger.record_audit_event({
            "action": "local_api.operator_session_bootstrapped",
            "entityType": "operator_session",
            "entityId": instance_id,
            "details": {
                "actor": actor,
                "operatorMode": "admin" if ":admin:" in actor else "local",
                "targetPath": urlsplit(next_target).path,
                "externalSubmission": "not_executed",
            },
        })
        return redirect(next_target, code=302)

    @app.after_request
    def harden_financial_responses(response):
        request_id = str(getattr(g, "fab_request_id", "") or secrets.token_hex(12))
        response.headers.setdefault("X-Request-ID", request_id)
        if request.path.startswith("/api/") and response.status_code >= 400 and response.is_json:
            payload = response.get_json(silent=True)
            if isinstance(payload, dict):
                payload = LocalOperationsLedger._redact_sensitive(payload)
                for diagnostic_key in ("error", "message"):
                    diagnostic_value = payload.get(diagnostic_key)
                    if isinstance(diagnostic_value, str):
                        payload[diagnostic_key] = diagnostic_value[:2000]
                error_value = payload.get("error")
                message_value = payload.get("message")
                if not isinstance(message_value, str) or not message_value.strip():
                    message_value = (
                        error_value
                        if isinstance(error_value, str) and error_value.strip()
                        else "Request failed"
                    )
                payload.setdefault("success", False)
                payload.setdefault("status", "error")
                payload.setdefault("errorCode", _api_error_code(response.status_code))
                payload.setdefault("message", message_value)
                payload.setdefault("requestId", request_id)
                response.set_data(app.json.dumps(payload, separators=(",", ":")))
                response.content_type = "application/json"
        response.headers.setdefault("Cache-Control", "no-store, max-age=0")
        response.headers.setdefault("Pragma", "no-cache")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'self'",
        )
        return response

    @app.errorhandler(HTTPException)
    def handle_http_exception(exc: HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"error": str(exc.description or exc.name)}), int(exc.code or 500)
        return exc

    @app.errorhandler(Exception)
    def handle_unexpected_exception(exc: Exception):
        request_id = str(getattr(g, "fab_request_id", "unknown"))
        try:
            ledger.record_audit_event({
                "action": "local_api.unhandled_exception",
                "entityType": "api_request",
                "entityId": request_id,
                "details": {
                    "requestId": request_id,
                    "method": request.method,
                    "endpoint": request.endpoint or "unmatched",
                    "errorType": type(exc).__name__,
                    "externalSubmission": "not_executed",
                },
            })
        except Exception:
            pass
        app.logger.error(
            "Unhandled FAB request %s (%s)",
            request_id,
            type(exc).__name__,
        )
        if request.path.startswith("/api/"):
            return jsonify({"error": "Internal server error"}), 500
        return "Internal server error", 500

    def build_health_payload(issue_limit: int) -> Dict[str, Any]:
        operations_health = LocalOperationsHealth(ledger, config).summarize(issue_limit=issue_limit)
        readiness = readiness_service.compact()
        wave_setup = LocalWaveSetupService(config).status(ledger, "waveapps_business")
        wave_activation = wave_setup.get("activation") if isinstance(wave_setup.get("activation"), dict) else {}
        core_target = {
            "id": "waveapps_business",
            "label": "Wave - Noodzakelijk Online",
            "status": wave_setup.get("status"),
            "ready": wave_setup.get("ready") is True,
            "currentStep": wave_activation.get("currentStep"),
            "nextAction": wave_activation.get("nextAction"),
            "externalSubmission": "not_executed",
        }
        readiness["coreTarget"] = core_target
        if not core_target["ready"]:
            readiness["status"] = _combined_health_status(readiness.get("status"), "attention")
            readiness["issueCount"] = int(readiness.get("issueCount") or 0) + 1
            readiness["attentionIssues"] = int(readiness.get("attentionIssues") or 0) + 1
        health_status = (
            "maintenance"
            if maintenance_mode
            else _combined_health_status(
                operations_health.get("status"),
                readiness.get("status"),
            )
        )
        if maintenance_mode:
            readiness["status"] = "maintenance"
            readiness["nextAction"] = (
                "Complete or cancel local recovery, then restart FAB in standard mode."
            )
        return {
            "service": "fab-ledger-api",
            "apiVersion": "1",
            "instanceId": instance_id,
            "status": health_status,
            "maintenanceMode": maintenance_mode,
            "runtimeMode": "maintenance" if maintenance_mode else "standard",
            "ledgerPath": app.config["FAB_LOCAL_LEDGER_PATH"],
            "authRequired": bool(token),
            "ledgerSchema": ledger.schema_status(),
            "intakePaths": app.config["FAB_LOCAL_INTAKE_PATHS"],
            "intakeExtensions": app.config["FAB_LOCAL_INTAKE_EXTENSIONS"],
            "operations": operations_health,
            "readiness": readiness,
        }

    @app.get("/api/health")
    def health():
        default_issue_limit = _bounded_positive_int(
            config.get("fab_health_api_issue_limit")
            or config.get("operations_health_api_issue_limit")
            or config.get("health_api_issue_limit"),
            default=50,
            maximum=500,
        )
        issue_limit = _bounded_positive_int(
            request.args.get("issueLimit"),
            default=default_issue_limit,
            maximum=500,
        )
        cache_status = "disabled"
        if health_cache_ttl_seconds > 0:
            with health_cache_lock:
                now = time.monotonic()
                cached = health_cache.get(issue_limit)
                if cached and now - cached[0] <= health_cache_ttl_seconds:
                    payload = cached[1]
                    health_cache.move_to_end(issue_limit)
                    cache_status = "hit"
                else:
                    payload = build_health_payload(issue_limit)
                    health_cache[issue_limit] = (time.monotonic(), payload)
                    health_cache.move_to_end(issue_limit)
                    while len(health_cache) > 8:
                        health_cache.popitem(last=False)
                    cache_status = "miss"
        else:
            payload = build_health_payload(issue_limit)
        response = jsonify(payload)
        response.headers["X-FAB-Health-Cache"] = cache_status
        response.headers["X-FAB-Health-Cache-TTL"] = f"{health_cache_ttl_seconds:g}"
        return response

    @app.get("/api/live")
    def live():
        return jsonify({
            "service": "fab-ledger-api",
            "apiVersion": "1",
            "instanceId": instance_id,
            "status": "ok",
            "authRequired": bool(token),
            "maintenanceMode": maintenance_mode,
            "runtimeMode": "maintenance" if maintenance_mode else "standard",
        })

    @app.get("/")
    def dashboard_page():
        metrics = ledger.dashboard_metrics()
        operations_health = LocalOperationsHealth(ledger, config).summarize(issue_limit=50)
        notification_service = LocalNotificationService(ledger, config)
        notifications = notification_service.list_notifications(
            status=ACTIVE_NOTIFICATION_STATUSES,
            limit=20,
        )
        notification_summary = notification_service.summary()
        notification_preferences = ledger.list_notification_preferences(limit=100)
        exceptions = LocalExceptionQueueService(ledger, config).list_exceptions(limit=50)
        readiness = readiness_service.summarize()
        autonomy_plan = _autonomy_service(
            ledger,
            config,
            readiness_service,
            intake_paths,
            intake_extensions,
        ).plan()
        autonomy_control = LocalAutonomyControlService(ledger).status()
        documents = ledger.list_documents(limit=25)
        connector_plan = LocalConnectorIntakeService(ledger, config).plan()
        photos_picker_service = LocalGooglePhotosPickerService(ledger, config)
        photos_picker_plan = photos_picker_service.plan()
        photos_picker_sessions = photos_picker_service.list_sessions(limit=10)
        sources = ledger.list_source_accounts(limit=25)
        document_groups = ledger.list_document_groups(limit=25)
        extracted_fields = ledger.list_extracted_fields(limit=40)
        duplicate_candidates = ledger.list_duplicate_candidates(limit=25)
        review_items = ledger.list_review_items(status=("pending", "in_review"), limit=12)
        review_documents = {
            item["document_id"]: ledger.get_document(int(item["document_id"]))
            for item in review_items
            if item.get("document_id")
        }
        routing_attempts = ledger.list_routing_attempts(limit=20)
        export_attempts = ledger.list_export_attempts(limit=20)
        reconciliation_matches = ledger.list_reconciliation_matches(limit=20)
        bank_transactions = ledger.list_bank_transactions(limit=25)
        bank_statement_imports = ledger.list_bank_statement_imports(limit=10)
        bookkeeping_records = ledger.list_bookkeeping_records(limit=25)
        master_ledger = LocalMasterLedgerService(ledger, config).project(limit=100)
        financial_report = LocalFinancialReportingService(ledger, config).generate()
        scheduled_report_service = LocalScheduledReportService(ledger, config)
        try:
            report_schedule_status = scheduled_report_service.schedule_status()
        except ValueError as exc:
            report_schedule_status = {
                "enabled": False,
                "status": "invalid",
                "error": str(exc),
                "externalSubmission": "not_executed",
            }
        financial_report_runs = ledger.list_financial_report_runs(limit=10)
        compliance_service = LocalComplianceService(ledger, config)
        compliance_summary = compliance_service.summary()
        compliance_assessments = ledger.list_compliance_assessments(limit=10)
        latest_compliance_assessment_id = (
            compliance_assessments[0].get("id") if compliance_assessments else None
        )
        compliance_findings = ledger.list_compliance_findings(
            assessment_id=latest_compliance_assessment_id,
            status=OPEN_FINDING_STATUSES,
            limit=25,
        ) if latest_compliance_assessment_id else []
        retention_records = ledger.list_retention_records(limit=25)
        backups = LocalBackupService(ledger, config).list_backups(limit=10)
        mijngeldzaken_service = LocalMijngeldzakenControlService(config)
        mijngeldzaken_control = mijngeldzaken_service.overview(ledger)
        mijngeldzaken_controls = mijngeldzaken_control.get("masterLedgerControls")
        wave_service = LocalWaveControlService(config)
        wave_control = wave_service.overview(ledger)
        wave_report_snapshots = ledger.list_wave_report_snapshots(limit=20)
        wave_operation_snapshots = ledger.list_wave_operation_snapshots(limit=20)
        wave_entities = ledger.list_wave_entities(limit=25)
        wave_sync_runs = ledger.list_wave_sync_runs(limit=10)
        wave_report_controls = wave_service.evaluate_report_controls(ledger)
        close_readiness = LocalCloseReadinessService(ledger, config).assess()
        close_packs = LocalClosePackService(ledger, config).list_packs(limit=10)
        rules = ledger.list_vendor_category_rules(limit=25)
        vendor_summaries = ledger.list_vendor_summaries(limit=25)
        category_summaries = ledger.list_category_summaries(limit=25)
        corrections = ledger.list_review_corrections(limit=25)
        audit_events = ledger.list_audit_events(limit=15)
        workflow_recovery_service = _workflow_recovery_service(
            ledger,
            config,
            readiness_service,
            intake_paths,
            intake_extensions,
        )
        workflow_runs = _workflow_runs_with_steps(
            ledger,
            limit=15,
            recovery_service=workflow_recovery_service,
        )
        workflow_recovery_schedule = _workflow_recovery_scheduler(
            ledger,
            config,
            readiness_service,
            intake_paths,
            intake_extensions,
        ).plan(limit=100)
        last_intake_summary = session.pop("fab_last_intake_summary", None)
        last_processing_summary = session.pop("fab_last_processing_summary", None)
        last_routing_summary = session.pop("fab_last_routing_summary", None)
        last_export_summary = session.pop("fab_last_export_summary", None)
        last_mijngeldzaken_plan = session.pop("fab_last_mijngeldzaken_plan", None)
        last_wave_plan = session.pop("fab_last_wave_plan", None)
        last_wave_entity_sync = session.pop("fab_last_wave_entity_sync", None)
        last_reconciliation_summary = session.pop("fab_last_reconciliation_summary", None)
        last_bank_import_summary = session.pop("fab_last_bank_import_summary", None)
        last_record_refresh_summary = session.pop("fab_last_record_refresh_summary", None)
        last_autonomy_summary = session.pop("fab_last_autonomy_summary", None)
        last_backup_summary = session.pop("fab_last_backup_summary", None)
        last_close_pack_summary = session.pop("fab_last_close_pack_summary", None)
        last_scheduled_report_summary = session.pop("fab_last_scheduled_report_summary", None)
        last_notification_refresh_summary = session.pop("fab_last_notification_refresh_summary", None)
        last_compliance_summary = session.pop("fab_last_compliance_summary", None)
        last_connector_sync = session.pop("fab_last_connector_sync", None)
        last_photos_picker_action = session.pop("fab_last_photos_picker_action", None)
        last_workflow_recovery = session.pop("fab_last_workflow_recovery", None)
        health_payload = {
            "status": operations_health["status"],
            "ledger_path": app.config["FAB_LOCAL_LEDGER_PATH"],
            "auth_required": bool(token),
            "host": app.config["FAB_LOCAL_API_HOST"],
            "intake_paths": app.config["FAB_LOCAL_INTAKE_PATHS"],
            "operations": operations_health,
            "readiness": readiness,
        }
        raw_payload = {
            "metrics": metrics,
            "operationsHealth": operations_health,
            "exceptions": exceptions,
            "readiness": readiness,
            "autonomyPlan": autonomy_plan,
            "connectorPlan": connector_plan,
            "photosPickerPlan": photos_picker_plan,
            "photosPickerSessions": photos_picker_sessions,
            "sources": sources,
            "documents": documents,
            "documentGroups": document_groups,
            "extractedFields": extracted_fields,
            "duplicateCandidates": duplicate_candidates,
            "reviewItems": review_items,
            "routingAttempts": routing_attempts,
            "exportAttempts": export_attempts,
            "bookkeepingRecords": bookkeeping_records,
            "masterLedger": master_ledger,
            "financialReport": financial_report,
            "reportScheduleStatus": report_schedule_status,
            "financialReportRuns": financial_report_runs,
            "notifications": notifications,
            "notificationSummary": notification_summary,
            "notificationPreferences": notification_preferences,
            "complianceSummary": compliance_summary,
            "complianceAssessments": compliance_assessments,
            "complianceFindings": compliance_findings,
            "retentionRecords": retention_records,
            "bankTransactions": bank_transactions,
            "bankStatementImports": bank_statement_imports,
            "mijngeldzakenControl": mijngeldzaken_control,
            "mijngeldzakenControls": mijngeldzaken_controls,
            "waveControl": wave_control,
            "waveReportSnapshots": wave_report_snapshots,
            "waveOperationSnapshots": wave_operation_snapshots,
            "waveEntities": wave_entities,
            "waveSyncRuns": wave_sync_runs,
            "waveReportControls": wave_report_controls,
            "closeReadiness": close_readiness,
            "closePacks": close_packs,
            "reconciliationMatches": reconciliation_matches,
            "backups": backups,
            "rules": rules,
            "vendors": vendor_summaries,
            "categories": category_summaries,
            "corrections": corrections,
            "auditEvents": audit_events,
            "workflowRuns": workflow_runs,
            "workflowRecoverySchedule": workflow_recovery_schedule,
            "health": health_payload,
            "lastIntakeSummary": last_intake_summary,
            "lastProcessingSummary": last_processing_summary,
            "lastGroupingSummary": session.get("fab_last_grouping_summary"),
            "lastRoutingSummary": last_routing_summary,
            "lastExportSummary": last_export_summary,
            "lastMijngeldzakenPlan": last_mijngeldzaken_plan,
            "lastWavePlan": last_wave_plan,
            "lastWaveEntitySync": last_wave_entity_sync,
            "lastReconciliationSummary": last_reconciliation_summary,
            "lastBankImportSummary": last_bank_import_summary,
            "lastRecordRefreshSummary": last_record_refresh_summary,
            "lastAutonomySummary": last_autonomy_summary,
            "lastBackupSummary": last_backup_summary,
            "lastClosePackSummary": last_close_pack_summary,
            "lastScheduledReportSummary": last_scheduled_report_summary,
            "lastNotificationRefreshSummary": last_notification_refresh_summary,
            "lastComplianceSummary": last_compliance_summary,
            "lastConnectorSync": last_connector_sync,
            "lastWorkflowRecovery": last_workflow_recovery,
        }
        return render_template_string(
            DASHBOARD_TEMPLATE,
            autonomy_plan=autonomy_plan,
            autonomy_control=autonomy_control,
            autonomy_summary=last_autonomy_summary,
            audit_events=audit_events,
            backup_summary=last_backup_summary,
            backups=backups,
            maintenance_mode=maintenance_mode,
            ledger_restore_mode=RESTORE_MODE_LEDGER_ONLY,
            full_restore_mode=RESTORE_MODE_FULL,
            recovery_package_format=BACKUP_FORMAT_V2,
            bank_import_summary=last_bank_import_summary,
            bank_statement_imports=bank_statement_imports,
            bank_transactions=bank_transactions,
            bookkeeping_records=bookkeeping_records,
            compact_json=_compact_json,
            connector_plan=connector_plan,
            connector_sync_summary=last_connector_sync,
            photos_picker_action=last_photos_picker_action,
            photos_picker_plan=photos_picker_plan,
            photos_picker_sessions=photos_picker_sessions,
            close_pack_summary=last_close_pack_summary,
            close_packs=close_packs,
            close_readiness=close_readiness,
            documents=documents,
            document_groups=document_groups,
            duplicate_candidates=duplicate_candidates,
            export_approval_phrase=EXPORT_APPROVAL_PHRASE,
            export_attempts=export_attempts,
            export_rejection_phrase=EXPORT_REJECTION_PHRASE,
            export_result_confirmation_phrase=EXPORT_RESULT_CONFIRMATION_PHRASE,
            export_summary=last_export_summary,
            exceptions=exceptions,
            extracted_fields=extracted_fields,
            financial_report=financial_report,
            financial_report_runs=financial_report_runs,
            format_confidence=_format_confidence,
            format_money=_format_money,
            grouping_summary=session.pop("fab_last_grouping_summary", None),
            health=health_payload,
            intake_extensions=app.config["FAB_LOCAL_INTAKE_EXTENSIONS"],
            intake_paths=app.config["FAB_LOCAL_INTAKE_PATHS"],
            intake_summary=last_intake_summary,
            ledger_name=os.path.basename(app.config["FAB_LOCAL_LEDGER_PATH"]),
            metric_cards=[
                {"label": "Documents", "value": metrics["documents"]},
                {"label": "Sources", "value": len(sources)},
                {"label": "Pending Review", "value": metrics["pending_review"]},
                {"label": "Duplicates", "value": metrics["duplicates"]},
                {"label": "Dupe Review", "value": metrics["open_duplicate_candidates"]},
                {"label": "Group Review", "value": metrics["open_document_groups"]},
                {"label": "Rule Review", "value": metrics["suggested_vendor_rules"]},
                {"label": "Unreconciled", "value": metrics["unreconciled_documents"]},
                {"label": "Bank Tx", "value": metrics["bank_transactions"]},
                {"label": "Records", "value": metrics["bookkeeping_records"]},
                {"label": "Master Rows", "value": master_ledger["summary"]["totalRows"]},
                {"label": "Export Ready", "value": metrics["export_ready_records"]},
                {"label": "Export Approvals", "value": metrics["export_attempts_needing_approval"]},
                {"label": "MGZ Rows", "value": mijngeldzaken_controls["rowCount"]},
                {"label": "MGZ Blocked", "value": mijngeldzaken_controls["blockingCount"]},
                {"label": "Unmatched Bank", "value": metrics["unreconciled_bank_transactions"]},
                {"label": "Failed", "value": metrics["failed_documents"]},
                {"label": "Wave Reports", "value": metrics["wave_report_snapshots"]},
                {"label": "Wave Ops", "value": metrics["wave_operation_snapshots"]},
                {"label": "Wave Entities", "value": metrics["wave_entities"]},
                {"label": "Report Runs", "value": metrics["financial_report_runs"]},
                {"label": "Unread Alerts", "value": metrics["unread_notifications"]},
                {"label": "Compliance", "value": compliance_summary["openFindings"]},
                {"label": "Workflow Steps", "value": metrics["workflow_steps"]},
                {"label": "Audit Events", "value": metrics["audit_events"]},
            ],
            metrics=metrics,
            master_ledger=master_ledger,
            mijngeldzaken_control=mijngeldzaken_control,
            mijngeldzaken_controls=mijngeldzaken_controls,
            mijngeldzaken_plan_summary=last_mijngeldzaken_plan,
            pretty_json=_pretty_json,
            processing_summary=last_processing_summary,
            operations_health=operations_health,
            notifications=notifications,
            notification_summary=notification_summary,
            notification_preferences=notification_preferences,
            notification_refresh_summary=last_notification_refresh_summary,
            compliance_summary=compliance_summary,
            compliance_assessments=compliance_assessments,
            compliance_findings=compliance_findings,
            retention_records=retention_records,
            last_compliance_summary=last_compliance_summary,
            readiness=readiness,
            raw_payload=_pretty_json(raw_payload),
            record_refresh_summary=last_record_refresh_summary,
            report_schedule_status=report_schedule_status,
            scheduled_report_summary=last_scheduled_report_summary,
            corrections=corrections,
            review_documents=review_documents,
            review_items=review_items,
            reconciliation_matches=reconciliation_matches,
            reconciliation_summary=last_reconciliation_summary,
            routing_attempts=routing_attempts,
            routing_summary=last_routing_summary,
            rules=rules,
            vendor_summaries=vendor_summaries,
            category_summaries=category_summaries,
            sources=sources,
            wave_control=wave_control,
            wave_account_discovery_summary=session.pop("fab_last_wave_account_discovery", None),
            wave_plan_summary=last_wave_plan,
            wave_report_snapshots=wave_report_snapshots,
            wave_operation_snapshots=wave_operation_snapshots,
            wave_entities=wave_entities,
            wave_sync_runs=wave_sync_runs,
            wave_entity_sync_summary=last_wave_entity_sync,
            wave_report_controls=wave_report_controls,
            workflow_runs=workflow_runs,
            workflow_recovery_schedule=workflow_recovery_schedule,
            workflow_recovery_summary=last_workflow_recovery,
        )

    @app.get("/api/dashboard")
    def dashboard():
        return jsonify(ledger.dashboard_metrics())

    def hai_connector() -> LocalHaiConnector:
        def rescan_intake_command(payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
            del payload, actor
            if not intake_paths:
                raise ValueError("No intake folders configured")
            return LocalFolderIntake(
                ledger,
                allowed_extensions=intake_extensions,
            ).rescan(intake_paths)

        def process_imported_command(payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
            del actor
            return LocalDocumentProcessor(ledger, config).process_imported(
                limit=_bounded_positive_int(payload.get("limit"), default=25, maximum=100)
            )

        def reprocess_incomplete_command(payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
            return LocalDocumentProcessor(ledger, config).reprocess_incomplete(
                limit=_bounded_positive_int(payload.get("limit"), default=25, maximum=100),
                actor=actor,
            )

        def reprocess_review_queue_command(payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
            return LocalDocumentProcessor(ledger, config).reprocess_review_queue(
                limit=_bounded_positive_int(payload.get("limit"), default=25, maximum=100),
                actor=actor,
            )

        def sync_sources_command(payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
            return LocalConnectorIntakeService(ledger, config).sync(
                sources=payload.get("sources"),
                actor=actor,
                trigger_source="hai_connector",
            )

        def run_safe_cycle_command(payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
            del actor
            return _autonomy_service(
                ledger,
                config,
                readiness_service,
                intake_paths,
                intake_extensions,
            ).run_cycle(
                limit=_bounded_positive_int(payload.get("limit"), default=25, maximum=100),
                include_wave_plan=True,
                include_wave_sync=True,
                dry_run=bool(payload.get("dryRun", False)),
            )

        def engage_emergency_stop_command(payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
            return LocalAutonomyControlService(ledger).engage(
                actor=actor,
                reason=str(payload.get("reason") or "HAI requested an immediate stop for operator inspection."),
                metadata={"source": "hai_connector"},
            )

        def run_due_recovery_command(payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
            return _workflow_recovery_scheduler(
                ledger,
                config,
                readiness_service,
                intake_paths,
                intake_extensions,
            ).run_due(
                actor=actor,
                limit=_bounded_positive_int(payload.get("limit"), default=5, maximum=50),
            )

        def run_reconciliation_command(payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
            del actor
            limit = _bounded_positive_int(payload.get("limit"), default=100, maximum=500)
            transactions = LocalBankTransactionImportService(
                ledger,
                config,
            ).transactions_for_reconciliation(limit=limit)
            return LocalReconciliationService(ledger, config).run(transactions, limit=limit)

        def record_wave_attachment_command(payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
            return DriveWaveDeliveryService(ledger, config).record_attachment_evidence(
                int(payload["documentId"]),
                payload.get("evidence") or {},
                actor=actor,
            )

        def archive_verified_drive_sources_command(payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
            return DriveWaveDeliveryService(ledger, config).archive_ready(
                limit=_bounded_positive_int(payload.get("limit"), default=25, maximum=100),
                actor=actor,
                dry_run=bool(payload.get("dryRun", True)),
            )

        return LocalHaiConnector(
            ledger,
            config,
            executors={
                "rescan_intake": rescan_intake_command,
                "process_imported": process_imported_command,
                "reprocess_incomplete": reprocess_incomplete_command,
                "reprocess_review_queue": reprocess_review_queue_command,
                "sync_sources": sync_sources_command,
                "run_safe_cycle": run_safe_cycle_command,
                "engage_emergency_stop": engage_emergency_stop_command,
                "run_due_recovery": run_due_recovery_command,
                "run_reconciliation": run_reconciliation_command,
                "refresh_notifications": lambda payload, actor: LocalNotificationService(
                    ledger,
                    config,
                ).refresh(actor=actor),
                "run_due_reports": lambda payload, actor: LocalScheduledReportService(
                    ledger,
                    config,
                ).run_due(actor=actor),
                "assess_compliance": lambda payload, actor: LocalComplianceService(
                    ledger,
                    config,
                ).assess(
                    from_date=payload.get("fromDate"),
                    to_date=payload.get("toDate"),
                    target_system=payload.get("targetSystem"),
                    actor=actor,
                ),
                "record_wave_attachment_verification": record_wave_attachment_command,
                "archive_verified_drive_sources": archive_verified_drive_sources_command,
            },
        )

    @app.get("/api/hai/manifest")
    def hai_manifest_api():
        return jsonify(hai_connector().manifest())

    @app.get("/api/hai/status")
    def hai_status_api():
        return jsonify(hai_connector().status())

    @app.get("/api/cloud/status")
    def cloud_status_api():
        runtime_path = config.get("fab_ngrok_runtime_path")
        result = LocalCloudAccessService(
            project_root=Path(__file__).resolve().parents[2],
            runtime_path=Path(str(runtime_path)) if runtime_path else None,
            api_token_configured=bool(token),
            shared_inspector_url=config.get("fab_ngrok_shared_inspector_url"),
        ).summarize()
        if maintenance_mode:
            result.update({
                "status": "disabled_for_maintenance",
                "active": False,
                "publicUrl": None,
                "haiManifestUrl": None,
                "maintenanceMode": True,
                "nextAction": (
                    "Complete or cancel local recovery and restart FAB in standard mode "
                    "before enabling cloud access."
                ),
                "externalSubmission": "not_executed",
            })
        else:
            result["maintenanceMode"] = False
        return jsonify(result)

    @app.post("/api/hai/commands/plan")
    def hai_command_plan_api():
        payload = request.get_json(silent=True) or {}
        command_payload = payload.get("payload")
        result = hai_connector().plan(
            str(payload.get("commandId") or ""),
            {} if command_payload is None else command_payload,
        )
        status_code = 200 if result.get("success") else 400
        if result.get("status") in {"connector_disabled", "not_allowed"}:
            status_code = 403
        return jsonify(result), status_code

    @app.post("/api/hai/commands/execute")
    def hai_command_execute_api():
        payload = request.get_json(silent=True) or {}
        command_payload = payload.get("payload")
        # Attribute HTTP commands to the verified credential class, not a body label.
        actor = "fab_hai_api:" + getattr(g, "fab_api_principal", "loopback")
        result = hai_connector().execute(
            request_id=payload.get("requestId"),
            command_id=str(payload.get("commandId") or ""),
            payload={} if command_payload is None else command_payload,
            actor=actor,
        )
        status = result.get("status")
        if result.get("success"):
            status_code = 200
        elif status in {"connector_disabled", "not_allowed"}:
            status_code = 403
        elif status == "failed":
            status_code = 500
        elif status in {
            "execution_in_progress",
            "execution_indeterminate",
            "idempotency_conflict",
            "previously_failed",
        }:
            status_code = 409
        else:
            status_code = 400
        return jsonify(result), status_code

    @app.get("/api/drive-wave/status")
    def drive_wave_status_api():
        return jsonify(DriveWaveDeliveryService(ledger, config).status())

    @app.get("/api/wave/receipt-executor/status")
    def wave_receipt_executor_status_api():
        return jsonify(LocalWaveReceiptExecutorService(ledger, config).status())

    @app.post("/api/wave/receipt-executor/session")
    def wave_receipt_executor_session_api():
        payload = request.get_json(silent=True) or {}
        session_payload = {key: value for key, value in payload.items() if key != "actor"}
        result = LocalWaveReceiptExecutorService(ledger, config).register(
            session_payload,
            actor=str(payload.get("actor") or "local_api_wave_receipt_executor"),
        )
        return jsonify(result), 200 if result.get("success") else 400

    @app.delete("/api/wave/receipt-executor/session")
    def wave_receipt_executor_disconnect_api():
        payload = request.get_json(silent=True) or {}
        result = LocalWaveReceiptExecutorService(ledger, config).disconnect(
            payload,
            actor=str(payload.get("actor") or "local_api"),
        )
        return jsonify(result), 200 if result.get("success") else 400

    @app.post("/api/wave/receipt-executor/claim")
    def wave_receipt_executor_claim_api():
        payload = request.get_json(silent=True) or {}
        result = LocalWaveReceiptExecutorService(ledger, config).claim_next(
            DriveWaveDeliveryService(ledger, config),
            payload,
            actor=str(payload.get("actor") or "local_api_wave_receipt_executor"),
        )
        return jsonify(result), 200 if result.get("success") else 409

    @app.post("/api/wave/receipt-executor/release")
    def wave_receipt_executor_release_api():
        payload = request.get_json(silent=True) or {}
        result = LocalWaveReceiptExecutorService(ledger, config).release(
            payload.get("documentId"),
            payload,
            actor=str(payload.get("actor") or "local_api_wave_receipt_executor"),
        )
        return jsonify(result), 200 if result.get("success") else 400

    @app.get("/api/drive-wave/candidates")
    def drive_wave_candidates_api():
        return jsonify(
            DriveWaveDeliveryService(ledger, config).list_candidates(limit=_limit_arg())
        )

    @app.get("/api/drive-wave/work-orders")
    def drive_wave_work_orders_api():
        view = str(request.args.get("view") or "complete").strip().lower()
        if view not in {"complete", "summary"}:
            return jsonify({
                "success": False,
                "status": "unsupported_view",
                "supportedViews": ["complete", "summary"],
            }), 400
        item_limit = request.args.get("itemsLimit")
        if item_limit is not None:
            try:
                item_limit = max(0, min(int(item_limit), 200))
            except (TypeError, ValueError):
                return jsonify({"error": "itemsLimit must be an integer from 0 to 200"}), 400
        return jsonify(
            DriveWaveDeliveryService(ledger, config).list_work_orders(
                limit=_limit_arg(),
                compact=view == "summary",
                item_limit=item_limit,
            )
        )

    @app.get("/api/drive-wave/documents/<int:document_id>/work-order")
    def drive_wave_document_work_order_api(document_id: int):
        result = DriveWaveDeliveryService(ledger, config).work_order(document_id)
        return jsonify(result), 200 if result.get("success") else 404

    @app.get("/api/drive-wave/documents/<int:document_id>/archive-plan")
    def drive_wave_archive_plan_api(document_id: int):
        return jsonify(DriveWaveDeliveryService(ledger, config).plan_archive(document_id))

    @app.post("/api/drive-wave/documents/<int:document_id>/attachment-evidence")
    def drive_wave_attachment_evidence_api(document_id: int):
        payload = request.get_json(silent=True) or {}
        result = DriveWaveDeliveryService(ledger, config).record_attachment_evidence(
            document_id,
            payload.get("evidence") or payload,
            actor=str(payload.get("actor") or "local_api"),
        )
        return jsonify(result), 200 if result.get("success") else 400

    @app.post("/api/drive-wave/documents/<int:document_id>/attachment-readback")
    def drive_wave_attachment_readback_api(document_id: int):
        attachment = request.files.get("attachment")
        if attachment is None:
            return jsonify({"error": "Multipart file field 'attachment' is required"}), 400
        evidence_text = request.form.get("evidence") or "{}"
        try:
            evidence = json.loads(evidence_text)
        except (TypeError, ValueError):
            return jsonify({"error": "evidence must be a valid JSON object"}), 400
        if not isinstance(evidence, dict):
            return jsonify({"error": "evidence must be a valid JSON object"}), 400
        content = attachment.stream.read(WAVE_RECEIPT_MAX_BYTES + 1)
        if len(content) > WAVE_RECEIPT_MAX_BYTES:
            return jsonify({
                "success": False,
                "status": "blocked",
                "reasons": ["wave_attachment_readback_exceeds_limit"],
                "maxBytes": WAVE_RECEIPT_MAX_BYTES,
                "externalSubmission": "not_executed",
            }), 413
        result = DriveWaveDeliveryService(ledger, config).record_attachment_readback(
            document_id,
            content,
            filename=attachment.filename,
            mime_type=attachment.mimetype,
            evidence=evidence,
            actor=str(request.form.get("actor") or "local_api_wave_browser"),
        )
        executor_id = str(request.form.get("executorId") or "").strip()
        session_id = str(request.form.get("sessionId") or "").strip()
        if executor_id and session_id:
            result["executorRelease"] = LocalWaveReceiptExecutorService(
                ledger,
                config,
            ).release(
                document_id,
                {
                    "executorId": executor_id,
                    "sessionId": session_id,
                    "outcome": "verified" if result.get("success") else "blocked",
                    "message": (
                        "Wave receipt binary readback passed FAB verification."
                        if result.get("success")
                        else "Wave receipt binary readback was blocked by FAB verification."
                    ),
                },
                actor=str(request.form.get("actor") or "local_api_wave_browser"),
            )
        return jsonify(result), 200 if result.get("success") else 400

    @app.post("/api/drive-wave/documents/<int:document_id>/archive")
    def drive_wave_archive_document_api(document_id: int):
        payload = request.get_json(silent=True) or {}
        if bool(payload.get("dryRun", False)):
            return jsonify(DriveWaveDeliveryService(ledger, config).plan_archive(document_id))
        result = DriveWaveDeliveryService(ledger, config).archive_document(
            document_id,
            actor=str(payload.get("actor") or "local_api"),
        )
        return jsonify(result), 200 if result.get("success") else 409

    @app.get("/api/workflows")
    def workflow_runs_api():
        return jsonify({
            "workflowRuns": ledger.list_workflow_runs(
                status=request.args.get("status"),
                trigger_source=request.args.get("triggerSource") or request.args.get("trigger_source"),
                limit=_limit_arg(),
            )
        })

    @app.get("/api/workflows/recovery")
    def workflow_recovery_schedule_api():
        return jsonify(_workflow_recovery_scheduler(
            ledger,
            config,
            readiness_service,
            intake_paths,
            intake_extensions,
        ).plan(limit=_limit_arg()))

    @app.post("/api/workflows/recovery/run-due")
    def run_due_workflow_recovery_api():
        payload = request.get_json(silent=True) or {}
        result = _workflow_recovery_scheduler(
            ledger,
            config,
            readiness_service,
            intake_paths,
            intake_extensions,
        ).run_due(
            actor=str(payload.get("actor") or "api")[:200],
            limit=_bounded_positive_int(payload.get("limit"), default=5, maximum=50),
        )
        return jsonify(result), 409 if result.get("status") == "already_running" else 200

    @app.post("/workflows/recovery/run-due")
    def run_due_workflow_recovery_form():
        result = _workflow_recovery_scheduler(
            ledger,
            config,
            readiness_service,
            intake_paths,
            intake_extensions,
        ).run_due(actor="local_user", limit=5)
        session["fab_last_workflow_recovery"] = _compact_scheduled_workflow_recovery(result)
        return redirect(url_for("dashboard_page", _anchor="workflows"))

    @app.get("/api/workflows/<int:workflow_run_id>")
    def workflow_detail_api(workflow_run_id: int):
        workflow_run = ledger.get_workflow_run_with_steps(workflow_run_id)
        if workflow_run is None:
            return jsonify({"error": "Workflow run not found"}), 404
        workflow_run["stepSummary"] = _workflow_step_summary(workflow_run.get("steps") or [])
        return jsonify(workflow_run)

    @app.get("/api/workflows/<int:workflow_run_id>/recovery-plan")
    def workflow_recovery_plan_api(workflow_run_id: int):
        recovery_plan = _workflow_recovery_service(
            ledger,
            config,
            readiness_service,
            intake_paths,
            intake_extensions,
        ).plan(workflow_run_id)
        return jsonify(recovery_plan), 404 if recovery_plan.get("status") == "not_found" else 200

    @app.post("/api/workflows/<int:workflow_run_id>/retry")
    def retry_workflow_api(workflow_run_id: int):
        payload = request.get_json(silent=True) or {}
        result = _workflow_recovery_service(
            ledger,
            config,
            readiness_service,
            intake_paths,
            intake_extensions,
        ).retry(
            workflow_run_id,
            actor=str(payload.get("actor") or "api")[:200],
            limit=_bounded_positive_int(payload.get("limit"), default=25, maximum=100),
        )
        if result.get("workflowRunId"):
            status_code = 200
        elif result.get("status") == "not_found":
            status_code = 404
        else:
            status_code = 409
        return jsonify(result), status_code

    @app.post("/workflows/<int:workflow_run_id>/retry")
    def retry_workflow_form(workflow_run_id: int):
        result = _workflow_recovery_service(
            ledger,
            config,
            readiness_service,
            intake_paths,
            intake_extensions,
        ).retry(workflow_run_id, actor="local_user", limit=25)
        session["fab_last_workflow_recovery"] = _compact_workflow_recovery(result)
        return redirect(url_for("dashboard_page", _anchor="workflows"))

    @app.get("/api/notifications")
    def notifications_api():
        return jsonify({
            "notifications": LocalNotificationService(ledger, config).list_notifications(
                status=request.args.get("status"),
                severity=request.args.get("severity"),
                event_type=request.args.get("eventType") or request.args.get("event_type"),
                limit=_limit_arg(),
            ),
            "summary": LocalNotificationService(ledger, config).summary(),
            "externalDelivery": "not_executed",
        })

    @app.post("/api/notifications/refresh")
    def refresh_notifications_api():
        payload = request.get_json(silent=True) or {}
        return jsonify(LocalNotificationService(ledger, config).refresh(
            actor=payload.get("actor") or "local_api",
        ))

    @app.post("/notifications/refresh")
    def refresh_notifications_form():
        session["fab_last_notification_refresh_summary"] = LocalNotificationService(
            ledger,
            config,
        ).refresh(actor="local_dashboard")
        return redirect(url_for("dashboard_page", _anchor="notifications"))

    @app.route("/api/notifications/<int:notification_id>/status", methods=["POST", "PATCH"])
    def notification_status_api(notification_id: int):
        payload = request.get_json(silent=True) or {}
        try:
            result = LocalNotificationService(ledger, config).update_status(
                notification_id,
                payload.get("status"),
                actor=payload.get("actor") or "local_api",
            )
        except ValueError as exc:
            return jsonify({"success": False, "status": "invalid", "error": str(exc)}), 400
        return jsonify(result), 200 if result.get("success") else 404

    @app.post("/notifications/<int:notification_id>/status")
    def notification_status_form(notification_id: int):
        try:
            LocalNotificationService(ledger, config).update_status(
                notification_id,
                request.form.get("status"),
                actor="local_dashboard",
            )
        except ValueError as exc:
            session["fab_last_notification_refresh_summary"] = {
                "success": False,
                "status": "invalid",
                "error": str(exc),
            }
        return redirect(url_for("dashboard_page", _anchor="notifications"))

    @app.get("/api/notification-preferences")
    def notification_preferences_api():
        return jsonify({
            "notificationPreferences": ledger.list_notification_preferences(limit=_limit_arg()),
            "externalDelivery": "disabled",
        })

    @app.post("/api/notification-preferences")
    def update_notification_preference_api():
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(LocalNotificationService(ledger, config).update_preference(
                payload,
                actor=payload.get("actor") or "local_api",
            ))
        except ValueError as exc:
            return jsonify({"success": False, "status": "invalid", "error": str(exc)}), 400

    @app.post("/notification-preferences")
    def notification_preference_form():
        try:
            LocalNotificationService(ledger, config).update_preference({
                "eventType": request.form.get("eventType"),
                "enabled": "enabled" in request.form,
                "inAppEnabled": "inAppEnabled" in request.form,
                "minimumSeverity": request.form.get("minimumSeverity") or "low",
            }, actor="local_dashboard")
        except ValueError as exc:
            session["fab_last_notification_refresh_summary"] = {
                "success": False,
                "status": "invalid_preference",
                "error": str(exc),
            }
        return redirect(url_for("dashboard_page", _anchor="notifications"))

    @app.get("/api/exceptions")
    def exceptions_api():
        include_entities = _bool_value(request.args.get("includeEntities"), default=True)
        return jsonify(LocalExceptionQueueService(ledger, config).list_exceptions(
            limit=_limit_arg(),
            include_entities=include_entities,
        ))

    @app.get("/api/close-readiness")
    def close_readiness_api():
        workflow_id = request.args.get("workflowId") or request.args.get("workflow_id") or "daily_reconciliation_run"
        return jsonify(LocalCloseReadinessService(ledger, config).assess(
            workflow_id=workflow_id,
            from_date=request.args.get("fromDate") or request.args.get("from_date"),
            to_date=request.args.get("toDate") or request.args.get("to_date"),
        ))

    @app.get("/api/close-packs")
    def close_packs_api():
        return jsonify(LocalClosePackService(ledger, config).list_packs(limit=_limit_arg()))

    @app.post("/api/close-packs")
    def prepare_close_pack_api():
        payload = request.get_json(silent=True) or {}
        result = LocalClosePackService(ledger, config).prepare(
            workflow_id=payload.get("workflowId") or payload.get("workflow_id") or "daily_reconciliation_run",
            from_date=payload.get("fromDate") or payload.get("from_date"),
            to_date=payload.get("toDate") or payload.get("to_date"),
            actor=payload.get("actor") or "local_api",
            require_ready=_bool_value(payload.get("requireReady"), default=True),
        )
        status_code = 200 if result.get("success") else 400
        return jsonify(result), status_code

    @app.get("/api/close-packs/inspect")
    def inspect_close_pack_api():
        close_pack_path = request.args.get("closePackPath") or request.args.get("closePackFilename")
        try:
            return jsonify(LocalClosePackService(ledger, config).inspect_pack(str(close_pack_path or "")))
        except Exception as exc:
            return jsonify({"success": False, "status": "invalid", "error": str(exc)}), 400

    @app.post("/close-packs/prepare")
    def prepare_close_pack_form():
        session["fab_last_close_pack_summary"] = LocalClosePackService(ledger, config).prepare(
            actor="dashboard",
        )
        return redirect(url_for("dashboard_page", _anchor="close"))

    @app.get("/api/document-groups")
    def document_groups_api():
        document_id = request.args.get("documentId") or request.args.get("document_id")
        try:
            parsed_document_id = int(document_id) if document_id else None
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid documentId"}), 400
        return jsonify({
            "documentGroups": ledger.list_document_groups(
                status=request.args.get("status"),
                group_type=request.args.get("groupType") or request.args.get("group_type"),
                document_id=parsed_document_id,
                limit=_limit_arg(),
            )
        })

    @app.post("/api/document-groups/detect")
    def detect_document_groups_api():
        payload = request.get_json(silent=True) or {}
        return jsonify(LocalDocumentGroupingService(ledger, config).detect_scanner_groups(
            limit=_bounded_positive_int(payload.get("limit"), default=100, maximum=500),
        ))

    @app.post("/document-groups/detect")
    def detect_document_groups_form():
        session["fab_last_grouping_summary"] = LocalDocumentGroupingService(ledger, config).detect_scanner_groups(limit=100)
        return redirect(url_for("dashboard_page", _anchor="groups"))

    @app.post("/api/document-groups/merge")
    def merge_document_group_api():
        payload = request.get_json(silent=True) or {}
        result = LocalDocumentGroupingService(ledger, config).merge_documents(
            payload.get("documentIds") or payload.get("document_ids") or [],
            title=payload.get("title"),
            reason=payload.get("reason"),
            actor=payload.get("actor") or "api",
        )
        status_code = 200 if result.get("success") else 400
        if result.get("status") == "not_found":
            status_code = 404
        return jsonify(result), status_code

    @app.post("/api/document-groups/<int:group_id>/split")
    def split_document_group_api(group_id: int):
        payload = request.get_json(silent=True) or {}
        try:
            document_id = int(payload.get("documentId") or payload.get("document_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "documentId is required"}), 400
        result = LocalDocumentGroupingService(ledger, config).split_document_from_group(
            group_id,
            document_id,
            reason=payload.get("reason"),
            actor=payload.get("actor") or "api",
        )
        status_code = 200 if result.get("success") else 400
        if result.get("status") == "not_found":
            status_code = 404
        return jsonify(result), status_code

    @app.get("/api/sources")
    def sources():
        return jsonify({
            "sources": ledger.list_source_accounts(
                source_type=request.args.get("sourceType") or request.args.get("source_type"),
                status=request.args.get("status"),
                limit=_limit_arg(),
            )
        })

    @app.get("/api/sources/readiness")
    def source_connector_readiness():
        return jsonify(LocalConnectorIntakeService(ledger, config).plan())

    @app.get("/api/sources/google-photos/sessions")
    def google_photos_picker_sessions_api():
        service = LocalGooglePhotosPickerService(ledger, config)
        return jsonify({
            "plan": service.plan(),
            "sessions": service.list_sessions(limit=_limit_arg()),
            "externalSubmission": "not_executed",
        })

    @app.post("/api/sources/google-photos/sessions")
    def start_google_photos_picker_api():
        payload = request.get_json(silent=True) or {}
        try:
            result = LocalGooglePhotosPickerService(ledger, config).create_session(
                actor=payload.get("actor") or "api",
            )
        except ValueError as exc:
            return jsonify({"error": str(exc), "externalSubmission": "not_executed"}), 409
        return jsonify(result), 200 if result.get("success") else 502

    @app.get("/api/sources/google-photos/sessions/<int:workflow_run_id>")
    def google_photos_picker_session_api(workflow_run_id: int):
        result = LocalGooglePhotosPickerService(ledger, config).get_session(workflow_run_id)
        if not result:
            return jsonify({"error": "Google Photos Picker session not found"}), 404
        return jsonify({"session": result, "externalSubmission": "not_executed"})

    @app.post("/api/sources/google-photos/sessions/<int:workflow_run_id>/collect")
    def collect_google_photos_picker_api(workflow_run_id: int):
        payload = request.get_json(silent=True) or {}
        try:
            result = LocalGooglePhotosPickerService(ledger, config).collect_session(
                workflow_run_id,
                actor=payload.get("actor") or "api",
            )
        except LookupError as exc:
            return jsonify({"error": str(exc), "externalSubmission": "not_executed"}), 404
        return jsonify(result), 200 if result.get("success") else 502

    @app.post("/api/sources/google-photos/sessions/<int:workflow_run_id>/cancel")
    def cancel_google_photos_picker_api(workflow_run_id: int):
        payload = request.get_json(silent=True) or {}
        try:
            result = LocalGooglePhotosPickerService(ledger, config).cancel_session(
                workflow_run_id,
                actor=payload.get("actor") or "api",
            )
        except LookupError as exc:
            return jsonify({"error": str(exc), "externalSubmission": "not_executed"}), 404
        return jsonify(result), 200 if result.get("success") else 502

    @app.post("/api/sources/sync")
    def sync_sources_api():
        payload = request.get_json(silent=True) or {}
        try:
            result = LocalConnectorIntakeService(ledger, config).sync(
                sources=payload.get("sources"),
                actor=payload.get("actor") or "api",
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(result)

    @app.post("/sources/sync")
    def sync_sources_form():
        try:
            result = LocalConnectorIntakeService(ledger, config).sync(
                actor="local_user",
            )
            session["fab_last_connector_sync"] = _compact_connector_sync(result)
        except ValueError as exc:
            session["fab_last_connector_sync"] = {
                "success": False,
                "status": "invalid_request",
                "error": str(exc),
                "externalSubmission": "not_executed",
            }
        return redirect(url_for("dashboard_page") + "#sources")

    @app.post("/sources/google-photos/sessions/start")
    def start_google_photos_picker_form():
        try:
            result = LocalGooglePhotosPickerService(ledger, config).create_session(
                actor="local_user",
            )
        except ValueError as exc:
            result = {
                "success": False,
                "status": "not_ready",
                "error": str(exc),
                "externalSubmission": "not_executed",
            }
        session["fab_last_photos_picker_action"] = _compact_photos_picker_result(result)
        return redirect(url_for("dashboard_page") + "#sources")

    @app.post("/sources/google-photos/sessions/<int:workflow_run_id>/collect")
    def collect_google_photos_picker_form(workflow_run_id: int):
        try:
            result = LocalGooglePhotosPickerService(ledger, config).collect_session(
                workflow_run_id,
                actor="local_user",
            )
        except LookupError as exc:
            result = {
                "success": False,
                "status": "not_found",
                "error": str(exc),
                "externalSubmission": "not_executed",
            }
        session["fab_last_photos_picker_action"] = _compact_photos_picker_result(result)
        return redirect(url_for("dashboard_page") + "#sources")

    @app.post("/sources/google-photos/sessions/<int:workflow_run_id>/cancel")
    def cancel_google_photos_picker_form(workflow_run_id: int):
        try:
            result = LocalGooglePhotosPickerService(ledger, config).cancel_session(
                workflow_run_id,
                actor="local_user",
            )
        except LookupError as exc:
            result = {
                "success": False,
                "status": "not_found",
                "error": str(exc),
                "externalSubmission": "not_executed",
            }
        session["fab_last_photos_picker_action"] = _compact_photos_picker_result(result)
        return redirect(url_for("dashboard_page") + "#sources")

    @app.post("/api/sources")
    def upsert_source():
        payload = request.get_json(silent=True) or {}
        try:
            source_account_id = ledger.upsert_source_account(payload)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        ledger.record_audit_event({
            "action": "local_api.source.upsert",
            "entityType": "source_account",
            "entityId": str(source_account_id),
            "details": {
                "sourceType": payload.get("sourceType") or payload.get("source_type"),
                "sourceIdentifier": payload.get("sourceIdentifier") or payload.get("source_identifier"),
                "status": payload.get("status") or "active",
                "metadataKeys": sorted((payload.get("metadata") or {}).keys())
                if isinstance(payload.get("metadata"), dict)
                else [],
            },
        })
        return jsonify({"success": True, "sourceAccountId": source_account_id})

    @app.get("/api/settings")
    def settings():
        return jsonify(readiness_service.summarize())

    @app.get("/api/doctor")
    def doctor():
        return jsonify(support_bundle_service.doctor())

    @app.post("/api/support-bundles")
    def create_support_bundle():
        payload = request.get_json(silent=True) or {}
        result = support_bundle_service.create(
            actor=str(payload.get("actor") or "fab_local_api")[:200],
            note=str(payload.get("note") or "")[:500],
        )
        return jsonify(result), 201

    @app.get("/api/autonomy/plan")
    def autonomy_plan():
        limit = _bounded_positive_int(request.args.get("limit"), default=25, maximum=100)
        include_wave_plan = _bool_value(request.args.get("includeWavePlan"), default=True)
        include_wave_sync = _bool_value(request.args.get("includeWaveSync"), default=True)
        include_connector_sync = _bool_value(request.args.get("includeConnectorSync"), default=True)
        with ledger.read_snapshot():
            plan = _autonomy_service(
                ledger,
                config,
                readiness_service,
                intake_paths,
                intake_extensions,
            ).plan(
                limit=limit,
                include_wave_plan=include_wave_plan,
                include_wave_sync=include_wave_sync,
                include_connector_sync=include_connector_sync,
            )
        return jsonify(plan)

    @app.post("/api/autonomy/run")
    def run_autonomy():
        payload = request.get_json(silent=True) or {}
        bank_transactions = payload.get("bankTransactions") if "bankTransactions" in payload else None
        if bank_transactions is not None and not isinstance(bank_transactions, list):
            return jsonify({"error": "bankTransactions must be a list"}), 400
        result = _autonomy_service(
            ledger,
            config,
            readiness_service,
            intake_paths,
            intake_extensions,
        ).run_cycle(
            limit=_bounded_positive_int(payload.get("limit"), default=25, maximum=100),
            bank_transactions=bank_transactions,
            include_wave_plan=_bool_value(payload.get("includeWavePlan"), default=True),
            include_wave_sync=_bool_value(payload.get("includeWaveSync"), default=True),
            include_connector_sync=_bool_value(payload.get("includeConnectorSync"), default=True),
            dry_run=bool(payload.get("dryRun", False)),
        )
        if result.get("status") == "already_running":
            status_code = 409
        elif result.get("status") == "blocked":
            status_code = 400
        else:
            status_code = 200
        return jsonify(result), status_code

    @app.get("/api/autonomy/emergency-stop")
    def autonomy_emergency_stop_status():
        return jsonify(LocalAutonomyControlService(ledger).status())

    @app.post("/api/autonomy/emergency-stop")
    def engage_autonomy_emergency_stop():
        payload = request.get_json(silent=True) or {}
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            return jsonify({"error": "reason is required"}), 400
        result = LocalAutonomyControlService(ledger).engage(
            actor=str(payload.get("actor") or "fab_local_api")[:200],
            reason=reason,
            metadata={"source": "local_api"},
        )
        return jsonify(result)

    @app.delete("/api/autonomy/emergency-stop")
    def clear_autonomy_emergency_stop():
        payload = request.get_json(silent=True) or {}
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            return jsonify({"error": "reason is required"}), 400
        result = LocalAutonomyControlService(ledger).clear(
            actor=str(payload.get("actor") or "fab_local_api")[:200],
            reason=reason,
            confirmation=str(payload.get("confirmation") or ""),
        )
        status_code = 200 if result.get("success") else 409 if result.get("status") == "cycle_still_active" else 400
        return jsonify(result), status_code

    @app.post("/autonomy/run")
    def run_autonomy_form():
        session["fab_last_autonomy_summary"] = _autonomy_service(
            ledger,
            config,
            readiness_service,
            intake_paths,
            intake_extensions,
        ).run_cycle(
            limit=25,
            include_wave_plan=True,
            include_wave_sync=True,
            include_connector_sync=True,
        )
        return redirect(url_for("dashboard_page", _anchor="autonomy"))

    @app.post("/autonomy/emergency-stop")
    def engage_autonomy_emergency_stop_form():
        LocalAutonomyControlService(ledger).engage(
            actor="fab_dashboard:local_operator",
            reason=str(request.form.get("reason") or "Operator emergency stop"),
            metadata={"source": "legacy_dashboard"},
        )
        return redirect(url_for("dashboard_page", _anchor="autonomy"))

    @app.post("/autonomy/emergency-stop/clear")
    def clear_autonomy_emergency_stop_form():
        session["fab_last_autonomy_summary"] = LocalAutonomyControlService(ledger).clear(
            actor="fab_dashboard:local_operator",
            reason=str(request.form.get("reason") or "Operator resumed autonomous processing"),
            confirmation=str(request.form.get("confirmation") or ""),
        )
        return redirect(url_for("dashboard_page", _anchor="autonomy"))

    @app.get("/api/wave")
    def wave_overview():
        return jsonify(LocalWaveControlService(config).overview(ledger))

    @app.get("/api/wave/setup")
    def wave_setup_status():
        target_system = str(
            request.args.get("targetSystem")
            or request.args.get("target_system")
            or "waveapps_business"
        )
        result = LocalWaveSetupService(config).status(ledger, target_system)
        return jsonify(result), 200 if result.get("success") else 400

    @app.put("/api/wave/setup")
    def save_wave_setup():
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"error": "Wave setup payload must be an object"}), 400
        allowed = {
            "targetSystem", "accessToken", "businessId", "anchorAccountId",
            "defaultCategoryAccountId", "categoryAccountIds", "clearAccessToken", "actor",
        }
        unexpected = sorted(set(payload) - allowed)
        if unexpected:
            return jsonify({"error": f"Unsupported Wave setup field(s): {', '.join(unexpected)}"}), 400
        target_system = payload.get("targetSystem", "waveapps_business")
        if target_system not in {"waveapps_business", "waveapps_personal"}:
            return jsonify({"error": "targetSystem must be waveapps_business or waveapps_personal"}), 400
        for field in ("accessToken", "businessId", "anchorAccountId", "defaultCategoryAccountId", "actor"):
            if field in payload and not isinstance(payload[field], str):
                return jsonify({"error": f"{field} must be a string"}), 400
        category_mapping = payload.get("categoryAccountIds")
        if category_mapping is not None and (
            not isinstance(category_mapping, dict)
            or len(category_mapping) > 250
            or any(not isinstance(key, str) or not isinstance(value, str) for key, value in category_mapping.items())
        ):
            return jsonify({"error": "categoryAccountIds must be an object of string account IDs with at most 250 entries"}), 400
        if payload.get("clearAccessToken") not in (None, True, False):
            return jsonify({"error": "clearAccessToken must be a boolean"}), 400
        if payload.get("clearAccessToken") is True and "accessToken" in payload:
            return jsonify({"error": "accessToken and clearAccessToken cannot be supplied together"}), 400
        try:
            result = LocalWaveSetupService(config).save(
                ledger,
                payload,
                actor=str(payload.get("actor") or "local_api_wave_setup"),
            )
        except (ValueError, RuntimeError) as exc:
            return jsonify({"success": False, "status": "blocked", "error": str(exc)}), 400
        return jsonify(result)

    @app.get("/api/wave/actions")
    def wave_actions():
        return jsonify(LocalWaveControlService(config).actions(
            surface=request.args.get("surface"),
            safety=request.args.get("safety"),
            mode=request.args.get("mode"),
        ))

    @app.get("/api/wave/account-mappings")
    def wave_account_mappings():
        return jsonify(WaveappsAccountDiscoveryService(config).mapping_status(
            request.args.get("targetSystem") or request.args.get("target_system"),
        ))

    def run_wave_account_discovery(target_system: str) -> Dict[str, Any]:
        result = WaveappsAccountDiscoveryService(config).discover(target_system)
        operation = result.get("operation") if isinstance(result.get("operation"), dict) else None
        if operation:
            operation["metadata"] = {
                "accountDiscovery": {
                    "targetSystem": result.get("targetSystem"),
                    "business": result.get("business"),
                    "accounts": result.get("accounts"),
                    "mapping": result.get("mapping"),
                    "externalSubmission": "not_executed",
                },
            }
            LocalWaveControlService(config).record_operation_snapshot(
                ledger,
                operation,
                workflow_id="wave_account_discovery",
                status=result.get("status") or "read_result_captured",
            )
        ledger.record_audit_event({
            "action": "local_wave.account_discovery_read",
            "entityType": "wave_account_discovery",
            "entityId": str((operation or {}).get("operation_id") or target_system),
            "details": {
                "targetSystem": target_system,
                "success": result.get("success"),
                "status": result.get("status"),
                "accountCount": len(result.get("accounts") or []),
                "externalSubmission": "not_executed",
            },
        })
        return result

    @app.post("/api/wave/setup/validate")
    def validate_wave_setup():
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"error": "Wave validation payload must be an object"}), 400
        unexpected = sorted(set(payload) - {"targetSystem"})
        if unexpected:
            return jsonify({"error": f"Unsupported Wave validation field(s): {', '.join(unexpected)}"}), 400
        target_system = str(payload.get("targetSystem") or "waveapps_business")
        if target_system not in {"waveapps_business", "waveapps_personal"}:
            return jsonify({"error": "targetSystem must be waveapps_business or waveapps_personal"}), 400
        discovery = run_wave_account_discovery(target_system)
        result = {
            "success": bool(discovery.get("success")),
            "status": discovery.get("status"),
            "discovery": discovery,
            "setup": LocalWaveSetupService(config).status(ledger, target_system),
        }
        if not result["success"]:
            result["error"] = discovery.get("message") or "Wave validation failed."
            result["nextAction"] = discovery.get("nextAction")
        status_code = _wave_discovery_http_status(discovery)
        return jsonify(result), status_code

    @app.post("/api/wave/accounts/discover")
    def discover_wave_accounts():
        payload = request.get_json(silent=True) or {}
        target_system = str(payload.get("targetSystem") or payload.get("target_system") or "waveapps_business")
        result = run_wave_account_discovery(target_system)
        status_code = _wave_discovery_http_status(result)
        return jsonify(result), status_code

    @app.post("/wave/accounts/discover")
    def discover_wave_accounts_form():
        target_system = str(request.form.get("targetSystem") or "waveapps_business")
        result = run_wave_account_discovery(target_system)
        session["fab_last_wave_account_discovery"] = {
            "success": result.get("success"),
            "status": result.get("status"),
            "targetSystem": target_system,
            "business": result.get("business"),
            "accounts": result.get("accounts"),
            "mapping": result.get("mapping"),
            "message": result.get("message"),
            "externalSubmission": "not_executed",
        }
        return redirect(url_for("dashboard_page", _anchor="wave"))

    def run_wave_entity_sync(
        target_system: str,
        entity_types: Any,
        page_size: int,
        max_pages: int,
        actor: str,
    ) -> Dict[str, Any]:
        result = WaveappsEntitySyncService(config).sync(
            ledger,
            target_system,
            entity_types=entity_types,
            page_size=page_size,
            max_pages=max_pages,
        )
        ledger.record_audit_event({
            "action": "local_wave.entity_sync_completed",
            "entityType": "wave_sync_run",
            "entityId": str(result.get("syncRunId") or target_system),
            "details": {
                "actor": actor,
                "targetSystem": target_system,
                "entityTypes": result.get("entityTypes") or entity_types,
                "success": result.get("success"),
                "status": result.get("status"),
                "pagesFetched": result.get("pagesFetched"),
                "entitiesSeen": result.get("entitiesSeen"),
                "missingMarked": result.get("missingMarked"),
                "externalSubmission": "not_executed",
            },
        })
        return result

    @app.get("/api/wave/entities")
    def wave_entities_api():
        return jsonify({
            "waveEntities": ledger.list_wave_entities(
                target_system=request.args.get("targetSystem") or request.args.get("target_system"),
                entity_type=request.args.get("entityType") or request.args.get("entity_type"),
                status=request.args.get("status"),
                presence_status=request.args.get("presenceStatus") or request.args.get("presence_status"),
                limit=_limit_arg(),
            )
        })

    @app.get("/api/wave/entity-sync-runs")
    def wave_entity_sync_runs_api():
        return jsonify({
            "waveSyncRuns": ledger.list_wave_sync_runs(
                target_system=request.args.get("targetSystem") or request.args.get("target_system"),
                status=request.args.get("status"),
                limit=_limit_arg(),
            )
        })

    @app.post("/api/wave/entities/sync")
    def sync_wave_entities_api():
        payload = request.get_json(silent=True) or {}
        entity_types = payload.get("entityTypes") or payload.get("entity_types")
        if entity_types is not None and not isinstance(entity_types, (list, str)):
            return jsonify({"error": "entityTypes must be a list or comma-separated string"}), 400
        target_system = str(payload.get("targetSystem") or payload.get("target_system") or "waveapps_business")
        result = run_wave_entity_sync(
            target_system,
            entity_types,
            _bounded_positive_int(payload.get("pageSize"), default=50, maximum=100),
            _bounded_positive_int(payload.get("maxPages"), default=100, maximum=500),
            str(payload.get("actor") or "api"),
        )
        status_code = 200 if result.get("success") else 400
        if result.get("status") in {"rate_limited", "quota_exhausted"}:
            status_code = 429
        elif result.get("status") == "provider_error":
            status_code = 502
        return jsonify(result), status_code

    @app.post("/wave/entities/sync")
    def sync_wave_entities_form():
        requested = str(request.form.get("entityTypes") or "").strip()
        result = run_wave_entity_sync(
            str(request.form.get("targetSystem") or "waveapps_business"),
            None if requested in {"", "all"} else [requested],
            50,
            100,
            "dashboard",
        )
        session["fab_last_wave_entity_sync"] = result
        return redirect(url_for("dashboard_page", _anchor="wave"))

    @app.get("/api/wave/reports")
    def wave_reports():
        return jsonify(LocalWaveControlService(config).reports(section=request.args.get("section")))

    @app.get("/api/wave/report-snapshots")
    def wave_report_snapshots():
        return jsonify({
            "waveReportSnapshots": ledger.list_wave_report_snapshots(
                report_type=request.args.get("reportType") or request.args.get("report_type"),
                workflow_id=request.args.get("workflowId") or request.args.get("workflow_id"),
                status=request.args.get("status"),
                limit=_limit_arg(),
            )
        })

    @app.get("/api/wave/report-controls")
    def wave_report_controls():
        workflow_id = request.args.get("workflowId") or request.args.get("workflow_id") or "daily_reconciliation_run"
        return jsonify(LocalWaveControlService(config).evaluate_report_controls(
            ledger,
            workflow_id=workflow_id,
            limit=_limit_arg(),
        ))

    @app.post("/api/wave/report-results")
    def record_wave_report_result():
        payload = request.get_json(silent=True) or {}
        service = LocalWaveControlService(config)
        result = service.record_report_result(ledger, payload)
        snapshot = result.get("waveReportSnapshot") or {}
        ledger.record_audit_event({
            "action": "local_wave.report_result_captured",
            "entityType": "wave_report_snapshot",
            "entityId": str(result.get("waveReportSnapshotId") or payload.get("snapshotId") or payload.get("operationId")),
            "details": {
                "status": result.get("status"),
                "success": result.get("success"),
                "reportType": snapshot.get("report_type") or payload.get("reportType"),
                "actionId": snapshot.get("action_id") or payload.get("actionId"),
                "workflowId": snapshot.get("workflow_id") or payload.get("workflowId"),
                "rowCount": (snapshot.get("row_count") if snapshot else (payload.get("result") or {}).get("rowCount")),
                "externalSubmission": result.get("externalSubmission"),
            },
        })
        status_code = 200 if result.get("success") else 400
        if result.get("status") == "not_found":
            status_code = 404
        return jsonify(result), status_code

    @app.get("/api/wave/operations")
    def wave_operation_snapshots():
        return jsonify({
            "waveOperationSnapshots": ledger.list_wave_operation_snapshots(
                surface=request.args.get("surface"),
                workflow_id=request.args.get("workflowId") or request.args.get("workflow_id"),
                action_id=request.args.get("actionId") or request.args.get("action_id"),
                safety=request.args.get("safety"),
                status=request.args.get("status"),
                operation_id=request.args.get("operationId") or request.args.get("operation_id"),
                limit=_limit_arg(),
            )
        })

    @app.get("/api/mijngeldzaken")
    def mijngeldzaken_overview():
        include_controls = _bool_value(request.args.get("includeControls"), default=True)
        return jsonify(LocalMijngeldzakenControlService(config).overview(ledger if include_controls else None))

    @app.get("/api/mijngeldzaken/actions")
    def mijngeldzaken_actions():
        return jsonify(LocalMijngeldzakenControlService(config).actions(
            surface=request.args.get("surface"),
            safety=request.args.get("safety"),
            mode=request.args.get("mode"),
        ))

    @app.post("/api/mijngeldzaken/plan")
    def mijngeldzaken_plan():
        payload = request.get_json(silent=True) or {}
        result = LocalMijngeldzakenControlService(config).plan_action(payload)
        operation = result.get("operation") or {}
        ledger.record_audit_event({
            "action": "local_mijngeldzaken.action_plan_prepared",
            "entityType": "mijngeldzaken_operation",
            "entityId": operation.get("operation_id"),
            "details": {
                "surface": operation.get("surface"),
                "actionId": operation.get("action_id"),
                "safety": operation.get("safety"),
                "planStatus": (operation.get("plan") or {}).get("status"),
                "externalSubmission": "not_executed",
            },
        })
        status_code = 400 if result.get("status") in {"unsupported", "invalid_payload"} else 200
        return jsonify(result), status_code

    @app.post("/api/mijngeldzaken/workflows/plan")
    def plan_mijngeldzaken_workflow():
        payload = request.get_json(silent=True) or {}
        service = LocalMijngeldzakenControlService(config)
        result = service.plan_workflow(payload)
        controls = service.evaluate_master_ledger_controls(ledger)
        result["masterLedgerControls"] = controls
        workflow_plan = result.get("workflowPlan") or {}
        ledger.record_audit_event({
            "action": "local_mijngeldzaken.workflow_plan_prepared",
            "entityType": "mijngeldzaken_workflow",
            "entityId": workflow_plan.get("workflowId"),
            "details": {
                "status": result.get("status"),
                "workflowId": workflow_plan.get("workflowId"),
                "operationCount": result.get("operationCount"),
                "masterLedgerControls": {
                    "status": controls.get("status"),
                    "rowCount": controls.get("rowCount"),
                    "blockingCount": controls.get("blockingCount"),
                    "readyForDraft": controls.get("readyForDraft"),
                    "readyForApproval": controls.get("readyForApproval"),
                    "readyForExternalExecution": controls.get("readyForExternalExecution"),
                },
                "externalSubmission": result.get("externalSubmission"),
            },
        })
        status_code = 400 if result.get("status") in {"unsupported", "invalid_payload"} else 200
        return jsonify(result), status_code

    @app.post("/mijngeldzaken/workflows/plan")
    def plan_mijngeldzaken_workflow_form():
        payload = {
            "workflowId": request.form.get("workflowId") or "master_ledger_downstream_sync",
            "fromDate": request.form.get("fromDate") or None,
            "toDate": request.form.get("toDate") or None,
            "actor": "dashboard",
        }
        service = LocalMijngeldzakenControlService(config)
        result = service.plan_workflow(payload)
        controls = service.evaluate_master_ledger_controls(ledger)
        result["masterLedgerControls"] = controls
        session["fab_last_mijngeldzaken_plan"] = _compact_mijngeldzaken_plan_summary(result)
        workflow_plan = result.get("workflowPlan") or {}
        ledger.record_audit_event({
            "action": "local_mijngeldzaken.workflow_plan_prepared",
            "entityType": "mijngeldzaken_workflow",
            "entityId": workflow_plan.get("workflowId"),
            "details": {
                "status": result.get("status"),
                "workflowId": workflow_plan.get("workflowId"),
                "operationCount": result.get("operationCount"),
                "masterLedgerControls": {
                    "status": controls.get("status"),
                    "rowCount": controls.get("rowCount"),
                    "blockingCount": controls.get("blockingCount"),
                    "readyForDraft": controls.get("readyForDraft"),
                    "readyForApproval": controls.get("readyForApproval"),
                    "readyForExternalExecution": controls.get("readyForExternalExecution"),
                },
                "externalSubmission": result.get("externalSubmission"),
            },
        })
        return redirect(url_for("dashboard_page", _anchor="mijngeldzaken"))

    @app.post("/api/wave/reports/plan")
    def plan_wave_report():
        payload = request.get_json(silent=True) or {}
        service = LocalWaveControlService(config)
        result = service.plan_report_action(
            str(payload.get("reportType") or payload.get("report_type") or "account-transactions"),
            from_date=payload.get("fromDate") or payload.get("from_date"),
            to_date=payload.get("toDate") or payload.get("to_date"),
            as_of_date=payload.get("asOfDate") or payload.get("as_of_date"),
            action_id=str(payload.get("actionId") or payload.get("action_id") or "report_table_read"),
            export_format=payload.get("format") or payload.get("exportFormat") or payload.get("export_format"),
            basis=str(payload.get("basis") or "accrual"),
            account_option=str(payload.get("accountOption") or payload.get("account_option") or "-1"),
            account_name=str(payload.get("accountName") or payload.get("account_name") or "All Accounts"),
            contact_option=str(payload.get("contactOption") or payload.get("contact_option") or "0"),
            contact_name=str(payload.get("contactName") or payload.get("contact_name") or "All Contacts"),
            cash_mode=str(payload.get("cashMode") or payload.get("cash_mode") or "1"),
        )
        operation = result.get("operation") or {}
        snapshot_id = None
        operation_snapshot_id = None
        if result.get("status") == "planned":
            snapshot_id = service.record_report_operation_snapshot(
                ledger,
                operation,
                workflow_id=payload.get("workflowId") or payload.get("workflow_id") or "ad_hoc_report_plan",
            )
            operation_snapshot_id = service.record_operation_snapshot(
                ledger,
                operation,
                workflow_id=payload.get("workflowId") or payload.get("workflow_id") or "ad_hoc_report_plan",
            )
        result["waveReportSnapshotId"] = snapshot_id
        result["waveOperationSnapshotId"] = operation_snapshot_id
        ledger.record_audit_event({
            "action": "local_wave.report_plan_prepared",
            "entityType": "wave_report_snapshot",
            "entityId": str(snapshot_id) if snapshot_id is not None else operation.get("operation_id"),
            "details": {
                "status": result.get("status"),
                "reportType": (operation.get("payload") or {}).get("reportType"),
                "actionId": operation.get("action_id"),
                "operationId": operation.get("operation_id"),
                "waveOperationSnapshotId": operation_snapshot_id,
                "externalSubmission": result.get("externalSubmission"),
            },
        })
        status_code = 400 if result.get("status") in {"unsupported", "invalid_payload"} else 200
        return jsonify(result), status_code

    @app.post("/api/wave/plan")
    def plan_wave_action():
        payload = request.get_json(silent=True) or {}
        service = LocalWaveControlService(config)
        result = service.plan_action(payload)
        operation = result.get("operation") or {}
        snapshot_id = None
        operation_snapshot_id = None
        if result.get("status") == "planned":
            operation_snapshot_id = service.record_operation_snapshot(
                ledger,
                operation,
                workflow_id=payload.get("workflowId") or payload.get("workflow_id") or "ad_hoc_action_plan",
            )
            if operation.get("surface") == "reports":
                snapshot_id = service.record_report_operation_snapshot(
                    ledger,
                    operation,
                    workflow_id=payload.get("workflowId") or payload.get("workflow_id") or "ad_hoc_action_plan",
                )
        if snapshot_id is not None:
            result["waveReportSnapshotId"] = snapshot_id
        if operation_snapshot_id is not None:
            result["waveOperationSnapshotId"] = operation_snapshot_id
        ledger.record_audit_event({
            "action": "local_wave.action_plan_prepared",
            "entityType": "wave_operation",
            "entityId": operation.get("operation_id"),
            "details": {
                "status": result.get("status"),
                "actionId": operation.get("action_id") or payload.get("actionId"),
                "surface": operation.get("surface") or payload.get("surface"),
                "safety": operation.get("safety"),
                "waveReportSnapshotId": snapshot_id,
                "waveOperationSnapshotId": operation_snapshot_id,
                "externalSubmission": result.get("externalSubmission"),
            },
        })
        status_code = 400 if result.get("status") in {"unsupported", "invalid_payload"} else 200
        return jsonify(result), status_code

    @app.post("/api/wave/workflows/plan")
    def plan_wave_workflow():
        payload = request.get_json(silent=True) or {}
        service = LocalWaveControlService(config)
        result = service.plan_workflow(payload)
        snapshot_summary = service.record_workflow_report_snapshots(ledger, result)
        operation_snapshot_summary = service.record_workflow_operation_snapshots(ledger, result)
        report_controls = service.evaluate_report_controls(
            ledger,
            workflow_id=(result.get("workflow_plan") or {}).get("workflow_id") or "daily_reconciliation_run",
        )
        result["waveReportSnapshots"] = snapshot_summary
        result["waveOperationSnapshots"] = operation_snapshot_summary
        result["waveReportControls"] = report_controls
        ledger.record_audit_event({
            "action": "local_wave.workflow_plan_prepared",
            "entityType": "wave_workflow",
            "entityId": result.get("workflow_plan", {}).get("workflow_id"),
            "details": {
                "status": result.get("status"),
                "workflowId": result.get("workflow_plan", {}).get("workflow_id"),
                "operationCount": result.get("operationCount"),
                "waveReportSnapshots": snapshot_summary,
                "waveOperationSnapshots": operation_snapshot_summary,
                "waveReportControls": {
                    "status": report_controls.get("status"),
                    "requiredReportCount": report_controls.get("requiredReportCount"),
                    "coveredReportCount": report_controls.get("coveredReportCount"),
                    "resultGapCount": report_controls.get("resultGapCount"),
                    "blockingCount": report_controls.get("blockingCount"),
                },
                "externalSubmission": result.get("externalSubmission"),
            },
        })
        return jsonify(result)

    @app.post("/wave/workflows/plan")
    def plan_wave_workflow_form():
        payload = {
            "workflowId": request.form.get("workflowId") or "daily_reconciliation_run",
            "fromDate": request.form.get("fromDate"),
            "toDate": request.form.get("toDate"),
        }
        service = LocalWaveControlService(config)
        wave_plan = service.plan_workflow(payload)
        snapshot_summary = service.record_workflow_report_snapshots(ledger, wave_plan)
        operation_snapshot_summary = service.record_workflow_operation_snapshots(ledger, wave_plan)
        report_controls = service.evaluate_report_controls(
            ledger,
            workflow_id=(wave_plan.get("workflow_plan") or {}).get("workflow_id") or "daily_reconciliation_run",
        )
        wave_plan["waveReportSnapshots"] = snapshot_summary
        wave_plan["waveOperationSnapshots"] = operation_snapshot_summary
        wave_plan["waveReportControls"] = report_controls
        session["fab_last_wave_plan"] = _compact_wave_plan_summary(wave_plan)
        workflow_plan = wave_plan.get("workflow_plan", {})
        ledger.record_audit_event({
            "action": "local_wave.workflow_plan_prepared",
            "entityType": "wave_workflow",
            "entityId": workflow_plan.get("workflow_id"),
            "details": {
                "status": wave_plan.get("status"),
                "workflowId": workflow_plan.get("workflow_id"),
                "operationCount": wave_plan.get("operationCount"),
                "waveReportSnapshots": snapshot_summary,
                "waveOperationSnapshots": operation_snapshot_summary,
                "waveReportControls": {
                    "status": report_controls.get("status"),
                    "requiredReportCount": report_controls.get("requiredReportCount"),
                    "coveredReportCount": report_controls.get("coveredReportCount"),
                    "resultGapCount": report_controls.get("resultGapCount"),
                    "blockingCount": report_controls.get("blockingCount"),
                },
                "externalSubmission": wave_plan.get("externalSubmission"),
            },
        })
        return redirect(url_for("dashboard_page", _anchor="wave"))

    @app.get("/api/documents")
    def documents():
        return jsonify({
            "documents": ledger.list_documents(
                status=request.args.get("status"),
                limit=_limit_arg(),
            )
        })

    @app.get("/documents/<int:document_id>")
    def document_detail_page(document_id: int):
        document = ledger.get_document(document_id)
        if not document:
            return render_template_string(
                "<h1>Document not found</h1><p>No local FAB document exists for this id.</p>"
            ), 404
        open_review_count = len([
            item
            for item in document.get("review_items") or []
            if item.get("status") in {"pending", "in_review"}
        ])
        return render_template_string(
            DOCUMENT_DETAIL_TEMPLATE,
            compact_json=_compact_json,
            document=document,
            format_confidence=_format_confidence,
            format_money=_format_money,
            open_review_count=open_review_count,
            pretty_json=_pretty_json,
            source_preview=_document_source_preview(document),
        )

    @app.get("/api/documents/<int:document_id>")
    def document_detail(document_id: int):
        document = ledger.get_document(document_id)
        if not document:
            return jsonify({"error": "Document not found"}), 404
        return jsonify(document)

    @app.get("/api/documents/<int:document_id>/source")
    def document_source(document_id: int):
        document = ledger.get_document(document_id)
        if not document:
            return jsonify({"error": "Document not found"}), 404

        source_path = str(document.get("storage_path") or "").strip()
        if not source_path:
            return jsonify({"error": "No retained source file is registered"}), 404
        normalized_path = os.path.abspath(os.path.expandvars(os.path.expanduser(source_path)))
        if not os.path.isfile(normalized_path):
            return jsonify({"error": "The retained source file is unavailable"}), 404

        expected_sha256 = str(document.get("content_sha256") or "").strip().lower()
        if len(expected_sha256) != 64 or any(character not in "0123456789abcdef" for character in expected_sha256):
            return jsonify({"error": "The retained source file has no valid integrity hash"}), 409

        digest = hashlib.sha256()
        source_chunks = []
        verified_size = 0
        try:
            flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
            descriptor = os.open(normalized_path, flags)
            try:
                with os.fdopen(descriptor, "rb", buffering=0, closefd=False) as source_file:
                    opened = os.fstat(descriptor)
                    if not stat.S_ISREG(opened.st_mode):
                        return jsonify({"error": "The retained source file could not be verified"}), 409
                    if opened.st_size > LOCAL_SOURCE_PREVIEW_MAX_BYTES:
                        return jsonify({
                            "error": "The retained source file exceeds the preview limit",
                            "maxBytes": LOCAL_SOURCE_PREVIEW_MAX_BYTES,
                        }), 413
                    while True:
                        # A growing file can consume at most one byte beyond the limit.
                        chunk = source_file.read(min(1024 * 1024, LOCAL_SOURCE_PREVIEW_MAX_BYTES - verified_size + 1))
                        if not chunk:
                            break
                        verified_size += len(chunk)
                        if verified_size > LOCAL_SOURCE_PREVIEW_MAX_BYTES:
                            return jsonify({
                                "error": "The retained source file exceeds the preview limit",
                                "maxBytes": LOCAL_SOURCE_PREVIEW_MAX_BYTES,
                            }), 413
                        digest.update(chunk)
                        source_chunks.append(chunk)
            finally:
                os.close(descriptor)
        except OSError:
            return jsonify({"error": "The retained source file could not be verified"}), 409
        if not hmac.compare_digest(digest.hexdigest(), expected_sha256):
            return jsonify({"error": "The retained source file failed its integrity check"}), 409

        original_filename = str(document.get("original_filename") or Path(normalized_path).name)
        declared_mime_type = str(document.get("mime_type") or "").split(";", 1)[0].strip().lower()
        guessed_mime_type = str(mimetypes.guess_type(original_filename)[0] or "").lower()
        preview_mime_type = (
            declared_mime_type
            if declared_mime_type in LOCAL_SOURCE_PREVIEW_MIME_TYPES
            else guessed_mime_type
        )
        if preview_mime_type not in LOCAL_SOURCE_PREVIEW_MIME_TYPES:
            return jsonify({"error": "This retained source file type cannot be previewed safely"}), 415

        response = Response(b"".join(source_chunks), mimetype=preview_mime_type)
        response.headers["Content-Disposition"] = "inline"
        response.headers["X-FAB-Source-Integrity"] = "verified"
        response.headers["X-FAB-Source-SHA256"] = expected_sha256
        return response

    @app.get("/api/duplicates")
    def duplicate_candidates_api():
        document_id = request.args.get("documentId") or request.args.get("document_id")
        candidate_document_id = request.args.get("candidateDocumentId") or request.args.get("candidate_document_id")
        try:
            parsed_document_id = int(document_id) if document_id else None
            parsed_candidate_document_id = int(candidate_document_id) if candidate_document_id else None
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid duplicate candidate document id"}), 400
        return jsonify({
            "duplicateCandidates": ledger.list_duplicate_candidates(
                status=request.args.get("status"),
                document_id=parsed_document_id,
                candidate_document_id=parsed_candidate_document_id,
                limit=_limit_arg(),
            )
        })

    @app.get("/api/extracted-fields")
    def extracted_fields():
        document_id = request.args.get("documentId")
        try:
            parsed_document_id = int(document_id) if document_id else None
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid documentId"}), 400
        return jsonify({
            "extractedFields": ledger.list_extracted_fields(
                document_id=parsed_document_id,
                field_name=request.args.get("fieldName") or request.args.get("field_name"),
                limit=_limit_arg(),
            )
        })

    @app.get("/api/bookkeeping-records")
    def bookkeeping_records():
        return jsonify({
            "bookkeepingRecords": ledger.list_bookkeeping_records(
                status=request.args.get("status"),
                export_status=request.args.get("exportStatus") or request.args.get("export_status"),
                reconciliation_status=request.args.get("reconciliationStatus") or request.args.get("reconciliation_status"),
                target_system=request.args.get("targetSystem") or request.args.get("target_system"),
                source_type=request.args.get("sourceType") or request.args.get("source_type"),
                from_date=request.args.get("fromDate") or request.args.get("from_date"),
                to_date=request.args.get("toDate") or request.args.get("to_date"),
                limit=_limit_arg(),
            )
        })

    @app.get("/api/master-ledger")
    def master_ledger_api():
        service = LocalMasterLedgerService(ledger, config)
        target_system = request.args.get("targetSystem") or request.args.get("target_system")
        export_format = str(request.args.get("format") or "json").strip().lower()
        limit = _bounded_positive_int(request.args.get("limit"), default=500, maximum=1000)
        if export_format == "csv":
            artifact = service.csv_artifact(target_system=target_system, limit=limit)
            response = Response(artifact["content"], mimetype=artifact["contentType"])
            response.headers["Content-Disposition"] = f"attachment; filename={artifact['filename']}"
            response.headers["X-FAB-External-Submission"] = artifact["externalSubmission"]
            response.headers["X-FAB-Master-Ledger-Checksum"] = artifact["ledgerChecksum"]
            response.headers["X-FAB-Master-Ledger-Rows"] = str(artifact["rowCount"])
            return response
        if export_format != "json":
            return jsonify({
                "success": False,
                "status": "unsupported_format",
                "supportedFormats": ["json", "csv"],
            }), 400
        projection = service.project(target_system=target_system, limit=limit)
        if _bool_value(
            request.args.get("summaryOnly") or request.args.get("summary_only"),
            default=False,
        ):
            rows = projection.pop("rows", [])
            projection["dataThroughDate"] = max(
                (
                    str(row.get("recordDate"))
                    for row in rows
                    if row.get("recordDate")
                ),
                default=None,
            )
            projection["rowsOmitted"] = len(rows)
        if _bool_value(request.args.get("audit"), default=False):
            service.record_projection_audit(projection, actor=request.args.get("actor") or "local_api")
        return jsonify(projection)

    @app.get("/api/reports")
    def financial_reports_api():
        service = LocalFinancialReportingService(ledger, config)
        report_type = request.args.get("reportType") or request.args.get("report_type") or "overview"
        basis = request.args.get("basis") or "accrual"
        from_date = request.args.get("fromDate") or request.args.get("from_date")
        to_date = request.args.get("toDate") or request.args.get("to_date")
        target_system = request.args.get("targetSystem") or request.args.get("target_system")
        export_format = str(request.args.get("format") or "json").strip().lower()
        try:
            if export_format == "csv":
                artifact = service.csv_artifact(
                    report_type=report_type,
                    basis=basis,
                    from_date=from_date,
                    to_date=to_date,
                    target_system=target_system,
                )
                response = Response(artifact["content"], mimetype=artifact["contentType"])
                response.headers["Content-Disposition"] = f"attachment; filename={artifact['filename']}"
                response.headers["X-FAB-External-Submission"] = artifact["externalSubmission"]
                response.headers["X-FAB-Report-Rows"] = str(artifact["rowCount"])
                return response
            if export_format != "json":
                return jsonify({
                    "success": False,
                    "status": "unsupported_format",
                    "supportedFormats": ["json", "csv"],
                }), 400
            return jsonify(service.generate(
                report_type=report_type,
                basis=basis,
                from_date=from_date,
                to_date=to_date,
                target_system=target_system,
                include_rows=_bool_value(request.args.get("includeRows"), default=False),
            ))
        except ValueError as exc:
            return jsonify({"success": False, "status": "invalid_request", "error": str(exc)}), 400

    @app.post("/api/reports")
    def generate_financial_report_api():
        payload = request.get_json(silent=True) or {}
        service = LocalFinancialReportingService(ledger, config)
        try:
            report = service.generate(
                report_type=payload.get("reportType") or payload.get("report_type") or "overview",
                basis=payload.get("basis") or "accrual",
                from_date=payload.get("fromDate") or payload.get("from_date"),
                to_date=payload.get("toDate") or payload.get("to_date"),
                target_system=payload.get("targetSystem") or payload.get("target_system"),
                include_rows=_bool_value(payload.get("includeRows"), default=False),
            )
        except ValueError as exc:
            return jsonify({"success": False, "status": "invalid_request", "error": str(exc)}), 400
        report["auditEventId"] = service.record_generation_audit(
            report,
            actor=payload.get("actor") or "local_api",
        )
        return jsonify(report)

    @app.get("/api/report-runs")
    def financial_report_runs_api():
        service = LocalScheduledReportService(ledger, config)
        try:
            schedule_status = service.schedule_status()
        except ValueError as exc:
            schedule_status = {
                "enabled": False,
                "status": "invalid",
                "error": str(exc),
                "externalSubmission": "not_executed",
            }
        return jsonify({
            "scheduleStatus": schedule_status,
            "reportRuns": ledger.list_financial_report_runs(
                schedule_id=request.args.get("scheduleId") or request.args.get("schedule_id"),
                status=request.args.get("status"),
                limit=_limit_arg(),
            ),
            "externalSubmission": "not_executed",
        })

    @app.post("/api/report-runs/run-due")
    def run_due_report_schedule_api():
        payload = request.get_json(silent=True) or {}
        try:
            result = LocalScheduledReportService(ledger, config).run_due(
                actor=payload.get("actor") or "local_api",
            )
        except ValueError as exc:
            return jsonify({"success": False, "status": "invalid_schedule", "error": str(exc)}), 400
        return jsonify(result), 200 if result.get("success") else 500

    @app.post("/report-runs/run-due")
    def run_due_report_schedule_form():
        try:
            result = LocalScheduledReportService(ledger, config).run_due(actor="fab_dashboard")
        except ValueError as exc:
            result = {"success": False, "status": "invalid_schedule", "error": str(exc)}
        session["fab_last_scheduled_report_summary"] = result
        return redirect(url_for("dashboard_page", _anchor="reports"))

    @app.get("/api/report-runs/<int:report_run_id>")
    def financial_report_run_detail_api(report_run_id: int):
        result = LocalScheduledReportService(ledger, config).inspect_run(report_run_id)
        return jsonify(result), 200 if result.get("status") != "not_found" else 404

    @app.get("/api/report-runs/<int:report_run_id>/artifact")
    def financial_report_run_artifact_api(report_run_id: int):
        format_name = str(request.args.get("format") or "json").strip().lower()
        try:
            artifact = LocalScheduledReportService(ledger, config).read_artifact(
                report_run_id,
                format_name,
            )
        except ValueError as exc:
            return jsonify({"success": False, "status": "invalid_artifact", "error": str(exc)}), 400
        response = Response(artifact["content"], mimetype=artifact["contentType"])
        response.headers["Content-Disposition"] = f"attachment; filename={artifact['filename']}"
        response.headers["X-FAB-External-Submission"] = artifact["externalSubmission"]
        response.headers["X-FAB-Report-SHA256"] = artifact["sha256"]
        return response

    @app.get("/api/compliance/assessments")
    def compliance_assessments_api():
        return jsonify({
            "summary": LocalComplianceService(ledger, config).summary(),
            "assessments": ledger.list_compliance_assessments(
                status=request.args.get("status"),
                limit=_limit_arg(),
            ),
            "statutoryStatus": "provisional",
            "filingStatus": "not_filed",
            "externalFiling": "not_executed",
        })

    @app.post("/api/compliance/assessments")
    def create_compliance_assessment_api():
        payload = request.get_json(silent=True) or {}
        try:
            result = LocalComplianceService(ledger, config).assess(
                from_date=payload.get("fromDate") or payload.get("from_date"),
                to_date=payload.get("toDate") or payload.get("to_date"),
                basis=payload.get("basis") or "accrual",
                target_system=payload.get("targetSystem") or payload.get("target_system"),
                actor=payload.get("actor") or "local_api",
            )
        except ValueError as exc:
            return jsonify({"success": False, "status": "invalid_request", "error": str(exc)}), 400
        return jsonify(result)

    @app.post("/compliance/assess")
    def run_compliance_assessment_form():
        try:
            result = LocalComplianceService(ledger, config).assess(actor="local_dashboard")
        except ValueError as exc:
            result = {"success": False, "status": "invalid_request", "error": str(exc)}
        session["fab_last_compliance_summary"] = result
        return redirect(url_for("dashboard_page", _anchor="compliance"))

    @app.get("/api/compliance/assessments/<int:assessment_id>")
    def compliance_assessment_detail_api(assessment_id: int):
        assessment = ledger.get_compliance_assessment(assessment_id)
        if not assessment:
            return jsonify({"success": False, "status": "not_found"}), 404
        return jsonify({
            "success": True,
            "assessment": assessment,
            "findings": ledger.list_compliance_findings(assessment_id=assessment_id, limit=500),
            "retentionRecords": [
                item for item in ledger.list_retention_records(limit=500)
                if item.get("assessment_id") == assessment_id
            ],
            "statutoryStatus": "provisional",
            "filingStatus": "not_filed",
            "externalFiling": "not_executed",
        })

    @app.get("/api/compliance/findings")
    def compliance_findings_api():
        assessment_id = request.args.get("assessmentId") or request.args.get("assessment_id")
        try:
            parsed_assessment_id = int(assessment_id) if assessment_id else None
        except (TypeError, ValueError):
            return jsonify({"success": False, "status": "invalid_request", "error": "assessmentId must be an integer"}), 400
        return jsonify({
            "findings": ledger.list_compliance_findings(
                assessment_id=parsed_assessment_id,
                status=request.args.get("status"),
                severity=request.args.get("severity"),
                code=request.args.get("code"),
                limit=_limit_arg(),
            ),
            "externalFiling": "not_executed",
        })

    @app.route("/api/compliance/findings/<int:finding_id>/status", methods=["POST", "PATCH"])
    def compliance_finding_status_api(finding_id: int):
        payload = request.get_json(silent=True) or {}
        try:
            result = LocalComplianceService(ledger, config).update_finding(
                finding_id,
                payload.get("status"),
                resolution=payload.get("resolution"),
                actor=payload.get("actor") or "local_api",
            )
        except ValueError as exc:
            return jsonify({"success": False, "status": "invalid_request", "error": str(exc)}), 400
        return jsonify(result), 200 if result.get("success") else 404

    @app.post("/compliance/findings/<int:finding_id>/status")
    def compliance_finding_status_form(finding_id: int):
        try:
            result = LocalComplianceService(ledger, config).update_finding(
                finding_id,
                request.form.get("status"),
                resolution=request.form.get("resolution") or None,
                actor="local_dashboard",
            )
        except ValueError as exc:
            result = {"success": False, "status": "invalid_request", "error": str(exc)}
        session["fab_last_compliance_summary"] = result
        return redirect(url_for("dashboard_page", _anchor="compliance"))

    @app.get("/api/compliance/retention")
    def compliance_retention_api():
        return jsonify({
            "retentionRecords": ledger.list_retention_records(
                status=request.args.get("status"),
                limit=_limit_arg(),
            ),
            "deletionAuthorized": False,
            "externalFiling": "not_executed",
        })

    @app.get("/api/bookkeeping-records/<int:record_id>")
    def bookkeeping_record_detail(record_id: int):
        record = ledger.get_bookkeeping_record(record_id)
        if not record:
            return jsonify({"error": "Bookkeeping record not found"}), 404
        record["routing_attempts"] = ledger.list_routing_attempts(
            bookkeeping_record_id=record_id,
            limit=_limit_arg(),
        )
        record["export_attempts"] = ledger.list_export_attempts(
            bookkeeping_record_id=record_id,
            limit=_limit_arg(),
        )
        return jsonify(record)

    @app.get("/bookkeeping-records/<int:record_id>")
    def bookkeeping_record_detail_page(record_id: int):
        record = ledger.get_bookkeeping_record(record_id)
        if not record:
            return render_template_string(
                "<h1>Bookkeeping record not found</h1><p>No local FAB bookkeeping record exists for this id.</p>"
            ), 404
        routing_attempts = ledger.list_routing_attempts(
            bookkeeping_record_id=record_id,
            limit=_limit_arg(),
        )
        export_attempts = ledger.list_export_attempts(
            bookkeeping_record_id=record_id,
            limit=_limit_arg(),
        )
        bank_transaction = None
        if record.get("bank_transaction_id"):
            bank_transaction = ledger.get_bank_transaction(int(record["bank_transaction_id"]))
        reconciliation_matches = _reconciliation_matches_for_record(
            ledger,
            record,
            bank_transaction=bank_transaction,
            limit=_limit_arg(),
        )
        audit_events = [
            event
            for event in ledger.list_audit_events(limit=500)
            if event.get("entity_type") == "bookkeeping_record"
            and str(event.get("entity_id") or "") == str(record_id)
        ]
        return render_template_string(
            BOOKKEEPING_RECORD_DETAIL_TEMPLATE,
            audit_events=audit_events,
            bank_transaction=bank_transaction,
            compact_json=_compact_json,
            export_attempts=export_attempts,
            format_confidence=_format_confidence,
            format_money=_format_money,
            pretty_json=_pretty_json,
            record=record,
            reconciliation_matches=reconciliation_matches,
            routing_attempts=routing_attempts,
        )

    @app.post("/api/bookkeeping-records/<int:record_id>/resolve")
    def resolve_bookkeeping_record(record_id: int):
        payload = request.get_json(silent=True) or {}
        status = str(payload.get("status") or payload.get("resolutionStatus") or "resolved")
        if status not in BOOKKEEPING_RECORD_RESOLUTION_STATUSES:
            return jsonify({"error": "Invalid bookkeeping record resolution status"}), 400
        result = LocalBookkeepingRecordService(ledger, config).resolve_record(
            record_id,
            status=status,
            resolution=payload.get("resolution"),
            corrections=payload.get("corrections") or _bookkeeping_record_corrections_from_mapping(payload),
            actor=str(payload.get("actor") or "fab_local_api"),
        )
        status_code = 404 if result.get("status") == "not_found" else 200
        return jsonify(result), status_code

    @app.post("/bookkeeping-records/<int:record_id>/resolve")
    def resolve_bookkeeping_record_form(record_id: int):
        status = str(request.form.get("status") or "resolved")
        if status not in BOOKKEEPING_RECORD_RESOLUTION_STATUSES:
            status = "resolved"
        LocalBookkeepingRecordService(ledger, config).resolve_record(
            record_id,
            status=status,
            resolution=request.form.get("resolution") or None,
            corrections=_bookkeeping_record_corrections_from_mapping(request.form),
            actor="fab_dashboard",
        )
        return redirect(url_for("dashboard_page", _anchor="records"))

    @app.get("/api/bookkeeping-records/<int:record_id>/line-items")
    def bookkeeping_record_line_items(record_id: int):
        if not ledger.get_bookkeeping_record(record_id):
            return jsonify({"error": "Bookkeeping record not found"}), 404
        return jsonify({
            "lineItems": ledger.list_bookkeeping_record_line_items(
                bookkeeping_record_id=record_id,
                limit=_limit_arg(),
            )
        })

    @app.post("/api/bookkeeping-records/refresh")
    def refresh_bookkeeping_records():
        payload = request.get_json(silent=True) or {}
        limit = _bounded_positive_int(payload.get("limit"), default=100, maximum=500)
        source_type = payload.get("sourceType") or payload.get("source_type") or "document"
        try:
            result = _refresh_bookkeeping_records(ledger, config, source_type=source_type, limit=limit)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(result)

    @app.post("/bookkeeping-records/refresh")
    def refresh_bookkeeping_records_form():
        source_type = request.form.get("sourceType") or request.form.get("source_type") or "document"
        try:
            session["fab_last_record_refresh_summary"] = _refresh_bookkeeping_records(
                ledger,
                config,
                source_type=source_type,
                limit=100,
            )
        except ValueError as exc:
            session["fab_last_record_refresh_summary"] = {
                "success": False,
                "status": "failed",
                "error": str(exc),
                "externalSubmission": "not_executed",
            }
        return redirect(url_for("dashboard_page", _anchor="records"))

    @app.post("/api/documents/<int:document_id>/process")
    def process_document(document_id: int):
        result = LocalDocumentProcessor(ledger, config).process_document(document_id)
        if result.get("status") == "not_found":
            return jsonify(result), 404
        return jsonify(result)

    @app.post("/api/documents/<int:document_id>/retry-processing")
    def retry_document_processing(document_id: int):
        payload = request.get_json(silent=True) or {}
        result = LocalDocumentProcessor(ledger, config).retry_document(
            document_id,
            actor=str(payload.get("actor") or "fab_local_api"),
        )
        if result.get("status") == "not_found":
            return jsonify(result), 404
        return jsonify(result)

    @app.post("/documents/<int:document_id>/process")
    def process_document_form(document_id: int):
        session["fab_last_processing_summary"] = {
            "requested": 1,
            "processed": 0,
            "needsReview": 0,
            "failed": 0,
            "skipped": 0,
            "documents": [LocalDocumentProcessor(ledger, config).process_document(document_id)],
        }
        status = session["fab_last_processing_summary"]["documents"][0].get("status")
        if status == "processed":
            session["fab_last_processing_summary"]["processed"] = 1
        elif status == "failed":
            session["fab_last_processing_summary"]["failed"] = 1
        elif session["fab_last_processing_summary"]["documents"][0].get("skipped"):
            session["fab_last_processing_summary"]["skipped"] = 1
        else:
            session["fab_last_processing_summary"]["needsReview"] = 1
        return redirect(url_for("dashboard_page", _anchor="ledger"))

    @app.post("/api/documents/process-imported")
    def process_imported_documents():
        payload = request.get_json(silent=True) or {}
        limit = _bounded_positive_int(payload.get("limit"), default=25, maximum=100)
        return jsonify(LocalDocumentProcessor(ledger, config).process_imported(limit=limit))

    @app.post("/api/documents/reprocess-incomplete")
    def reprocess_incomplete_documents():
        payload = request.get_json(silent=True) or {}
        limit = _bounded_positive_int(payload.get("limit"), default=25, maximum=100)
        actor = str(payload.get("actor") or "fab_local_api")
        return jsonify(LocalDocumentProcessor(ledger, config).reprocess_incomplete(
            limit=limit,
            actor=actor,
        ))

    @app.post("/api/documents/reprocess-review-queue")
    def reprocess_review_queue():
        payload = request.get_json(silent=True) or {}
        limit = _bounded_positive_int(payload.get("limit"), default=25, maximum=100)
        actor = str(payload.get("actor") or "fab_local_api")
        return jsonify(LocalDocumentProcessor(ledger, config).reprocess_review_queue(
            limit=limit,
            actor=actor,
        ))

    @app.post("/documents/process-imported")
    def process_imported_form():
        session["fab_last_processing_summary"] = LocalDocumentProcessor(ledger, config).process_imported(limit=25)
        return redirect(url_for("dashboard_page", _anchor="intake"))

    @app.post("/api/documents/retry-failed")
    def retry_failed_processing():
        payload = request.get_json(silent=True) or {}
        limit = _bounded_positive_int(payload.get("limit"), default=25, maximum=100)
        actor = str(payload.get("actor") or "fab_local_api")
        return jsonify(LocalDocumentProcessor(ledger, config).retry_failed(limit=limit, actor=actor))

    @app.post("/documents/retry-failed")
    def retry_failed_processing_form():
        session["fab_last_processing_summary"] = LocalDocumentProcessor(ledger, config).retry_failed(
            limit=25,
            actor="fab_dashboard",
        )
        return redirect(url_for("dashboard_page", _anchor="intake"))

    @app.post("/documents/<int:document_id>/retry-processing")
    def retry_document_processing_form(document_id: int):
        result = LocalDocumentProcessor(ledger, config).retry_document(
            document_id,
            actor="fab_dashboard",
        )
        session["fab_last_processing_summary"] = _single_processing_summary(result, retried=bool(result.get("retry")))
        return redirect(url_for("dashboard_page", _anchor="ledger"))

    @app.get("/api/routing")
    def routing_attempts():
        document_id = request.args.get("documentId")
        bookkeeping_record_id = request.args.get("bookkeepingRecordId") or request.args.get("bookkeeping_record_id")
        try:
            parsed_document_id = int(document_id) if document_id else None
            parsed_bookkeeping_record_id = int(bookkeeping_record_id) if bookkeeping_record_id else None
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid routing source id"}), 400
        return jsonify({
            "routingAttempts": ledger.list_routing_attempts(
                status=request.args.get("status"),
                target=request.args.get("target"),
                document_id=parsed_document_id,
                bookkeeping_record_id=parsed_bookkeeping_record_id,
                limit=_limit_arg(),
            )
        })

    @app.post("/api/documents/<int:document_id>/route")
    def route_document(document_id: int):
        payload = request.get_json(silent=True) or {}
        result = LocalRoutingService(ledger, config).prepare_document_route(
            document_id,
            target_system=payload.get("targetSystem"),
            workflow_run_id=payload.get("workflowRunId"),
        )
        status_code = 404 if result.get("status") == "not_found" else 200
        return jsonify(result), status_code

    @app.post("/documents/<int:document_id>/route")
    def route_document_form(document_id: int):
        session["fab_last_routing_summary"] = {
            "requested": 1,
            "draftPrepared": 0,
            "alreadyPrepared": 0,
            "needsReview": 0,
            "blocked": 0,
            "documents": [LocalRoutingService(ledger, config).prepare_document_route(document_id)],
        }
        status = session["fab_last_routing_summary"]["documents"][0].get("status")
        if status == "draft_prepared":
            session["fab_last_routing_summary"]["draftPrepared"] = 1
        elif status == "already_prepared":
            session["fab_last_routing_summary"]["alreadyPrepared"] = 1
        elif status == "needs_review":
            session["fab_last_routing_summary"]["needsReview"] = 1
        else:
            session["fab_last_routing_summary"]["blocked"] = 1
        return redirect(url_for("dashboard_page", _anchor="routing"))

    @app.post("/api/bookkeeping-records/<int:record_id>/route")
    def route_bookkeeping_record(record_id: int):
        payload = request.get_json(silent=True) or {}
        result = LocalRoutingService(ledger, config).prepare_bookkeeping_record_route(
            record_id,
            target_system=payload.get("targetSystem"),
            workflow_run_id=payload.get("workflowRunId"),
        )
        status_code = 404 if result.get("status") == "not_found" else 200
        return jsonify(result), status_code

    @app.post("/bookkeeping-records/<int:record_id>/route")
    def route_bookkeeping_record_form(record_id: int):
        result = LocalRoutingService(ledger, config).prepare_bookkeeping_record_route(record_id)
        session["fab_last_routing_summary"] = _single_record_routing_summary(result)
        return redirect(url_for("dashboard_page", _anchor="routing"))

    @app.post("/api/routing/prepare-ready")
    def prepare_ready_routes():
        payload = request.get_json(silent=True) or {}
        limit = _bounded_positive_int(payload.get("limit"), default=25, maximum=100)
        source_type = payload.get("sourceType") or payload.get("source_type") or "all"
        try:
            result = _prepare_ready_routes(ledger, config, source_type=source_type, limit=limit)
        except ValueError as exc:
            return jsonify({"success": False, "status": "invalid_source_type", "error": str(exc)}), 400
        return jsonify(result)

    @app.post("/routing/prepare-ready")
    def prepare_ready_routes_form():
        source_type = request.form.get("sourceType") or request.form.get("source_type") or "all"
        try:
            session["fab_last_routing_summary"] = _prepare_ready_routes(
                ledger,
                config,
                source_type=source_type,
                limit=25,
            )
        except ValueError as exc:
            session["fab_last_routing_summary"] = {
                "success": False,
                "status": "invalid_source_type",
                "error": str(exc),
                "externalSubmission": "not_executed",
            }
        return redirect(url_for("dashboard_page", _anchor="routing"))

    @app.get("/api/export-attempts")
    def export_attempts_api():
        document_id = request.args.get("documentId")
        bookkeeping_record_id = request.args.get("bookkeepingRecordId") or request.args.get("bookkeeping_record_id")
        try:
            parsed_document_id = int(document_id) if document_id else None
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid documentId"}), 400
        try:
            parsed_bookkeeping_record_id = int(bookkeeping_record_id) if bookkeeping_record_id else None
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid bookkeepingRecordId"}), 400
        return jsonify({
            "exportAttempts": ledger.list_export_attempts(
                status=request.args.get("status"),
                external_submission=request.args.get("externalSubmission") or request.args.get("external_submission"),
                target_system=request.args.get("targetSystem") or request.args.get("target_system"),
                document_id=parsed_document_id,
                bookkeeping_record_id=parsed_bookkeeping_record_id,
                limit=_limit_arg(),
            ),
            "approvalPhrase": EXPORT_APPROVAL_PHRASE,
            "rejectionPhrase": EXPORT_REJECTION_PHRASE,
            "resultConfirmationPhrase": EXPORT_RESULT_CONFIRMATION_PHRASE,
        })

    @app.get("/api/export-attempts/<int:export_attempt_id>")
    def export_attempt_detail(export_attempt_id: int):
        attempt = ledger.get_export_attempt(export_attempt_id)
        if not attempt:
            return jsonify({"error": "Export attempt not found"}), 404
        return jsonify(attempt)

    @app.get("/api/export-attempts/<int:export_attempt_id>/artifact")
    def export_attempt_artifact(export_attempt_id: int):
        result = LocalExportAttemptService(ledger, config).artifact_for_attempt(
            export_attempt_id,
            export_format=request.args.get("format") or "json",
            actor=request.args.get("actor") or "api",
        )
        if not result.get("success"):
            status_code = 404 if result.get("status") == "not_found" else 400
            return jsonify(result), status_code
        artifact = result["artifact"]
        if artifact.get("format") == "json":
            return jsonify({
                "status": result["status"],
                "artifact": {
                    key: value
                    for key, value in artifact.items()
                    if key != "content"
                },
                "content": json.loads(artifact["content"]),
            })
        return Response(
            artifact["content"],
            mimetype=artifact["contentType"],
            headers={
                "Content-Disposition": f"attachment; filename={artifact['filename']}",
                "X-FAB-External-Submission": artifact["externalSubmission"],
                "X-FAB-Master-Ledger-Checksum": artifact.get("checksum") or "",
            },
        )

    @app.post("/api/routing/<int:routing_attempt_id>/export-attempt")
    def prepare_export_attempt_from_route(routing_attempt_id: int):
        payload = request.get_json(silent=True) or {}
        result = LocalExportAttemptService(ledger, config).prepare_from_routing_attempt(
            routing_attempt_id,
            actor=payload.get("actor") or "api",
        )
        status_code = 404 if result.get("status") == "not_found" else 200
        return jsonify(result), status_code

    @app.post("/api/export-attempts/prepare-ready")
    def prepare_ready_export_attempts():
        payload = request.get_json(silent=True) or {}
        limit = _bounded_positive_int(payload.get("limit"), default=25, maximum=100)
        return jsonify(LocalExportAttemptService(ledger, config).prepare_ready_exports(limit=limit))

    @app.post("/exports/prepare-ready")
    def prepare_ready_export_attempts_form():
        session["fab_last_export_summary"] = LocalExportAttemptService(ledger, config).prepare_ready_exports(limit=25)
        return redirect(url_for("dashboard_page", _anchor="exports"))

    @app.post("/api/export-attempts/<int:export_attempt_id>/approve")
    def approve_export_attempt(export_attempt_id: int):
        payload = request.get_json(silent=True) or {}
        result = LocalExportAttemptService(ledger, config).approve_attempt(
            export_attempt_id,
            actor=payload.get("actor") or "api",
            confirmation=payload.get("confirmation"),
            resolution=payload.get("resolution"),
        )
        status_code = 200 if result.get("success") else 400
        if result.get("status") == "not_found":
            status_code = 404
        return jsonify(result), status_code

    @app.post("/export-attempts/<int:export_attempt_id>/approve")
    def approve_export_attempt_form(export_attempt_id: int):
        session["fab_last_export_summary"] = LocalExportAttemptService(ledger, config).approve_attempt(
            export_attempt_id,
            actor="dashboard",
            confirmation=request.form.get("confirmation"),
            resolution="Approved from local FAB dashboard.",
        )
        return redirect(url_for("dashboard_page", _anchor="exports"))

    @app.post("/api/export-attempts/<int:export_attempt_id>/reject")
    def reject_export_attempt(export_attempt_id: int):
        payload = request.get_json(silent=True) or {}
        result = LocalExportAttemptService(ledger, config).reject_attempt(
            export_attempt_id,
            actor=payload.get("actor") or "api",
            confirmation=payload.get("confirmation"),
            resolution=payload.get("resolution"),
        )
        status_code = 200 if result.get("success") else 400
        if result.get("status") == "not_found":
            status_code = 404
        return jsonify(result), status_code

    @app.post("/export-attempts/<int:export_attempt_id>/reject")
    def reject_export_attempt_form(export_attempt_id: int):
        session["fab_last_export_summary"] = LocalExportAttemptService(ledger, config).reject_attempt(
            export_attempt_id,
            actor="dashboard",
            confirmation=request.form.get("confirmation"),
            resolution=request.form.get("resolution") or "Rejected from local FAB dashboard.",
        )
        return redirect(url_for("dashboard_page", _anchor="exports"))

    @app.post("/api/export-attempts/<int:export_attempt_id>/regenerate")
    def regenerate_export_attempt(export_attempt_id: int):
        payload = request.get_json(silent=True) or {}
        result = LocalExportAttemptService(ledger, config).regenerate_attempt(
            export_attempt_id,
            actor=payload.get("actor") or "api",
        )
        status_code = 200 if result.get("success") else 400
        if result.get("status") == "not_found":
            status_code = 404
        return jsonify(result), status_code

    @app.post("/export-attempts/<int:export_attempt_id>/regenerate")
    def regenerate_export_attempt_form(export_attempt_id: int):
        session["fab_last_export_summary"] = LocalExportAttemptService(ledger, config).regenerate_attempt(
            export_attempt_id,
            actor="dashboard",
        )
        return redirect(url_for("dashboard_page", _anchor="exports"))

    @app.post("/api/export-attempts/<int:export_attempt_id>/execute")
    def execute_export_attempt(export_attempt_id: int):
        result = LocalExportAttemptService(ledger, config).execute_attempt(
            export_attempt_id,
            actor="api",
        )
        status_code = 200 if result.get("success") else 400
        if result.get("status") == "not_found":
            status_code = 404
        return jsonify(result), status_code

    @app.post("/export-attempts/<int:export_attempt_id>/execute")
    def execute_export_attempt_form(export_attempt_id: int):
        session["fab_last_export_summary"] = LocalExportAttemptService(ledger, config).execute_attempt(
            export_attempt_id,
            actor="dashboard",
        )
        return redirect(url_for("dashboard_page", _anchor="exports"))

    @app.post("/api/export-attempts/<int:export_attempt_id>/result")
    def record_export_attempt_result(export_attempt_id: int):
        payload = request.get_json(silent=True) or {}
        result = LocalExportAttemptService(ledger, config).record_result(
            export_attempt_id,
            status=payload.get("status"),
            external_id=payload.get("externalId") or payload.get("external_id"),
            result=payload.get("result"),
            actor=payload.get("actor") or "api",
            confirmation=payload.get("confirmation"),
        )
        status_code = 200 if result.get("success") else 400
        if result.get("status") == "not_found":
            status_code = 404
        return jsonify(result), status_code

    @app.post("/export-attempts/<int:export_attempt_id>/result")
    def record_export_attempt_result_form(export_attempt_id: int):
        session["fab_last_export_summary"] = LocalExportAttemptService(ledger, config).record_result(
            export_attempt_id,
            status=request.form.get("status") or "executed",
            external_id=request.form.get("externalId") or None,
            result={"source": "dashboard"},
            actor="dashboard",
            confirmation=request.form.get("confirmation"),
        )
        return redirect(url_for("dashboard_page", _anchor="exports"))

    @app.get("/api/bank-transactions")
    def bank_transactions():
        return jsonify({
            "bankTransactions": ledger.list_bank_transactions(
                account_identifier=request.args.get("accountIdentifier") or request.args.get("account_identifier"),
                status=request.args.get("status"),
                reconciliation_status=request.args.get("reconciliationStatus") or request.args.get("reconciliation_status"),
                limit=_limit_arg(),
            ),
            "bankStatementImports": ledger.list_bank_statement_imports(
                account_identifier=request.args.get("accountIdentifier") or request.args.get("account_identifier"),
                limit=25,
            ),
        })

    @app.post("/api/bank-transactions/import")
    def import_bank_transactions():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({
                "error": "Bank import body must be a JSON object",
                "externalSubmission": "not_executed",
            }), 400
        allowed_fields = {
            "accountIdentifier",
            "account_identifier",
            "actor",
            "bankTransactions",
            "contentBase64",
            "filename",
            "format",
            "source",
            "statementText",
            "statement_text",
            "transactions",
        }
        unexpected = sorted(set(payload) - allowed_fields)
        if unexpected:
            return jsonify({
                "error": f"Unsupported bank import field(s): {', '.join(unexpected)}",
                "externalSubmission": "not_executed",
            }), 400
        service = LocalBankTransactionImportService(ledger, config)
        account_identifier = payload.get("accountIdentifier") or payload.get("account_identifier") or "default"
        source = payload.get("source") or "api_import"
        filename = secure_filename(str(payload.get("filename") or "")) or None
        format_name = str(payload.get("format") or "json").strip().lower()
        actor = str(payload.get("actor") or "local_api_bank_import")[:200]
        if format_name not in SUPPORTED_BANK_STATEMENT_FORMATS:
            return jsonify({
                "error": f"Unsupported bank statement format: {format_name}",
                "supportedFormats": sorted(SUPPORTED_BANK_STATEMENT_FORMATS),
                "externalSubmission": "not_executed",
            }), 400
        encoded_content = payload.get("contentBase64")
        statement_text = payload.get("statementText") or payload.get("statement_text")
        if encoded_content is not None and statement_text is not None:
            return jsonify({
                "error": "Provide either contentBase64 or statementText, not both",
                "externalSubmission": "not_executed",
            }), 400
        try:
            if encoded_content is not None:
                if not filename or not isinstance(encoded_content, str):
                    return jsonify({
                        "error": "filename and contentBase64 are required for file import",
                        "externalSubmission": "not_executed",
                    }), 400
                max_encoded_length = 4 * ((MAX_BANK_STATEMENT_BYTES + 2) // 3)
                if len(encoded_content) > max_encoded_length:
                    return jsonify({
                        "error": "Bank statement exceeds the local import limit",
                        "maxBytes": MAX_BANK_STATEMENT_BYTES,
                        "externalSubmission": "not_executed",
                    }), 413
                extension = os.path.splitext(filename)[1].lower()
                if extension not in BANK_STATEMENT_FILE_EXTENSIONS[format_name]:
                    return jsonify({
                        "error": f"File extension does not match the {format_name} bank statement format",
                        "allowedExtensions": sorted(BANK_STATEMENT_FILE_EXTENSIONS[format_name]),
                        "externalSubmission": "not_executed",
                    }), 400
                try:
                    content = base64.b64decode(encoded_content, validate=True)
                except (binascii.Error, ValueError):
                    return jsonify({
                        "error": "contentBase64 is not valid base64",
                        "externalSubmission": "not_executed",
                    }), 400
                if not content or len(content) > MAX_BANK_STATEMENT_BYTES:
                    return jsonify({
                        "error": "Bank statement is empty or exceeds the local import limit",
                        "maxBytes": MAX_BANK_STATEMENT_BYTES,
                        "externalSubmission": "not_executed",
                    }), 413
                result = service.import_statement_bytes(
                    content,
                    format=format_name,
                    account_identifier=account_identifier,
                    source=source,
                    filename=filename,
                    actor=actor,
                )
            elif statement_text is not None:
                if not isinstance(statement_text, str):
                    return jsonify({
                        "error": "statementText must be a string",
                        "externalSubmission": "not_executed",
                    }), 400
                result = service.import_statement_text(
                    statement_text,
                    format=format_name,
                    account_identifier=account_identifier,
                    source=source,
                    filename=filename,
                    actor=actor,
                )
            else:
                transactions = payload.get("bankTransactions") or payload.get("transactions") or []
                if not isinstance(transactions, list):
                    return jsonify({"error": "bankTransactions must be a list"}), 400
                result = service.import_transactions(
                    transactions,
                    account_identifier=account_identifier,
                    source=source,
                    filename=filename,
                    format=format_name,
                    actor=actor,
                )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return jsonify({
                "error": str(exc),
                "externalSubmission": "not_executed",
            }), 400
        return jsonify(result)

    @app.post("/bank-transactions/import")
    def import_bank_transactions_form():
        service = LocalBankTransactionImportService(ledger, config)
        try:
            session["fab_last_bank_import_summary"] = service.import_statement_text(
                request.form.get("statementText") or "",
                format=request.form.get("format") or "json",
                account_identifier=request.form.get("accountIdentifier") or "default",
                source=request.form.get("source") or "manual_upload",
                filename=request.form.get("filename") or None,
            )
        except Exception as exc:
            session["fab_last_bank_import_summary"] = {
                "success": False,
                "status": "failed",
                "error": f"Could not import bank transactions: {exc}",
                "rowsSeen": 0,
                "rowsImported": 0,
                "duplicates": 0,
                "skipped": 0,
                "externalSubmission": "not_executed",
            }
        return redirect(url_for("dashboard_page", _anchor="bank"))

    @app.get("/api/reconciliation")
    def reconciliation_matches():
        document_id = request.args.get("documentId")
        try:
            parsed_document_id = int(document_id) if document_id else None
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid documentId"}), 400
        return jsonify({
            "reconciliationMatches": ledger.list_reconciliation_matches(
                status=request.args.get("status"),
                document_id=parsed_document_id,
                bank_transaction_id=request.args.get("bankTransactionId"),
                limit=_limit_arg(),
            )
        })

    @app.post("/api/reconciliation/run")
    def run_reconciliation():
        try:
            payload = request.get_json(silent=True)
        except (ValueError, RecursionError):
            return jsonify({"error": "Reconciliation request must be a JSON object"}), 400
        if payload is None:
            if request.get_data():
                return jsonify({"error": "Reconciliation request must be a JSON object"}), 400
            payload = {}
        if not isinstance(payload, dict):
            return jsonify({"error": "Reconciliation request must be a JSON object"}), 400
        if _json_depth_exceeds(payload, 32):
            return jsonify({"error": "Reconciliation request exceeds the permitted JSON depth"}), 400
        if "bankTransactions" in payload:
            bank_transactions = payload["bankTransactions"]
        else:
            bank_transactions = LocalBankTransactionImportService(ledger, config).transactions_for_reconciliation(
                limit=_bounded_positive_int(payload.get("limit"), default=100, maximum=500),
            )
        if not isinstance(bank_transactions, list):
            return jsonify({"error": "bankTransactions must be a list"}), 400
        try:
            result = LocalReconciliationService(ledger, config).run(
                bank_transactions,
                document_ids=payload.get("documentIds"),
                limit=_bounded_positive_int(payload.get("limit"), default=100, maximum=500),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(result)

    @app.post("/reconciliation/run")
    def run_reconciliation_form():
        raw_transactions = request.form.get("bankTransactionsJson") or ""
        try:
            if raw_transactions.strip():
                bank_transactions = json.loads(raw_transactions)
                if not isinstance(bank_transactions, list):
                    raise ValueError("Bank transactions JSON must be a list.")
            else:
                bank_transactions = LocalBankTransactionImportService(ledger, config).transactions_for_reconciliation(limit=100)
            session["fab_last_reconciliation_summary"] = LocalReconciliationService(ledger, config).run(
                bank_transactions,
                limit=100,
            )
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
            session["fab_last_reconciliation_summary"] = {
                "error": f"Could not run reconciliation: {exc}",
                "requestedTransactions": 0,
                "candidateDocuments": 0,
                "matchedCandidates": 0,
                "missingReceipts": 0,
                "unmatchedDocuments": 0,
                "matchesRecorded": 0,
                "reviewItemsCreated": 0,
                "results": [],
            }
        return redirect(url_for("dashboard_page", _anchor="reconciliation"))

    @app.post("/api/reconciliation/<int:reconciliation_match_id>/resolve")
    def resolve_reconciliation(reconciliation_match_id: int):
        payload = request.get_json(silent=True) or {}
        status = str(payload.get("status") or "resolved")
        if status not in RECONCILIATION_RESOLUTION_STATUSES:
            return jsonify({"error": "Invalid reconciliation resolution status"}), 400
        result = LocalReconciliationService(ledger, config).resolve_match(
            reconciliation_match_id,
            status=status,
            resolution=payload.get("resolution"),
        )
        status_code = 404 if result.get("status") == "not_found" else 200
        if result.get("status") == "invalid_status":
            status_code = 400
        if result.get("status") == "stale_evidence":
            status_code = 409
        return jsonify(result), status_code

    @app.post("/reconciliation/<int:reconciliation_match_id>/resolve")
    def resolve_reconciliation_form(reconciliation_match_id: int):
        status = str(request.form.get("status") or "resolved")
        if status not in RECONCILIATION_RESOLUTION_STATUSES:
            status = "resolved"
        result = LocalReconciliationService(ledger, config).resolve_match(
            reconciliation_match_id,
            status=status,
            resolution=f"Resolved from FAB dashboard as {status}.",
        )
        if not result.get("success"):
            return jsonify(result), {"not_found": 404, "invalid_status": 400}.get(result.get("status"), 409)
        return redirect(url_for("dashboard_page", _anchor="reconciliation"))

    @app.get("/api/backups")
    def list_backups():
        hai_principal = getattr(g, "fab_api_principal", None) == "hai"
        result = LocalBackupService(ledger, config).list_backups(
            limit=_limit_arg(),
            deep_verify=(
                False
                if hai_principal
                else _bool_value(request.args.get("verify"), default=True)
            ),
        )
        if hai_principal:
            result = _hai_backup_status_projection(result)
        return jsonify(result)

    @app.post("/api/backups")
    def create_backup():
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(LocalBackupService(ledger, config).create_backup(
                note=payload.get("note"),
                require_complete_source_evidence=(
                    payload.get("requireCompleteSourceEvidence", True) is not False
                ),
                actor=str(payload.get("actor") or "fab_local_api")[:200],
            ))
        except ValueError as exc:
            return jsonify({"success": False, "status": "blocked", "error": str(exc)}), 409
        except Exception as exc:
            return jsonify({"success": False, "status": "failed", "error": str(exc)}), 500

    @app.post("/backups/create")
    def create_backup_form():
        try:
            session["fab_last_backup_summary"] = LocalBackupService(ledger, config).create_backup(
                note="Created from FAB dashboard.",
                require_complete_source_evidence=True,
                actor="fab_local_dashboard",
            )
        except Exception as exc:
            session["fab_last_backup_summary"] = {
                "success": False,
                "status": "failed",
                "error": str(exc),
            }
        return redirect(url_for("dashboard_page", _anchor="backups"))

    @app.get("/api/backups/inspect")
    def inspect_backup():
        backup_path = request.args.get("backupPath") or request.args.get("backupFilename")
        try:
            return jsonify(LocalBackupService(ledger, config).inspect_backup(str(backup_path or "")))
        except Exception as exc:
            return jsonify({"success": False, "status": "invalid", "error": str(exc)}), 400

    @app.get("/api/backups/restore-plan")
    def plan_backup_restore():
        backup_path = request.args.get("backupPath") or request.args.get("backupFilename")
        try:
            return jsonify(LocalBackupService(ledger, config).plan_restore(
                str(backup_path or ""),
                restore_mode=str(request.args.get("restoreMode") or RESTORE_MODE_LEDGER_ONLY),
            ))
        except Exception as exc:
            return jsonify({
                "success": False,
                "status": "invalid",
                "error": str(exc),
                "externalSubmission": "not_executed",
            }), 400

    @app.post("/api/backups/restore")
    def restore_backup():
        payload = request.get_json(silent=True) or {}
        try:
            result = LocalBackupService(ledger, config).restore_backup(
                str(payload.get("backupPath") or payload.get("backupFilename") or ""),
                str(payload.get("confirmation") or ""),
                restore_mode=str(payload.get("restoreMode") or RESTORE_MODE_LEDGER_ONLY),
                actor=str(payload.get("actor") or "fab_local_api")[:200],
            )
        except Exception as exc:
            return jsonify({"success": False, "status": "failed", "error": str(exc)}), 400
        status_code = 200 if result.get("success") else (
            423 if result.get("status") in {"blocked", "worker_active"} else 400
        )
        return jsonify(result), status_code

    @app.post("/backups/restore")
    def restore_backup_form():
        try:
            session["fab_last_backup_summary"] = LocalBackupService(ledger, config).restore_backup(
                str(request.form.get("backupPath") or ""),
                str(request.form.get("confirmation") or ""),
                restore_mode=str(request.form.get("restoreMode") or RESTORE_MODE_LEDGER_ONLY),
                actor="fab_local_dashboard",
            )
        except Exception as exc:
            session["fab_last_backup_summary"] = {
                "success": False,
                "status": "failed",
                "error": str(exc),
            }
        return redirect(url_for("dashboard_page", _anchor="backups"))

    @app.post("/api/intake/rescan")
    def rescan_intake():
        paths = app.config["FAB_LOCAL_INTAKE_PATHS"]
        if not paths:
            return jsonify({
                "error": "No intake folders configured",
                "intakePaths": [],
            }), 400
        summary = LocalFolderIntake(
            ledger,
            allowed_extensions=app.config["FAB_LOCAL_INTAKE_EXTENSIONS"],
        ).rescan(paths)
        return jsonify(summary)

    @app.post("/api/intake/upload")
    def upload_intake_document():
        paths = app.config["FAB_LOCAL_INTAKE_PATHS"]
        if not paths:
            return jsonify({"error": "No intake folders configured"}), 400
        payload = request.get_json(silent=True) or {}
        filename = secure_filename(str(payload.get("filename") or ""))
        encoded_content = payload.get("contentBase64")
        if not filename or not isinstance(encoded_content, str):
            return jsonify({"error": "filename and contentBase64 are required"}), 400

        extension = os.path.splitext(filename)[1].lower().lstrip(".")
        allowed_extensions = {
            str(value).lower().lstrip(".")
            for value in app.config["FAB_LOCAL_INTAKE_EXTENSIONS"]
        }
        if "*" not in allowed_extensions and extension not in allowed_extensions:
            return jsonify({
                "error": "Unsupported intake file extension",
                "allowedExtensions": sorted(allowed_extensions),
            }), 400
        try:
            content = base64.b64decode(encoded_content, validate=True)
        except (binascii.Error, ValueError):
            return jsonify({"error": "contentBase64 is not valid base64"}), 400

        try:
            max_bytes = int(
                config.get("fab_local_upload_max_bytes")
                or config.get("operations_local_upload_max_bytes")
                or config.get("local_upload_max_bytes")
                or DEFAULT_LOCAL_UPLOAD_MAX_BYTES
            )
        except (TypeError, ValueError):
            max_bytes = DEFAULT_LOCAL_UPLOAD_MAX_BYTES
        max_bytes = max(1, min(max_bytes, DEFAULT_LOCAL_UPLOAD_MAX_BYTES))
        if not content or len(content) > max_bytes:
            return jsonify({
                "error": "Uploaded file is empty or exceeds the local upload limit",
                "maxBytes": max_bytes,
            }), 413

        intake_root = os.path.abspath(str(paths[0]))
        os.makedirs(intake_root, exist_ok=True)
        destination = os.path.abspath(os.path.join(intake_root, filename))
        if os.path.commonpath((intake_root, destination)) != intake_root:
            return jsonify({"error": "Invalid intake filename"}), 400
        content_digest = hashlib.sha256(content).hexdigest()
        reused_existing = False
        if os.path.lexists(destination):
            reused_existing = _file_matches_content(destination, content)
        if os.path.lexists(destination) and not reused_existing:
            stem, suffix = os.path.splitext(filename)
            destination = os.path.join(
                intake_root,
                f"{stem}-{content_digest[:12]}{suffix}",
            )
            if os.path.lexists(destination):
                reused_existing = _file_matches_content(destination, content)
                if not reused_existing:
                    return jsonify({"error": "An intake filename already exists with different content"}), 409
        if not reused_existing:
            try:
                with open(destination, "xb") as handle:
                    handle.write(content)
            except FileExistsError:
                if not _file_matches_content(destination, content):
                    return jsonify({"error": "An intake filename already exists with different content"}), 409
                reused_existing = True

        intake = LocalFolderIntake(
            ledger,
            allowed_extensions=app.config["FAB_LOCAL_INTAKE_EXTENSIONS"],
        )
        registration = intake.register_local_file(intake_root, destination)
        document = registration.get("document") if isinstance(registration, dict) else None
        if not isinstance(document, dict) or not document.get("id"):
            return jsonify({
                "success": False,
                "status": "stored_not_registered",
                "error": "The upload was stored but could not be confirmed in the bookkeeping ledger. Run an intake rescan before retrying.",
                "filename": os.path.basename(destination),
                "externalSubmission": "not_executed",
            }), 503
        return jsonify({
            "success": True,
            "status": "registered",
            "filename": os.path.basename(destination),
            "sizeBytes": len(content),
            "document": document,
            "idempotentReplay": reused_existing,
            "externalSubmission": "not_executed",
        }), 200 if reused_existing else 201

    @app.get("/api/connectors/google-drive/relay")
    def google_drive_relay_status_api():
        return jsonify(DriveRelayIntakeService(ledger, config).status())

    @app.get("/api/connectors/gmail/authorization")
    def gmail_authorization_status_api():
        return jsonify(app.config["FAB_GMAIL_AUTH"].status())

    @app.post("/api/connectors/gmail/credentials")
    def install_gmail_credentials_api():
        if host not in LOOPBACK_HOSTS:
            return jsonify({"error": "Desktop OAuth setup is available only on the loopback FAB API"}), 403
        payload = request.get_json(silent=True) or {}
        encoded = str(payload.get("contentBase64") or "")
        if len(encoded) > 90_000:
            return jsonify({"error": "Google OAuth credential payload exceeds the 64 KB limit"}), 413
        replace = payload.get("replace", False)
        if not isinstance(replace, bool):
            return jsonify({"error": "replace must be a boolean"}), 400
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return jsonify({"error": "contentBase64 must contain valid base64 data"}), 400
        try:
            result = app.config["FAB_GMAIL_AUTH"].install_credentials(
                content,
                filename=str(payload.get("filename") or ""),
                replace=replace,
                actor=payload.get("actor") or "local_api",
            )
        except ValueError as exc:
            return jsonify({"error": str(exc), "externalSubmission": "not_executed"}), 400
        except (FileExistsError, RuntimeError) as exc:
            return jsonify({"error": str(exc), "externalSubmission": "not_executed"}), 409
        return jsonify(result), 201

    @app.post("/api/connectors/gmail/authorization/start")
    def start_gmail_authorization_api():
        if host not in LOOPBACK_HOSTS:
            return jsonify({"error": "Desktop OAuth setup is available only on the loopback FAB API"}), 403
        payload = request.get_json(silent=True) or {}
        result = app.config["FAB_GMAIL_AUTH"].start(
            actor=payload.get("actor") or "local_api",
        )
        if result.get("success"):
            status_code = 200 if result.get("status") == "authorization_in_progress" else 202
        else:
            status_code = 409
        return jsonify(result), status_code

    @app.get("/api/connectors/google-drive/authorization")
    def google_drive_authorization_status_api():
        return jsonify(app.config["FAB_GOOGLE_DRIVE_AUTH"].status())

    @app.post("/api/connectors/google-drive/credentials")
    def install_google_drive_credentials_api():
        if host not in LOOPBACK_HOSTS:
            return jsonify({"error": "Desktop OAuth setup is available only on the loopback FAB API"}), 403
        payload = request.get_json(silent=True) or {}
        encoded = str(payload.get("contentBase64") or "")
        if len(encoded) > 90_000:
            return jsonify({"error": "Google OAuth credential payload exceeds the 64 KB limit"}), 413
        replace = payload.get("replace", False)
        if not isinstance(replace, bool):
            return jsonify({"error": "replace must be a boolean"}), 400
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return jsonify({"error": "contentBase64 must contain valid base64 data"}), 400
        try:
            result = app.config["FAB_GOOGLE_DRIVE_AUTH"].install_credentials(
                content,
                filename=str(payload.get("filename") or ""),
                replace=replace,
                actor=payload.get("actor") or "local_api",
            )
        except ValueError as exc:
            return jsonify({"error": str(exc), "externalSubmission": "not_executed"}), 400
        except (FileExistsError, RuntimeError) as exc:
            return jsonify({"error": str(exc), "externalSubmission": "not_executed"}), 409
        return jsonify(result), 201

    @app.post("/api/connectors/google-drive/authorization/start")
    def start_google_drive_authorization_api():
        if host not in LOOPBACK_HOSTS:
            return jsonify({"error": "Desktop OAuth setup is available only on the loopback FAB API"}), 403
        payload = request.get_json(silent=True) or {}
        result = app.config["FAB_GOOGLE_DRIVE_AUTH"].start(
            actor=payload.get("actor") or "local_api",
        )
        if result.get("success"):
            status_code = 200 if result.get("status") == "authorization_in_progress" else 202
        else:
            status_code = 409
        return jsonify(result), status_code

    @app.post("/api/connectors/google-drive/relay")
    def google_drive_relay_intake_api():
        upload = request.files.get("file")
        if upload is None:
            return jsonify({"error": "Multipart file field 'file' is required"}), 400
        metadata_text = request.form.get("metadata") or "{}"
        try:
            metadata = json.loads(metadata_text)
        except (TypeError, ValueError):
            return jsonify({"error": "metadata must be a valid JSON object"}), 400
        if not isinstance(metadata, dict):
            return jsonify({"error": "metadata must be a valid JSON object"}), 400

        service = DriveRelayIntakeService(ledger, config)
        max_bytes = int(service.status()["maxBytes"])
        content = upload.stream.read(max_bytes + 1)
        if len(content) > max_bytes:
            return jsonify({
                "success": False,
                "status": "rejected",
                "reasons": ["drive_file_exceeds_relay_limit"],
                "maxBytes": max_bytes,
                "externalSubmission": "not_executed",
            }), 413
        result = service.ingest(
            content,
            provider_file_id=metadata.get("providerFileId"),
            source_folder_id=metadata.get("sourceFolderId"),
            filename=metadata.get("filename") or upload.filename,
            mime_type=metadata.get("mimeType") or upload.mimetype,
            provider_size=metadata.get("sizeBytes"),
            expected_sha256=metadata.get("sha256"),
            created_time=metadata.get("createdTime"),
            modified_time=metadata.get("modifiedTime"),
            md5_checksum=metadata.get("md5Checksum"),
            web_view_link=metadata.get("webViewLink"),
            actor=metadata.get("actor") or "local_api_drive_relay",
        )
        return jsonify(result), 201 if result.get("success") else 400

    @app.post("/intake/rescan")
    def rescan_intake_form():
        paths = app.config["FAB_LOCAL_INTAKE_PATHS"]
        if paths:
            session["fab_last_intake_summary"] = LocalFolderIntake(
                ledger,
                allowed_extensions=app.config["FAB_LOCAL_INTAKE_EXTENSIONS"],
            ).rescan(paths)
        else:
            session["fab_last_intake_summary"] = {
                "folders": [],
                "allowedExtensions": app.config["FAB_LOCAL_INTAKE_EXTENSIONS"],
                "scanned": 0,
                "registered": 0,
                "duplicates": 0,
                "alreadyRegistered": 0,
                "skipped": [{"reason": "no_intake_folders_configured"}],
                "documents": [],
            }
        return redirect(url_for("dashboard_page", _anchor="intake"))

    @app.get("/api/review")
    def review_queue():
        requested_status = str(request.args.get("status") or "").strip().lower()
        status_filter: Any = requested_status or None
        if requested_status == "open":
            status_filter = ("pending", "in_review")
        limit = _limit_arg()
        offset = _bounded_nonnegative_int(
            request.args.get("offset"),
            default=0,
            maximum=10_000_000,
        )
        review_items = ledger.list_review_work_item_page(
            status=status_filter,
            limit=limit,
            offset=offset,
        )
        work_items = _review_work_items(ledger, review_items, config)
        include_summary = _bool_value(request.args.get("includeSummary"), default=True)
        summary = _review_queue_summary(ledger, status_filter) if include_summary else {}
        total_work_items = (
            int(summary.get("workItems") or 0)
            if include_summary
            else ledger.count_review_work_item_groups(status=status_filter)
        )
        include_category_options = _bool_value(
            request.args.get("includeCategoryOptions"),
            default=True,
        )
        summary_view = str(request.args.get("view") or "").strip().lower() == "summary"
        response_work_items = (
            [_compact_review_work_item_summary(item) for item in work_items]
            if summary_view
            else work_items
        )
        returned_work_items = len(work_items)
        next_offset = offset + returned_work_items
        has_more = next_offset < total_work_items
        return jsonify({
            "reviewItems": [] if summary_view else review_items,
            "workItems": response_work_items,
            "categoryOptions": (
                _review_category_options(ledger, config)
                if include_category_options
                else []
            ),
            "capabilities": {
                "exactVendorCategoryBatch": {
                    "enabled": True,
                    "requiresExplicitApproval": True,
                    "matchPolicy": "exact_normalized_vendor_and_target_system",
                    "propagatedFields": ["category", "targetSystem"],
                    "preservedFields": ["vendorName", "transactionDate", "totalAmount", "taxAmount"],
                    "excludedDocuments": ["marked_duplicate"],
                    "preservedReviewGates": ["duplicate_candidate", "validation_failed"],
                },
            },
            "summary": summary,
            "pagination": {
                "scope": "work_items",
                "offset": offset,
                "limit": limit,
                "returned": returned_work_items,
                "total": total_work_items,
                "hasMore": has_more,
                "nextOffset": next_offset if has_more else None,
                "previousOffset": max(0, offset - limit) if offset else None,
            },
        })

    @app.post("/api/review/<int:review_item_id>/resolve")
    def resolve_review(review_item_id: int):
        payload = request.get_json(silent=True) or {}
        status = str(payload.get("status") or "resolved")
        if status not in REVIEW_RESOLUTION_STATUSES:
            return jsonify({"error": "Invalid review resolution status"}), 400
        resolution = payload.get("resolution")
        result = LocalReviewService(ledger).resolve_review_item(
            review_item_id,
            status=status,
            resolution=str(resolution) if resolution is not None else None,
            corrections=payload.get("corrections") or _corrections_from_mapping(payload),
            learn_rule=bool(payload.get("learnRule", True)),
            apply_to_matching_vendor=bool(payload.get("applyToMatchingVendor", False)),
        )
        status_code = {
            "not_found": 404,
            "already_resolved": 409,
            "duplicate_candidate_not_found": 404,
            "duplicate_candidate_already_resolved": 409,
            "duplicate_candidate_mismatch": 409,
            "duplicate_candidate_selection_required": 409,
            "invalid_duplicate_decision": 400,
            "invalid_financial_correction": 400,
            "stale_evidence": 409,
        }.get(result.get("status"), 200)
        return jsonify(result), status_code

    @app.post("/review/<int:review_item_id>/resolve")
    def resolve_review_form(review_item_id: int):
        status = str(request.form.get("status") or "resolved")
        if status not in REVIEW_RESOLUTION_STATUSES:
            status = "resolved"
        resolution = request.form.get("resolution") or None
        result = LocalReviewService(ledger).resolve_review_item(
            review_item_id,
            status=status,
            resolution=resolution,
            corrections=_corrections_from_mapping(request.form),
            learn_rule=True,
        )
        if not result.get("success"):
            return jsonify(result), {"not_found": 404, "invalid_financial_correction": 400}.get(result.get("status"), 409)
        return redirect(url_for("dashboard_page", _anchor="review"))

    @app.get("/api/rules")
    def rules():
        return jsonify({
            "vendorCategoryRules": ledger.list_vendor_category_rules(
                vendor_name=request.args.get("vendorName"),
                status=request.args.get("status"),
                limit=_limit_arg(),
            )
        })

    @app.get("/api/vendors")
    def vendors():
        return jsonify({
            "vendors": ledger.list_vendor_summaries(limit=_limit_arg()),
            "externalSubmission": "not_executed",
        })

    @app.get("/api/categories")
    def categories():
        return jsonify({
            "categories": ledger.list_category_summaries(limit=_limit_arg()),
            "externalSubmission": "not_executed",
        })

    @app.post("/api/rules")
    def upsert_rule():
        payload = request.get_json(silent=True) or {}
        payload.setdefault("status", "suggested")
        try:
            rule_id = ledger.upsert_vendor_category_rule(payload)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        ledger.record_audit_event({
            "action": "local_api.vendor_category_rule.upsert",
            "entityType": "vendor_category_rule",
            "entityId": str(rule_id),
            "details": {
                "vendorName": payload.get("vendorName"),
                "category": payload.get("category"),
                "status": payload.get("status", "suggested"),
            },
        })
        return jsonify({"success": True, "ruleId": rule_id})

    @app.post("/api/rules/<int:rule_id>/resolve")
    def resolve_rule(rule_id: int):
        payload = request.get_json(silent=True) or {}
        status = str(payload.get("status") or "").strip()
        if status not in RULE_RESOLUTION_STATUSES:
            return jsonify({"error": "Unsupported rule status"}), 400
        try:
            rule = ledger.update_vendor_category_rule_status(
                rule_id,
                status,
                resolution=payload.get("resolution"),
                actor=payload.get("actor") or "local_api",
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if not rule:
            return jsonify({"error": "Vendor category rule not found"}), 404
        ledger.record_audit_event({
            "action": "local_api.vendor_category_rule.status_changed",
            "entityType": "vendor_category_rule",
            "entityId": str(rule_id),
            "details": {
                "status": status,
                "resolution": payload.get("resolution"),
                "actor": payload.get("actor") or "local_api",
            },
        })
        return jsonify({"success": True, "rule": rule})

    @app.post("/rules/<int:rule_id>/resolve")
    def resolve_rule_form(rule_id: int):
        status = str(request.form.get("status") or "").strip()
        if status not in RULE_RESOLUTION_STATUSES:
            status = "suggested"
        rule = ledger.update_vendor_category_rule_status(
            rule_id,
            status,
            resolution=request.form.get("resolution") or None,
            actor="local_dashboard",
        )
        if rule:
            ledger.record_audit_event({
                "action": "local_dashboard.vendor_category_rule.status_changed",
                "entityType": "vendor_category_rule",
                "entityId": str(rule_id),
                "details": {
                    "status": status,
                    "resolution": request.form.get("resolution") or None,
                    "actor": "local_dashboard",
                },
            })
        return redirect(url_for("dashboard_page", _anchor="rules"))

    @app.get("/api/corrections")
    def corrections():
        document_id = request.args.get("documentId")
        return jsonify({
            "reviewCorrections": ledger.list_review_corrections(
                document_id=int(document_id) if document_id else None,
                limit=_limit_arg(),
            )
        })

    @app.get("/api/audit")
    def audit_events():
        return jsonify({"auditEvents": ledger.list_audit_events(limit=_limit_arg())})

    @app.get("/api/control-center/resources")
    def control_center_resources_api():
        resources: Dict[str, Dict[str, Any]] = {}
        with ledger.read_snapshot():
            shared_resource_names = {
                "autonomy",
                "metrics",
                "masterLedger",
                "exceptions",
                "health",
                "closeReadiness",
                "driveWaveWorkOrders",
            }
            try:
                shared_metrics = ledger.dashboard_metrics()
                shared_master_ledger = LocalMasterLedgerService(ledger, config).project(limit=250)
                shared_health = LocalOperationsHealth(ledger, config).summarize(
                    metrics=shared_metrics,
                    master_ledger=shared_master_ledger,
                )
                shared_exceptions = LocalExceptionQueueService(ledger, config).list_exceptions(
                    limit=25,
                    include_entities=True,
                    health=shared_health,
                    master_ledger=shared_master_ledger,
                )
                shared_close_readiness = LocalCloseReadinessService(ledger, config).assess(
                    metrics=shared_metrics,
                    health=shared_health,
                    master_ledger=shared_master_ledger,
                )
                compact_master_ledger = dict(shared_master_ledger)
                master_rows = list(compact_master_ledger.pop("rows", []) or [])
                compact_master_ledger["dataThroughDate"] = max(
                    (
                        str(row.get("recordDate"))
                        for row in master_rows
                        if row.get("recordDate")
                    ),
                    default=None,
                )
                compact_master_ledger["rowsOmitted"] = len(master_rows)
                shared_values = {
                    "metrics": shared_metrics,
                    "masterLedger": compact_master_ledger,
                    "exceptions": shared_exceptions,
                    "health": shared_health,
                    "closeReadiness": shared_close_readiness,
                }
                for resource_name, value in shared_values.items():
                    resources[resource_name] = {"ok": True, "value": value}
                try:
                    shared_readiness = readiness_service.summarize()
                    resources["sourceReadiness"] = {
                        "ok": True,
                        "value": shared_readiness,
                    }
                    resources["autonomy"] = {
                        "ok": True,
                        "value": _autonomy_service(
                            ledger,
                            config,
                            readiness_service,
                            intake_paths,
                            intake_extensions,
                        ).plan(
                            limit=25,
                            readiness_summary=shared_readiness,
                            health_summary=shared_health,
                            exceptions_summary=shared_exceptions,
                            close_readiness_summary=shared_close_readiness,
                            metrics_summary=shared_metrics,
                            master_ledger_summary=shared_master_ledger,
                        ),
                    }
                except Exception as exc:
                    app.logger.warning(
                        "Shared autonomy control-center projection failed (%s)",
                        type(exc).__name__,
                    )
                    resources["autonomy"] = {
                        "ok": False,
                        "error": "FAB resource could not be read",
                    }
            except Exception as exc:
                app.logger.warning(
                    "Shared control-center projection failed (%s)",
                    type(exc).__name__,
                )
                for resource_name in shared_resource_names:
                    resources[resource_name] = {
                        "ok": False,
                        "error": "FAB resource could not be read",
                    }
            try:
                resources["compliance"] = {
                    "ok": True,
                    "value": {
                        "summary": LocalComplianceService(ledger, config).summary(
                            metrics=shared_metrics,
                        ),
                        "assessments": ledger.list_compliance_assessments(limit=5),
                        "statutoryStatus": "provisional",
                        "filingStatus": "not_filed",
                        "externalFiling": "not_executed",
                    },
                }
            except Exception as exc:
                app.logger.warning(
                    "Shared compliance control-center projection failed (%s)",
                    type(exc).__name__,
                )
            try:
                shared_category_intents = fab_category_intents(
                    ledger,
                    config,
                    target_system="waveapps_business",
                )
                shared_wave_setup = LocalWaveSetupService(config).status(
                    ledger,
                    "waveapps_business",
                    category_intents=shared_category_intents,
                )
                shared_receipt_executor = LocalWaveReceiptExecutorService(
                    ledger,
                    config,
                ).status()
                shared_drive_wave_status = DriveWaveDeliveryService(
                    ledger,
                    config,
                ).status(
                    wave_setup=shared_wave_setup,
                    receipt_executor=shared_receipt_executor,
                )
                resources.update({
                    "waveSetup": {"ok": True, "value": shared_wave_setup},
                    "waveReceiptExecutor": {
                        "ok": True,
                        "value": shared_receipt_executor,
                    },
                    "driveWaveStatus": {
                        "ok": True,
                        "value": shared_drive_wave_status,
                    },
                })
                try:
                    resources["driveWaveWorkOrders"] = {
                        "ok": True,
                        "value": DriveWaveDeliveryService(ledger, config).list_work_orders(
                            limit=200,
                            compact=True,
                            item_limit=25,
                            connector_status=shared_drive_wave_status,
                        ),
                    }
                except Exception as exc:
                    app.logger.warning(
                        "Shared delivery control-center projection failed (%s)",
                        type(exc).__name__,
                    )
                    resources["driveWaveWorkOrders"] = {
                        "ok": False,
                        "error": "FAB resource could not be read",
                    }
            except Exception as exc:
                app.logger.warning(
                    "Shared Wave control-center projection failed (%s)",
                    type(exc).__name__,
                )
            for resource_name, resource_path in CONTROL_CENTER_BATCH_PATHS.items():
                if resource_name in resources:
                    continue
                try:
                    resources[resource_name] = {
                        "ok": True,
                        "value": _read_internal_json_resource(
                            app,
                            resource_path,
                            forbidden_endpoint="control_center_resources_api",
                        ),
                    }
                except Exception as exc:
                    app.logger.warning(
                        "Control-center resource %s failed (%s)",
                        resource_name,
                        type(exc).__name__,
                    )
                    resources[resource_name] = {
                        "ok": False,
                        "error": "FAB resource could not be read",
                    }
            review_envelope = resources.get("reviewQueue") or {}
            review_value = review_envelope.get("value")
            if review_envelope.get("ok") is True and isinstance(review_value, dict):
                wave_setup_value = (resources.get("waveSetup") or {}).get("value") or {}
                category_options = [
                    str(intent.get("category"))
                    for intent in wave_setup_value.get("categoryIntents") or []
                    if isinstance(intent, dict) and intent.get("category")
                ]
                if not category_options:
                    category_options = _review_category_options(ledger, config)
                review_value["categoryOptions"] = category_options
        return jsonify({
            "version": 1,
            "resources": resources,
        })

    def _limit_arg() -> int:
        return _bounded_positive_int(
            request.args.get("limit"),
            default=100,
            maximum=500,
        )

    return app


def _json_depth_exceeds(value: Any, maximum: int) -> bool:
    pending = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > maximum:
            return True
        if isinstance(current, dict):
            pending.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            pending.extend((child, depth + 1) for child in current)
    return False


def _read_internal_json_resource(
    app: Flask,
    path: str,
    *,
    forbidden_endpoint: str,
) -> Dict[str, Any]:
    with app.test_request_context(path, method="GET"):
        adapter = app.url_map.bind_to_environ(request.environ)
        endpoint, values = adapter.match(method="GET")
        if endpoint == forbidden_endpoint:
            raise RuntimeError("Recursive control-center resource")
        response = app.make_response(app.view_functions[endpoint](**values))
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError("Control-center resource returned an error")
        payload = response.get_json(silent=True)
        if not isinstance(payload, dict):
            raise RuntimeError("Control-center resource returned invalid JSON")
        return payload


def _format_money(value: Any) -> str:
    if value is None or value == "":
        return "-"
    try:
        return f"EUR {float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def _format_confidence(value: Any) -> str:
    if value is None or value == "":
        return "-"
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return str(value)


def _reconciliation_matches_for_record(
    ledger: LocalOperationsLedger,
    record: Dict[str, Any],
    bank_transaction: Optional[Dict[str, Any]] = None,
    limit: int = 100,
) -> list:
    matches: list = []
    seen_ids = set()

    def add_rows(rows: list) -> None:
        for row in rows:
            row_id = row.get("id")
            if row_id in seen_ids:
                continue
            seen_ids.add(row_id)
            matches.append(row)

    if record.get("document_id"):
        add_rows(ledger.list_reconciliation_matches(
            document_id=int(record["document_id"]),
            limit=limit,
        ))

    bank_transaction_id = None
    if bank_transaction:
        bank_transaction_id = bank_transaction.get("transaction_id")
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    latest = metadata.get("latestReconciliation") if isinstance(metadata.get("latestReconciliation"), dict) else {}
    if not bank_transaction_id:
        bank_transaction_id = latest.get("bankTransactionId")
    if bank_transaction_id:
        add_rows(ledger.list_reconciliation_matches(
            bank_transaction_id=str(bank_transaction_id),
            limit=limit,
        ))

    return matches[: _bounded_positive_int(limit, default=100, maximum=500)]


def _document_source_preview(document: Dict[str, Any], character_limit: int = 5000) -> Dict[str, Any]:
    path = document.get("storage_path")
    if not path:
        return {"status": "No source file path is recorded.", "path": None, "text": None}
    normalized_path = os.path.abspath(os.path.expandvars(os.path.expanduser(str(path))))
    if not os.path.exists(normalized_path):
        return {"status": "Source file is missing from disk.", "path": normalized_path, "text": None}
    extension = os.path.splitext(normalized_path)[1].lower()
    mime_type = str(document.get("mime_type") or "").lower()
    file_size = os.path.getsize(normalized_path)
    if extension in {".txt", ".csv", ".json", ".xml"} or mime_type.startswith("text/"):
        try:
            with open(normalized_path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read(character_limit + 1)
        except OSError as exc:
            return {"status": f"Could not read source preview: {exc}", "path": normalized_path, "text": None}
        truncated = len(text) > character_limit
        if truncated:
            text = text[:character_limit] + "\n... truncated ..."
        return {
            "status": f"Text preview from local source file ({file_size} bytes).",
            "path": normalized_path,
            "text": text,
        }
    return {
        "status": f"Binary source file recorded ({file_size} bytes). Open from local path if visual inspection is needed.",
        "path": normalized_path,
        "text": None,
    }


def _compact_json(value: Any) -> str:
    if value is None:
        return "-"
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _pretty_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, indent=2)


def _readiness_service(
    config: Dict[str, Any],
    ledger_path: str,
    host: str,
    token_configured: bool,
    intake_paths: list,
    intake_extensions: list,
) -> LocalReadinessService:
    return LocalReadinessService(
        config,
        ledger_path=ledger_path,
        api_host=host,
        api_token_configured=token_configured,
        intake_paths=intake_paths,
        intake_extensions=intake_extensions,
    )


def _autonomy_service(
    ledger: LocalOperationsLedger,
    config: Dict[str, Any],
    readiness: LocalReadinessService,
    intake_paths: list,
    intake_extensions: list,
) -> LocalAutonomousService:
    return LocalAutonomousService(
        ledger,
        config,
        readiness=readiness,
        intake_paths=intake_paths,
        intake_extensions=intake_extensions,
    )


def _combined_health_status(*statuses: Any) -> str:
    rank = {
        "ok": 0,
        "ready": 0,
        "attention": 1,
        "attention_required": 1,
        "blocked": 2,
    }
    normalized = [str(status or "ok") for status in statuses]
    return max(normalized, key=lambda status: rank.get(status, 1))


def _workflow_recovery_service(
    ledger: LocalOperationsLedger,
    config: Dict[str, Any],
    readiness: LocalReadinessService,
    intake_paths: list,
    intake_extensions: list,
) -> LocalWorkflowRecoveryService:
    return LocalWorkflowRecoveryService(
        ledger,
        config,
        readiness=readiness,
        intake_paths=intake_paths,
        intake_extensions=intake_extensions,
    )


def _workflow_recovery_scheduler(
    ledger: LocalOperationsLedger,
    config: Dict[str, Any],
    readiness: LocalReadinessService,
    intake_paths: list,
    intake_extensions: list,
) -> LocalWorkflowRecoveryScheduler:
    return LocalWorkflowRecoveryScheduler(
        ledger,
        config,
        readiness=readiness,
        intake_paths=intake_paths,
        intake_extensions=intake_extensions,
    )


def _compact_wave_plan_summary(plan: Dict[str, Any]) -> Dict[str, Any]:
    workflow_plan = plan.get("workflow_plan") or {}
    operations = plan.get("operations") or []
    return {
        "status": plan.get("status"),
        "canRunAutonomously": plan.get("can_run_autonomously"),
        "workflowId": workflow_plan.get("workflow_id"),
        "operationCount": plan.get("operationCount"),
        "waveReportSnapshots": (plan.get("waveReportSnapshots") or {}).get("snapshotCount"),
        "waveOperationSnapshots": (plan.get("waveOperationSnapshots") or {}).get("snapshotCount"),
        "waveReportControls": {
            "status": (plan.get("waveReportControls") or {}).get("status"),
            "requiredReportCount": (plan.get("waveReportControls") or {}).get("requiredReportCount"),
            "coveredReportCount": (plan.get("waveReportControls") or {}).get("coveredReportCount"),
            "resultGapCount": (plan.get("waveReportControls") or {}).get("resultGapCount"),
            "blockingCount": (plan.get("waveReportControls") or {}).get("blockingCount"),
        },
        "externalSubmission": plan.get("externalSubmission"),
        "guardrail": plan.get("guardrail"),
        "missingSignals": workflow_plan.get("missing_signals", []),
        "reviewGates": workflow_plan.get("review_gates", []),
        "actions": [
            {
                "operationId": operation.get("operation_id"),
                "actionId": operation.get("action_id"),
                "surface": operation.get("surface"),
                "safety": operation.get("safety"),
                "planStatus": (operation.get("plan") or {}).get("status"),
            }
            for operation in operations[:25]
        ],
    }


def _compact_mijngeldzaken_plan_summary(plan: Dict[str, Any]) -> Dict[str, Any]:
    workflow_plan = plan.get("workflowPlan") or {}
    controls = plan.get("masterLedgerControls") or {}
    operations = plan.get("operations") or []
    return {
        "status": plan.get("status"),
        "workflowId": workflow_plan.get("workflowId"),
        "operationCount": plan.get("operationCount"),
        "blockingOperations": len(plan.get("blockingOperations") or []),
        "masterLedgerControls": {
            "status": controls.get("status"),
            "rowCount": controls.get("rowCount"),
            "blockingCount": controls.get("blockingCount"),
            "readyForDraft": controls.get("readyForDraft"),
            "readyForApproval": controls.get("readyForApproval"),
            "readyForExternalExecution": controls.get("readyForExternalExecution"),
            "staleDrafts": controls.get("staleDrafts"),
        },
        "externalSubmission": plan.get("externalSubmission"),
        "guardrail": plan.get("guardrail"),
        "actions": [
            {
                "operationId": operation.get("operation_id"),
                "actionId": operation.get("action_id"),
                "surface": operation.get("surface"),
                "safety": operation.get("safety"),
                "planStatus": (operation.get("plan") or {}).get("status"),
            }
            for operation in operations[:25]
        ],
    }


def _refresh_bookkeeping_records(
    ledger: LocalOperationsLedger,
    config: Dict[str, Any],
    source_type: str = "document",
    limit: int = 100,
) -> Dict[str, Any]:
    service = LocalBookkeepingRecordService(ledger, config)
    normalized_source = str(source_type or "document").strip().lower()
    if normalized_source in {"document", "documents"}:
        result = service.refresh_documents(limit=limit)
        result["sourceType"] = "document"
        result["externalSubmission"] = "not_executed"
        return result
    if normalized_source in {"bank_transaction", "bank_transactions", "bank", "transactions"}:
        result = service.refresh_bank_transactions(limit=limit)
        result["sourceType"] = "bank_transaction"
        result["externalSubmission"] = "not_executed"
        return result
    if normalized_source == "all":
        document_refresh = service.refresh_documents(limit=limit)
        bank_refresh = service.refresh_bank_transactions(limit=limit)
        return {
            "success": True,
            "status": "completed",
            "sourceType": "all",
            "externalSubmission": "not_executed",
            "documentRefresh": document_refresh,
            "bankTransactionRefresh": bank_refresh,
            "requested": int(document_refresh.get("requested", 0)) + int(bank_refresh.get("requested", 0)),
            "updated": int(document_refresh.get("updated", 0)) + int(bank_refresh.get("updated", 0)),
            "failed": int(document_refresh.get("failed", 0)) + int(bank_refresh.get("failed", 0)),
        }
    raise ValueError("sourceType must be one of: document, bank_transaction, all")


def _prepare_ready_routes(
    ledger: LocalOperationsLedger,
    config: Dict[str, Any],
    source_type: str = "all",
    limit: int = 25,
) -> Dict[str, Any]:
    service = LocalRoutingService(ledger, config)
    normalized_source = str(source_type or "all").strip().lower()
    if normalized_source in {"document", "documents"}:
        result = service.prepare_ready_documents(limit=limit)
        result["sourceType"] = "document"
        result["externalSubmission"] = "not_executed"
        return result
    if normalized_source in {"bookkeeping_record", "bookkeeping_records", "bank_transaction", "bank_transactions", "bank", "transactions"}:
        result = service.prepare_ready_bookkeeping_records(limit=limit)
        result["sourceType"] = "bank_transaction"
        result["externalSubmission"] = "not_executed"
        return result
    if normalized_source == "all":
        document_routing = service.prepare_ready_documents(limit=limit)
        bank_record_routing = service.prepare_ready_bookkeeping_records(limit=limit)
        return _combined_routing_summary(
            document_routing,
            bank_record_routing,
            source_type="all",
        )
    raise ValueError("sourceType must be one of: document, bank_transaction, all")


def _combined_routing_summary(
    document_routing: Dict[str, Any],
    bank_record_routing: Dict[str, Any],
    source_type: str = "all",
) -> Dict[str, Any]:
    return {
        "success": True,
        "status": "completed",
        "sourceType": source_type,
        "externalSubmission": "not_executed",
        "documentRouting": document_routing,
        "bankRecordRouting": bank_record_routing,
        "requested": int(document_routing.get("requested", 0)) + int(bank_record_routing.get("requested", 0)),
        "draftPrepared": int(document_routing.get("draftPrepared", 0)) + int(bank_record_routing.get("draftPrepared", 0)),
        "alreadyPrepared": int(document_routing.get("alreadyPrepared", 0)) + int(bank_record_routing.get("alreadyPrepared", 0)),
        "needsReview": int(document_routing.get("needsReview", 0)) + int(bank_record_routing.get("needsReview", 0)),
        "blocked": int(document_routing.get("blocked", 0)) + int(bank_record_routing.get("blocked", 0)),
    }


def _single_record_routing_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    status = result.get("status")
    summary = {
        "sourceType": "bank_transaction",
        "externalSubmission": "not_executed",
        "requested": 1,
        "draftPrepared": 0,
        "alreadyPrepared": 0,
        "needsReview": 0,
        "blocked": 0,
        "bookkeepingRecords": [result],
    }
    if status == "draft_prepared":
        summary["draftPrepared"] = 1
    elif status == "already_prepared":
        summary["alreadyPrepared"] = 1
    elif status == "needs_review":
        summary["needsReview"] = 1
    else:
        summary["blocked"] = 1
    return summary


def _single_processing_summary(result: Dict[str, Any], retried: bool = False) -> Dict[str, Any]:
    status = result.get("status")
    summary = {
        "requested": 1,
        "retried": 1 if retried else 0,
        "processed": 0,
        "needsReview": 0,
        "failed": 0,
        "skipped": 0,
        "documents": [result],
    }
    if result.get("skipped"):
        summary["skipped"] = 1
    elif status == "processed":
        summary["processed"] = 1
    elif status in {"failed", "not_found"}:
        summary["failed"] = 1
    else:
        summary["needsReview"] = 1
    return summary


def _bounded_positive_int(value: Any, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def _bounded_nonnegative_int(value: Any, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0, min(parsed, maximum))


def _bounded_nonnegative_float(value: Any, default: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        parsed = default
    return max(0.0, min(parsed, maximum))


def _bool_value(value: Any, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _review_work_items(
    ledger: LocalOperationsLedger,
    review_items: list,
    config: Optional[Dict[str, Any]] = None,
) -> list:
    grouped: Dict[str, list] = {}
    for item in review_items:
        document_id = item.get("document_id")
        key = f"document:{document_id}" if document_id is not None else f"review:{item.get('id')}"
        grouped.setdefault(key, []).append(item)

    document_ids = [
        int(group[0]["document_id"])
        for group in grouped.values()
        if group[0].get("document_id") is not None
    ]
    review_context = ledger.get_review_work_item_context(document_ids)
    documents = review_context["documents"]
    bookkeeping_records = review_context["bookkeeping_records"]
    duplicate_links = review_context["duplicate_candidates"]
    duplicate_detector = DuplicateDetector(config or {})

    work_items = []
    for group in grouped.values():
        group = sorted(group, key=lambda item: int(item.get("id") or 0), reverse=True)
        document_id = group[0].get("document_id")
        document = documents.get(int(document_id)) if document_id is not None else None
        bookkeeping_record = bookkeeping_records.get(int(document_id)) if document_id is not None else None
        compact_document = (
            _compact_review_document(document, bookkeeping_record)
            if document
            else None
        )
        duplicate_candidates = []
        for candidate in duplicate_links.get(int(document_id), []) if document_id is not None else []:
            if candidate.get("status") not in {"pending", "in_review"}:
                continue
            other_id = candidate.get("candidate_document_id")
            if int(candidate.get("document_id") or 0) != int(document_id or 0):
                other_id = candidate.get("document_id")
            other_document = documents.get(int(other_id)) if other_id is not None else None
            comparison = (
                duplicate_detector.compare_identity_evidence(document, other_document)
                if document and other_document
                else {}
            )
            duplicate_candidates.append({
                "id": candidate.get("id"),
                "candidateDocumentId": other_id,
                "matchType": candidate.get("match_type"),
                "confidenceScore": candidate.get("confidence_score"),
                "reason": candidate.get("reason"),
                "evidence": candidate.get("evidence") or {},
                "matchedIdentityFields": comparison.get("matched_identity_fields") or [],
                "conflictingIdentityFields": comparison.get("conflicting_identity_fields") or [],
                "similarityScore": comparison.get("similarity_score"),
                "comparableFields": comparison.get("comparable_fields"),
                "currentIdentity": _compact_duplicate_identity(comparison.get("left") or {}),
                "candidateIdentity": _compact_duplicate_identity(comparison.get("right") or {}),
                "document": _compact_review_document(other_document) if other_document else None,
            })
        work_items.append({
            "id": f"document-{document_id}" if document_id is not None else f"review-{group[0].get('id')}",
            "documentId": document_id,
            "status": "in_review" if any(item.get("status") == "in_review" for item in group) else "pending",
            "reasons": list(dict.fromkeys(str(item.get("reason") or "manual_review") for item in group)),
            "reviewItems": [
                {
                    "id": item.get("id"),
                    "reason": item.get("reason"),
                    "details": item.get("details"),
                    "status": item.get("status"),
                    "correctedData": item.get("corrected_data") or {},
                    "createdAt": item.get("created_at"),
                    "updatedAt": item.get("updated_at"),
                }
                for item in group
            ],
            "document": compact_document,
            "duplicateCandidates": duplicate_candidates,
            "reviewPath": f"/documents/{document_id}" if document_id is not None else "/#review",
        })
    return sorted(
        work_items,
        key=lambda item: max(
            [int(review.get("id") or 0) for review in item.get("reviewItems") or []] or [0]
        ),
        reverse=True,
    )


def _compact_duplicate_identity(evidence: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "postingPolarity": evidence.get("posting_polarity"),
        "vendor": evidence.get("vendor"),
        "date": evidence.get("date"),
        "amount": evidence.get("amount"),
        "tax": evidence.get("tax"),
        "invoiceNumber": evidence.get("invoice_number"),
        "receiptNumber": evidence.get("receipt_number"),
        "orderNumber": evidence.get("order_number"),
        "transactionReference": evidence.get("transaction_reference"),
    }


def _compact_review_work_item_summary(item: Dict[str, Any]) -> Dict[str, Any]:
    document = _mapping_fields(item.get("document"), (
        "category",
        "classifiedDocumentType",
        "currency",
        "documentType",
        "duplicateOfDocumentId",
        "filename",
        "financialFieldIssues",
        "normalizedRecordDate",
        "normalizedVatAmount",
        "ocrExcerpt",
        "orderNumber",
        "postingEligible",
        "processingStatus",
        "source",
        "sourceUrl",
        "targetSystem",
        "totalAmount",
        "transactionDate",
        "transactionReference",
        "vatAmount",
        "vendorName",
        "invoiceNumber",
        "receiptNumber",
    ))
    category_suggestion = _mapping_fields(
        (item.get("document") or {}).get("categorySuggestion"),
        ("category", "confidenceScore", "matchPolicy", "rationale", "source"),
    )
    if category_suggestion:
        document["categorySuggestion"] = category_suggestion

    duplicate_candidates = []
    for candidate in item.get("duplicateCandidates") or []:
        compact_candidate = _mapping_fields(candidate, (
            "candidateDocumentId",
            "confidenceScore",
            "conflictingIdentityFields",
            "comparableFields",
            "id",
            "matchType",
            "matchedIdentityFields",
            "reason",
            "similarityScore",
        ))
        compact_candidate["currentIdentity"] = _mapping_fields(
            candidate.get("currentIdentity"),
            (
                "amount",
                "date",
                "invoiceNumber",
                "orderNumber",
                "postingPolarity",
                "receiptNumber",
                "tax",
                "transactionReference",
                "vendor",
            ),
        )
        compact_candidate["candidateIdentity"] = _mapping_fields(
            candidate.get("candidateIdentity"),
            (
                "amount",
                "date",
                "invoiceNumber",
                "orderNumber",
                "postingPolarity",
                "receiptNumber",
                "tax",
                "transactionReference",
                "vendor",
            ),
        )
        compact_candidate["document"] = _mapping_fields(
            candidate.get("document"),
            (
                "currency",
                "documentType",
                "filename",
                "invoiceNumber",
                "orderNumber",
                "receiptNumber",
                "source",
                "totalAmount",
                "transactionDate",
                "transactionReference",
                "vendorName",
            ),
        )
        duplicate_candidates.append(compact_candidate)

    compact = _mapping_fields(item, ("documentId", "id", "reasons", "reviewPath", "status"))
    compact["document"] = document
    compact["duplicateCandidates"] = duplicate_candidates
    compact["reviewItems"] = [
        _mapping_fields(
            review,
            ("createdAt", "details", "id", "reason", "status", "updatedAt"),
        )
        for review in item.get("reviewItems") or []
    ]
    return compact


def _review_queue_summary(
    ledger: LocalOperationsLedger,
    status: Optional[Any],
) -> Dict[str, Any]:
    groups = ledger.list_review_summary_groups(status=status)
    review_items = 0
    documents = 0
    posting_blocked_documents = 0
    posting_blocked_review_items = 0
    evidence_only_documents = 0
    evidence_only_review_items = 0
    duplicate_candidates = 0
    category_suggestions = 0
    oldest_created_at = None
    newest_created_at = None

    for group in groups:
        review_count = int(group.get("review_count") or 0)
        document_id = group.get("document_id")
        has_document = document_id is not None
        evidence_only = has_document and not _review_document_posting_eligible(group)
        review_items += review_count
        documents += int(has_document)
        duplicate_candidates += int(bool(group.get("has_duplicate_candidate")))
        if evidence_only:
            evidence_only_documents += 1
            evidence_only_review_items += review_count
        else:
            posting_blocked_documents += int(has_document)
            posting_blocked_review_items += review_count
        if has_document and suggest_category_intent(group):
            category_suggestions += 1
        oldest_created_at = _earliest_iso_text(oldest_created_at, group.get("oldest_created_at"))
        newest_created_at = _latest_iso_text(newest_created_at, group.get("newest_created_at"))

    return {
        "reviewItems": review_items,
        "workItems": len(groups),
        "documents": documents,
        "postingBlockedDocuments": posting_blocked_documents,
        "postingBlockedReviewItems": posting_blocked_review_items,
        "evidenceOnlyDocuments": evidence_only_documents,
        "evidenceOnlyReviewItems": evidence_only_review_items,
        "duplicateCandidates": duplicate_candidates,
        "categorySuggestions": category_suggestions,
        "oldestReviewCreatedAt": oldest_created_at,
        "newestReviewCreatedAt": newest_created_at,
    }


def _earliest_iso_text(current: Any, candidate: Any) -> Optional[str]:
    candidate_text = str(candidate or "").strip()
    if not candidate_text:
        return str(current) if current else None
    current_text = str(current or "").strip()
    return candidate_text if not current_text or candidate_text < current_text else current_text


def _latest_iso_text(current: Any, candidate: Any) -> Optional[str]:
    candidate_text = str(candidate or "").strip()
    if not candidate_text:
        return str(current) if current else None
    current_text = str(current or "").strip()
    return candidate_text if not current_text or candidate_text > current_text else current_text


def _mapping_fields(value: Any, fields: tuple) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        field: value[field]
        for field in fields
        if field in value
    }


def _compact_review_document(
    document: Optional[Dict[str, Any]],
    bookkeeping_record: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if not document:
        return None
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    provider = metadata.get("providerMetadata") if isinstance(metadata.get("providerMetadata"), dict) else {}
    extracted = document.get("extracted_data") if isinstance(document.get("extracted_data"), dict) else {}
    ocr_text = str(document.get("ocr_text") or "").strip()
    target_system = str(metadata.get("targetSystem") or metadata.get("target_system") or "waveapps_business")
    processing = metadata.get("processing") if isinstance(metadata.get("processing"), dict) else {}
    classification = (
        processing.get("documentTypeClassification")
        if isinstance(processing.get("documentTypeClassification"), dict)
        else {}
    )
    classified_document_type = str(classification.get("documentType") or "").strip().lower()
    document_type = str(document.get("document_type") or "").strip().lower()
    posting_eligible = _review_document_posting_eligible(document)
    record_metadata = (
        bookkeeping_record.get("metadata")
        if isinstance((bookkeeping_record or {}).get("metadata"), dict)
        else {}
    )
    return {
        "id": document.get("id"),
        "filename": document.get("original_filename"),
        "mimeType": document.get("mime_type"),
        "source": document.get("source"),
        "sourceDocumentId": document.get("source_document_id"),
        "sourceUrl": provider.get("web_view_link"),
        "processingStatus": document.get("processing_status"),
        "documentType": document_type,
        "classifiedDocumentType": classified_document_type or None,
        "postingEligible": posting_eligible,
        "vendorName": document.get("vendor_name"),
        "transactionDate": document.get("transaction_date"),
        "totalAmount": document.get("total_amount"),
        "vatAmount": document.get("vat_amount"),
        "bookkeepingRecordId": (bookkeeping_record or {}).get("id"),
        "bookkeepingExportStatus": (bookkeeping_record or {}).get("export_status"),
        "normalizedRecordDate": (bookkeeping_record or {}).get("record_date"),
        "normalizedVatAmount": (bookkeeping_record or {}).get("vat_amount"),
        "financialFieldIssues": record_metadata.get("financialFieldIssues") or [],
        "currency": extracted.get("currency") or "EUR",
        "category": document.get("category"),
        "categorySuggestion": suggest_category_intent(document),
        "targetSystem": target_system,
        "invoiceNumber": extracted.get("invoice_number"),
        "receiptNumber": extracted.get("receipt_number"),
        "orderNumber": extracted.get("order_number"),
        "transactionReference": extracted.get("transaction_reference"),
        "confidenceScore": document.get("confidence_score"),
        "duplicateOfDocumentId": document.get("duplicate_of_document_id"),
        "ocrExcerpt": ocr_text[:1200],
    }


def _review_document_posting_eligible(document: Dict[str, Any]) -> bool:
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    processing = metadata.get("processing") if isinstance(metadata.get("processing"), dict) else {}
    classification = (
        processing.get("documentTypeClassification")
        if isinstance(processing.get("documentTypeClassification"), dict)
        else {}
    )
    classified_document_type = str(classification.get("documentType") or "").strip().lower()
    review_metadata = metadata.get("review") if isinstance(metadata.get("review"), dict) else {}
    override = review_metadata.get("documentTypeOverride")
    override_document_type = (
        str(override.get("documentType") or "").strip().lower()
        if isinstance(override, dict)
        else ""
    )
    document_type = str(document.get("document_type") or "").strip().lower()
    effective_document_type = override_document_type or document_type
    return not is_non_posting_document_type(effective_document_type) and not (
        not override_document_type and is_non_posting_document_type(classified_document_type)
    )


def _review_category_options(ledger: LocalOperationsLedger, config: Dict[str, Any]) -> list:
    return fab_category_options(ledger, config)


def _wave_discovery_http_status(result: Dict[str, Any]) -> int:
    if result.get("success"):
        return 200
    return {
        "authentication_failed": 401,
        "authorization_failed": 403,
        "business_not_found": 404,
        "rate_limited": 429,
        "quota_exhausted": 429,
        "provider_error": 502,
        "pagination_incomplete": 502,
        "internal_error": 500,
    }.get(str(result.get("status") or ""), 400)


def _corrections_from_mapping(values: Any) -> Dict[str, Any]:
    keys = (
        "vendorName",
        "category",
        "transactionDate",
        "totalAmount",
        "vatAmount",
        "targetSystem",
        "duplicateOfDocumentId",
        "duplicateCandidateId",
        "documentType",
    )
    corrections = {}
    for key in keys:
        value = values.get(key) if hasattr(values, "get") else None
        if value not in (None, ""):
            corrections[key] = value
    return corrections


def _bookkeeping_record_corrections_from_mapping(values: Any) -> Dict[str, Any]:
    keys = (
        "vendorName",
        "category",
        "recordDate",
        "transactionDate",
        "amount",
        "totalAmount",
        "vatAmount",
        "currency",
        "description",
        "targetSystem",
        "targetAccount",
        "recordType",
        "confidenceScore",
    )
    corrections = {}
    for key in keys:
        value = values.get(key) if hasattr(values, "get") else None
        if value not in (None, ""):
            corrections[key] = value
    return corrections


def _compact_connector_sync(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "success": result.get("success"),
        "status": result.get("status"),
        "workflowRunId": result.get("workflowRunId"),
        "summary": result.get("summary") or {},
        "sources": [
            {
                "source": item.get("source"),
                "status": item.get("status"),
                "registered": item.get("registered", 0),
                "duplicates": item.get("duplicates", 0),
                "revisions": item.get("revisions", 0),
                "nextAction": item.get("nextAction"),
            }
            for item in result.get("results") or []
        ],
        "externalSubmission": "not_executed",
    }


def _workflow_runs_with_steps(
    ledger: LocalOperationsLedger,
    limit: int = 15,
    recovery_service: Optional[LocalWorkflowRecoveryService] = None,
) -> list:
    workflow_runs = ledger.list_workflow_runs(limit=limit)
    for workflow_run in workflow_runs:
        steps = ledger.list_workflow_steps(
            workflow_run_id=int(workflow_run["id"]),
            limit=500,
        )
        workflow_run["steps"] = steps
        workflow_run["step_count"] = len(steps)
        workflow_run["step_summary"] = _workflow_step_summary(steps)
        workflow_run["recovery"] = (
            recovery_service.plan(int(workflow_run["id"]))
            if recovery_service
            and workflow_run.get("status") in {"failed", "completed_with_errors", "attention_required"}
            else None
        )
    return workflow_runs


def _compact_workflow_recovery(result: Dict[str, Any]) -> Dict[str, Any]:
    plan = result.get("plan") or {}
    return {
        "success": result.get("success"),
        "status": result.get("status"),
        "workflowRunId": result.get("workflowRunId"),
        "sourceWorkflowRunId": result.get("sourceWorkflowRunId"),
        "recoveryType": result.get("recoveryType") or plan.get("recoveryType"),
        "selectedStepKeys": result.get("selectedStepKeys") or plan.get("selectedStepKeys") or [],
        "continuationStepKeys": result.get("continuationStepKeys") or plan.get("continuationStepKeys") or [],
        "nextAction": plan.get("nextAction"),
        "externalSubmission": "not_executed",
    }


def _compact_scheduled_workflow_recovery(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "success": result.get("success"),
        "status": result.get("status"),
        "attempted": result.get("attempted", 0),
        "succeeded": result.get("succeeded", 0),
        "failed": result.get("failed", 0),
        "interruptedWorkflowRunIds": result.get("interruptedWorkflowRunIds") or [],
        "connectorSourcesHeldBack": result.get("connectorSourcesHeldBack") or [],
        "externalSubmission": "not_executed",
    }


def _workflow_step_summary(steps: list) -> Dict[str, int]:
    summary: Dict[str, int] = {}
    for step in steps:
        status = str(step.get("status") or "unknown")
        summary[status] = summary.get(status, 0) + 1
    return summary


def _compact_photos_picker_result(result: Dict[str, Any]) -> Dict[str, Any]:
    picker_session = result.get("session") if isinstance(result.get("session"), dict) else {}
    return {
        "success": result.get("success"),
        "status": result.get("status"),
        "error": result.get("error"),
        "session": {
            "id": picker_session.get("id"),
            "status": picker_session.get("status"),
            "selectedItemCount": picker_session.get("selectedItemCount", 0),
            "providerSessionDeleted": picker_session.get("providerSessionDeleted", False),
        } if picker_session else None,
        "summary": result.get("summary") or {},
        "externalSubmission": "not_executed",
    }


def _list_config(config: Dict[str, Any], *keys: str) -> list:
    value = _config_value(config, *keys)
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw_items = value
    else:
        raw_items = str(value).replace("\n", ",").replace(";", ",").split(",")
    return [str(item).strip() for item in raw_items if str(item).strip()]


def _config_value(config: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = config.get(key)
        if value not in (None, ""):
            return value
    for key in keys:
        if "_" not in key:
            continue
        section, option = key.split("_", 1)
        section_values = config.get(section)
        if isinstance(section_values, dict):
            value = section_values.get(option)
            if value not in (None, ""):
                return value
    return None


def run(config: Optional[Dict[str, Any]] = None):
    config = config or ConfigLoader(config_file="config/config.ini").get_all_config()
    app = create_app(config)
    host = str(config.get("fab_local_api_host") or config.get("operations_api_host") or "127.0.0.1")
    port = int(config.get("fab_local_api_port") or config.get("operations_api_port") or 5001)
    threads = max(4, min(32, int(
        config.get("fab_local_api_threads")
        or config.get("operations_api_threads")
        or 8
    )))
    from waitress import serve

    serve(
        app,
        host=host,
        port=port,
        threads=threads,
        channel_timeout=120,
        ident="FAB",
    )


if __name__ == "__main__":
    run()
