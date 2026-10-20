import hashlib
import html
import hmac
import json
import secrets
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Dict

from flask import Flask, abort, jsonify, redirect, request, session

from src.backup.backup_manager import BackupManager
from src.data_entry.posting_approval import PostingApprovalService
from src.exceptions.exception_memory import ExceptionMemory
from src.learning.correction_learning import CorrectionLearningService
from src.missing_receipts.follow_up_manager import MissingReceiptFollowUpManager
from src.reports.reporting_service import ReportingService
from src.storage.database import Database
from src.storage.schema_extender import SchemaExtender

LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60
MAX_LOGIN_FAILURES_PER_CLIENT = 5
MAX_TRACKED_LOGIN_CLIENTS = 4096


def create_app(config: Dict[str, Any] = None) -> Flask:
    config = config or {}
    app = Flask(__name__)
    app.secret_key = config.get("dashboard_session_secret") or secrets.token_urlsafe(32)
    secure_cookie = config.get("dashboard_cookie_secure", False)
    secure_cookie = secure_cookie is True or str(secure_cookie).strip().lower() in {"1", "true", "yes", "on"}
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=secure_cookie,
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )
    app.permanent_session_lifetime = timedelta(hours=8)
    database = Database(config)
    SchemaExtender(config).ensure_learning_schema()
    backup_manager = BackupManager(config)
    exception_memory = ExceptionMemory(config)
    follow_up_manager = MissingReceiptFollowUpManager(config)
    correction_learning = CorrectionLearningService(config)
    reporting_service = ReportingService(config)
    posting_approval = PostingApprovalService(config)
    dashboard_token = config.get("dashboard_access_token")
    token_fingerprint = (
        hmac.new(app.secret_key.encode("utf-8"), dashboard_token.encode("utf-8"), hashlib.sha256).hexdigest()
        if isinstance(dashboard_token, str) and dashboard_token
        else ""
    )
    login_failures: OrderedDict[str, tuple[int, float]] = OrderedDict()
    login_failures_lock = threading.Lock()

    def login_lockout_seconds(client_key: str, now: float) -> int:
        with login_failures_lock:
            state = login_failures.get(client_key)
            if state is None:
                return 0
            attempts, expires_at = state
            if expires_at <= now:
                login_failures.pop(client_key, None)
                return 0
            login_failures.move_to_end(client_key)
            return max(1, int(expires_at - now + 0.999)) if attempts >= MAX_LOGIN_FAILURES_PER_CLIENT else 0

    def record_login_failure(client_key: str, now: float) -> None:
        with login_failures_lock:
            attempts, expires_at = login_failures.get(client_key, (0, now + LOGIN_FAILURE_WINDOW_SECONDS))
            if expires_at <= now:
                attempts, expires_at = 0, now + LOGIN_FAILURE_WINDOW_SECONDS
            login_failures[client_key] = (attempts + 1, expires_at)
            login_failures.move_to_end(client_key)
            while len(login_failures) > MAX_TRACKED_LOGIN_CLIENTS:
                login_failures.popitem(last=False)

    def has_authenticated_session() -> bool:
        stored_fingerprint = session.get("dashboard_token_fingerprint", "")
        session_id = session.get("dashboard_session_id", "")
        if not (
            token_fingerprint
            and isinstance(stored_fingerprint, str)
            and hmac.compare_digest(stored_fingerprint, token_fingerprint)
            and isinstance(session_id, str)
            and 32 <= len(session_id) <= 128
        ):
            return False
        session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return database.dashboard_session_is_active(session_hash)

    @app.before_request
    def reject_query_string_credentials():
        if "token" in request.args:
            abort(400, description="Dashboard credentials are not accepted in URLs.")

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    def require_auth(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if not dashboard_token:
                abort(503, description="Dashboard token is not configured.")
            provided = request.headers.get("X-FAB-Token") or ""
            header_authenticated = bool(
                dashboard_token and hmac.compare_digest(provided, dashboard_token)
            )
            session_authenticated = has_authenticated_session()
            if not header_authenticated and not session_authenticated:
                abort(401, description="Invalid or missing FAB dashboard token.")
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not header_authenticated:
                csrf_token = request.headers.get("X-CSRF-Token", "")
                expected_csrf = session.get("dashboard_csrf_token", "")
                if not csrf_token or not expected_csrf or not hmac.compare_digest(csrf_token, expected_csrf):
                    abort(403, description="A valid CSRF token is required for session-authenticated changes.")
            return func(*args, **kwargs)
        return wrapper

    def json_object_payload() -> Dict[str, Any]:
        raw_body = request.get_data(cache=True)
        if not raw_body.strip():
            return {}
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            abort(400, description="A JSON object is required.")
        return payload

    def bounded_text(value: Any, field: str, maximum: int, *, allow_empty: bool = False) -> str:
        if (
            not isinstance(value, str)
            or len(value) > maximum
            or (not allow_empty and not value.strip())
        ):
            qualifier = "text" if allow_empty else "non-empty text"
            abort(400, description=f"{field} must be {qualifier} of at most {maximum} characters.")
        return value

    def document_correction_payload(payload: Dict[str, Any], default_explanation: str):
        correction = payload.get("correction", payload)
        if not isinstance(correction, dict):
            abort(400, description="correction must be an object.")
        if not isinstance(correction.get("extracted_data", {}), dict):
            abort(400, description="correction.extracted_data must be an object.")
        for field in ("vendor_name", "category", "vendor_alias", "correction_type"):
            value = correction.get(field)
            if value is not None and (not isinstance(value, str) or len(value) > 500):
                abort(400, description=f"correction.{field} must be text of at most 500 characters.")
        category_path = correction.get("category_path", [])
        if not isinstance(category_path, list) or any(not isinstance(part, str) for part in category_path):
            abort(400, description="correction.category_path must be an array of text values.")
        explanation = payload.get("explanation", default_explanation)
        if not isinstance(explanation, str) or not explanation.strip() or len(explanation) > 2000:
            abort(400, description="explanation must be a non-empty string of at most 2000 characters.")
        return correction, explanation

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "service": "FAB local dashboard"})

    @app.get("/ui")
    def operator_ui():
        if not dashboard_token:
            abort(503, description="Dashboard token is not configured.")
        if not has_authenticated_session():
            return """<!doctype html>
            <html><head><meta charset="utf-8"><title>FAB sign in</title></head>
            <body><main><h1>FAB Operator Dashboard</h1>
            <form method="post" action="/login">
              <label for="dashboard-token">Dashboard access token</label>
              <input id="dashboard-token" name="token" type="password" required autocomplete="current-password">
              <button type="submit">Sign in</button>
            </form></main></body></html>"""
        csrf_token = session.get("dashboard_csrf_token") or secrets.token_urlsafe(32)
        session["dashboard_csrf_token"] = csrf_token
        summary = reporting_service.summary()
        safe_summary = html.escape(json.dumps(summary, ensure_ascii=True, indent=2, default=str))
        page_html = f"""
        <!doctype html>
        <html><head><title>FAB Operator Dashboard</title>
        <style>
        body {{ font-family: Arial, sans-serif; background:#111; color:#eee; margin: 24px; }}
        a {{ color:#9ad; }} code {{ background:#222; padding:2px 4px; }}
        .card {{ background:#1c1c1c; border:1px solid #333; border-radius:8px; padding:16px; margin:12px 0; }}
        table {{ border-collapse: collapse; width: 100%; }} th,td {{ border:1px solid #333; padding:8px; }}
        </style></head><body>
        <h1>FAB Operator Dashboard</h1>
        <div class='card'><h2>Summary</h2><pre>{safe_summary}</pre></div>
        <div class='card'><h2>Key Links</h2>
          <p><a href='/documents'>Documents</a></p>
          <p><a href='/manual-review'>Manual Review</a></p>
          <p><a href='/posting-attempts'>Posting Attempts</a></p>
          <p><a href='/reports/summary'>Reports Summary</a></p>
          <p><a href='/missing-receipts'>Missing Receipts</a></p>
          <p><a href='/outreach-reminders'>Outreach Reminders</a></p>
          <p><a href='/backups'>Backups</a></p>
        </div>
        <form method="post" action="/logout">
          <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}">
          <button type="submit">Sign out</button>
        </form>
        </body></html>
        """
        return page_html

    @app.post("/login")
    def login():
        if not dashboard_token:
            abort(503, description="Dashboard token is not configured.")
        client_key = request.remote_addr or "unknown"
        retry_after = login_lockout_seconds(client_key, time.monotonic())
        if retry_after:
            response = jsonify({"error": "Too many failed login attempts. Try again later."})
            response.status_code = 429
            response.headers["Retry-After"] = str(retry_after)
            response.headers["Cache-Control"] = "no-store"
            return response
        provided = request.form.get("token", "")
        if not provided or not hmac.compare_digest(provided, dashboard_token):
            record_login_failure(client_key, time.monotonic())
            abort(401, description="Invalid dashboard access token.")
        with login_failures_lock:
            login_failures.pop(client_key, None)
        session_id = secrets.token_urlsafe(32)
        session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        expires_at = (datetime.now(timezone.utc) + app.permanent_session_lifetime).isoformat()
        database.register_dashboard_session(session_hash, expires_at)
        session.clear()
        session.permanent = True
        session["dashboard_token_fingerprint"] = token_fingerprint
        session["dashboard_session_id"] = session_id
        session["dashboard_csrf_token"] = secrets.token_urlsafe(32)
        return redirect("/ui", code=303)

    @app.post("/logout")
    def logout():
        expected_csrf = session.get("dashboard_csrf_token", "")
        provided_csrf = request.form.get("csrf_token", "")
        if not has_authenticated_session():
            abort(401, description="Not signed in.")
        if not expected_csrf or not provided_csrf or not hmac.compare_digest(provided_csrf, expected_csrf):
            abort(403, description="A valid CSRF token is required to sign out.")
        session_id = session.get("dashboard_session_id", "")
        if isinstance(session_id, str) and 32 <= len(session_id) <= 128:
            database.revoke_dashboard_session(hashlib.sha256(session_id.encode("utf-8")).hexdigest())
        session.clear()
        return redirect("/ui", code=303)

    @app.get("/")
    @require_auth
    def index():
        summary = reporting_service.summary()
        return jsonify({"application": "FAB", "mode": "local-loopback", "summary": summary})

    @app.get("/documents")
    @require_auth
    def documents():
        return jsonify(database.fetch_all("SELECT id, source, original_filename, current_state, created_at, updated_at FROM documents ORDER BY updated_at DESC LIMIT 200"))

    @app.get("/documents/<document_id>")
    @require_auth
    def document_detail(document_id: str):
        document = database.fetch_one("SELECT * FROM documents WHERE id = ?", (document_id,))
        if not document:
            abort(404, description="Document not found")
        return jsonify({
            "document": document,
            "ocr_results": database.fetch_all("SELECT * FROM ocr_results WHERE document_id = ? ORDER BY created_at DESC", (document_id,)),
            "extracted_fields": database.fetch_all("SELECT * FROM extracted_fields WHERE document_id = ? ORDER BY field_name", (document_id,)),
            "manual_reviews": database.fetch_all("SELECT * FROM manual_review_items WHERE document_id = ? ORDER BY created_at DESC", (document_id,)),
            "audit_log": database.fetch_all("SELECT * FROM audit_log WHERE entity_id = ? ORDER BY created_at DESC LIMIT 100", (document_id,)),
            "posting_attempts": database.fetch_all("SELECT * FROM posting_attempts WHERE document_id = ? ORDER BY created_at DESC", (document_id,)),
            "reconciliation_results": database.fetch_all("SELECT * FROM reconciliation_results WHERE document_id = ? ORDER BY created_at DESC", (document_id,)),
            "corrections": database.fetch_all("SELECT * FROM document_corrections WHERE document_id = ? ORDER BY created_at DESC", (document_id,)),
        })

    @app.post("/documents/<document_id>/corrections")
    @require_auth
    def apply_document_correction(document_id: str):
        payload = json_object_payload()
        correction, explanation = document_correction_payload(payload, "Manual correction from dashboard")
        return jsonify(correction_learning.apply_document_correction(document_id, correction, explanation))

    @app.get("/manual-review")
    @require_auth
    def manual_review():
        status = request.args.get("status", "pending")
        return jsonify(database.fetch_all("SELECT * FROM manual_review_items WHERE status = ? ORDER BY created_at DESC LIMIT 200", (status,)))

    @app.post("/manual-review/<int:item_id>/resolve")
    @require_auth
    def resolve_manual_review(item_id: int):
        payload = json_object_payload()
        resolution = payload.get("resolution", "resolved")
        requested_status = payload.get("status", "resolved")
        if not isinstance(resolution, str) or not resolution.strip() or len(resolution) > 2000:
            abort(400, description="resolution must be a non-empty string of at most 2000 characters.")
        if requested_status != "resolved":
            abort(400, description="A manual review can only be marked resolved on this route.")
        review = database.fetch_one(
            "SELECT id, document_id, status FROM manual_review_items WHERE id = ?",
            (item_id,),
        )
        if not review:
            abort(404, description="Manual review item not found.")
        if review["status"] != "pending":
            abort(409, description="Manual review item is no longer pending.")
        with database.connect() as connection:
            cursor = connection.execute(
                "UPDATE manual_review_items SET status = 'resolved', resolution = ?, resolved_at = ? WHERE id = ? AND status = 'pending'",
                (resolution, database.now(), item_id),
            )
            if cursor.rowcount != 1:
                abort(409, description="Manual review item changed during resolution.")
        database.add_audit_log(
            "manual_review_item",
            str(item_id),
            "resolved",
            review,
            {"status": "resolved", "resolution": resolution},
            resolution,
            "user",
        )
        return jsonify({"status": "resolved", "item_id": item_id})

    @app.post("/manual-review/<int:item_id>/apply-correction")
    @require_auth
    def apply_review_correction(item_id: int):
        review = database.fetch_one("SELECT * FROM manual_review_items WHERE id = ?", (item_id,))
        if not review:
            abort(404, description="Manual review item not found")
        payload = json_object_payload()
        correction, explanation = document_correction_payload(payload, "Manual review correction applied")
        result = correction_learning.apply_document_correction(review["document_id"], correction, explanation)
        with database.connect() as connection:
            connection.execute("UPDATE manual_review_items SET status = 'resolved', resolution = ?, resolved_at = ? WHERE id = ?", (explanation, database.now(), item_id))
        return jsonify(result)

    @app.get("/audit-log")
    @require_auth
    def audit_log():
        return jsonify(database.fetch_all("SELECT * FROM audit_log ORDER BY created_at DESC LIMIT 300"))

    @app.get("/posting-attempts")
    @require_auth
    def posting_attempts():
        status = request.args.get("status")
        return jsonify(posting_approval.list_attempts(status))

    @app.post("/posting-attempts/<int:attempt_id>/approve")
    @require_auth
    def approve_posting_attempt(attempt_id: int):
        payload = json_object_payload()
        reason = bounded_text(payload.get("reason", "Approved from dashboard"), "reason", 2000, allow_empty=True)
        return jsonify(posting_approval.approve_attempt(attempt_id, reason=reason))

    @app.post("/posting-attempts/<int:attempt_id>/reject")
    @require_auth
    def reject_posting_attempt(attempt_id: int):
        payload = json_object_payload()
        reason = bounded_text(payload.get("reason", "Rejected from dashboard"), "reason", 2000, allow_empty=True)
        return jsonify(posting_approval.reject_attempt(attempt_id, reason=reason))

    @app.post("/posting-attempts/<int:attempt_id>/complete-supervised")
    @require_auth
    def complete_supervised_posting_attempt(attempt_id: int):
        payload = json_object_payload()
        confirmation = bounded_text(payload.get("confirmation", ""), "confirmation", 100, allow_empty=True)
        external_id = payload.get("external_id")
        if external_id is not None:
            external_id = bounded_text(external_id, "external_id", 500, allow_empty=True)
        evidence = payload.get("evidence", {})
        if not isinstance(evidence, dict):
            abort(400, description="evidence must be an object.")
        reason = bounded_text(
            payload.get("reason", "Supervised external submission completed from dashboard"),
            "reason",
            2000,
            allow_empty=True,
        )
        return jsonify(posting_approval.complete_supervised_attempt(
            attempt_id,
            confirmation=confirmation,
            completed_by="dashboard_user",
            external_id=external_id,
            evidence=evidence,
            reason=reason,
        ))

    @app.get("/reconciliation-results")
    @require_auth
    def reconciliation_results():
        return jsonify(database.fetch_all("SELECT * FROM reconciliation_results ORDER BY created_at DESC LIMIT 300"))

    @app.get("/missing-receipts")
    @require_auth
    def missing_receipts():
        status = request.args.get("status", "open")
        return jsonify(database.fetch_all("SELECT * FROM missing_receipt_alerts WHERE status = ? ORDER BY created_at DESC LIMIT 200", (status,)))

    @app.get("/bank-transactions")
    @require_auth
    def bank_transactions():
        return jsonify(database.fetch_all("SELECT * FROM bank_transactions ORDER BY transaction_date DESC LIMIT 300"))

    @app.get("/exceptions")
    @require_auth
    def exceptions():
        active = request.args.get("active", "1")
        if active not in {"0", "1"}:
            abort(400, description="active must be 0 or 1.")
        return jsonify(database.fetch_all("SELECT * FROM exceptions WHERE active = ? ORDER BY created_at DESC LIMIT 300", (int(active),)))

    @app.post("/exceptions")
    @require_auth
    def approve_exception():
        payload = json_object_payload()
        exception_type = payload.get("exception_type")
        context = payload.get("context", {})
        explanation = payload.get("explanation")
        exception_type = bounded_text(exception_type, "exception_type", 200)
        explanation = bounded_text(explanation, "explanation", 2000)
        if not isinstance(context, dict):
            abort(400, description="context must be an object.")
        return jsonify(exception_memory.approve_exception(exception_type, context, explanation))

    @app.post("/exceptions/<fingerprint>/deactivate")
    @require_auth
    def deactivate_exception(fingerprint: str):
        payload = json_object_payload()
        reason = bounded_text(
            payload.get("reason", "Exception deactivated from dashboard"),
            "reason",
            2000,
            allow_empty=True,
        )
        status = "deactivated" if exception_memory.deactivate_exception(fingerprint, reason) else "not_found"
        return jsonify({"status": status})

    @app.get("/outreach-reminders")
    @require_auth
    def outreach_reminders():
        status = request.args.get("status", "open")
        return jsonify(database.fetch_all("SELECT * FROM outreach_reminders WHERE status = ? ORDER BY updated_at DESC LIMIT 300", (status,)))

    @app.post("/outreach-reminders/<transaction_id>/complete")
    @require_auth
    def complete_outreach(transaction_id: str):
        payload = json_object_payload()
        reason = bounded_text(payload.get("reason", "Receipt received and processed"), "reason", 2000, allow_empty=True)
        return jsonify({"completed": follow_up_manager.mark_completed(transaction_id, reason)})

    @app.post("/outreach-reminders/<transaction_id>/stop")
    @require_auth
    def stop_outreach(transaction_id: str):
        payload = json_object_payload()
        reason = bounded_text(payload.get("reason", "Stopped from dashboard"), "reason", 2000, allow_empty=True)
        return jsonify({"stopped": follow_up_manager.stop_follow_up(transaction_id, reason)})

    @app.get("/outreach-reminders/due")
    @require_auth
    def due_outreach():
        return jsonify(follow_up_manager.reminders_due())

    @app.get("/vendors")
    @require_auth
    def vendors():
        return jsonify(database.fetch_all("SELECT * FROM vendors ORDER BY updated_at DESC LIMIT 300"))

    @app.get("/category-decisions")
    @require_auth
    def category_decisions():
        return jsonify(database.fetch_all("SELECT * FROM category_decisions ORDER BY created_at DESC LIMIT 300"))

    @app.get("/category-rules")
    @require_auth
    def category_rules():
        return jsonify(database.fetch_all("SELECT * FROM category_rules WHERE active = 1 ORDER BY updated_at DESC LIMIT 300"))

    @app.post("/category-rules")
    @require_auth
    def create_category_rule():
        payload = json_object_payload()
        fields = {key: payload.get(key) for key in ("rule_name", "category", "pattern")}
        for key, value in fields.items():
            if not isinstance(value, str) or not value.strip() or len(value) > 500:
                abort(400, description=f"{key} must be a non-empty string of at most 500 characters.")
        rule_type = payload.get("rule_type", "text_contains")
        if not isinstance(rule_type, str) or not rule_type.strip() or len(rule_type) > 64:
            abort(400, description="rule_type must be a non-empty string of at most 64 characters.")
        return jsonify(correction_learning.create_category_rule(
            fields["rule_name"].strip(),
            fields["category"].strip(),
            fields["pattern"].strip(),
            rule_type.strip(),
        ))

    @app.get("/document-corrections")
    @require_auth
    def document_corrections():
        return jsonify(database.fetch_all("SELECT * FROM document_corrections ORDER BY created_at DESC LIMIT 300"))

    @app.get("/reports/summary")
    @require_auth
    def report_summary():
        return jsonify(reporting_service.summary())

    @app.get("/reports/expense-by-vendor")
    @require_auth
    def report_expense_by_vendor():
        return jsonify(reporting_service.expense_by_vendor())

    @app.get("/reports/expense-by-category")
    @require_auth
    def report_expense_by_category():
        return jsonify(reporting_service.expense_by_category())

    @app.post("/exports/<table_name>")
    @require_auth
    def export_table(table_name: str):
        return jsonify(reporting_service.export_table_csv(table_name))

    @app.post("/exports/all")
    @require_auth
    def export_all():
        return jsonify(reporting_service.export_all_core_csv())

    @app.get("/backups")
    @require_auth
    def list_backups():
        return jsonify(backup_manager.list_backups())

    @app.post("/backups")
    @require_auth
    def create_backup():
        payload = json_object_payload()
        paths = payload.get("paths")
        backup_config = payload.get("backup_config", {"type": "zip"})
        if paths is not None and not isinstance(paths, list):
            abort(400, description="paths must be an array.")
        if not isinstance(backup_config, dict):
            abort(400, description="backup_config must be an object.")
        result = backup_manager.perform_backup(paths, backup_config)
        raw_status = result.get("status")
        status = raw_status if raw_status in ("success", "failure") else "unknown"
        audit_result = {"status": status}
        if status == "success":
            included_paths = result.get("included_paths")
            audit_result["included_source_count"] = len(included_paths) if isinstance(included_paths, list) else 0
        database.add_audit_log(
            "backup", "archive", "backup_created", None, audit_result,
            "Backup attempt succeeded." if status == "success" else "Backup attempt failed.", "user"
        )
        return jsonify(result)

    @app.post("/backups/restore")
    @require_auth
    def restore_backup():
        payload = json_object_payload()
        backup_path = payload.get("backup_path")
        restore_dir = payload.get("restore_dir", "restore_preview")
        allow_overwrite = payload.get("allow_overwrite", False)
        if not isinstance(backup_path, str) or not backup_path.strip():
            abort(400, description="backup_path must be a non-empty path.")
        if not isinstance(restore_dir, str) or not restore_dir.strip():
            abort(400, description="restore_dir must be a non-empty path.")
        if not isinstance(allow_overwrite, bool):
            abort(400, description="allow_overwrite must be a boolean.")
        result = backup_manager.restore_backup(backup_path, restore_dir, allow_overwrite=allow_overwrite)
        raw_status = result.get("status")
        status = raw_status if raw_status in ("success", "failure") else "unknown"
        database.add_audit_log(
            "backup", "restore", "backup_restore_requested", None, {"status": status},
            "Restore attempt succeeded." if status == "success" else "Restore attempt failed.", "user"
        )
        return jsonify(result)

    return app
