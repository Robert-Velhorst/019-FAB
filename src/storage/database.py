import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional


class Database:
    """Small SQLite persistence layer for the local FAB runtime."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config or {}
        self.db_path = self.config.get("database_path", "data/fab.sqlite3")
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    source_external_id TEXT,
                    original_filename TEXT,
                    local_path TEXT,
                    mime_type TEXT,
                    content_hash TEXT,
                    current_state TEXT NOT NULL DEFAULT 'received',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS document_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id TEXT NOT NULL,
                    version_number INTEGER NOT NULL,
                    file_path TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(id)
                );

                CREATE TABLE IF NOT EXISTS ocr_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id TEXT NOT NULL,
                    ocr_engine TEXT,
                    language TEXT,
                    ocr_text TEXT,
                    confidence_score REAL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(id)
                );

                CREATE TABLE IF NOT EXISTS extracted_fields (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    field_value TEXT,
                    confidence_score REAL,
                    source TEXT,
                    requires_review INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(id)
                );

                CREATE TABLE IF NOT EXISTS manual_review_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id TEXT,
                    reason TEXT NOT NULL,
                    details TEXT,
                    severity TEXT NOT NULL DEFAULT 'normal',
                    status TEXT NOT NULL DEFAULT 'pending',
                    resolution TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT,
                    action TEXT NOT NULL,
                    before_json TEXT,
                    after_json TEXT,
                    reason TEXT,
                    actor TEXT NOT NULL DEFAULT 'system',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS posting_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id TEXT NOT NULL,
                    target_system TEXT NOT NULL,
                    target_account TEXT,
                    dry_run INTEGER NOT NULL DEFAULT 1,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    external_id TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS vendors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL UNIQUE,
                    approved INTEGER NOT NULL DEFAULT 0,
                    default_category TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS duplicate_fingerprints (
                    fingerprint TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS exceptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exception_type TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE,
                    explanation TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bank_transactions (
                    id TEXT PRIMARY KEY,
                    source TEXT,
                    transaction_date TEXT,
                    amount REAL,
                    currency TEXT,
                    description TEXT,
                    counterparty TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reconciliation_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    result_type TEXT NOT NULL,
                    transaction_id TEXT,
                    document_id TEXT,
                    matched INTEGER NOT NULL DEFAULT 0,
                    match_score REAL NOT NULL DEFAULT 0,
                    match_reason_json TEXT NOT NULL DEFAULT '[]',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS missing_receipt_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    transaction_id TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    alert_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );

                CREATE TABLE IF NOT EXISTS outreach_reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    transaction_id TEXT NOT NULL,
                    vendor_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'open',
                    message_template TEXT NOT NULL,
                    reminder_count INTEGER NOT NULL DEFAULT 0,
                    last_sent_at TEXT,
                    next_reminder_at TEXT,
                    stopped_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS dashboard_sessions (
                    session_hash TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                """
            )

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def json_dumps(value: Any) -> str:
        return json.dumps(value if value is not None else {}, sort_keys=True, default=str)

    def upsert_document(self, document: Dict[str, Any], state: str = "received") -> None:
        document_id = str(document.get("id") or document.get("document_id"))
        if not document_id or document_id == "None":
            raise ValueError("Document id is required for persistence")

        metadata = document.get("metadata", {})
        now = self.now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO documents (
                    id, source, source_external_id, original_filename, local_path,
                    mime_type, content_hash, current_state, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source=excluded.source,
                    source_external_id=excluded.source_external_id,
                    original_filename=excluded.original_filename,
                    local_path=excluded.local_path,
                    mime_type=excluded.mime_type,
                    content_hash=excluded.content_hash,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    document_id,
                    document.get("source", "unknown"),
                    document.get("source_external_id") or document_id,
                    document.get("original_filename"),
                    document.get("local_path"),
                    document.get("metadata", {}).get("mime_type"),
                    document.get("content_hash"),
                    state,
                    self.json_dumps(metadata),
                    now,
                    now,
                ),
            )

    def set_document_state(self, document_id: str, new_state: str, reason: str = "", actor: str = "system") -> None:
        with self.connect() as connection:
            current = connection.execute("SELECT current_state FROM documents WHERE id = ?", (document_id,)).fetchone()
            previous_state = current["current_state"] if current else None
            connection.execute(
                "UPDATE documents SET current_state = ?, updated_at = ? WHERE id = ?",
                (new_state, self.now(), document_id),
            )
            self._insert_audit_log(connection, "document", document_id, "state_transition", {"state": previous_state}, {"state": new_state}, reason, actor)

    def add_manual_review_item(self, document_id: str, reason: str, details: str = "", severity: str = "normal") -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO manual_review_items (document_id, reason, details, severity, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
                (document_id, reason, details, severity, self.now()),
            )
            self._insert_audit_log(connection, "document", document_id, "manual_review_required", None, {"reason": reason, "details": details}, reason, "system")

    def add_reconciliation_result(self, result: Dict[str, Any]) -> None:
        transaction = result.get("bank_transaction", {}) or result.get("transaction", {}) or {}
        document = result.get("document", {}) or {}
        transaction_id = transaction.get("id")
        document_id = document.get("document_id")
        with self.connect() as connection:
            if transaction_id:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO bank_transactions (id, source, transaction_date, amount, currency, description, counterparty, metadata_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        transaction_id,
                        transaction.get("source", "bank_import"),
                        transaction.get("date") or transaction.get("transaction_date"),
                        transaction.get("amount"),
                        transaction.get("currency"),
                        transaction.get("description"),
                        transaction.get("counterparty"),
                        self.json_dumps(transaction),
                        self.now(),
                    ),
                )
            connection.execute(
                """
                INSERT INTO reconciliation_results (result_type, transaction_id, document_id, matched, match_score, match_reason_json, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.get("type"),
                    transaction_id,
                    document_id,
                    1 if result.get("matched") else 0,
                    result.get("match_score", 0.0),
                    self.json_dumps(result.get("match_reason", [])),
                    self.json_dumps(result),
                    self.now(),
                ),
            )

    def add_missing_receipt_alert(self, alert: Dict[str, Any]) -> None:
        transaction = alert.get("transaction", {}) or {}
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO missing_receipt_alerts (transaction_id, status, alert_json, created_at) VALUES (?, 'open', ?, ?)",
                (transaction.get("id"), self.json_dumps(alert), self.now()),
            )

    def add_audit_log(self, entity_type: str, entity_id: str, action: str, before: Any = None, after: Any = None, reason: str = "", actor: str = "system") -> None:
        with self.connect() as connection:
            self._insert_audit_log(connection, entity_type, entity_id, action, before, after, reason, actor)

    def _insert_audit_log(self, connection: sqlite3.Connection, entity_type: str, entity_id: Optional[str], action: str, before: Any, after: Any, reason: str, actor: str) -> None:
        connection.execute(
            "INSERT INTO audit_log (entity_type, entity_id, action, before_json, after_json, reason, actor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (entity_type, entity_id, action, self.json_dumps(before), self.json_dumps(after), reason, actor, self.now()),
        )

    def fetch_all(self, query: str, parameters: Iterable[Any] = ()):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, tuple(parameters)).fetchall()]

    def fetch_one(self, query: str, parameters: Iterable[Any] = ()):
        with self.connect() as connection:
            row = connection.execute(query, tuple(parameters)).fetchone()
            return dict(row) if row else None

    def register_dashboard_session(self, session_hash: str, expires_at: str) -> None:
        now = self.now()
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM dashboard_sessions WHERE expires_at <= ? OR revoked_at IS NOT NULL",
                (now,),
            )
            connection.execute(
                "INSERT INTO dashboard_sessions (session_hash, created_at, expires_at) VALUES (?, ?, ?)",
                (session_hash, now, expires_at),
            )

    def dashboard_session_is_active(self, session_hash: str, now: Optional[str] = None) -> bool:
        return self.fetch_one(
            "SELECT 1 AS active FROM dashboard_sessions WHERE session_hash = ? AND revoked_at IS NULL AND expires_at > ?",
            (session_hash, now or self.now()),
        ) is not None

    def revoke_dashboard_session(self, session_hash: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE dashboard_sessions SET revoked_at = ? WHERE session_hash = ? AND revoked_at IS NULL",
                (self.now(), session_hash),
            )
