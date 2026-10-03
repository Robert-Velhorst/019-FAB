import hashlib
import json
import math
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional, Sequence


VENDOR_CATEGORY_RULE_STATUSES = {"suggested", "approved", "rejected", "disabled", "learned"}
GOVERNED_VENDOR_CATEGORY_RULE_STATUSES = {"approved", "rejected", "disabled"}
LEDGER_SCHEMA_VERSION = 1
MISSING_REVIEW_MATCH_REFERENCE_SQL = (
    "CASE WHEN json_valid(corrected_data_json) THEN CAST(COALESCE("
    "json_extract(corrected_data_json, '$.reconciliationMatchId'), "
    "json_extract(corrected_data_json, '$.reconciliation_match_id')) AS INTEGER) END"
)
RECONCILIATION_BANK_REFERENCE_SQL = (
    "CASE WHEN json_valid(metadata_json) THEN CAST(COALESCE("
    "json_extract(metadata_json, '$.bankTransaction.ledgerBankTransactionId'), "
    "json_extract(metadata_json, '$.bankTransaction.ledger_bank_transaction_id'), "
    "json_extract(metadata_json, '$.bankTransaction.bankTransactionRecordId'), "
    "json_extract(metadata_json, '$.bankTransaction.bank_transaction_record_id')) AS INTEGER) END"
)
RECONCILIATION_AD_HOC_ACCOUNT_SQL = (
    "CASE WHEN json_valid(metadata_json) THEN COALESCE("
    "json_extract(metadata_json, '$.bankTransaction.account_identifier'), "
    "json_extract(metadata_json, '$.bankTransaction.accountIdentifier'), '') END"
)
LEDGER_SCHEMA_MIGRATIONS = {
    1: {
        "name": "operations_ledger_baseline_2026_08_09",
        "checksum": hashlib.sha256(
            b"FAB operations ledger schema v1: baseline through runtime controls and support diagnostics"
        ).hexdigest(),
    },
}


def default_ledger_path() -> str:
    """Return a Windows-friendly local FAB ledger path without using the repo."""
    base_dir = (
        os.environ.get("FAB_LOCAL_DATA_DIR")
        or os.environ.get("LOCALAPPDATA")
        or os.path.join(os.path.expanduser("~"), ".fab")
    )
    return os.path.join(base_dir, "FAB", "fab_operations.sqlite3")


class LocalOperationsLedger:
    """SQLite operations ledger for local-first FAB workflow runs.

    The ledger mirrors the web operations API shape closely enough that the
    existing Python pipeline can keep one reporting surface for both online and
    offline/local operation. It stores metadata, OCR text, statuses, review
    items, routing attempts, reconciliation matches, and audit events, but it
    does not store credentials or raw attachment bytes.
    """

    def __init__(self, path: str):
        if not path:
            raise ValueError("Local ledger path is required")
        self.path = os.path.abspath(os.path.expanduser(path))
        self._read_snapshot_connection: ContextVar[Optional[sqlite3.Connection]] = (
            ContextVar(f"fab_ledger_read_snapshot_{id(self)}", default=None)
        )
        self._write_transaction_connection: ContextVar[Optional[sqlite3.Connection]] = (
            ContextVar(f"fab_ledger_write_transaction_{id(self)}", default=None)
        )
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._last_schema_backup = self._prepare_schema_migration_backup()
        self._init_schema()
        if self._last_schema_backup:
            self.record_audit_event({
                "action": "local_ledger.pre_migration_backup_created",
                "entityType": "schema_migration",
                "entityId": str(LEDGER_SCHEMA_VERSION),
                "details": {
                    "sourceSchemaVersion": self._last_schema_backup["sourceSchemaVersion"],
                    "targetSchemaVersion": LEDGER_SCHEMA_VERSION,
                    "backupFilename": self._last_schema_backup["backupFilename"],
                    "manifestFilename": self._last_schema_backup["manifestFilename"],
                    "sha256": self._last_schema_backup["sha256"],
                    "externalSubmission": "not_executed",
                },
            })

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextmanager
    def _connection(self):
        transaction_connection = self._write_transaction_connection.get()
        if transaction_connection is not None:
            yield transaction_connection
            return
        snapshot_connection = self._read_snapshot_connection.get()
        if snapshot_connection is not None:
            yield snapshot_connection
            return
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    @contextmanager
    def read_snapshot(self):
        """Reuse one query-only SQLite snapshot across a compound read."""
        if self._write_transaction_connection.get() is not None:
            yield self
            return
        active_connection = self._read_snapshot_connection.get()
        if active_connection is not None:
            yield self
            return

        connection = self._connect()
        token = None
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            token = self._read_snapshot_connection.set(connection)
            yield self
        finally:
            if token is not None:
                self._read_snapshot_connection.reset(token)
            try:
                connection.rollback()
            finally:
                connection.close()

    @contextmanager
    def write_transaction(self):
        """Commit a compound local change once, with savepoints for nested changes."""
        if self._read_snapshot_connection.get() is not None:
            raise RuntimeError("Cannot start a write transaction inside a read-only snapshot")
        active = self._write_transaction_connection.get()
        if active is not None:
            savepoint = "fab_write_" + uuid.uuid4().hex
            active.execute(f"SAVEPOINT {savepoint}")
            try:
                yield self
                active.execute(f"RELEASE SAVEPOINT {savepoint}")
            except BaseException:
                active.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                active.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            return

        connection = self._connect()
        token = None
        try:
            # Reserve the writer before reading a decision that will be changed.
            connection.execute("BEGIN IMMEDIATE")
            token = self._write_transaction_connection.set(connection)
            yield self
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            if token is not None:
                self._write_transaction_connection.reset(token)
            connection.close()

    def _init_schema(self) -> None:
        with self._connection() as connection:
            self._validate_schema_migration_history(connection)
            # WAL keeps dashboard reads available while the autonomous worker
            # commits a short bookkeeping transaction in another process.
            journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(journal_mode).lower() != "wal":
                raise RuntimeError("FAB local ledger requires SQLite WAL mode")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL,
                    trigger_source TEXT NOT NULL,
                    recovery_source_workflow_run_id INTEGER,
                    recovery_root_workflow_run_id INTEGER,
                    documents_imported INTEGER NOT NULL DEFAULT 0,
                    documents_processed INTEGER NOT NULL DEFAULT 0,
                    documents_needing_review INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    metadata_json TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(recovery_source_workflow_run_id) REFERENCES workflow_runs(id) ON DELETE SET NULL,
                    FOREIGN KEY(recovery_root_workflow_run_id) REFERENCES workflow_runs(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS source_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type TEXT NOT NULL,
                    source_identifier TEXT NOT NULL,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    last_seen_at TEXT,
                    last_scan_at TEXT,
                    documents_seen INTEGER NOT NULL DEFAULT 0,
                    documents_imported INTEGER NOT NULL DEFAULT 0,
                    duplicates_detected INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_type, source_identifier)
                );

                CREATE INDEX IF NOT EXISTS idx_local_sources_type
                    ON source_accounts(source_type);
                CREATE INDEX IF NOT EXISTS idx_local_sources_status
                    ON source_accounts(status);

                CREATE TABLE IF NOT EXISTS bookkeeping_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_account_id INTEGER,
                    source TEXT NOT NULL,
                    source_document_id TEXT,
                    original_filename TEXT NOT NULL,
                    mime_type TEXT,
                    storage_path TEXT,
                    document_type TEXT NOT NULL DEFAULT 'unknown',
                    processing_status TEXT NOT NULL DEFAULT 'imported',
                    duplicate_fingerprint TEXT,
                    content_sha256 TEXT,
                    duplicate_of_document_id INTEGER,
                    vendor_name TEXT,
                    category TEXT,
                    transaction_date TEXT,
                    total_amount REAL,
                    vat_amount REAL,
                    confidence_score REAL,
                    reconciliation_status TEXT NOT NULL DEFAULT 'not_started',
                    ocr_text TEXT,
                    extracted_data_json TEXT,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source, source_document_id)
                );

                CREATE INDEX IF NOT EXISTS idx_local_docs_status
                    ON bookkeeping_documents(processing_status);
                CREATE INDEX IF NOT EXISTS idx_local_docs_source
                    ON bookkeeping_documents(source, source_document_id);
                CREATE INDEX IF NOT EXISTS idx_local_docs_duplicate
                    ON bookkeeping_documents(duplicate_fingerprint);

                CREATE INDEX IF NOT EXISTS idx_local_runs_status
                    ON workflow_runs(status);

                CREATE TABLE IF NOT EXISTS review_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER,
                    reason TEXT NOT NULL,
                    details TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    corrected_data_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_local_review_status
                    ON review_items(status);

                CREATE INDEX IF NOT EXISTS idx_local_review_status_document_order
                    ON review_items(status, document_id, created_at DESC, id DESC);

                CREATE TABLE IF NOT EXISTS duplicate_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL,
                    candidate_document_id INTEGER NOT NULL,
                    match_type TEXT NOT NULL,
                    confidence_score REAL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    reason TEXT,
                    evidence_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(document_id, candidate_document_id, match_type)
                );

                CREATE INDEX IF NOT EXISTS idx_local_duplicates_document
                    ON duplicate_candidates(document_id);
                CREATE INDEX IF NOT EXISTS idx_local_duplicates_candidate
                    ON duplicate_candidates(candidate_document_id);
                CREATE INDEX IF NOT EXISTS idx_local_duplicates_status
                    ON duplicate_candidates(status);

                CREATE TABLE IF NOT EXISTS document_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_key TEXT NOT NULL,
                    group_type TEXT NOT NULL DEFAULT 'scanner_batch',
                    title TEXT,
                    status TEXT NOT NULL DEFAULT 'candidate',
                    primary_document_id INTEGER,
                    confidence_score REAL,
                    reason TEXT,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(group_key)
                );

                CREATE INDEX IF NOT EXISTS idx_local_document_groups_status
                    ON document_groups(status);
                CREATE INDEX IF NOT EXISTS idx_local_document_groups_type
                    ON document_groups(group_type);

                CREATE TABLE IF NOT EXISTS document_group_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL,
                    document_id INTEGER NOT NULL,
                    role TEXT NOT NULL DEFAULT 'page',
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active',
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(group_id, document_id)
                );

                CREATE INDEX IF NOT EXISTS idx_local_group_members_group
                    ON document_group_members(group_id);
                CREATE INDEX IF NOT EXISTS idx_local_group_members_document
                    ON document_group_members(document_id);
                CREATE INDEX IF NOT EXISTS idx_local_group_members_status
                    ON document_group_members(status);

                CREATE TABLE IF NOT EXISTS extracted_fields (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL,
                    field_name TEXT NOT NULL,
                    field_value_json TEXT,
                    normalized_value TEXT,
                    confidence_score REAL,
                    source TEXT NOT NULL DEFAULT 'local_processing',
                    provenance_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_local_extracted_fields_document
                    ON extracted_fields(document_id);
                CREATE INDEX IF NOT EXISTS idx_local_extracted_fields_name
                    ON extracted_fields(field_name);

                CREATE TABLE IF NOT EXISTS review_corrections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    review_item_id INTEGER,
                    document_id INTEGER,
                    original_data_json TEXT,
                    corrected_data_json TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_local_corrections_document
                    ON review_corrections(document_id);

                CREATE TABLE IF NOT EXISTS vendor_category_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    normalized_vendor_name TEXT NOT NULL,
                    vendor_name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    target_system TEXT NOT NULL DEFAULT 'none',
                    confidence_score REAL,
                    status TEXT NOT NULL DEFAULT 'learned',
                    source_document_id TEXT,
                    usage_count INTEGER NOT NULL DEFAULT 1,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(normalized_vendor_name, category, target_system)
                );

                CREATE INDEX IF NOT EXISTS idx_local_vendor_rules_vendor
                    ON vendor_category_rules(normalized_vendor_name);
                CREATE INDEX IF NOT EXISTS idx_local_vendor_rules_status
                    ON vendor_category_rules(status);

                CREATE TABLE IF NOT EXISTS routing_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER,
                    bookkeeping_record_id INTEGER,
                    workflow_run_id INTEGER,
                    target TEXT NOT NULL,
                    status TEXT NOT NULL,
                    external_id TEXT,
                    message TEXT,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS export_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bookkeeping_record_id INTEGER,
                    document_id INTEGER,
                    routing_attempt_id INTEGER,
                    workflow_run_id INTEGER,
                    target_system TEXT NOT NULL DEFAULT 'waveapps',
                    target_account TEXT,
                    action_id TEXT,
                    surface TEXT,
                    operation_id TEXT,
                    status TEXT NOT NULL DEFAULT 'approval_required',
                    safety TEXT NOT NULL DEFAULT 'requires_confirmation',
                    approval_required INTEGER NOT NULL DEFAULT 1,
                    approved_at TEXT,
                    approved_by TEXT,
                    external_submission TEXT NOT NULL DEFAULT 'not_executed',
                    submitted_at TEXT,
                    external_id TEXT,
                    message TEXT,
                    payload_json TEXT,
                    result_json TEXT,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reconciliation_matches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER,
                    bank_transaction_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    confidence_score REAL,
                    amount_difference REAL,
                    matched_at TEXT,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_local_reconciliation_status
                    ON reconciliation_matches(status);
                CREATE INDEX IF NOT EXISTS idx_local_reconciliation_document
                    ON reconciliation_matches(document_id);
                CREATE INDEX IF NOT EXISTS idx_local_reconciliation_bank_tx
                    ON reconciliation_matches(bank_transaction_id);

                CREATE TABLE IF NOT EXISTS bank_statement_imports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL DEFAULT 'manual_import',
                    account_identifier TEXT NOT NULL DEFAULT 'default',
                    filename TEXT,
                    format TEXT NOT NULL DEFAULT 'json',
                    status TEXT NOT NULL DEFAULT 'running',
                    rows_seen INTEGER NOT NULL DEFAULT 0,
                    rows_imported INTEGER NOT NULL DEFAULT 0,
                    duplicates INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_local_bank_imports_account
                    ON bank_statement_imports(account_identifier);
                CREATE INDEX IF NOT EXISTS idx_local_bank_imports_status
                    ON bank_statement_imports(status);

                CREATE TABLE IF NOT EXISTS bank_transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    import_id INTEGER,
                    account_identifier TEXT NOT NULL DEFAULT 'default',
                    transaction_id TEXT NOT NULL,
                    transaction_date TEXT,
                    amount REAL,
                    currency TEXT NOT NULL DEFAULT 'EUR',
                    description TEXT,
                    counterparty TEXT,
                    status TEXT NOT NULL DEFAULT 'imported',
                    reconciliation_status TEXT NOT NULL DEFAULT 'not_started',
                    duplicate_fingerprint TEXT,
                    source TEXT NOT NULL DEFAULT 'manual_import',
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(account_identifier, transaction_id)
                );

                CREATE INDEX IF NOT EXISTS idx_local_bank_tx_account
                    ON bank_transactions(account_identifier);
                CREATE INDEX IF NOT EXISTS idx_local_bank_tx_status
                    ON bank_transactions(status);
                CREATE INDEX IF NOT EXISTS idx_local_bank_tx_reconciliation
                    ON bank_transactions(reconciliation_status);
                CREATE INDEX IF NOT EXISTS idx_local_bank_tx_date
                    ON bank_transactions(transaction_date);
                CREATE INDEX IF NOT EXISTS idx_local_bank_tx_fingerprint
                    ON bank_transactions(duplicate_fingerprint);

                CREATE TABLE IF NOT EXISTS bookkeeping_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER,
                    bank_transaction_id INTEGER,
                    source_type TEXT NOT NULL DEFAULT 'document',
                    record_type TEXT NOT NULL DEFAULT 'expense',
                    status TEXT NOT NULL DEFAULT 'draft',
                    target_system TEXT NOT NULL DEFAULT 'waveapps',
                    target_account TEXT,
                    vendor_name TEXT,
                    category TEXT,
                    record_date TEXT,
                    amount REAL,
                    vat_amount REAL,
                    currency TEXT NOT NULL DEFAULT 'EUR',
                    description TEXT,
                    confidence_score REAL,
                    review_required INTEGER NOT NULL DEFAULT 0,
                    export_status TEXT NOT NULL DEFAULT 'not_started',
                    reconciliation_status TEXT NOT NULL DEFAULT 'not_started',
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bookkeeping_record_line_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bookkeeping_record_id INTEGER NOT NULL,
                    line_index INTEGER NOT NULL DEFAULT 0,
                    item_name TEXT,
                    description TEXT,
                    quantity REAL,
                    unit_price REAL,
                    amount REAL,
                    tax_amount REAL,
                    tax_rate REAL,
                    tax_code TEXT,
                    category TEXT,
                    account_name TEXT,
                    source TEXT NOT NULL DEFAULT 'extraction',
                    confidence_score REAL,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(bookkeeping_record_id, line_index),
                    FOREIGN KEY(bookkeeping_record_id) REFERENCES bookkeeping_records(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS wave_report_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_run_id INTEGER,
                    operation_id TEXT NOT NULL,
                    workflow_id TEXT,
                    report_type TEXT NOT NULL,
                    report_section TEXT,
                    action_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'planned',
                    safety TEXT NOT NULL DEFAULT 'read_only',
                    from_date TEXT,
                    to_date TEXT,
                    as_of_date TEXT,
                    basis TEXT,
                    account_option TEXT,
                    account_name TEXT,
                    contact_option TEXT,
                    contact_name TEXT,
                    cash_mode TEXT,
                    export_format TEXT,
                    row_count INTEGER,
                    total_debits REAL,
                    total_credits REAL,
                    total_amount REAL,
                    external_submission TEXT NOT NULL DEFAULT 'not_executed',
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(operation_id)
                );

                CREATE INDEX IF NOT EXISTS idx_local_wave_reports_type
                    ON wave_report_snapshots(report_type);
                CREATE INDEX IF NOT EXISTS idx_local_wave_reports_status
                    ON wave_report_snapshots(status);
                CREATE INDEX IF NOT EXISTS idx_local_wave_reports_workflow
                    ON wave_report_snapshots(workflow_id);

                CREATE TABLE IF NOT EXISTS wave_operation_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_run_id INTEGER,
                    workflow_id TEXT,
                    operation_id TEXT NOT NULL,
                    surface TEXT,
                    action_id TEXT,
                    mode TEXT,
                    safety TEXT NOT NULL DEFAULT 'unsupported',
                    status TEXT NOT NULL DEFAULT 'planned',
                    plan_status TEXT,
                    plan_json TEXT,
                    capability_plan_json TEXT,
                    requires_confirmation INTEGER,
                    requires_credentials INTEGER,
                    required_fields_json TEXT,
                    missing_fields_json TEXT,
                    external_submission TEXT NOT NULL DEFAULT 'not_executed',
                    payload_json TEXT,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(operation_id)
                );

                CREATE TABLE IF NOT EXISTS wave_sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_system TEXT NOT NULL,
                    entity_types_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    page_size INTEGER NOT NULL DEFAULT 50,
                    pages_fetched INTEGER NOT NULL DEFAULT 0,
                    entities_seen INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    metadata_json TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workflow_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_run_id INTEGER NOT NULL,
                    step_key TEXT NOT NULL,
                    stage TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempt INTEGER NOT NULL DEFAULT 1,
                    step_order INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT,
                    finished_at TEXT,
                    duration_ms INTEGER,
                    error_message TEXT,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(workflow_run_id, step_key, attempt),
                    FOREIGN KEY(workflow_run_id) REFERENCES workflow_runs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_local_workflow_steps_run
                    ON workflow_steps(workflow_run_id, step_order, id);
                CREATE INDEX IF NOT EXISTS idx_local_workflow_steps_status
                    ON workflow_steps(status);
                CREATE INDEX IF NOT EXISTS idx_local_workflow_steps_key
                    ON workflow_steps(step_key);

                CREATE TABLE IF NOT EXISTS runtime_leases (
                    lease_name TEXT PRIMARY KEY,
                    owner_token TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_local_runtime_leases_expiry
                    ON runtime_leases(expires_at);

                CREATE TABLE IF NOT EXISTS runtime_controls (
                    control_name TEXT PRIMARY KEY,
                    active INTEGER NOT NULL DEFAULT 0,
                    reason TEXT,
                    updated_by TEXT NOT NULL,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS wave_entities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_system TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    name TEXT,
                    status TEXT,
                    email TEXT,
                    currency TEXT,
                    amount REAL,
                    entity_date TEXT,
                    due_date TEXT,
                    modified_at TEXT,
                    presence_status TEXT NOT NULL DEFAULT 'present',
                    last_sync_run_id INTEGER,
                    payload_json TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(target_system, entity_type, external_id)
                );

                CREATE TABLE IF NOT EXISTS financial_report_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_id TEXT NOT NULL,
                    schedule_slot TEXT NOT NULL,
                    report_type TEXT NOT NULL DEFAULT 'overview',
                    basis TEXT NOT NULL DEFAULT 'accrual',
                    period_from TEXT NOT NULL,
                    period_to TEXT NOT NULL,
                    target_system TEXT,
                    status TEXT NOT NULL DEFAULT 'running',
                    readiness TEXT,
                    scheduled_for TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 1,
                    next_retry_at TEXT,
                    json_path TEXT,
                    csv_path TEXT,
                    json_sha256 TEXT,
                    csv_sha256 TEXT,
                    json_bytes INTEGER,
                    csv_bytes INTEGER,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    blocker_count INTEGER NOT NULL DEFAULT 0,
                    external_submission TEXT NOT NULL DEFAULT 'not_executed',
                    error_message TEXT,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(schedule_id, schedule_slot)
                );

                CREATE INDEX IF NOT EXISTS idx_local_financial_reports_status
                    ON financial_report_runs(status);
                CREATE INDEX IF NOT EXISTS idx_local_financial_reports_scheduled
                    ON financial_report_runs(scheduled_for);
                CREATE INDEX IF NOT EXISTS idx_local_financial_reports_schedule
                    ON financial_report_runs(schedule_id, schedule_slot);

                CREATE TABLE IF NOT EXISTS notification_preferences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    in_app_enabled INTEGER NOT NULL DEFAULT 1,
                    minimum_severity TEXT NOT NULL DEFAULT 'low',
                    external_delivery TEXT NOT NULL DEFAULT 'disabled',
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(event_type)
                );

                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'low',
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    entity_type TEXT,
                    entity_id TEXT,
                    status TEXT NOT NULL DEFAULT 'unread',
                    source TEXT NOT NULL DEFAULT 'operations_health',
                    occurrence_count INTEGER NOT NULL DEFAULT 1,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    read_at TEXT,
                    acknowledged_at TEXT,
                    resolved_at TEXT,
                    external_delivery TEXT NOT NULL DEFAULT 'not_executed',
                    payload_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(fingerprint)
                );

                CREATE INDEX IF NOT EXISTS idx_local_notifications_status
                    ON notifications(status);
                CREATE INDEX IF NOT EXISTS idx_local_notifications_severity
                    ON notifications(severity);
                CREATE INDEX IF NOT EXISTS idx_local_notifications_event_type
                    ON notifications(event_type);
                CREATE INDEX IF NOT EXISTS idx_local_notifications_last_seen
                    ON notifications(last_seen_at);

                CREATE TABLE IF NOT EXISTS compliance_assessments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    assessment_key TEXT NOT NULL,
                    period_from TEXT NOT NULL,
                    period_to TEXT NOT NULL,
                    basis TEXT NOT NULL DEFAULT 'accrual',
                    target_system TEXT,
                    status TEXT NOT NULL DEFAULT 'ready',
                    record_count INTEGER NOT NULL DEFAULT 0,
                    finding_count INTEGER NOT NULL DEFAULT 0,
                    blocking_count INTEGER NOT NULL DEFAULT 0,
                    attention_count INTEGER NOT NULL DEFAULT 0,
                    vat_summary_json TEXT,
                    source_checksum TEXT NOT NULL,
                    statutory_status TEXT NOT NULL DEFAULT 'provisional',
                    external_filing TEXT NOT NULL DEFAULT 'not_executed',
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(assessment_key)
                );

                CREATE INDEX IF NOT EXISTS idx_local_compliance_assessments_period
                    ON compliance_assessments(period_from, period_to);
                CREATE INDEX IF NOT EXISTS idx_local_compliance_assessments_status
                    ON compliance_assessments(status);

                CREATE TABLE IF NOT EXISTS compliance_findings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    assessment_id INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL,
                    code TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'medium',
                    status TEXT NOT NULL DEFAULT 'open',
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    bookkeeping_record_id INTEGER,
                    document_id INTEGER,
                    evidence_json TEXT,
                    resolution TEXT,
                    resolved_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(assessment_id, fingerprint),
                    FOREIGN KEY(assessment_id) REFERENCES compliance_assessments(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_local_compliance_findings_assessment
                    ON compliance_findings(assessment_id);
                CREATE INDEX IF NOT EXISTS idx_local_compliance_findings_status
                    ON compliance_findings(status);
                CREATE INDEX IF NOT EXISTS idx_local_compliance_findings_severity
                    ON compliance_findings(severity);
                CREATE INDEX IF NOT EXISTS idx_local_compliance_findings_record
                    ON compliance_findings(bookkeeping_record_id);

                CREATE TABLE IF NOT EXISTS retention_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL,
                    assessment_id INTEGER,
                    source_date TEXT,
                    retention_years INTEGER NOT NULL DEFAULT 7,
                    retain_until TEXT,
                    status TEXT NOT NULL DEFAULT 'retain_required',
                    source_file_present INTEGER,
                    metadata_json TEXT,
                    last_assessed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(document_id),
                    FOREIGN KEY(assessment_id) REFERENCES compliance_assessments(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_local_retention_status
                    ON retention_records(status);
                CREATE INDEX IF NOT EXISTS idx_local_retention_until
                    ON retention_records(retain_until);

                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_user_id INTEGER,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT,
                    details_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_local_audit_entity
                    ON audit_events(entity_type, entity_id);
                CREATE INDEX IF NOT EXISTS idx_local_audit_created
                    ON audit_events(created_at);
                CREATE INDEX IF NOT EXISTS idx_local_audit_action_created
                    ON audit_events(action, created_at DESC, id DESC);
                """
            )
            self._ensure_column(
                connection,
                "workflow_runs",
                "recovery_source_workflow_run_id",
                "INTEGER",
            )
            self._ensure_column(
                connection,
                "workflow_runs",
                "recovery_root_workflow_run_id",
                "INTEGER",
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_local_workflow_recovery_source
                    ON workflow_runs(recovery_source_workflow_run_id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_local_workflow_recovery_root
                    ON workflow_runs(recovery_root_workflow_run_id)
                """
            )
            self._ensure_column(
                connection,
                "bookkeeping_documents",
                "source_account_id",
                "INTEGER",
            )
            self._ensure_column(
                connection,
                "bookkeeping_documents",
                "reconciliation_status",
                "TEXT NOT NULL DEFAULT 'not_started'",
            )
            self._ensure_column(
                connection,
                "bookkeeping_documents",
                "content_sha256",
                "TEXT",
            )
            self._backfill_document_content_hashes(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_local_docs_content_sha256
                    ON bookkeeping_documents(content_sha256)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_local_docs_reconciliation
                    ON bookkeeping_documents(reconciliation_status)
                """
            )
            self._ensure_bookkeeping_record_schema(connection)
            self._ensure_bookkeeping_record_line_item_schema(connection)
            self._ensure_routing_attempt_schema(connection)
            self._ensure_export_attempt_schema(connection)
            self._ensure_wave_operation_snapshot_schema(connection)
            self._ensure_wave_entity_mirror_schema(connection)
            self._ensure_review_item_schema(connection)
            self._ensure_reconciliation_lookup_indexes(connection)
            self._record_schema_migration(connection)

    def schema_status(self) -> Dict[str, Any]:
        with self._connection() as connection:
            self._validate_schema_migration_history(connection)
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            rows = connection.execute(
                "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
            ).fetchall()
            backup_row = connection.execute(
                """
                SELECT created_at FROM audit_events
                WHERE action = 'local_ledger.pre_migration_backup_created'
                  AND entity_type = 'schema_migration'
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        migrations = [self._row_to_dict(row) for row in rows]
        versions = {int(item["version"]) for item in migrations}
        return {
            "status": "current" if user_version == LEDGER_SCHEMA_VERSION else "attention_required",
            "currentVersion": user_version,
            "supportedVersion": LEDGER_SCHEMA_VERSION,
            "historyComplete": versions == set(range(1, LEDGER_SCHEMA_VERSION + 1)),
            "appliedMigrations": len(migrations),
            "latestAppliedAt": migrations[-1]["applied_at"] if migrations else None,
            "preMigrationBackupCreated": backup_row is not None,
            "lastPreMigrationBackupAt": backup_row["created_at"] if backup_row else None,
        }

    def _prepare_schema_migration_backup(self) -> Optional[Dict[str, Any]]:
        if not os.path.isfile(self.path) or os.path.getsize(self.path) == 0:
            return None
        state = self._inspect_existing_schema()
        current_version = int(state["version"])
        if current_version > LEDGER_SCHEMA_VERSION:
            raise RuntimeError(
                f"FAB ledger schema version {current_version} is newer than supported version "
                f"{LEDGER_SCHEMA_VERSION}; refusing to modify it"
            )
        if not state["hasUserTables"] or current_version >= LEDGER_SCHEMA_VERSION:
            return None

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_dir = os.path.join(os.path.dirname(self.path), "schema_backups")
        os.makedirs(backup_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(self.path))[0]
        backup_filename = (
            f"{stem}.pre-schema-v{current_version}-to-v{LEDGER_SCHEMA_VERSION}.{timestamp}.sqlite3"
        )
        manifest_filename = f"{backup_filename}.json"
        backup_path = os.path.join(backup_dir, backup_filename)
        manifest_path = os.path.join(backup_dir, manifest_filename)
        source: Optional[sqlite3.Connection] = None
        destination: Optional[sqlite3.Connection] = None
        try:
            source = sqlite3.connect(self.path, timeout=30.0)
            destination = sqlite3.connect(backup_path)
            source.execute("PRAGMA busy_timeout = 30000")
            source.backup(destination)
            integrity = destination.execute("PRAGMA integrity_check").fetchone()
            if not integrity or str(integrity[0]).lower() != "ok":
                raise RuntimeError("Pre-migration ledger backup failed SQLite integrity verification")
            destination.close()
            destination = None
            source.close()
            source = None
            digest = self._file_sha256(backup_path)
            manifest = {
                "schemaVersion": 1,
                "sourceSchemaVersion": current_version,
                "targetSchemaVersion": LEDGER_SCHEMA_VERSION,
                "backupFilename": backup_filename,
                "sha256": digest,
                "createdAt": self._now(),
                "integrityCheck": "ok",
            }
            temporary_manifest = f"{manifest_path}.tmp"
            with open(temporary_manifest, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(manifest, handle, sort_keys=True, indent=2, ensure_ascii=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_manifest, manifest_path)
            for private_path in (backup_path, manifest_path):
                try:
                    os.chmod(private_path, 0o600)
                except OSError:
                    pass
            return {
                **manifest,
                "manifestFilename": manifest_filename,
            }
        except Exception:
            for partial_path in (f"{manifest_path}.tmp", manifest_path, backup_path):
                try:
                    if os.path.isfile(partial_path):
                        os.remove(partial_path)
                except OSError:
                    pass
            raise
        finally:
            if destination is not None:
                destination.close()
            if source is not None:
                source.close()

    def _inspect_existing_schema(self) -> Dict[str, Any]:
        connection = sqlite3.connect(self.path, timeout=30.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 30000")
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            has_user_tables = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                LIMIT 1
                """
            ).fetchone() is not None
            has_history = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone() is not None
            history_version = 0
            if has_history:
                row = connection.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
                history_version = int(row["version"] or 0)
            return {
                "version": max(user_version, history_version),
                "hasUserTables": has_user_tables,
            }
        except sqlite3.DatabaseError as exc:
            raise RuntimeError("FAB ledger could not be inspected safely before migration") from exc
        finally:
            connection.close()

    @staticmethod
    def _file_sha256(path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _migration_table_exists(connection: sqlite3.Connection) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone() is not None

    def _validate_schema_migration_history(self, connection: sqlite3.Connection) -> None:
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if user_version > LEDGER_SCHEMA_VERSION:
            raise RuntimeError(
                f"FAB ledger schema version {user_version} is newer than supported version "
                f"{LEDGER_SCHEMA_VERSION}"
            )
        if not self._migration_table_exists(connection):
            return
        rows = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        versions = {int(row["version"]) for row in rows}
        expected_versions = set(range(1, user_version + 1))
        if versions != expected_versions:
            raise RuntimeError(
                "FAB ledger schema migration history does not match its declared schema version"
            )
        for row in rows:
            version = int(row["version"])
            expected = LEDGER_SCHEMA_MIGRATIONS.get(version)
            if expected is None:
                raise RuntimeError(f"FAB ledger contains unknown schema migration version {version}")
            if row["name"] != expected["name"] or row["checksum"] != expected["checksum"]:
                raise RuntimeError(f"FAB ledger schema migration {version} failed checksum validation")

    def _record_schema_migration(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        applied_at = self._now()
        for version in range(1, LEDGER_SCHEMA_VERSION + 1):
            migration = LEDGER_SCHEMA_MIGRATIONS[version]
            connection.execute(
                """
                INSERT OR IGNORE INTO schema_migrations (version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (version, migration["name"], migration["checksum"], applied_at),
            )
        connection.execute(f"PRAGMA user_version = {LEDGER_SCHEMA_VERSION}")
        self._validate_schema_migration_history(connection)

    def acquire_runtime_lease(
        self,
        lease_name: str,
        owner_token: str,
        ttl_seconds: float = 21600,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        lease_name = str(lease_name or "").strip()
        owner_token = str(owner_token or "").strip()
        if not lease_name or not owner_token:
            raise ValueError("lease_name and owner_token are required")
        try:
            ttl_seconds = max(float(ttl_seconds), 1.0)
        except (TypeError, ValueError):
            ttl_seconds = 21600.0
        now_value = datetime.now(timezone.utc)
        now = now_value.isoformat()
        expires_at = datetime.fromtimestamp(
            now_value.timestamp() + ttl_seconds,
            tz=timezone.utc,
        ).isoformat()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM runtime_leases WHERE lease_name = ? LIMIT 1",
                (lease_name,),
            ).fetchone()
            existing = self._row_to_dict(row) if row else None
            if existing and existing.get("owner_token") != owner_token:
                existing_expiry = self._parse_datetime(existing.get("expires_at"))
                if existing_expiry is None or existing_expiry > now_value:
                    return {
                        "acquired": False,
                        "status": "already_held" if existing_expiry else "invalid_lease",
                        "lease": self._public_runtime_lease(existing, now_value),
                    }
            values = (
                lease_name,
                owner_token,
                now,
                now,
                expires_at,
                self._json(self._redact_sensitive(metadata or {})),
                now,
                now,
            )
            connection.execute(
                """
                INSERT INTO runtime_leases (
                    lease_name, owner_token, acquired_at, heartbeat_at,
                    expires_at, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(lease_name) DO UPDATE SET
                    owner_token = excluded.owner_token,
                    acquired_at = excluded.acquired_at,
                    heartbeat_at = excluded.heartbeat_at,
                    expires_at = excluded.expires_at,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                values,
            )
            row = connection.execute(
                "SELECT * FROM runtime_leases WHERE lease_name = ? LIMIT 1",
                (lease_name,),
            ).fetchone()
        lease = self._row_to_dict(row) if row else None
        return {
            "acquired": True,
            "status": "acquired",
            "lease": self._public_runtime_lease(lease or {}, now_value),
        }

    def release_runtime_lease(self, lease_name: str, owner_token: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM runtime_leases WHERE lease_name = ? AND owner_token = ?",
                (str(lease_name or "").strip(), str(owner_token or "").strip()),
            )
            return int(cursor.rowcount) == 1

    def force_release_runtime_lease(
        self,
        lease_name: str,
        *,
        actor: str,
        reason: str,
    ) -> bool:
        """Release a lease after its owning FAB services have been stopped."""
        normalized_name = str(lease_name or "").strip()
        normalized_actor = str(actor or "").strip()
        normalized_reason = str(reason or "").strip()
        if not normalized_name or not normalized_actor or not normalized_reason:
            raise ValueError("lease_name, actor, and reason are required")

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._force_release_runtime_lease_with_connection(
                connection, normalized_name, normalized_actor, normalized_reason
            )

    def force_release_stopped_runtime_leases(self, *, actor: str) -> list:
        """Release only local worker/API leases after all owning services stop.

        The caller must verify shutdown first. A failed delete or audit insert
        rolls back the entire batch, including leases from earlier pages.
        """
        normalized_actor = str(actor or "").strip()
        if not normalized_actor:
            raise ValueError("actor is required")
        released = []
        last_name = ""
        prefix = "hai_command:"
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            while True:
                rows = connection.execute(
                    "SELECT lease_name FROM runtime_leases WHERE "
                    "(lease_name IN (?, ?) OR substr(lease_name, 1, length(?)) = ?) "
                    "AND lease_name > ? ORDER BY lease_name LIMIT 100",
                    ("local_connector_intake", "local_autonomous_cycle", prefix, prefix, last_name),
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    name = row["lease_name"]
                    reason = "owned_hai_api_stopped" if name.startswith(prefix) else "owned_services_stopped"
                    if not self._force_release_runtime_lease_with_connection(
                        connection, name, normalized_actor, reason
                    ):
                        raise RuntimeError("Stopped runtime lease could not be released")
                    released.append(name)
                last_name = rows[-1]["lease_name"]
        return released

    def _force_release_runtime_lease_with_connection(
        self, connection: sqlite3.Connection, name: str, actor: str, reason: str
    ) -> bool:
        row = connection.execute(
            "SELECT owner_token FROM runtime_leases WHERE lease_name = ? LIMIT 1",
            (name,),
        ).fetchone()
        if not row:
            return False
        owner_token = str(row["owner_token"] or "").strip()
        if not owner_token:
            return False
        deleted = connection.execute(
            "DELETE FROM runtime_leases WHERE lease_name = ? AND owner_token = ?",
            (name, owner_token),
        )
        if int(deleted.rowcount) != 1:
            return False
        self._record_audit_event_with_connection(connection, {
            "action": "runtime_lease.force_released",
            "entityType": "runtime_lease",
            "entityId": name,
            "details": {
                "actor": actor,
                "reason": reason,
                "externalSubmission": "not_executed",
            },
        })
        return True

    def get_runtime_lease(self, lease_name: str) -> Optional[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_leases WHERE lease_name = ? LIMIT 1",
                (str(lease_name or "").strip(),),
            ).fetchone()
        if not row:
            return None
        return self._public_runtime_lease(self._row_to_dict(row), now)

    def list_runtime_leases(
        self,
        name_prefix: Optional[str] = None,
        limit: int = 100,
    ) -> list:
        query = "SELECT * FROM runtime_leases"
        params: list = []
        normalized_prefix = str(name_prefix or "").strip()
        if normalized_prefix:
            query += " WHERE substr(lease_name, 1, length(?)) = ?"
            params.extend((normalized_prefix, normalized_prefix))
        query += " ORDER BY updated_at DESC, lease_name ASC LIMIT ?"
        params.append(self._bounded_limit(limit))
        now = datetime.now(timezone.utc)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            self._public_runtime_lease(self._row_to_dict(row), now)
            for row in rows
        ]

    def set_runtime_control(
        self,
        control_name: str,
        active: bool,
        *,
        actor: str,
        reason: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        normalized_name = str(control_name or "").strip()
        normalized_actor = str(actor or "").strip()[:200]
        normalized_reason = str(reason or "").strip()[:1000]
        if not normalized_name or not normalized_actor or not normalized_reason:
            raise ValueError("control_name, actor, and reason are required")

        now = self._now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO runtime_controls (
                    control_name, active, reason, updated_by, metadata_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(control_name) DO UPDATE SET
                    active = excluded.active,
                    reason = excluded.reason,
                    updated_by = excluded.updated_by,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized_name,
                    1 if active else 0,
                    normalized_reason,
                    normalized_actor,
                    self._json(self._redact_sensitive(metadata or {})),
                    now,
                    now,
                ),
            )

        control = self.get_runtime_control(normalized_name)
        self.record_audit_event({
            "action": "runtime_control.engaged" if active else "runtime_control.cleared",
            "entityType": "runtime_control",
            "entityId": normalized_name,
            "details": {
                "actor": normalized_actor,
                "reason": normalized_reason,
                "active": bool(active),
                "externalSubmission": "not_executed",
            },
        })
        return control

    def get_runtime_control(self, control_name: str) -> Dict[str, Any]:
        normalized_name = str(control_name or "").strip()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_controls WHERE control_name = ? LIMIT 1",
                (normalized_name,),
            ).fetchone()
        if not row:
            return {
                "controlName": normalized_name,
                "active": False,
                "reason": None,
                "updatedBy": None,
                "updatedAt": None,
                "metadata": {},
            }
        control = self._row_to_dict(row)
        return {
            "controlName": control.get("control_name"),
            "active": bool(control.get("active")),
            "reason": control.get("reason"),
            "updatedBy": control.get("updated_by"),
            "updatedAt": control.get("updated_at"),
            "metadata": control.get("metadata") or {},
        }

    def upsert_source_account(self, payload: Dict[str, Any]) -> int:
        source_type = str(payload.get("sourceType") or payload.get("source_type") or "unknown").strip()
        source_identifier = str(payload.get("sourceIdentifier") or payload.get("source_identifier") or "").strip()
        if not source_identifier:
            raise ValueError("sourceIdentifier is required for a source account")
        label = str(payload.get("label") or os.path.basename(source_identifier) or source_identifier).strip()
        status = str(payload.get("status") or "active").strip() or "active"
        now = self._now()
        last_seen_at = self._date_text(payload.get("lastSeenAt") or payload.get("last_seen_at"))
        last_scan_at = self._date_text(payload.get("lastScanAt") or payload.get("last_scan_at"))
        documents_seen = self._int(payload.get("documentsSeen") or payload.get("documents_seen"), 0)
        documents_imported = self._int(payload.get("documentsImported") or payload.get("documents_imported"), 0)
        duplicates_detected = self._int(payload.get("duplicatesDetected") or payload.get("duplicates_detected"), 0)

        with self._connection() as connection:
            existing = connection.execute(
                """
                SELECT * FROM source_accounts
                WHERE source_type = ? AND source_identifier = ?
                LIMIT 1
                """,
                (source_type, source_identifier),
            ).fetchone()
            if existing:
                source_account_id = int(existing["id"])
                self._update_with_connection(
                    connection,
                    "source_accounts",
                    source_account_id,
                    {
                        "label": label,
                        "status": status,
                        "last_seen_at": last_seen_at or existing["last_seen_at"],
                        "last_scan_at": last_scan_at or existing["last_scan_at"],
                        "documents_seen": int(existing["documents_seen"] or 0) + documents_seen,
                        "documents_imported": int(existing["documents_imported"] or 0) + documents_imported,
                        "duplicates_detected": int(existing["duplicates_detected"] or 0) + duplicates_detected,
                        "metadata_json": self._json(self._redact_sensitive(payload.get("metadata"))),
                        "updated_at": now,
                    },
                )
                return source_account_id
            return self._insert_with_connection(
                connection,
                "source_accounts",
                {
                    "source_type": source_type,
                    "source_identifier": source_identifier,
                    "label": label,
                    "status": status,
                    "last_seen_at": last_seen_at,
                    "last_scan_at": last_scan_at,
                    "documents_seen": documents_seen,
                    "documents_imported": documents_imported,
                    "duplicates_detected": duplicates_detected,
                    "metadata_json": self._json(self._redact_sensitive(payload.get("metadata"))),
                    "created_at": now,
                    "updated_at": now,
                },
            )

    def create_workflow_run(self, payload: Dict[str, Any], preferred_id: Optional[int] = None) -> int:
        now = self._now()
        metadata = payload.get("metadata")
        recovery = metadata.get("recovery") if isinstance(metadata, dict) else {}
        recovery = recovery if isinstance(recovery, dict) else {}
        values = {
            "id": preferred_id,
            "status": payload.get("status", "running"),
            "trigger_source": payload.get("triggerSource") or payload.get("trigger_source") or "manual",
            "recovery_source_workflow_run_id": self._optional_int(
                self._payload_value(
                    payload,
                    "recoverySourceWorkflowRunId",
                    "recovery_source_workflow_run_id",
                )
                or recovery.get("sourceWorkflowRunId")
            ),
            "recovery_root_workflow_run_id": self._optional_int(
                self._payload_value(
                    payload,
                    "recoveryRootWorkflowRunId",
                    "recovery_root_workflow_run_id",
                )
                or recovery.get("rootWorkflowRunId")
            ),
            "documents_imported": self._int(payload.get("documentsImported"), 0),
            "documents_processed": self._int(payload.get("documentsProcessed"), 0),
            "documents_needing_review": self._int(payload.get("documentsNeedingReview"), 0),
            "error_message": self._redact_error_message(payload.get("errorMessage")),
            "metadata_json": self._json(self._redact_sensitive(metadata)),
            "started_at": self._date_text(payload.get("startedAt")) or now,
            "finished_at": self._date_text(payload.get("finishedAt")),
            "created_at": now,
            "updated_at": now,
        }
        return self._insert("workflow_runs", values)

    def update_workflow_run(self, workflow_run_id: int, payload: Dict[str, Any]) -> None:
        fields = {
            "status": payload.get("status"),
            "documents_imported": payload.get("documentsImported"),
            "documents_processed": payload.get("documentsProcessed"),
            "documents_needing_review": payload.get("documentsNeedingReview"),
            "error_message": self._redact_error_message(payload.get("errorMessage")),
            "metadata_json": (
                self._json(self._redact_sensitive(payload.get("metadata")))
                if "metadata" in payload
                else None
            ),
            "started_at": self._date_text(payload.get("startedAt")),
            "finished_at": self._date_text(payload.get("finishedAt")),
            "updated_at": self._now(),
        }
        self._update("workflow_runs", workflow_run_id, fields)

    def get_workflow_run(self, workflow_run_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_runs WHERE id = ? LIMIT 1",
                (workflow_run_id,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_workflow_recovery_child(self, source_workflow_run_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM workflow_runs
                WHERE recovery_source_workflow_run_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(source_workflow_run_id),),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def workflow_run_ids_with_recovery_children(
        self,
        source_workflow_run_ids: Sequence[int],
    ) -> set[int]:
        bounded_ids = []
        seen_ids = set()
        for value in source_workflow_run_ids:
            workflow_run_id = self._optional_int(value)
            if workflow_run_id is None or workflow_run_id in seen_ids:
                continue
            seen_ids.add(workflow_run_id)
            bounded_ids.append(workflow_run_id)
            if len(bounded_ids) >= 500:
                break
        if not bounded_ids:
            return set()
        placeholders = ", ".join("?" for _ in bounded_ids)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT DISTINCT recovery_source_workflow_run_id
                FROM workflow_runs
                WHERE recovery_source_workflow_run_id IN ({placeholders})
                """,
                bounded_ids,
            ).fetchall()
        return {
            int(row["recovery_source_workflow_run_id"])
            for row in rows
            if row["recovery_source_workflow_run_id"] is not None
        }

    def create_workflow_step(self, payload: Dict[str, Any], preferred_id: Optional[int] = None) -> int:
        workflow_run_id = self._optional_int(
            self._payload_value(payload, "workflowRunId", "workflow_run_id")
        )
        if workflow_run_id is None:
            raise ValueError("Workflow run id is required")
        step_key = str(self._payload_value(payload, "stepKey", "step_key") or "").strip()
        if not step_key:
            raise ValueError("Workflow step key is required")
        now = self._now()
        status = str(payload.get("status") or "pending").strip() or "pending"
        started_at = self._date_text(self._payload_value(payload, "startedAt", "started_at"))
        if status == "running" and not started_at:
            started_at = now
        error_message = self._payload_value(payload, "errorMessage", "error_message")
        values = {
            "id": preferred_id,
            "workflow_run_id": workflow_run_id,
            "step_key": step_key[:200],
            "stage": self._payload_value(payload, "stage"),
            "status": status[:80],
            "attempt": max(1, self._int(payload.get("attempt"), 1)),
            "step_order": max(
                0,
                self._int(self._payload_value(payload, "stepOrder", "step_order"), 0),
            ),
            "started_at": started_at,
            "finished_at": self._date_text(
                self._payload_value(payload, "finishedAt", "finished_at")
            ),
            "duration_ms": self._nonnegative_optional_int(
                self._payload_value(payload, "durationMs", "duration_ms")
            ),
            "error_message": self._redact_error_message(error_message),
            "metadata_json": self._json(self._redact_sensitive(payload.get("metadata"))),
            "created_at": now,
            "updated_at": now,
        }
        return self._insert("workflow_steps", values)

    def update_workflow_step(self, workflow_step_id: int, payload: Dict[str, Any]) -> None:
        error_message = self._payload_value(payload, "errorMessage", "error_message")
        fields = {
            "stage": payload.get("stage"),
            "status": payload.get("status"),
            "started_at": self._date_text(
                self._payload_value(payload, "startedAt", "started_at")
            ),
            "finished_at": self._date_text(
                self._payload_value(payload, "finishedAt", "finished_at")
            ),
            "duration_ms": self._nonnegative_optional_int(
                self._payload_value(payload, "durationMs", "duration_ms")
            ),
            "error_message": self._redact_error_message(error_message),
            "metadata_json": (
                self._json(self._redact_sensitive(payload.get("metadata")))
                if "metadata" in payload
                else None
            ),
            "updated_at": self._now(),
        }
        self._update("workflow_steps", workflow_step_id, fields)

    def get_workflow_step(self, workflow_step_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_steps WHERE id = ? LIMIT 1",
                (workflow_step_id,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_workflow_run_with_steps(self, workflow_run_id: int) -> Optional[Dict[str, Any]]:
        workflow_run = self.get_workflow_run(workflow_run_id)
        if workflow_run is None:
            return None
        workflow_run["steps"] = self.list_workflow_steps(
            workflow_run_id=workflow_run_id,
            limit=500,
        )
        workflow_run["step_count"] = len(workflow_run["steps"])
        return workflow_run

    def register_document(self, payload: Dict[str, Any], preferred_id: Optional[int] = None) -> int:
        now = self._now()
        values = {
            "id": preferred_id,
            "source_account_id": payload.get("sourceAccountId") or payload.get("source_account_id"),
            "source": payload.get("source", "unknown"),
            "source_document_id": payload.get("sourceDocumentId"),
            "original_filename": payload.get("originalFilename", "unknown"),
            "mime_type": payload.get("mimeType"),
            "storage_path": payload.get("storagePath"),
            "document_type": payload.get("documentType", "unknown"),
            "processing_status": payload.get("processingStatus", "imported"),
            "duplicate_fingerprint": payload.get("duplicateFingerprint"),
            "content_sha256": payload.get("contentSha256"),
            "duplicate_of_document_id": payload.get("duplicateOfDocumentId"),
            "vendor_name": payload.get("vendorName"),
            "category": payload.get("category"),
            "transaction_date": payload.get("transactionDate"),
            "total_amount": self._float(payload.get("totalAmount")),
            "vat_amount": self._float(payload.get("vatAmount")),
            "confidence_score": self._float(payload.get("confidenceScore")),
            "reconciliation_status": payload.get("reconciliationStatus", "not_started"),
            "ocr_text": payload.get("ocrText"),
            "extracted_data_json": self._json(self._redact_sensitive(payload.get("extractedData"))),
            "metadata_json": self._json(self._redact_sensitive(payload.get("metadata"))),
            "created_at": now,
            "updated_at": now,
        }
        with self._connection() as connection:
            existing_id = self._existing_document_id(connection, values["source"], values["source_document_id"])
            if existing_id is not None:
                update_values = dict(values)
                update_values.pop("id", None)
                update_values.pop("created_at", None)
                self._update_with_connection(connection, "bookkeeping_documents", existing_id, update_values)
                return existing_id
            return self._insert_with_connection(connection, "bookkeeping_documents", values)

    def update_document(self, document_id: int, payload: Dict[str, Any]) -> None:
        fields = {
            "source_account_id": payload.get("sourceAccountId") or payload.get("source_account_id"),
            "source": payload.get("source"),
            "source_document_id": payload.get("sourceDocumentId"),
            "original_filename": payload.get("originalFilename"),
            "mime_type": payload.get("mimeType"),
            "storage_path": payload.get("storagePath"),
            "document_type": payload.get("documentType"),
            "processing_status": payload.get("processingStatus"),
            "duplicate_fingerprint": payload.get("duplicateFingerprint"),
            "content_sha256": payload.get("contentSha256"),
            "duplicate_of_document_id": payload.get("duplicateOfDocumentId"),
            "vendor_name": payload.get("vendorName"),
            "category": payload.get("category"),
            "transaction_date": payload.get("transactionDate"),
            "total_amount": self._float(payload.get("totalAmount")),
            "vat_amount": self._float(payload.get("vatAmount")),
            "confidence_score": self._float(payload.get("confidenceScore")),
            "reconciliation_status": payload.get("reconciliationStatus"),
            "ocr_text": payload.get("ocrText"),
            "extracted_data_json": self._json(self._redact_sensitive(payload.get("extractedData"))),
            "metadata_json": self._json(self._redact_sensitive(payload.get("metadata"))),
            "updated_at": self._now(),
        }
        self._update("bookkeeping_documents", document_id, fields)

    def clear_document_financial_fields(self, document_id: int, field_names: Any) -> None:
        """Explicitly clear unsupported extracted values without broad null updates."""
        allowed = {"total_amount", "vat_amount"}
        selected = sorted({str(field) for field in (field_names or [])} & allowed)
        if not selected:
            return
        assignments = ", ".join(f"{field} = NULL" for field in selected)
        with self._connection() as connection:
            connection.execute(
                f"UPDATE bookkeeping_documents SET {assignments}, updated_at = ? WHERE id = ?",
                (self._now(), int(document_id)),
            )

    def clear_document_duplicate(self, document_id: int) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE bookkeeping_documents
                SET duplicate_of_document_id = NULL, updated_at = ?
                WHERE id = ?
                """,
                (self._now(), document_id),
            )

    def create_review_item(self, payload: Dict[str, Any], preferred_id: Optional[int] = None) -> int:
        now = self._now()
        document_id = self._optional_int(payload.get("documentId"))
        reason = str(payload.get("reason") or "manual_review").strip() or "manual_review"
        status = str(payload.get("status") or "pending").strip() or "pending"
        with self._connection() as connection:
            def refresh_existing(row: sqlite3.Row) -> int:
                corrected_data = self._json_load(row["corrected_data_json"]) or {}
                if not isinstance(corrected_data, dict):
                    corrected_data = {}
                incoming = payload.get("correctedData")
                if isinstance(incoming, dict):
                    corrected_data.update(incoming)
                existing_id = int(row["id"])
                self._update_with_connection(
                    connection,
                    "review_items",
                    existing_id,
                    {
                        "details": payload.get("details"),
                        "corrected_data_json": self._json(corrected_data) if corrected_data else None,
                        "updated_at": now,
                    },
                )
                return existing_id

            if document_id is not None and status in {"pending", "in_review"}:
                existing = connection.execute(
                    """
                    SELECT id, corrected_data_json FROM review_items
                    WHERE document_id = ? AND reason = ?
                      AND status IN ('pending', 'in_review')
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (document_id, reason),
                ).fetchone()
                if existing:
                    return refresh_existing(existing)
            try:
                return self._insert_with_connection(
                    connection,
                    "review_items",
                    {
                        "id": preferred_id,
                        "document_id": document_id,
                        "reason": reason,
                        "details": payload.get("details"),
                        "status": status,
                        "corrected_data_json": self._json(payload.get("correctedData")),
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            except sqlite3.IntegrityError:
                if document_id is None or status not in {"pending", "in_review"}:
                    raise
                existing = connection.execute(
                    """
                    SELECT id, corrected_data_json FROM review_items
                    WHERE document_id = ? AND reason = ?
                      AND status IN ('pending', 'in_review')
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (document_id, reason),
                ).fetchone()
                if not existing:
                    raise
                return refresh_existing(existing)

    def record_duplicate_candidate(self, payload: Dict[str, Any], preferred_id: Optional[int] = None) -> int:
        document_id = self._optional_int(payload.get("documentId") or payload.get("document_id"))
        candidate_document_id = self._optional_int(
            payload.get("candidateDocumentId") or payload.get("candidate_document_id")
        )
        if document_id is None or candidate_document_id is None:
            raise ValueError("documentId and candidateDocumentId are required for a duplicate candidate")
        if document_id == candidate_document_id:
            raise ValueError("duplicate candidate cannot reference the same document twice")
        match_type = str(payload.get("matchType") or payload.get("match_type") or "unknown").strip() or "unknown"
        now = self._now()
        values = {
            "id": preferred_id,
            "document_id": document_id,
            "candidate_document_id": candidate_document_id,
            "match_type": match_type,
            "confidence_score": self._float(self._first_present(payload.get("confidenceScore"), payload.get("confidence_score"))),
            "status": payload.get("status") or "pending",
            "reason": payload.get("reason"),
            "evidence_json": self._json(self._redact_sensitive(payload.get("evidence"))),
            "created_at": now,
            "updated_at": now,
        }
        with self._connection() as connection:
            existing = connection.execute(
                """
                SELECT id FROM duplicate_candidates
                WHERE document_id = ? AND candidate_document_id = ? AND match_type = ?
                LIMIT 1
                """,
                (document_id, candidate_document_id, match_type),
            ).fetchone()
            if existing:
                candidate_id = int(existing["id"])
                update_values = dict(values)
                update_values.pop("id", None)
                update_values.pop("created_at", None)
                self._update_with_connection(connection, "duplicate_candidates", candidate_id, update_values)
                return candidate_id
            return self._insert_with_connection(connection, "duplicate_candidates", values)

    def list_duplicate_candidates(
        self,
        status: Optional[Any] = None,
        document_id: Optional[int] = None,
        candidate_document_id: Optional[int] = None,
        limit: int = 100,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT * FROM duplicate_candidates"
        params = []
        where = []
        self._append_status_filter(where, params, "status", status)
        if document_id is not None:
            where.append("document_id = ?")
            params.append(document_id)
        if candidate_document_id is not None:
            where.append("candidate_document_id = ?")
            params.append(candidate_document_id)
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        query = f"{query} ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_duplicate_candidate(self, candidate_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM duplicate_candidates WHERE id = ? LIMIT 1",
                (int(candidate_id),),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def resolve_duplicate_candidates_for_document(
        self,
        document_id: int,
        status: str,
        resolution: Optional[str] = None,
    ) -> int:
        metadata = {"resolution": resolution} if resolution else None
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM duplicate_candidates
                WHERE document_id = ? AND status IN ('pending', 'in_review')
                """,
                (document_id,),
            ).fetchall()
            updated = 0
            for row in rows:
                evidence = self._row_to_dict(row).get("evidence") or {}
                if metadata:
                    evidence = {**evidence, **metadata}
                self._update_with_connection(
                    connection,
                    "duplicate_candidates",
                    int(row["id"]),
                    {
                        "status": status,
                        "evidence_json": self._json(evidence),
                        "updated_at": self._now(),
                    },
                )
                updated += 1
            return updated

    def resolve_duplicate_candidate(
        self,
        candidate_id: int,
        status: str,
        resolution: Optional[str] = None,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM duplicate_candidates WHERE id = ? LIMIT 1",
                (int(candidate_id),),
            ).fetchone()
            if not row:
                return False
            current_evidence = self._row_to_dict(row).get("evidence") or {}
            updated_evidence = {
                **current_evidence,
                **(evidence or {}),
            }
            if resolution:
                updated_evidence["resolution"] = resolution
            self._update_with_connection(
                connection,
                "duplicate_candidates",
                int(candidate_id),
                {
                    "status": status,
                    "evidence_json": self._json(updated_evidence),
                    "updated_at": self._now(),
                },
            )
        return True

    def upsert_document_group(self, payload: Dict[str, Any], preferred_id: Optional[int] = None) -> int:
        group_key = str(payload.get("groupKey") or payload.get("group_key") or "").strip()
        if not group_key:
            raise ValueError("groupKey is required for a document group")
        now = self._now()
        values = {
            "id": preferred_id,
            "group_key": group_key,
            "group_type": payload.get("groupType") or payload.get("group_type") or "scanner_batch",
            "title": payload.get("title"),
            "status": payload.get("status") or "candidate",
            "primary_document_id": self._optional_int(
                payload.get("primaryDocumentId") or payload.get("primary_document_id")
            ),
            "confidence_score": self._float(self._first_present(payload.get("confidenceScore"), payload.get("confidence_score"))),
            "reason": payload.get("reason"),
            "metadata_json": self._json(self._redact_sensitive(payload.get("metadata"))),
            "created_at": now,
            "updated_at": now,
        }
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT id FROM document_groups WHERE group_key = ? LIMIT 1",
                (group_key,),
            ).fetchone()
            if existing:
                group_id = int(existing["id"])
                update_values = dict(values)
                update_values.pop("id", None)
                update_values.pop("created_at", None)
                self._update_with_connection(connection, "document_groups", group_id, update_values)
                return group_id
            return self._insert_with_connection(connection, "document_groups", values)

    def add_document_to_group(self, group_id: int, document_id: int, payload: Optional[Dict[str, Any]] = None) -> int:
        payload = payload or {}
        now = self._now()
        values = {
            "group_id": group_id,
            "document_id": document_id,
            "role": payload.get("role") or "page",
            "sort_order": self._int(payload.get("sortOrder") or payload.get("sort_order"), 0),
            "status": payload.get("status") or "active",
            "metadata_json": self._json(self._redact_sensitive(payload.get("metadata"))),
            "created_at": now,
            "updated_at": now,
        }
        with self._connection() as connection:
            existing = connection.execute(
                """
                SELECT id FROM document_group_members
                WHERE group_id = ? AND document_id = ?
                LIMIT 1
                """,
                (group_id, document_id),
            ).fetchone()
            if existing:
                member_id = int(existing["id"])
                update_values = dict(values)
                update_values.pop("group_id", None)
                update_values.pop("document_id", None)
                update_values.pop("created_at", None)
                self._update_with_connection(connection, "document_group_members", member_id, update_values)
                return member_id
            return self._insert_with_connection(connection, "document_group_members", values)

    def remove_document_from_group(self, group_id: int, document_id: int, reason: Optional[str] = None) -> int:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM document_group_members
                WHERE group_id = ? AND document_id = ?
                LIMIT 1
                """,
                (group_id, document_id),
            ).fetchone()
            if not row:
                return 0
            member = self._row_to_dict(row)
            metadata = member.get("metadata") or {}
            if reason:
                metadata["removedReason"] = reason
            self._update_with_connection(
                connection,
                "document_group_members",
                int(row["id"]),
                {
                    "status": "removed",
                    "metadata_json": self._json(metadata),
                    "updated_at": self._now(),
                },
            )
            return 1

    def update_document_group_status(self, group_id: int, status: str, resolution: Optional[str] = None) -> None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM document_groups WHERE id = ? LIMIT 1",
                (group_id,),
            ).fetchone()
            if not row:
                return
            group = self._row_to_dict(row)
            metadata = group.get("metadata") or {}
            if resolution:
                metadata["resolution"] = resolution
            self._update_with_connection(
                connection,
                "document_groups",
                group_id,
                {
                    "status": status,
                    "metadata_json": self._json(metadata),
                    "updated_at": self._now(),
                },
            )

    def get_document_group(self, group_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM document_groups WHERE id = ? LIMIT 1",
                (group_id,),
            ).fetchone()
            return self._document_group_with_members(connection, row) if row else None

    def list_document_groups(
        self,
        status: Optional[Any] = None,
        group_type: Optional[str] = None,
        document_id: Optional[int] = None,
        limit: int = 100,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT DISTINCT g.* FROM document_groups g"
        params = []
        where = []
        if document_id is not None:
            query = f"{query} JOIN document_group_members m ON m.group_id = g.id"
            where.append("m.document_id = ?")
            params.append(document_id)
        self._append_status_filter(where, params, "g.status", status)
        if group_type:
            where.append("g.group_type = ?")
            params.append(group_type)
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        query = f"{query} ORDER BY g.updated_at DESC, g.id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
            return [self._document_group_with_members(connection, row) for row in rows]

    def replace_extracted_fields(
        self,
        document_id: int,
        fields: Sequence[Dict[str, Any]],
        source: str = "local_processing",
    ) -> None:
        now = self._now()
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM extracted_fields WHERE document_id = ? AND source = ?",
                (document_id, source),
            )
            for field in fields:
                field_name = str(field.get("fieldName") or field.get("field_name") or "").strip()
                if not field_name:
                    continue
                field_source = str(field.get("source") or source)
                connection.execute(
                    """
                    INSERT INTO extracted_fields (
                        document_id,
                        field_name,
                        field_value_json,
                        normalized_value,
                        confidence_score,
                        source,
                        provenance_json,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        field_name,
                        self._json(field.get("value")),
                        self._normalized_field_value(field.get("value")),
                        self._float(field.get("confidenceScore") or field.get("confidence_score")),
                        field_source,
                        self._json(field.get("provenance")),
                        now,
                        now,
                    ),
                )

    def list_extracted_fields(
        self,
        document_id: Optional[int] = None,
        field_name: Optional[str] = None,
        limit: int = 100,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT * FROM extracted_fields"
        params = []
        where = []
        if document_id is not None:
            where.append("document_id = ?")
            params.append(document_id)
        if field_name:
            where.append("field_name = ?")
            params.append(field_name)
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        query = f"{query} ORDER BY id ASC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def create_routing_attempt(self, payload: Dict[str, Any], preferred_id: Optional[int] = None) -> int:
        return self._insert(
            "routing_attempts",
            {
                "id": preferred_id,
                "document_id": payload.get("documentId"),
                "bookkeeping_record_id": self._optional_int(self._payload_value(
                    payload,
                    "bookkeepingRecordId",
                    "bookkeeping_record_id",
                )),
                "workflow_run_id": payload.get("workflowRunId"),
                "target": payload.get("target", "none"),
                "status": payload.get("status", "pending"),
                "external_id": payload.get("externalId"),
                "message": payload.get("message"),
                "metadata_json": self._json(payload.get("metadata")),
                "created_at": self._now(),
            },
        )

    def get_routing_attempt(self, routing_attempt_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM routing_attempts WHERE id = ? LIMIT 1",
                (routing_attempt_id,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def upsert_export_attempt(self, payload: Dict[str, Any], preferred_id: Optional[int] = None) -> int:
        now = self._now()
        values = self._export_attempt_values(payload, now=now, include_defaults=True)
        values["id"] = preferred_id
        values["created_at"] = now
        values["updated_at"] = now
        with self._connection() as connection:
            existing_id = self._existing_export_attempt_id(
                connection,
                routing_attempt_id=values.get("routing_attempt_id"),
                operation_id=values.get("operation_id"),
            )
            if existing_id is not None:
                update_values = self._export_attempt_values(payload, now=now, include_defaults=False)
                update_values["updated_at"] = now
                self._update_with_connection(connection, "export_attempts", existing_id, update_values)
                return existing_id
            return self._insert_with_connection(connection, "export_attempts", values)

    def update_export_attempt(self, export_attempt_id: int, payload: Dict[str, Any]) -> None:
        values = self._export_attempt_values(payload, now=self._now(), include_defaults=False)
        values["updated_at"] = self._now()
        self._update("export_attempts", export_attempt_id, values)

    def claim_export_attempt(
        self,
        export_attempt_id: int,
        allowed_statuses: Optional[Any] = None,
    ) -> Dict[str, Any]:
        allowed = {str(status) for status in (allowed_statuses or ("approved",))}
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM export_attempts WHERE id = ? LIMIT 1",
                (export_attempt_id,),
            ).fetchone()
            if not row:
                return {"status": "not_found", "exportAttemptId": export_attempt_id}
            current_status = str(row["status"] or "")
            if current_status not in allowed:
                return {
                    "status": "not_claimable",
                    "exportAttemptId": export_attempt_id,
                    "currentStatus": current_status,
                }
            now = self._now()
            cursor = connection.execute(
                """
                UPDATE export_attempts
                SET status = 'execution_in_progress', updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (now, export_attempt_id, current_status),
            )
            if cursor.rowcount != 1:
                return {"status": "already_claimed", "exportAttemptId": export_attempt_id}
            return {
                "status": "claimed",
                "exportAttemptId": export_attempt_id,
                "attempt": self._row_to_dict(row),
            }

    def get_export_attempt(self, export_attempt_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM export_attempts WHERE id = ? LIMIT 1",
                (export_attempt_id,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def find_export_attempt(
        self,
        *,
        routing_attempt_id: Optional[int] = None,
        operation_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Find an idempotent export attempt through its indexed identities."""
        with self._connection() as connection:
            if routing_attempt_id is not None:
                row = connection.execute(
                    "SELECT * FROM export_attempts WHERE routing_attempt_id = ? LIMIT 1",
                    (int(routing_attempt_id),),
                ).fetchone()
                if row:
                    return self._row_to_dict(row)
            if operation_id:
                row = connection.execute(
                    "SELECT * FROM export_attempts WHERE operation_id = ? LIMIT 1",
                    (str(operation_id),),
                ).fetchone()
                if row:
                    return self._row_to_dict(row)
        return None

    def list_export_attempts(
        self,
        status: Optional[Any] = None,
        external_submission: Optional[Any] = None,
        target_system: Optional[str] = None,
        document_id: Optional[int] = None,
        bookkeeping_record_id: Optional[int] = None,
        limit: int = 100,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT * FROM export_attempts"
        params = []
        where = []
        self._append_status_filter(where, params, "status", status)
        self._append_status_filter(where, params, "external_submission", external_submission)
        if target_system:
            where.append("target_system = ?")
            params.append(target_system)
        if document_id is not None:
            where.append("document_id = ?")
            params.append(document_id)
        if bookkeeping_record_id is not None:
            where.append("bookkeeping_record_id = ?")
            params.append(bookkeeping_record_id)
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        query = f"{query} ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def create_reconciliation_match(self, payload: Dict[str, Any], preferred_id: Optional[int] = None) -> int:
        return self._insert(
            "reconciliation_matches",
            {
                "id": preferred_id,
                "document_id": payload.get("documentId"),
                "bank_transaction_id": payload.get("bankTransactionId", "unknown"),
                "status": payload.get("status", "review"),
                "confidence_score": self._float(payload.get("confidenceScore")),
                "amount_difference": self._float(payload.get("amountDifference")),
                "matched_at": self._date_text(payload.get("matchedAt")),
                "metadata_json": self._json(payload.get("metadata")),
                "created_at": self._now(),
            },
        )

    def update_reconciliation_match(self, reconciliation_match_id: int, payload: Dict[str, Any]) -> None:
        self._update(
            "reconciliation_matches",
            reconciliation_match_id,
            {
                "document_id": payload.get("documentId"),
                "bank_transaction_id": payload.get("bankTransactionId"),
                "status": payload.get("status"),
                "confidence_score": self._float(payload.get("confidenceScore")),
                "amount_difference": self._float(payload.get("amountDifference")),
                "matched_at": self._date_text(payload.get("matchedAt")),
                "metadata_json": self._json(payload.get("metadata")),
            },
        )

    def upsert_bookkeeping_record(self, payload: Dict[str, Any], preferred_id: Optional[int] = None) -> int:
        now = self._now()
        document_id = self._optional_int(self._payload_value(payload, "documentId", "document_id"))
        bank_transaction_id = self._optional_int(self._payload_value(
            payload,
            "bankTransactionId",
            "bank_transaction_id",
        ))
        insert_values = self._bookkeeping_record_values(payload, now=now, include_defaults=True)
        insert_values["id"] = preferred_id
        insert_values["created_at"] = now
        insert_values["updated_at"] = now

        with self._connection() as connection:
            existing_id = self._existing_bookkeeping_record_id(
                connection,
                document_id=document_id,
                bank_transaction_id=bank_transaction_id,
            )
            if existing_id is not None:
                update_values = self._bookkeeping_record_values(payload, now=now, include_defaults=False)
                update_values["updated_at"] = now
                self._update_with_connection(connection, "bookkeeping_records", existing_id, update_values)
                return existing_id
            return self._insert_with_connection(connection, "bookkeeping_records", insert_values)

    def update_bookkeeping_record(self, record_id: int, payload: Dict[str, Any]) -> None:
        values = self._bookkeeping_record_values(payload, now=self._now(), include_defaults=False)
        values["updated_at"] = self._now()
        self._update("bookkeeping_records", record_id, values)

    def clear_bookkeeping_record_financial_values(self, record_id: int) -> None:
        """Remove transaction-only values when a record becomes supporting evidence."""
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE bookkeeping_records
                SET amount = NULL, vat_amount = NULL, updated_at = ?
                WHERE id = ?
                """,
                (self._now(), record_id),
            )

    def clear_bookkeeping_record_vat_amount(self, record_id: int) -> None:
        """Remove invalid normalized VAT while retaining the transaction amount."""
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE bookkeeping_records
                SET vat_amount = NULL, updated_at = ?
                WHERE id = ?
                """,
                (self._now(), record_id),
            )

    def clear_bookkeeping_record_date(self, record_id: int) -> None:
        """Remove an invalid normalized date while retaining source evidence."""
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE bookkeeping_records
                SET record_date = NULL, updated_at = ?
                WHERE id = ?
                """,
                (self._now(), record_id),
            )

    def get_bookkeeping_record(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM bookkeeping_records WHERE id = ? LIMIT 1",
                (record_id,),
            ).fetchone()
            return self._bookkeeping_record_with_line_items(connection, row) if row else None

    def get_bookkeeping_record_by_document(self, document_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM bookkeeping_records WHERE document_id = ? LIMIT 1",
                (document_id,),
            ).fetchone()
            return self._bookkeeping_record_with_line_items(connection, row) if row else None

    def get_bookkeeping_record_by_bank_transaction(self, bank_transaction_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM bookkeeping_records WHERE bank_transaction_id = ? LIMIT 1",
                (bank_transaction_id,),
            ).fetchone()
            return self._bookkeeping_record_with_line_items(connection, row) if row else None

    def list_bookkeeping_records(
        self,
        status: Optional[Any] = None,
        export_status: Optional[Any] = None,
        reconciliation_status: Optional[Any] = None,
        target_system: Optional[str] = None,
        source_type: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        limit: int = 100,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT * FROM bookkeeping_records"
        params = []
        where = []
        self._append_status_filter(where, params, "status", status)
        self._append_status_filter(where, params, "export_status", export_status)
        self._append_status_filter(where, params, "reconciliation_status", reconciliation_status)
        if target_system:
            where.append("target_system = ?")
            params.append(target_system)
        if source_type:
            where.append("source_type = ?")
            params.append(source_type)
        if from_date:
            where.append("record_date >= ?")
            params.append(from_date)
        if to_date:
            where.append("record_date <= ?")
            params.append(to_date)
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        query = f"{query} ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
            return self._bookkeeping_records_with_line_items(connection, rows)

    def count_bookkeeping_records(
        self,
        target_system: Optional[str] = None,
        source_type: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> int:
        query = "SELECT COUNT(*) FROM bookkeeping_records"
        params = []
        where = []
        if target_system:
            where.append("target_system = ?")
            params.append(target_system)
        if source_type:
            where.append("source_type = ?")
            params.append(source_type)
        if from_date:
            where.append("record_date >= ?")
            params.append(from_date)
        if to_date:
            where.append("record_date <= ?")
            params.append(to_date)
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        with self._connection() as connection:
            return int(connection.execute(query, params).fetchone()[0])

    def count_undated_bookkeeping_records(
        self,
        target_system: Optional[str] = None,
        source_type: Optional[str] = None,
        excluded_statuses: Optional[Sequence[str]] = None,
    ) -> int:
        query = "SELECT COUNT(*) FROM bookkeeping_records"
        params = []
        where = ["(record_date IS NULL OR TRIM(record_date) = '')"]
        if target_system:
            where.append("target_system = ?")
            params.append(target_system)
        if source_type:
            where.append("source_type = ?")
            params.append(source_type)
        statuses = [str(item) for item in excluded_statuses or [] if item]
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            where.append(f"status NOT IN ({placeholders})")
            params.extend(statuses)
        query = f"{query} WHERE {' AND '.join(where)}"
        with self._connection() as connection:
            return int(connection.execute(query, params).fetchone()[0])

    def replace_bookkeeping_record_line_items(
        self,
        bookkeeping_record_id: int,
        line_items: Sequence[Dict[str, Any]],
    ) -> int:
        now = self._now()
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM bookkeeping_record_line_items WHERE bookkeeping_record_id = ?",
                (bookkeeping_record_id,),
            )
            inserted = 0
            for index, item in enumerate(line_items or []):
                values = self._bookkeeping_line_item_values(
                    item,
                    bookkeeping_record_id=bookkeeping_record_id,
                    line_index=index,
                    now=now,
                )
                self._insert_with_connection(connection, "bookkeeping_record_line_items", values)
                inserted += 1
            return inserted

    def list_bookkeeping_record_line_items(
        self,
        bookkeeping_record_id: Optional[int] = None,
        limit: int = 100,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT * FROM bookkeeping_record_line_items"
        params = []
        if bookkeeping_record_id is not None:
            query = f"{query} WHERE bookkeeping_record_id = ?"
            params.append(bookkeeping_record_id)
        query = f"{query} ORDER BY bookkeeping_record_id DESC, line_index ASC, id ASC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_vendor_summaries(self, limit: int = 100) -> list:
        limit = self._bounded_limit(limit)
        records = self.list_bookkeeping_records(limit=500)
        documents = self.list_documents(limit=500)
        rules = self.list_vendor_category_rules(limit=500)
        summaries: Dict[str, Dict[str, Any]] = {}

        for record in records:
            vendor_name = _directory_label(record.get("vendor_name"), "Unknown vendor")
            summary = _vendor_summary(summaries, vendor_name)
            summary["recordCount"] += 1
            if record.get("document_id"):
                summary["documentIds"].add(int(record["document_id"]))
            if record.get("bank_transaction_id"):
                summary["bankTransactionCount"] += 1
            _increment(summary["categories"], _directory_label(record.get("category"), "Unassigned"))
            _increment(summary["targetSystems"], _directory_label(record.get("target_system"), "unknown"))
            _increment(summary["recordStatuses"], _directory_label(record.get("status"), "unknown"))
            _increment(summary["exportStatuses"], _directory_label(record.get("export_status"), "unknown"))
            _increment(summary["reconciliationStatuses"], _directory_label(record.get("reconciliation_status"), "unknown"))
            currency = str(record.get("currency") or "EUR")
            summary["amountByCurrency"][currency] = round(
                summary["amountByCurrency"].get(currency, 0.0) + (self._float(record.get("amount")) or 0.0),
                2,
            )
            if bool(record.get("review_required")) or str(record.get("status") or "") in {"needs_review", "failed", "duplicate"}:
                summary["reviewRequiredCount"] += 1
            if str(record.get("export_status") or "") in {"ready", "draft_prepared", "awaiting_approval"}:
                summary["exportReadyCount"] += 1
            if str(record.get("status") or "") == "failed":
                summary["failedCount"] += 1
            _latest(summary, record.get("updated_at") or record.get("created_at"))

        for document in documents:
            vendor_name = _directory_label(document.get("vendor_name"), "Unknown vendor")
            summary = _vendor_summary(summaries, vendor_name)
            summary["documentIds"].add(int(document["id"]))
            _increment(summary["documentStatuses"], _directory_label(document.get("processing_status"), "unknown"))
            if str(document.get("processing_status") or "") in {"needs_review", "failed", "duplicate"}:
                summary["reviewRequiredCount"] += 1
            if str(document.get("processing_status") or "") == "failed":
                summary["failedCount"] += 1
            _latest(summary, document.get("updated_at") or document.get("imported_at") or document.get("created_at"))

        for rule in rules:
            vendor_name = _directory_label(rule.get("vendor_name"), "Unknown vendor")
            summary = _vendor_summary(summaries, vendor_name)
            summary["ruleCount"] += 1
            if rule.get("status") == "approved":
                summary["approvedRuleCount"] += 1
            if rule.get("status") == "suggested":
                summary["suggestedRuleCount"] += 1
            _increment(summary["ruleStatuses"], _directory_label(rule.get("status"), "unknown"))
            _increment(summary["categories"], _directory_label(rule.get("category"), "Unassigned"))
            if len(summary["rules"]) < 8:
                summary["rules"].append({
                    "id": rule.get("id"),
                    "category": rule.get("category"),
                    "targetSystem": rule.get("target_system"),
                    "status": rule.get("status"),
                    "usageCount": rule.get("usage_count"),
                })
            _latest(summary, rule.get("updated_at") or rule.get("created_at"))

        return _finalize_directory_summaries(summaries.values(), limit=limit)

    def list_category_summaries(self, limit: int = 100) -> list:
        limit = self._bounded_limit(limit)
        records = self.list_bookkeeping_records(limit=500)
        documents = self.list_documents(limit=500)
        rules = self.list_vendor_category_rules(limit=500)
        summaries: Dict[str, Dict[str, Any]] = {}

        for record in records:
            category = _directory_label(record.get("category"), "Unassigned")
            summary = _category_summary(summaries, category)
            summary["recordCount"] += 1
            if record.get("document_id"):
                summary["documentIds"].add(int(record["document_id"]))
            if record.get("bank_transaction_id"):
                summary["bankTransactionCount"] += 1
            _increment(summary["vendors"], _directory_label(record.get("vendor_name"), "Unknown vendor"))
            _increment(summary["targetSystems"], _directory_label(record.get("target_system"), "unknown"))
            _increment(summary["recordStatuses"], _directory_label(record.get("status"), "unknown"))
            _increment(summary["exportStatuses"], _directory_label(record.get("export_status"), "unknown"))
            _increment(summary["reconciliationStatuses"], _directory_label(record.get("reconciliation_status"), "unknown"))
            currency = str(record.get("currency") or "EUR")
            summary["amountByCurrency"][currency] = round(
                summary["amountByCurrency"].get(currency, 0.0) + (self._float(record.get("amount")) or 0.0),
                2,
            )
            if bool(record.get("review_required")) or str(record.get("status") or "") in {"needs_review", "failed", "duplicate"}:
                summary["reviewRequiredCount"] += 1
            if str(record.get("export_status") or "") in {"ready", "draft_prepared", "awaiting_approval"}:
                summary["exportReadyCount"] += 1
            if str(record.get("status") or "") == "failed":
                summary["failedCount"] += 1
            _latest(summary, record.get("updated_at") or record.get("created_at"))

        for document in documents:
            category = _directory_label(document.get("category"), "Unassigned")
            summary = _category_summary(summaries, category)
            summary["documentIds"].add(int(document["id"]))
            _increment(summary["documentStatuses"], _directory_label(document.get("processing_status"), "unknown"))
            _increment(summary["vendors"], _directory_label(document.get("vendor_name"), "Unknown vendor"))
            if str(document.get("processing_status") or "") in {"needs_review", "failed", "duplicate"}:
                summary["reviewRequiredCount"] += 1
            if str(document.get("processing_status") or "") == "failed":
                summary["failedCount"] += 1
            _latest(summary, document.get("updated_at") or document.get("imported_at") or document.get("created_at"))

        for rule in rules:
            category = _directory_label(rule.get("category"), "Unassigned")
            summary = _category_summary(summaries, category)
            summary["ruleCount"] += 1
            if rule.get("status") == "approved":
                summary["approvedRuleCount"] += 1
            if rule.get("status") == "suggested":
                summary["suggestedRuleCount"] += 1
            _increment(summary["ruleStatuses"], _directory_label(rule.get("status"), "unknown"))
            _increment(summary["vendors"], _directory_label(rule.get("vendor_name"), "Unknown vendor"))
            if len(summary["rules"]) < 8:
                summary["rules"].append({
                    "id": rule.get("id"),
                    "vendorName": rule.get("vendor_name"),
                    "targetSystem": rule.get("target_system"),
                    "status": rule.get("status"),
                    "usageCount": rule.get("usage_count"),
                })
            _latest(summary, rule.get("updated_at") or rule.get("created_at"))

        return _finalize_directory_summaries(summaries.values(), limit=limit)

    def record_wave_report_snapshot(self, payload: Dict[str, Any], preferred_id: Optional[int] = None) -> int:
        operation_id = str(payload.get("operationId") or payload.get("operation_id") or "").strip()
        if not operation_id:
            raise ValueError("operationId is required for a Wave report snapshot")
        report_type = str(payload.get("reportType") or payload.get("report_type") or "unknown").strip() or "unknown"
        now = self._now()
        values = {
            "id": preferred_id,
            "workflow_run_id": payload.get("workflowRunId") or payload.get("workflow_run_id"),
            "operation_id": operation_id,
            "workflow_id": payload.get("workflowId") or payload.get("workflow_id"),
            "report_type": report_type,
            "report_section": payload.get("reportSection") or payload.get("report_section"),
            "action_id": payload.get("actionId") or payload.get("action_id") or "report_table_read",
            "status": payload.get("status", "planned"),
            "safety": payload.get("safety", "read_only"),
            "from_date": self._date_text(payload.get("fromDate") or payload.get("from_date")),
            "to_date": self._date_text(payload.get("toDate") or payload.get("to_date")),
            "as_of_date": self._date_text(payload.get("asOfDate") or payload.get("as_of_date")),
            "basis": payload.get("basis"),
            "account_option": self._first_present(payload.get("accountOption"), payload.get("account_option")),
            "account_name": self._first_present(payload.get("accountName"), payload.get("account_name")),
            "contact_option": self._first_present(payload.get("contactOption"), payload.get("contact_option")),
            "contact_name": self._first_present(payload.get("contactName"), payload.get("contact_name")),
            "cash_mode": self._first_present(payload.get("cashMode"), payload.get("cash_mode")),
            "export_format": self._first_present(payload.get("exportFormat"), payload.get("export_format"), payload.get("format")),
            "row_count": self._optional_int(self._first_present(payload.get("rowCount"), payload.get("row_count"))),
            "total_debits": self._float(self._first_present(payload.get("totalDebits"), payload.get("total_debits"))),
            "total_credits": self._float(self._first_present(payload.get("totalCredits"), payload.get("total_credits"))),
            "total_amount": self._float(self._first_present(payload.get("totalAmount"), payload.get("total_amount"))),
            "external_submission": self._first_present(payload.get("externalSubmission"), payload.get("external_submission")) or "not_executed",
            "metadata_json": self._json(self._redact_sensitive(payload.get("metadata"))),
            "created_at": now,
            "updated_at": now,
        }
        with self._connection() as connection:
            existing = connection.execute(
                """
                SELECT id FROM wave_report_snapshots
                WHERE operation_id = ?
                LIMIT 1
                """,
                (operation_id,),
            ).fetchone()
            if existing:
                snapshot_id = int(existing["id"])
                update_values = dict(values)
                update_values.pop("id", None)
                update_values.pop("created_at", None)
                self._update_with_connection(connection, "wave_report_snapshots", snapshot_id, update_values)
                return snapshot_id
            return self._insert_with_connection(connection, "wave_report_snapshots", values)

    def get_wave_report_snapshot(self, snapshot_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM wave_report_snapshots WHERE id = ? LIMIT 1",
                (int(snapshot_id),),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_wave_report_snapshots(
        self,
        report_type: Optional[str] = None,
        workflow_id: Optional[str] = None,
        status: Optional[Any] = None,
        limit: int = 100,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT * FROM wave_report_snapshots"
        params = []
        where = []
        if report_type:
            where.append("report_type = ?")
            params.append(report_type)
        if workflow_id:
            where.append("workflow_id = ?")
            params.append(workflow_id)
        if status:
            if isinstance(status, Sequence) and not isinstance(status, str):
                statuses = [str(item) for item in status if item]
                if statuses:
                    placeholders = ", ".join("?" for _ in statuses)
                    where.append(f"status IN ({placeholders})")
                    params.extend(statuses)
            else:
                where.append("status = ?")
                params.append(status)
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        query = f"{query} ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def record_wave_operation_snapshot(self, payload: Dict[str, Any], preferred_id: Optional[int] = None) -> int:
        operation_id = str(payload.get("operationId") or payload.get("operation_id") or "").strip()
        if not operation_id:
            raise ValueError("operationId is required for a Wave operation snapshot")
        now = self._now()
        plan = payload.get("plan") or {}
        capability_plan = payload.get("capability_plan") or payload.get("capabilityPlan") or {}

        with self._connection() as connection:
            existing_snapshot = connection.execute(
                """
                SELECT * FROM wave_operation_snapshots
                WHERE operation_id = ?
                LIMIT 1
                """,
                (operation_id,),
            ).fetchone()
            if existing_snapshot:
                existing_snapshot = self._row_to_dict(existing_snapshot)

        has_payload_key = lambda key: key in payload
        requires_confirmation = self._payload_value(payload, "requiresConfirmation", "requires_confirmation")
        if requires_confirmation is None:
            requires_confirmation = self._payload_value(plan, "requires_confirmation")
        if (
            requires_confirmation is None
            and existing_snapshot is not None
            and not (has_payload_key("requiresConfirmation") or has_payload_key("requires_confirmation"))
        ):
            requires_confirmation = existing_snapshot.get("requires_confirmation")

        requires_credentials = self._payload_value(payload, "requiresCredentials", "requires_credentials")
        if requires_credentials is None:
            requires_credentials = self._payload_value(plan, "requires_credentials")
        if (
            requires_credentials is None
            and existing_snapshot is not None
            and not (has_payload_key("requiresCredentials") or has_payload_key("requires_credentials"))
        ):
            requires_credentials = existing_snapshot.get("requires_credentials")

        required_fields = self._payload_value(
            payload,
            "requiredFields",
            "required_fields",
            "requiredFieldsList",
            default=self._payload_value(plan, "required_fields"),
        )
        if (
            required_fields is None
            and existing_snapshot is not None
            and not (
                has_payload_key("requiredFields")
                or has_payload_key("required_fields")
                or has_payload_key("requiredFieldsList")
            )
        ):
            required_fields = existing_snapshot.get("required_fields")
        missing_fields = self._payload_value(
            payload,
            "missingFields",
            "missing_fields",
            default=self._payload_value(plan, "missing_fields"),
        )
        if (
            missing_fields is None
            and existing_snapshot is not None
            and not (has_payload_key("missingFields") or has_payload_key("missing_fields"))
        ):
            missing_fields = existing_snapshot.get("missing_fields")

        status = self._payload_value(payload, "status")
        if status is None and not has_payload_key("status") and existing_snapshot is not None:
            status = existing_snapshot.get("status")
        safety = self._payload_value(payload, "safety")
        if safety is None and not has_payload_key("safety") and existing_snapshot is not None:
            safety = existing_snapshot.get("safety")
        surface = self._payload_value(payload, "surface")
        if surface is None and not has_payload_key("surface") and existing_snapshot is not None:
            surface = existing_snapshot.get("surface")
        action_id = self._payload_value(payload, "actionId", "action_id")
        if (
            action_id is None
            and not (has_payload_key("actionId") or has_payload_key("action_id"))
            and existing_snapshot is not None
        ):
            action_id = existing_snapshot.get("action_id")
        mode = self._payload_value(payload, "mode")
        if mode is None and not has_payload_key("mode") and existing_snapshot is not None:
            mode = existing_snapshot.get("mode")
        workflow_run_id = self._payload_value(payload, "workflowRunId", "workflow_run_id")
        if (
            workflow_run_id is None
            and not (has_payload_key("workflowRunId") or has_payload_key("workflow_run_id"))
            and existing_snapshot is not None
        ):
            workflow_run_id = existing_snapshot.get("workflow_run_id")
        workflow_id = self._payload_value(payload, "workflowId", "workflow_id")
        if (
            workflow_id is None
            and not (has_payload_key("workflowId") or has_payload_key("workflow_id"))
            and existing_snapshot is not None
        ):
            workflow_id = existing_snapshot.get("workflow_id")
        plan_status = self._payload_value(payload, "planStatus", "plan_status")
        if plan_status is None and not (
            has_payload_key("planStatus") or has_payload_key("plan_status") or has_payload_key("plan")
        ):
            plan_status = existing_snapshot.get("plan_status") if existing_snapshot else None
        if plan_status is None:
            plan_status = self._payload_value(plan, "status")

        external_submission = self._payload_value(payload, "externalSubmission", "external_submission")
        if (
            external_submission is None
            and not (has_payload_key("externalSubmission") or has_payload_key("external_submission"))
            and existing_snapshot is not None
        ):
            external_submission = existing_snapshot.get("external_submission")

        if status is None:
            status = "planned"
        if safety is None:
            safety = "unsupported"
        if external_submission is None:
            external_submission = "not_executed"

        operation_payload = self._payload_value(payload, "payload")
        if (
            operation_payload is None
            and existing_snapshot is not None
            and not (has_payload_key("payload") or has_payload_key("payload_json"))
        ):
            operation_payload = existing_snapshot.get("payload")

        metadata = payload.get("metadata")
        if metadata is None and existing_snapshot is not None and not has_payload_key("metadata"):
            metadata = existing_snapshot.get("metadata")

        values = {
            "id": preferred_id,
            "workflow_run_id": workflow_run_id,
            "operation_id": operation_id,
            "workflow_id": workflow_id,
            "surface": surface,
            "action_id": action_id,
            "mode": mode,
            "safety": safety,
            "status": status,
            "plan_status": plan_status,
            "plan_json": self._json(self._redact_sensitive(plan)),
            "capability_plan_json": self._json(self._redact_sensitive(capability_plan)),
            "requires_confirmation": self._bool_int(requires_confirmation) if requires_confirmation is not None else None,
            "requires_credentials": self._bool_int(requires_credentials) if requires_credentials is not None else None,
            "required_fields_json": self._json(required_fields),
            "missing_fields_json": self._json(missing_fields),
            "external_submission": external_submission,
            "payload_json": self._json(self._redact_sensitive(operation_payload)),
            "metadata_json": self._json(self._redact_sensitive(metadata)),
            "created_at": now,
            "updated_at": now,
        }

        with self._connection() as connection:
            existing = connection.execute(
                """
                SELECT id FROM wave_operation_snapshots
                WHERE operation_id = ?
                LIMIT 1
                """,
                (operation_id,),
            ).fetchone()
            if existing:
                snapshot_id = int(existing["id"])
                update_values = dict(values)
                update_values.pop("id", None)
                update_values.pop("created_at", None)
                self._update_with_connection(connection, "wave_operation_snapshots", snapshot_id, update_values)
                return snapshot_id
            return self._insert_with_connection(connection, "wave_operation_snapshots", values)

    def _ensure_wave_operation_snapshot_schema(self, connection: sqlite3.Connection) -> None:
        index_names = {
            row["name"]: row["unique"]
            for row in connection.execute("PRAGMA index_list(wave_operation_snapshots)").fetchall()
        }
        unique_indexes_to_replace = {
            "idx_local_wave_ops_status",
            "idx_local_wave_ops_surface",
            "idx_local_wave_ops_safety",
            "idx_local_wave_ops_workflow",
        }
        for index_name in unique_indexes_to_replace:
            if index_names.get(index_name) == 1:
                connection.execute(f'DROP INDEX "{index_name}"')
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS wave_operation_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_run_id INTEGER,
                workflow_id TEXT,
                operation_id TEXT NOT NULL,
                surface TEXT,
                action_id TEXT,
                mode TEXT,
                safety TEXT NOT NULL DEFAULT 'unsupported',
                status TEXT NOT NULL DEFAULT 'planned',
                plan_status TEXT,
                plan_json TEXT,
                capability_plan_json TEXT,
                requires_confirmation INTEGER,
                requires_credentials INTEGER,
                required_fields_json TEXT,
                missing_fields_json TEXT,
                external_submission TEXT NOT NULL DEFAULT 'not_executed',
                payload_json TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(operation_id)
            )
            """
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(wave_operation_snapshots)").fetchall()
        }
        required_columns = {
            "workflow_run_id": "INTEGER",
            "workflow_id": "TEXT",
            "operation_id": "TEXT NOT NULL",
            "surface": "TEXT",
            "action_id": "TEXT",
            "mode": "TEXT",
            "safety": "TEXT NOT NULL DEFAULT 'unsupported'",
            "status": "TEXT NOT NULL DEFAULT 'planned'",
            "plan_status": "TEXT",
            "plan_json": "TEXT",
            "capability_plan_json": "TEXT",
            "requires_confirmation": "INTEGER",
            "requires_credentials": "INTEGER",
            "required_fields_json": "TEXT",
            "missing_fields_json": "TEXT",
            "external_submission": "TEXT NOT NULL DEFAULT 'not_executed'",
            "payload_json": "TEXT",
            "metadata_json": "TEXT",
            "created_at": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in required_columns.items():
            if column not in columns:
                connection.execute(f"ALTER TABLE wave_operation_snapshots ADD COLUMN {column} {definition}")
        connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_local_wave_ops_status
                ON wave_operation_snapshots(status);
            CREATE INDEX IF NOT EXISTS idx_local_wave_ops_surface
                ON wave_operation_snapshots(surface);
            CREATE INDEX IF NOT EXISTS idx_local_wave_ops_safety
                ON wave_operation_snapshots(safety);
            CREATE INDEX IF NOT EXISTS idx_local_wave_ops_workflow
                ON wave_operation_snapshots(workflow_id);
            """
        )

    def _ensure_wave_entity_mirror_schema(self, connection: sqlite3.Connection) -> None:
        sync_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(wave_sync_runs)").fetchall()
        }
        sync_required = {
            "target_system": "TEXT NOT NULL DEFAULT 'waveapps_business'",
            "entity_types_json": "TEXT NOT NULL DEFAULT '[]'",
            "status": "TEXT NOT NULL DEFAULT 'running'",
            "page_size": "INTEGER NOT NULL DEFAULT 50",
            "pages_fetched": "INTEGER NOT NULL DEFAULT 0",
            "entities_seen": "INTEGER NOT NULL DEFAULT 0",
            "error_message": "TEXT",
            "metadata_json": "TEXT",
            "started_at": "TEXT NOT NULL DEFAULT ''",
            "finished_at": "TEXT",
            "created_at": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in sync_required.items():
            if column not in sync_columns:
                connection.execute(f"ALTER TABLE wave_sync_runs ADD COLUMN {column} {definition}")

        entity_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(wave_entities)").fetchall()
        }
        entity_required = {
            "target_system": "TEXT NOT NULL DEFAULT 'waveapps_business'",
            "entity_type": "TEXT NOT NULL DEFAULT 'unknown'",
            "external_id": "TEXT NOT NULL DEFAULT ''",
            "name": "TEXT",
            "status": "TEXT",
            "email": "TEXT",
            "currency": "TEXT",
            "amount": "REAL",
            "entity_date": "TEXT",
            "due_date": "TEXT",
            "modified_at": "TEXT",
            "presence_status": "TEXT NOT NULL DEFAULT 'present'",
            "last_sync_run_id": "INTEGER",
            "payload_json": "TEXT",
            "first_seen_at": "TEXT NOT NULL DEFAULT ''",
            "last_seen_at": "TEXT NOT NULL DEFAULT ''",
            "created_at": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in entity_required.items():
            if column not in entity_columns:
                connection.execute(f"ALTER TABLE wave_entities ADD COLUMN {column} {definition}")
        connection.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_local_wave_entities_unique
                ON wave_entities(target_system, entity_type, external_id);
            CREATE INDEX IF NOT EXISTS idx_local_wave_entities_target_type
                ON wave_entities(target_system, entity_type);
            CREATE INDEX IF NOT EXISTS idx_local_wave_entities_presence
                ON wave_entities(presence_status);
            CREATE INDEX IF NOT EXISTS idx_local_wave_entities_status
                ON wave_entities(status);
            CREATE INDEX IF NOT EXISTS idx_local_wave_sync_target
                ON wave_sync_runs(target_system);
            CREATE INDEX IF NOT EXISTS idx_local_wave_sync_status
                ON wave_sync_runs(status);
            """
        )

    def get_wave_operation_snapshot(self, operation_id: str) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM wave_operation_snapshots WHERE operation_id = ? LIMIT 1",
                (operation_id,),
            ).fetchone()
        if not row:
            return None
        return self._row_to_dict(row)

    def list_wave_operation_snapshots(
        self,
        surface: Optional[str] = None,
        workflow_id: Optional[str] = None,
        action_id: Optional[str] = None,
        safety: Optional[str] = None,
        status: Optional[Any] = None,
        operation_id: Optional[str] = None,
        limit: int = 100,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT * FROM wave_operation_snapshots"
        params = []
        where = []
        if surface:
            where.append("surface = ?")
            params.append(surface)
        if workflow_id:
            where.append("workflow_id = ?")
            params.append(workflow_id)
        if action_id:
            where.append("action_id = ?")
            params.append(action_id)
        if safety:
            where.append("safety = ?")
            params.append(safety)
        if status:
            if isinstance(status, Sequence) and not isinstance(status, str):
                statuses = [str(item) for item in status if item]
                if statuses:
                    placeholders = ", ".join("?" for _ in statuses)
                    where.append(f"status IN ({placeholders})")
                    params.extend(statuses)
            else:
                where.append("status = ?")
                params.append(status)
        if operation_id:
            where.append("operation_id = ?")
            params.append(operation_id)
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        query = f"{query} ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def create_wave_sync_run(self, payload: Dict[str, Any]) -> int:
        now = self._now()
        return self._insert(
            "wave_sync_runs",
            {
                "target_system": payload.get("targetSystem") or payload.get("target_system"),
                "entity_types_json": self._json(payload.get("entityTypes") or payload.get("entity_types") or []),
                "status": payload.get("status") or "running",
                "page_size": self._int(payload.get("pageSize") or payload.get("page_size"), 50),
                "pages_fetched": self._int(payload.get("pagesFetched") or payload.get("pages_fetched"), 0),
                "entities_seen": self._int(payload.get("entitiesSeen") or payload.get("entities_seen"), 0),
                "error_message": self._redact_error_message(
                    payload.get("errorMessage") or payload.get("error_message")
                ),
                "metadata_json": self._json(self._redact_sensitive(payload.get("metadata"))),
                "started_at": self._date_text(payload.get("startedAt") or payload.get("started_at")) or now,
                "finished_at": self._date_text(payload.get("finishedAt") or payload.get("finished_at")),
                "created_at": now,
                "updated_at": now,
            },
        )

    def update_wave_sync_run(self, sync_run_id: int, payload: Dict[str, Any]) -> None:
        pages_fetched = self._payload_value(payload, "pagesFetched", "pages_fetched")
        entities_seen = self._payload_value(payload, "entitiesSeen", "entities_seen")
        self._update(
            "wave_sync_runs",
            int(sync_run_id),
            {
                "status": payload.get("status"),
                "pages_fetched": self._optional_int(pages_fetched),
                "entities_seen": self._optional_int(entities_seen),
                "error_message": self._redact_error_message(
                    payload.get("errorMessage") or payload.get("error_message")
                ),
                "metadata_json": self._json(self._redact_sensitive(payload.get("metadata"))) if "metadata" in payload else None,
                "finished_at": self._date_text(payload.get("finishedAt") or payload.get("finished_at")),
                "updated_at": self._now(),
            },
        )

    def get_wave_sync_run(self, sync_run_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM wave_sync_runs WHERE id = ? LIMIT 1",
                (int(sync_run_id),),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_wave_sync_runs(
        self,
        target_system: Optional[str] = None,
        status: Optional[Any] = None,
        limit: int = 100,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT * FROM wave_sync_runs"
        params = []
        where = []
        if target_system:
            where.append("target_system = ?")
            params.append(target_system)
        self._append_status_filter(where, params, "status", status)
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        query = f"{query} ORDER BY started_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def upsert_wave_entity(self, payload: Dict[str, Any]) -> int:
        target_system = str(payload.get("targetSystem") or payload.get("target_system") or "").strip()
        entity_type = str(payload.get("entityType") or payload.get("entity_type") or "").strip()
        external_id = str(payload.get("externalId") or payload.get("external_id") or "").strip()
        if not target_system or not entity_type or not external_id:
            raise ValueError("targetSystem, entityType, and externalId are required for a Wave entity")
        now = self._now()
        values = {
            "target_system": target_system,
            "entity_type": entity_type,
            "external_id": external_id,
            "name": payload.get("name"),
            "status": payload.get("status"),
            "email": payload.get("email"),
            "currency": payload.get("currency"),
            "amount": self._float(payload.get("amount")),
            "entity_date": self._date_text(payload.get("entityDate") or payload.get("entity_date")),
            "due_date": self._date_text(payload.get("dueDate") or payload.get("due_date")),
            "modified_at": self._date_text(payload.get("modifiedAt") or payload.get("modified_at")),
            "presence_status": payload.get("presenceStatus") or payload.get("presence_status") or "present",
            "last_sync_run_id": self._optional_int(payload.get("lastSyncRunId") or payload.get("last_sync_run_id")),
            "payload_json": self._json(self._redact_sensitive(payload.get("payload"))),
            "last_seen_at": self._date_text(payload.get("lastSeenAt") or payload.get("last_seen_at")) or now,
            "updated_at": now,
        }
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id FROM wave_entities
                WHERE target_system = ? AND entity_type = ? AND external_id = ?
                LIMIT 1
                """,
                (target_system, entity_type, external_id),
            ).fetchone()
            if row:
                entity_id = int(row["id"])
                self._update_with_connection(connection, "wave_entities", entity_id, values)
                return entity_id
            return self._insert_with_connection(
                connection,
                "wave_entities",
                {
                    **values,
                    "first_seen_at": now,
                    "created_at": now,
                },
            )

    def mark_wave_entities_missing(
        self,
        target_system: str,
        entity_type: str,
        sync_run_id: int,
    ) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE wave_entities
                SET presence_status = 'missing_downstream', updated_at = ?
                WHERE target_system = ? AND entity_type = ?
                  AND (last_sync_run_id IS NULL OR last_sync_run_id != ?)
                  AND presence_status != 'missing_downstream'
                """,
                (self._now(), target_system, entity_type, int(sync_run_id)),
            )
            return int(cursor.rowcount)

    def get_wave_entity(self, entity_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM wave_entities WHERE id = ? LIMIT 1",
                (int(entity_id),),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_wave_entities(
        self,
        target_system: Optional[str] = None,
        entity_type: Optional[str] = None,
        status: Optional[Any] = None,
        presence_status: Optional[Any] = None,
        limit: int = 100,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT * FROM wave_entities"
        params = []
        where = []
        if target_system:
            where.append("target_system = ?")
            params.append(target_system)
        if entity_type:
            where.append("entity_type = ?")
            params.append(entity_type)
        self._append_status_filter(where, params, "status", status)
        self._append_status_filter(where, params, "presence_status", presence_status)
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        query = f"{query} ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def claim_financial_report_run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        schedule_id = str(payload.get("scheduleId") or payload.get("schedule_id") or "").strip()
        schedule_slot = str(payload.get("scheduleSlot") or payload.get("schedule_slot") or "").strip()
        if not schedule_id or not schedule_slot:
            raise ValueError("scheduleId and scheduleSlot are required for a financial report run")
        now = self._date_text(payload.get("startedAt") or payload.get("started_at")) or self._now()
        now_value = self._parse_datetime(now) or datetime.now(timezone.utc)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM financial_report_runs
                WHERE schedule_id = ? AND schedule_slot = ?
                LIMIT 1
                """,
                (schedule_id, schedule_slot),
            ).fetchone()
            if row:
                existing = self._row_to_dict(row)
                status = str(existing.get("status") or "unknown")
                retry_at = self._parse_datetime(existing.get("next_retry_at"))
                retry_due = status == "failed" and (retry_at is None or retry_at <= now_value)
                if not retry_due:
                    return {
                        "acquired": False,
                        "status": "retry_deferred" if status == "failed" else "already_claimed",
                        "reportRun": existing,
                    }
                report_run_id = int(existing["id"])
                connection.execute(
                    """
                    UPDATE financial_report_runs
                    SET status = 'running', readiness = NULL, started_at = ?,
                        finished_at = NULL, attempt_count = ?, next_retry_at = NULL,
                        error_message = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, int(existing.get("attempt_count") or 0) + 1, now, report_run_id),
                )
            else:
                report_run_id = self._insert_with_connection(
                    connection,
                    "financial_report_runs",
                    {
                        "schedule_id": schedule_id,
                        "schedule_slot": schedule_slot,
                        "report_type": payload.get("reportType") or payload.get("report_type") or "overview",
                        "basis": payload.get("basis") or "accrual",
                        "period_from": payload.get("periodFrom") or payload.get("period_from"),
                        "period_to": payload.get("periodTo") or payload.get("period_to"),
                        "target_system": payload.get("targetSystem") or payload.get("target_system"),
                        "status": "running",
                        "scheduled_for": self._date_text(
                            payload.get("scheduledFor") or payload.get("scheduled_for")
                        ) or now,
                        "started_at": now,
                        "attempt_count": 1,
                        "external_submission": "not_executed",
                        "metadata_json": self._json(self._redact_sensitive(payload.get("metadata") or {})),
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            updated = connection.execute(
                "SELECT * FROM financial_report_runs WHERE id = ? LIMIT 1",
                (report_run_id,),
            ).fetchone()
        return {
            "acquired": True,
            "status": "acquired",
            "reportRun": self._row_to_dict(updated),
        }

    def complete_financial_report_run(self, report_run_id: int, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        status = str(payload.get("status") or "prepared").strip()
        allowed_statuses = {"prepared", "prepared_needs_review", "failed"}
        if status not in allowed_statuses:
            raise ValueError(f"Unsupported financial report run status: {status}")
        now = self._date_text(payload.get("finishedAt") or payload.get("finished_at")) or self._now()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM financial_report_runs WHERE id = ? LIMIT 1",
                (int(report_run_id),),
            ).fetchone()
            if not row:
                return None
            existing = self._row_to_dict(row)
            metadata = dict(existing.get("metadata") or {})
            if isinstance(payload.get("metadata"), dict):
                metadata.update(self._redact_sensitive(payload["metadata"]))
            self._update_with_connection(
                connection,
                "financial_report_runs",
                int(report_run_id),
                {
                    "status": status,
                    "readiness": payload.get("readiness"),
                    "finished_at": now,
                    "next_retry_at": self._date_text(payload.get("nextRetryAt") or payload.get("next_retry_at")),
                    "json_path": payload.get("jsonPath") or payload.get("json_path"),
                    "csv_path": payload.get("csvPath") or payload.get("csv_path"),
                    "json_sha256": payload.get("jsonSha256") or payload.get("json_sha256"),
                    "csv_sha256": payload.get("csvSha256") or payload.get("csv_sha256"),
                    "json_bytes": self._optional_int(self._first_present(payload.get("jsonBytes"), payload.get("json_bytes"))),
                    "csv_bytes": self._optional_int(self._first_present(payload.get("csvBytes"), payload.get("csv_bytes"))),
                    "row_count": self._int(self._first_present(payload.get("rowCount"), payload.get("row_count")), 0),
                    "blocker_count": self._int(self._first_present(payload.get("blockerCount"), payload.get("blocker_count")), 0),
                    "external_submission": "not_executed",
                    "error_message": self._redact_error_message(
                        payload.get("errorMessage") or payload.get("error_message")
                    ),
                    "metadata_json": self._json(metadata),
                    "updated_at": now,
                },
            )
            updated = connection.execute(
                "SELECT * FROM financial_report_runs WHERE id = ? LIMIT 1",
                (int(report_run_id),),
            ).fetchone()
        return self._row_to_dict(updated) if updated else None

    def get_financial_report_run(self, report_run_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM financial_report_runs WHERE id = ? LIMIT 1",
                (int(report_run_id),),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_financial_report_run_by_slot(self, schedule_id: str, schedule_slot: str) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM financial_report_runs
                WHERE schedule_id = ? AND schedule_slot = ?
                LIMIT 1
                """,
                (str(schedule_id or "").strip(), str(schedule_slot or "").strip()),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_financial_report_runs(
        self,
        schedule_id: Optional[str] = None,
        status: Optional[Any] = None,
        limit: int = 100,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT * FROM financial_report_runs"
        params = []
        where = []
        if schedule_id:
            where.append("schedule_id = ?")
            params.append(schedule_id)
        self._append_status_filter(where, params, "status", status)
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        query = f"{query} ORDER BY scheduled_for DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def record_audit_event(self, payload: Dict[str, Any], preferred_id: Optional[int] = None) -> int:
        with self._connection() as connection:
            return self._record_audit_event_with_connection(connection, payload, preferred_id)

    def _record_audit_event_with_connection(
        self, connection: sqlite3.Connection, payload: Dict[str, Any], preferred_id: Optional[int] = None
    ) -> int:
        return self._insert_with_connection(
            connection,
            "audit_events",
            {
                "id": preferred_id,
                "actor_user_id": payload.get("actorUserId"),
                "action": payload.get("action", "workflow.event"),
                "entity_type": payload.get("entityType", "unknown"),
                "entity_id": payload.get("entityId"),
                "details_json": self._json(
                    self._redact_sensitive(payload.get("details") or {})
                ),
                "created_at": self._now(),
            },
        )

    def upsert_notification_preference(self, payload: Dict[str, Any]) -> int:
        event_type = str(payload.get("eventType") or payload.get("event_type") or "").strip()
        if not event_type:
            raise ValueError("eventType is required")
        minimum_severity = str(
            payload.get("minimumSeverity") or payload.get("minimum_severity") or "low"
        ).strip().lower()
        if minimum_severity not in {"low", "medium", "high"}:
            raise ValueError(f"Unsupported minimum notification severity: {minimum_severity}")
        now = self._now()
        values = {
            "enabled": self._bool_int(payload.get("enabled", True)),
            "in_app_enabled": self._bool_int(
                self._first_present(payload.get("inAppEnabled"), payload.get("in_app_enabled"), True)
            ),
            "minimum_severity": minimum_severity,
            "external_delivery": "disabled",
            "metadata_json": self._json(self._redact_sensitive(payload.get("metadata") or {})),
            "updated_at": now,
        }
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id FROM notification_preferences WHERE event_type = ? LIMIT 1",
                (event_type,),
            ).fetchone()
            if row:
                preference_id = int(row["id"])
                self._update_with_connection(connection, "notification_preferences", preference_id, values)
            else:
                preference_id = self._insert_with_connection(
                    connection,
                    "notification_preferences",
                    {"event_type": event_type, "created_at": now, **values},
                )
        return preference_id

    def get_notification_preference(self, event_type: str) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM notification_preferences WHERE event_type = ? LIMIT 1",
                (str(event_type or "").strip(),),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_notification_preferences(self, limit: int = 100) -> list:
        limit = self._bounded_limit(limit)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM notification_preferences ORDER BY event_type ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def upsert_notification(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        fingerprint = str(payload.get("fingerprint") or "").strip()
        event_type = str(payload.get("eventType") or payload.get("event_type") or "").strip()
        if not fingerprint or not event_type:
            raise ValueError("fingerprint and eventType are required")
        now = self._date_text(payload.get("seenAt") or payload.get("seen_at")) or self._now()
        desired_status = str(payload.get("status") or "unread").strip()
        if desired_status not in {"unread", "suppressed"}:
            raise ValueError(f"Unsupported notification upsert status: {desired_status}")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM notifications WHERE fingerprint = ? LIMIT 1",
                (fingerprint,),
            ).fetchone()
            created = row is None
            reopened = False
            if row:
                existing = self._row_to_dict(row)
                status = str(existing.get("status") or "unread")
                if status == "resolved":
                    status = desired_status
                    reopened = True
                elif status == "suppressed" and desired_status == "unread":
                    status = "unread"
                    reopened = True
                elif desired_status == "suppressed":
                    status = "suppressed"
                occurrence_count = int(existing.get("occurrence_count") or 1) + (1 if reopened else 0)
                notification_id = int(existing["id"])
                self._update_with_connection(
                    connection,
                    "notifications",
                    notification_id,
                    {
                        "event_type": event_type,
                        "severity": payload.get("severity") or "low",
                        "title": payload.get("title") or event_type.replace("_", " ").title(),
                        "message": payload.get("message") or "FAB detected an operating event.",
                        "entity_type": payload.get("entityType") or payload.get("entity_type"),
                        "entity_id": self._date_text(payload.get("entityId") or payload.get("entity_id")),
                        "status": status,
                        "source": payload.get("source") or "operations_health",
                        "occurrence_count": occurrence_count,
                        "last_seen_at": now,
                        "read_at": None if reopened else existing.get("read_at"),
                        "acknowledged_at": None if reopened else existing.get("acknowledged_at"),
                        "resolved_at": None if reopened else existing.get("resolved_at"),
                        "external_delivery": "not_executed",
                        "payload_json": self._json(self._redact_sensitive(payload.get("payload") or {})),
                        "updated_at": now,
                    },
                )
                if reopened:
                    connection.execute(
                        "UPDATE notifications SET read_at = NULL, acknowledged_at = NULL, resolved_at = NULL WHERE id = ?",
                        (notification_id,),
                    )
            else:
                notification_id = self._insert_with_connection(
                    connection,
                    "notifications",
                    {
                        "fingerprint": fingerprint,
                        "event_type": event_type,
                        "severity": payload.get("severity") or "low",
                        "title": payload.get("title") or event_type.replace("_", " ").title(),
                        "message": payload.get("message") or "FAB detected an operating event.",
                        "entity_type": payload.get("entityType") or payload.get("entity_type"),
                        "entity_id": self._date_text(payload.get("entityId") or payload.get("entity_id")),
                        "status": desired_status,
                        "source": payload.get("source") or "operations_health",
                        "first_seen_at": now,
                        "last_seen_at": now,
                        "external_delivery": "not_executed",
                        "payload_json": self._json(self._redact_sensitive(payload.get("payload") or {})),
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            updated = connection.execute(
                "SELECT * FROM notifications WHERE id = ? LIMIT 1",
                (notification_id,),
            ).fetchone()
        return {
            "created": created,
            "reopened": reopened,
            "notification": self._row_to_dict(updated),
        }

    def update_notification_status(self, notification_id: int, status: str) -> Optional[Dict[str, Any]]:
        status = str(status or "").strip().lower()
        if status not in {"unread", "read", "acknowledged", "resolved"}:
            raise ValueError(f"Unsupported notification status: {status}")
        now = self._now()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id FROM notifications WHERE id = ? LIMIT 1",
                (int(notification_id),),
            ).fetchone()
            if not row:
                return None
            values = {"status": status, "updated_at": now}
            if status == "unread":
                values.update({"read_at": None, "acknowledged_at": None, "resolved_at": None})
            elif status == "read":
                values["read_at"] = now
            elif status == "acknowledged":
                values.update({"read_at": now, "acknowledged_at": now})
            elif status == "resolved":
                values.update({"read_at": now, "resolved_at": now})
            self._update_with_connection(connection, "notifications", int(notification_id), values)
            if status == "unread":
                connection.execute(
                    "UPDATE notifications SET read_at = NULL, acknowledged_at = NULL, resolved_at = NULL WHERE id = ?",
                    (int(notification_id),),
                )
            updated = connection.execute(
                "SELECT * FROM notifications WHERE id = ? LIMIT 1",
                (int(notification_id),),
            ).fetchone()
        return self._row_to_dict(updated) if updated else None

    def resolve_inactive_notifications(self, source: str, active_fingerprints: Any) -> int:
        fingerprints = {str(value) for value in (active_fingerprints or []) if value}
        now = self._now()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, fingerprint FROM notifications WHERE source = ? AND status != 'resolved'",
                (str(source or "operations_health"),),
            ).fetchall()
            inactive_ids = [int(row["id"]) for row in rows if row["fingerprint"] not in fingerprints]
            if inactive_ids:
                placeholders = ", ".join("?" for _ in inactive_ids)
                connection.execute(
                    f"UPDATE notifications SET status = 'resolved', resolved_at = ?, updated_at = ? "
                    f"WHERE id IN ({placeholders})",
                    (now, now, *inactive_ids),
                )
        return len(inactive_ids)

    def get_notification(self, notification_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM notifications WHERE id = ? LIMIT 1",
                (int(notification_id),),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_notifications(
        self,
        status: Optional[Any] = None,
        severity: Optional[Any] = None,
        event_type: Optional[str] = None,
        limit: int = 100,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT * FROM notifications"
        params = []
        where = []
        self._append_status_filter(where, params, "status", status)
        self._append_status_filter(where, params, "severity", severity)
        if event_type:
            where.append("event_type = ?")
            params.append(event_type)
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        query = f"{query} ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, last_seen_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def create_compliance_assessment(
        self,
        payload: Dict[str, Any],
        findings: Optional[Sequence[Dict[str, Any]]] = None,
        retention_records: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        assessment_key = str(payload.get("assessmentKey") or payload.get("assessment_key") or "").strip()
        source_checksum = str(payload.get("sourceChecksum") or payload.get("source_checksum") or "").strip()
        if not assessment_key or not source_checksum:
            raise ValueError("assessmentKey and sourceChecksum are required")
        period_from = payload.get("periodFrom") or payload.get("period_from")
        period_to = payload.get("periodTo") or payload.get("period_to")
        basis = payload.get("basis") or "accrual"
        target_system = payload.get("targetSystem") or payload.get("target_system")
        now = self._date_text(payload.get("createdAt") or payload.get("created_at")) or self._now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM compliance_assessments WHERE assessment_key = ? LIMIT 1",
                (assessment_key,),
            ).fetchone()
            if existing:
                return {"created": False, "assessment": self._row_to_dict(existing)}
            assessment_id = self._insert_with_connection(
                connection,
                "compliance_assessments",
                {
                    "assessment_key": assessment_key,
                    "period_from": period_from,
                    "period_to": period_to,
                    "basis": basis,
                    "target_system": target_system,
                    "status": payload.get("status") or "ready",
                    "record_count": self._int(self._first_present(payload.get("recordCount"), payload.get("record_count")), 0),
                    "finding_count": self._int(self._first_present(payload.get("findingCount"), payload.get("finding_count")), 0),
                    "blocking_count": self._int(self._first_present(payload.get("blockingCount"), payload.get("blocking_count")), 0),
                    "attention_count": self._int(self._first_present(payload.get("attentionCount"), payload.get("attention_count")), 0),
                    "vat_summary_json": self._json(self._redact_sensitive(payload.get("vatSummary") or payload.get("vat_summary") or {})),
                    "source_checksum": source_checksum,
                    "statutory_status": "provisional",
                    "external_filing": "not_executed",
                    "metadata_json": self._json(self._redact_sensitive(payload.get("metadata") or {})),
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                """
                UPDATE compliance_findings
                SET status = 'superseded',
                    resolution = ?,
                    resolved_at = ?,
                    updated_at = ?
                WHERE assessment_id IN (
                    SELECT id
                    FROM compliance_assessments
                    WHERE id != ?
                      AND period_from = ?
                      AND period_to = ?
                      AND basis = ?
                      AND COALESCE(target_system, '') = COALESCE(?, '')
                )
                  AND status IN ('open', 'acknowledged')
                """,
                (
                    f"Superseded by compliance assessment #{assessment_id}.",
                    now,
                    now,
                    assessment_id,
                    period_from,
                    period_to,
                    basis,
                    target_system,
                ),
            )
            for finding in findings or []:
                self._insert_with_connection(
                    connection,
                    "compliance_findings",
                    {
                        "assessment_id": assessment_id,
                        "fingerprint": finding.get("fingerprint"),
                        "code": finding.get("code"),
                        "severity": finding.get("severity") or "medium",
                        "status": finding.get("status") or "open",
                        "title": finding.get("title") or str(finding.get("code") or "finding").replace("_", " ").title(),
                        "message": finding.get("message") or "Compliance evidence requires review.",
                        "bookkeeping_record_id": self._optional_int(
                            self._first_present(finding.get("bookkeepingRecordId"), finding.get("bookkeeping_record_id"))
                        ),
                        "document_id": self._optional_int(
                            self._first_present(finding.get("documentId"), finding.get("document_id"))
                        ),
                        "evidence_json": self._json(self._redact_sensitive(finding.get("evidence") or {})),
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            for retention in retention_records or []:
                document_id = self._optional_int(
                    self._first_present(retention.get("documentId"), retention.get("document_id"))
                )
                if document_id is None:
                    continue
                values = {
                    "assessment_id": assessment_id,
                    "source_date": retention.get("sourceDate") or retention.get("source_date"),
                    "retention_years": self._int(
                        self._first_present(retention.get("retentionYears"), retention.get("retention_years")),
                        7,
                    ),
                    "retain_until": retention.get("retainUntil") or retention.get("retain_until"),
                    "status": retention.get("status") or "retain_required",
                    "source_file_present": (
                        None
                        if self._first_present(retention.get("sourceFilePresent"), retention.get("source_file_present")) is None
                        else self._bool_int(
                            self._first_present(retention.get("sourceFilePresent"), retention.get("source_file_present"))
                        )
                    ),
                    "metadata_json": self._json(self._redact_sensitive(retention.get("metadata") or {})),
                    "last_assessed_at": now,
                    "updated_at": now,
                }
                row = connection.execute(
                    "SELECT id FROM retention_records WHERE document_id = ? LIMIT 1",
                    (document_id,),
                ).fetchone()
                if row:
                    self._update_with_connection(connection, "retention_records", int(row["id"]), values)
                else:
                    self._insert_with_connection(
                        connection,
                        "retention_records",
                        {"document_id": document_id, "created_at": now, **values},
                    )
            assessment = connection.execute(
                "SELECT * FROM compliance_assessments WHERE id = ? LIMIT 1",
                (assessment_id,),
            ).fetchone()
        return {"created": True, "assessment": self._row_to_dict(assessment)}

    def get_compliance_assessment(self, assessment_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM compliance_assessments WHERE id = ? LIMIT 1",
                (int(assessment_id),),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_compliance_assessments(
        self,
        status: Optional[Any] = None,
        limit: int = 100,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT * FROM compliance_assessments"
        params = []
        where = []
        self._append_status_filter(where, params, "status", status)
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        query = f"{query} ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_compliance_findings(
        self,
        assessment_id: Optional[int] = None,
        status: Optional[Any] = None,
        severity: Optional[Any] = None,
        code: Optional[str] = None,
        limit: int = 100,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT * FROM compliance_findings"
        params = []
        where = []
        if assessment_id is not None:
            where.append("assessment_id = ?")
            params.append(int(assessment_id))
        self._append_status_filter(where, params, "status", status)
        self._append_status_filter(where, params, "severity", severity)
        if code:
            where.append("code = ?")
            params.append(code)
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        query = f"{query} ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_compliance_finding(self, finding_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM compliance_findings WHERE id = ? LIMIT 1",
                (int(finding_id),),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def update_compliance_finding_status(
        self,
        finding_id: int,
        status: str,
        resolution: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        status = str(status or "").strip().lower()
        if status not in {"open", "acknowledged", "resolved", "accepted_exception"}:
            raise ValueError(f"Unsupported compliance finding status: {status}")
        if status in {"resolved", "accepted_exception"} and not str(resolution or "").strip():
            raise ValueError("resolution is required when closing a compliance finding")
        now = self._now()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id, assessment_id FROM compliance_findings WHERE id = ? LIMIT 1",
                (int(finding_id),),
            ).fetchone()
            if not row:
                return None
            values = {
                "status": status,
                "resolution": resolution,
                "resolved_at": now if status in {"resolved", "accepted_exception"} else None,
                "updated_at": now,
            }
            self._update_with_connection(connection, "compliance_findings", int(finding_id), values)
            if status == "open":
                connection.execute(
                    "UPDATE compliance_findings SET resolution = NULL, resolved_at = NULL WHERE id = ?",
                    (int(finding_id),),
                )
            assessment_id = int(row["assessment_id"])
            counters = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status IN ('open', 'acknowledged') THEN 1 ELSE 0 END) AS open_count,
                    SUM(CASE WHEN status IN ('open', 'acknowledged') AND severity = 'high' THEN 1 ELSE 0 END) AS blocking_count,
                    SUM(CASE WHEN status IN ('open', 'acknowledged') AND severity IN ('medium', 'low') THEN 1 ELSE 0 END) AS attention_count
                FROM compliance_findings
                WHERE assessment_id = ?
                """,
                (assessment_id,),
            ).fetchone()
            open_count = int(counters["open_count"] or 0)
            blocking_count = int(counters["blocking_count"] or 0)
            attention_count = int(counters["attention_count"] or 0)
            assessment_status = "blocked" if blocking_count else "needs_review" if open_count else "ready"
            connection.execute(
                """
                UPDATE compliance_assessments
                SET status = ?, blocking_count = ?, attention_count = ?, updated_at = ?
                WHERE id = ?
                """,
                (assessment_status, blocking_count, attention_count, now, assessment_id),
            )
            updated = connection.execute(
                "SELECT * FROM compliance_findings WHERE id = ? LIMIT 1",
                (int(finding_id),),
            ).fetchone()
        return self._row_to_dict(updated) if updated else None

    def list_retention_records(
        self,
        status: Optional[Any] = None,
        limit: int = 100,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT * FROM retention_records"
        params = []
        where = []
        self._append_status_filter(where, params, "status", status)
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        query = f"{query} ORDER BY retain_until ASC, id ASC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def dashboard_metrics(self) -> Dict[str, int]:
        with self._connection() as connection:
            return {
                "documents": self._count(connection, "bookkeeping_documents"),
                "pending_review": self._count(
                    connection,
                    "review_items",
                    "status IN ('pending', 'in_review')",
                ),
                "duplicates": self._count(
                    connection,
                    "bookkeeping_documents",
                    "duplicate_of_document_id IS NOT NULL",
                ),
                "duplicate_candidates": self._count(connection, "duplicate_candidates"),
                "open_duplicate_candidates": self._count(
                    connection,
                    "duplicate_candidates",
                    "status IN ('pending', 'in_review')",
                ),
                "document_groups": self._count(connection, "document_groups"),
                "open_document_groups": self._count(
                    connection,
                    "document_groups",
                    "status IN ('candidate', 'needs_review', 'in_review')",
                ),
                "suggested_vendor_rules": self._count(
                    connection,
                    "vendor_category_rules",
                    "status = 'suggested'",
                ),
                "unreconciled_documents": self._count(
                    connection,
                    "bookkeeping_documents",
                    "processing_status IN ('processed', 'reviewed', 'validated', 'ready_to_route', 'export_draft_prepared', 'routed') "
                    "AND reconciliation_status NOT IN ('approved', 'reconciled')",
                ),
                "failed_documents": self._count(
                    connection,
                    "bookkeeping_documents",
                    "processing_status = 'failed'",
                ),
                "bank_statement_imports": self._count(connection, "bank_statement_imports"),
                "bank_transactions": self._count(connection, "bank_transactions"),
                "unreconciled_bank_transactions": self._count(
                    connection,
                    "bank_transactions",
                    "reconciliation_status NOT IN ('approved', 'reconciled', 'ignored')",
                ),
                "bookkeeping_records": self._count(connection, "bookkeeping_records"),
                "bookkeeping_record_line_items": self._count(connection, "bookkeeping_record_line_items"),
                "bookkeeping_records_needing_review": self._count(
                    connection,
                    "bookkeeping_records",
                    "review_required = 1 OR status IN ('needs_review', 'failed', 'duplicate')",
                ),
                "export_ready_records": self._count(
                    connection,
                    "bookkeeping_records",
                    "status IN ('ready_to_route', 'reviewed', 'validated') "
                    "AND export_status IN ('not_started', 'ready') "
                    "AND review_required = 0",
                ),
                "export_attempts": self._count(connection, "export_attempts"),
                "export_attempts_needing_approval": self._count(
                    connection,
                    "export_attempts",
                    "approval_required = 1 AND status IN ('approval_required', 'prepared')",
                ),
                "approved_export_attempts": self._count(
                    connection,
                    "export_attempts",
                    "status = 'approved' AND external_submission = 'approved_not_executed'",
                ),
                "attention_export_attempts": self._count(
                    connection,
                    "export_attempts",
                    "status = 'attention_required' AND external_submission = 'not_executed'",
                ),
                "supervised_export_attempts": self._count(
                    connection,
                    "export_attempts",
                    "status = 'supervision_required' AND external_submission = 'not_executed'",
                ),
                "deferred_export_attempts": self._count(
                    connection,
                    "export_attempts",
                    "status = 'deferred' AND external_submission = 'not_executed'",
                ),
                "executed_export_attempts": self._count(
                    connection,
                    "export_attempts",
                    "external_submission IN ('executed', 'submitted')",
                ),
                "wave_report_snapshots": self._count(connection, "wave_report_snapshots"),
                "wave_operation_snapshots": self._count(connection, "wave_operation_snapshots"),
                "wave_sync_runs": self._count(connection, "wave_sync_runs"),
                "wave_entities": self._count(connection, "wave_entities"),
                "wave_entities_missing_downstream": self._count(
                    connection,
                    "wave_entities",
                    "presence_status = 'missing_downstream'",
                ),
                "financial_report_runs": self._count(connection, "financial_report_runs"),
                "financial_report_runs_needing_attention": self._count(
                    connection,
                    "financial_report_runs",
                    "status IN ('failed', 'prepared_needs_review')",
                ),
                "notifications": self._count(connection, "notifications"),
                "unread_notifications": self._count(
                    connection,
                    "notifications",
                    "status = 'unread'",
                ),
                "active_notifications": self._count(
                    connection,
                    "notifications",
                    "status IN ('unread', 'read', 'acknowledged')",
                ),
                "notification_preferences": self._count(connection, "notification_preferences"),
                "compliance_assessments": self._count(connection, "compliance_assessments"),
                "open_compliance_findings": self._count(
                    connection,
                    "compliance_findings",
                    "status IN ('open', 'acknowledged')",
                ),
                "blocking_compliance_findings": self._count(
                    connection,
                    "compliance_findings",
                    "status IN ('open', 'acknowledged') AND severity = 'high'",
                ),
                "retention_records": self._count(connection, "retention_records"),
                "workflow_runs": self._count(connection, "workflow_runs"),
                "workflow_steps": self._count(connection, "workflow_steps"),
                "failed_workflow_steps": self._count(
                    connection,
                    "workflow_steps",
                    "status = 'failed'",
                ),
                "audit_events": self._count(connection, "audit_events"),
            }

    def list_workflow_runs(
        self,
        status: Optional[Any] = None,
        trigger_source: Optional[str] = None,
        limit: int = 100,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT * FROM workflow_runs"
        params = []
        where = []
        if status:
            if isinstance(status, Sequence) and not isinstance(status, str):
                statuses = [str(item) for item in status if item]
                if statuses:
                    placeholders = ", ".join("?" for _ in statuses)
                    where.append(f"status IN ({placeholders})")
                    params.extend(statuses)
            else:
                where.append("status = ?")
                params.append(status)
        if trigger_source:
            where.append("trigger_source = ?")
            params.append(trigger_source)
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        query = f"{query} ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_workflow_steps(
        self,
        workflow_run_id: Optional[int] = None,
        status: Optional[Any] = None,
        step_key: Optional[str] = None,
        limit: int = 100,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT * FROM workflow_steps"
        params = []
        where = []
        if workflow_run_id is not None:
            where.append("workflow_run_id = ?")
            params.append(int(workflow_run_id))
        self._append_status_filter(where, params, "status", status)
        if step_key:
            where.append("step_key = ?")
            params.append(str(step_key))
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        query = f"{query} ORDER BY workflow_run_id DESC, step_order ASC, attempt ASC, id ASC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_source_accounts(
        self,
        source_type: Optional[str] = None,
        status: Optional[Any] = None,
        limit: int = 100,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT * FROM source_accounts"
        params = []
        where = []
        if source_type:
            where.append("source_type = ?")
            params.append(source_type)
        if status:
            if isinstance(status, Sequence) and not isinstance(status, str):
                statuses = [str(item) for item in status if item]
                if statuses:
                    placeholders = ", ".join("?" for _ in statuses)
                    where.append(f"status IN ({placeholders})")
                    params.extend(statuses)
            else:
                where.append("status = ?")
                params.append(status)
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        query = f"{query} ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_documents(self, status: Optional[Any] = None, limit: int = 100) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT * FROM bookkeeping_documents"
        params = []
        if status:
            if isinstance(status, Sequence) and not isinstance(status, str):
                statuses = [str(item) for item in status if item]
                if statuses:
                    placeholders = ", ".join("?" for _ in statuses)
                    query = f"{query} WHERE processing_status IN ({placeholders})"
                    params.extend(statuses)
            else:
                query = f"{query} WHERE processing_status = ?"
                params.append(status)
        query = f"{query} ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_reconcilable_documents(self, statuses: Sequence[str], limit: int = 100) -> list:
        where = ["COALESCE(reconciliation_status, 'not_started') NOT IN ('approved', 'reconciled', 'ignored')",
                 "COALESCE(duplicate_of_document_id, 0) = 0"]
        params = []
        self._append_status_filter(where, params, "processing_status", statuses)
        query = "SELECT * FROM bookkeeping_documents WHERE " + " AND ".join(where)
        query += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(self._bounded_limit(limit))
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_documents_with_review_items(
        self,
        document_ids: Sequence[int],
        review_status: Optional[Any] = None,
    ) -> Dict[int, Dict[str, Any]]:
        """Load document rows and their selected reviews without full histories."""
        bounded_ids = []
        seen_ids = set()
        for value in document_ids:
            document_id = self._optional_int(value)
            if document_id is None or document_id in seen_ids:
                continue
            seen_ids.add(document_id)
            bounded_ids.append(document_id)
            if len(bounded_ids) >= 10000:
                break
        if not bounded_ids:
            return {}

        documents: Dict[int, Dict[str, Any]] = {}
        review_rows = []
        with self._connection() as connection:
            for chunk_start in range(0, len(bounded_ids), 500):
                chunk = bounded_ids[chunk_start:chunk_start + 500]
                placeholders = ", ".join("?" for _ in chunk)
                document_rows = connection.execute(
                    f"""
                    SELECT * FROM bookkeeping_documents
                    WHERE id IN ({placeholders})
                    """,
                    chunk,
                ).fetchall()
                for row in document_rows:
                    document = self._row_to_dict(row)
                    document["review_items"] = []
                    documents[int(document["id"])] = document

                where = [f"document_id IN ({placeholders})"]
                params = list(chunk)
                self._append_status_filter(
                    where,
                    params,
                    "status",
                    review_status,
                )
                review_rows.extend(
                    connection.execute(
                        f"""
                        SELECT * FROM review_items
                        WHERE {' AND '.join(where)}
                        ORDER BY created_at DESC, id DESC
                        """,
                        params,
                    ).fetchall()
                )

        for row in review_rows:
            review = self._row_to_dict(row)
            document = documents.get(int(review["document_id"]))
            if document is not None:
                document["review_items"].append(review)
        for document in documents.values():
            document["review_items"].sort(
                key=lambda item: (
                    str(item.get("created_at") or ""),
                    int(item.get("id") or 0),
                ),
                reverse=True,
            )
        return documents

    def get_exception_entity_context(
        self,
        references: Dict[str, Sequence[int]],
    ) -> Dict[str, Dict[int, Dict[str, Any]]]:
        """Load compact exception entities through one bounded read snapshot."""
        tables = {
            "bookkeeping_document": "bookkeeping_documents",
            "review_item": "review_items",
            "bookkeeping_record": "bookkeeping_records",
            "export_attempt": "export_attempts",
            "routing_attempt": "routing_attempts",
            "workflow_run": "workflow_runs",
            "source_account": "source_accounts",
        }
        bounded_references: Dict[str, list] = {}
        for entity_type, values in (references or {}).items():
            if entity_type not in tables:
                continue
            entity_ids = []
            seen_ids = set()
            for value in values or []:
                entity_id = self._optional_int(value)
                if entity_id is None or entity_id in seen_ids:
                    continue
                seen_ids.add(entity_id)
                entity_ids.append(entity_id)
                if len(entity_ids) >= 500:
                    break
            if entity_ids:
                bounded_references[entity_type] = entity_ids
        if not bounded_references:
            return {}

        context: Dict[str, Dict[int, Dict[str, Any]]] = {
            entity_type: {}
            for entity_type in bounded_references
        }
        with self._connection() as connection:
            for entity_type, entity_ids in bounded_references.items():
                table = tables[entity_type]
                for chunk_start in range(0, len(entity_ids), 500):
                    chunk = entity_ids[chunk_start:chunk_start + 500]
                    placeholders = ", ".join("?" for _ in chunk)
                    rows = connection.execute(
                        f"SELECT * FROM {table} WHERE id IN ({placeholders})",
                        chunk,
                    ).fetchall()
                    for row in rows:
                        entity = self._row_to_dict(row)
                        context[entity_type][int(entity["id"])] = entity

            document_ids = bounded_references.get("bookkeeping_document") or []
            for document in context.get("bookkeeping_document", {}).values():
                document["review_item_count"] = 0
            for chunk_start in range(0, len(document_ids), 500):
                chunk = document_ids[chunk_start:chunk_start + 500]
                placeholders = ", ".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT document_id, COUNT(*) AS review_item_count
                    FROM review_items
                    WHERE document_id IN ({placeholders})
                    GROUP BY document_id
                    """,
                    chunk,
                ).fetchall()
                for row in rows:
                    document = context["bookkeeping_document"].get(
                        int(row["document_id"])
                    )
                    if document is not None:
                        document["review_item_count"] = int(
                            row["review_item_count"] or 0
                        )

            workflow_ids = bounded_references.get("workflow_run") or []
            for workflow in context.get("workflow_run", {}).values():
                workflow["steps"] = []
                workflow["step_count"] = 0
            for chunk_start in range(0, len(workflow_ids), 500):
                chunk = workflow_ids[chunk_start:chunk_start + 500]
                placeholders = ", ".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT * FROM (
                        SELECT
                            workflow_steps.*,
                            ROW_NUMBER() OVER (
                                PARTITION BY workflow_run_id
                                ORDER BY step_order ASC, attempt ASC, id ASC
                            ) AS exception_row_number
                        FROM workflow_steps
                        WHERE workflow_run_id IN ({placeholders})
                    ) ranked_steps
                    WHERE exception_row_number <= 500
                    ORDER BY workflow_run_id DESC, step_order ASC, attempt ASC, id ASC
                    """,
                    chunk,
                ).fetchall()
                for row in rows:
                    step = self._row_to_dict(row)
                    step.pop("exception_row_number", None)
                    workflow = context["workflow_run"].get(
                        int(step["workflow_run_id"])
                    )
                    if workflow is not None:
                        workflow["steps"].append(step)
                        workflow["step_count"] += 1
        return context

    def get_document_delivery_context(
        self,
        document_ids: Sequence[int],
        evidence_action: str,
    ) -> Dict[int, Dict[str, Any]]:
        """Load the fields needed to build source delivery work orders in one snapshot."""
        bounded_ids = []
        seen_ids = set()
        for value in document_ids:
            document_id = self._optional_int(value)
            if document_id is None or document_id in seen_ids:
                continue
            seen_ids.add(document_id)
            bounded_ids.append(document_id)
            if len(bounded_ids) >= 500:
                break
        if not bounded_ids:
            return {}

        placeholders = ", ".join("?" for _ in bounded_ids)
        context = {
            document_id: {
                "review_items": [],
                "bookkeeping_record": None,
                "export_attempts": [],
                "evidence_event": None,
            }
            for document_id in bounded_ids
        }
        with self._connection() as connection:
            review_rows = connection.execute(
                f"""
                SELECT * FROM review_items
                WHERE document_id IN ({placeholders})
                ORDER BY created_at DESC, id DESC
                """,
                bounded_ids,
            ).fetchall()
            record_rows = connection.execute(
                f"""
                SELECT * FROM bookkeeping_records
                WHERE document_id IN ({placeholders})
                ORDER BY id ASC
                """,
                bounded_ids,
            ).fetchall()
            export_rows = connection.execute(
                f"""
                SELECT * FROM export_attempts
                WHERE document_id IN ({placeholders})
                ORDER BY updated_at DESC, id DESC
                """,
                bounded_ids,
            ).fetchall()
            evidence_rows = connection.execute(
                f"""
                SELECT * FROM audit_events
                WHERE action = ?
                  AND entity_type = 'bookkeeping_document'
                  AND entity_id IN ({placeholders})
                ORDER BY created_at DESC, id DESC
                """,
                [str(evidence_action), *[str(document_id) for document_id in bounded_ids]],
            ).fetchall()

            records_by_id = {}
            for row in record_rows:
                record = self._row_to_dict(row)
                record["line_items"] = []
                record["line_item_count"] = 0
                records_by_id[int(record["id"])] = record
                document_id = int(record["document_id"])
                if context[document_id]["bookkeeping_record"] is None:
                    context[document_id]["bookkeeping_record"] = record

            if records_by_id:
                record_ids = list(records_by_id)
                record_placeholders = ", ".join("?" for _ in record_ids)
                line_rows = connection.execute(
                    f"""
                    SELECT * FROM bookkeeping_record_line_items
                    WHERE bookkeeping_record_id IN ({record_placeholders})
                    ORDER BY bookkeeping_record_id ASC, line_index ASC, id ASC
                    """,
                    record_ids,
                ).fetchall()
                for row in line_rows:
                    line_item = self._row_to_dict(row)
                    record = records_by_id.get(int(line_item["bookkeeping_record_id"]))
                    if record is not None:
                        record["line_items"].append(line_item)
                        record["line_item_count"] += 1

        for row in review_rows:
            review = self._row_to_dict(row)
            document_id = int(review["document_id"])
            context[document_id]["review_items"].append(review)
        for row in export_rows:
            export = self._row_to_dict(row)
            document_id = int(export["document_id"])
            if len(context[document_id]["export_attempts"]) < 10:
                context[document_id]["export_attempts"].append(export)
        for row in evidence_rows:
            evidence = self._row_to_dict(row)
            document_id = int(evidence["entity_id"])
            if context[document_id]["evidence_event"] is None:
                context[document_id]["evidence_event"] = evidence
        return context

    def get_review_work_item_context(
        self,
        document_ids: Sequence[int],
    ) -> Dict[str, Any]:
        """Load compact review documents, records, and duplicate links in bounded batches."""
        source_ids = []
        seen_ids = set()
        for value in document_ids:
            document_id = self._optional_int(value)
            if document_id is None or document_id in seen_ids:
                continue
            seen_ids.add(document_id)
            source_ids.append(document_id)
            if len(source_ids) >= 500:
                break
        if not source_ids:
            return {
                "documents": {},
                "bookkeeping_records": {},
                "duplicate_candidates": {},
            }

        duplicate_rows_by_id = {}
        with self._connection() as connection:
            for chunk_start in range(0, len(source_ids), 250):
                chunk = source_ids[chunk_start:chunk_start + 250]
                placeholders = ", ".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT * FROM duplicate_candidates
                    WHERE document_id IN ({placeholders})
                       OR candidate_document_id IN ({placeholders})
                    ORDER BY updated_at DESC, id DESC
                    """,
                    [*chunk, *chunk],
                ).fetchall()
                for row in rows:
                    duplicate_rows_by_id[int(row["id"])] = row

            related_ids = set(source_ids)
            for row in duplicate_rows_by_id.values():
                related_ids.add(int(row["document_id"]))
                related_ids.add(int(row["candidate_document_id"]))

            document_rows = []
            ordered_related_ids = sorted(related_ids)
            for chunk_start in range(0, len(ordered_related_ids), 500):
                chunk = ordered_related_ids[chunk_start:chunk_start + 500]
                placeholders = ", ".join("?" for _ in chunk)
                document_rows.extend(
                    connection.execute(
                        f"""
                        SELECT * FROM bookkeeping_documents
                        WHERE id IN ({placeholders})
                        """,
                        chunk,
                    ).fetchall()
                )

            record_rows = []
            for chunk_start in range(0, len(source_ids), 500):
                chunk = source_ids[chunk_start:chunk_start + 500]
                placeholders = ", ".join("?" for _ in chunk)
                record_rows.extend(
                    connection.execute(
                        f"""
                        SELECT * FROM bookkeeping_records
                        WHERE document_id IN ({placeholders})
                        ORDER BY id ASC
                        """,
                        chunk,
                    ).fetchall()
                )

        documents = {
            int(row["id"]): self._row_to_dict(row)
            for row in document_rows
        }
        bookkeeping_records = {}
        for row in record_rows:
            record = self._row_to_dict(row)
            bookkeeping_records.setdefault(int(record["document_id"]), record)
        duplicate_candidates = {document_id: [] for document_id in source_ids}
        for row in duplicate_rows_by_id.values():
            candidate = self._row_to_dict(row)
            for document_id in (
                int(candidate["document_id"]),
                int(candidate["candidate_document_id"]),
            ):
                if document_id in duplicate_candidates:
                    duplicate_candidates[document_id].append(candidate)
        for candidates in duplicate_candidates.values():
            candidates.sort(
                key=lambda item: (
                    str(item.get("updated_at") or ""),
                    int(item.get("id") or 0),
                ),
                reverse=True,
            )
        return {
            "documents": documents,
            "bookkeeping_records": bookkeeping_records,
            "duplicate_candidates": duplicate_candidates,
        }

    def get_document_by_source(self, source: str, source_document_id: str) -> Optional[Dict[str, Any]]:
        if not source_document_id:
            return None
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM bookkeeping_documents
                WHERE source = ? AND source_document_id = ?
                LIMIT 1
                """,
                (source, source_document_id),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def find_document_by_fingerprint(
        self,
        duplicate_fingerprint: str,
        exclude_source_document_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not duplicate_fingerprint:
            return None
        query = """
            SELECT * FROM bookkeeping_documents
            WHERE duplicate_fingerprint = ?
        """
        params = [duplicate_fingerprint]
        if exclude_source_document_id:
            query = f"{query} AND COALESCE(source_document_id, '') != ?"
            params.append(exclude_source_document_id)
        query = f"{query} ORDER BY id ASC LIMIT 1"
        with self._connection() as connection:
            row = connection.execute(query, params).fetchone()
        return self._row_to_dict(row) if row else None

    def get_document_core(self, document_id: int) -> Optional[Dict[str, Any]]:
        """Read retained document facts without materializing related histories."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM bookkeeping_documents WHERE id = ? LIMIT 1", (document_id,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_document(self, document_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            document = connection.execute(
                "SELECT * FROM bookkeeping_documents WHERE id = ? LIMIT 1",
                (document_id,),
            ).fetchone()
            if not document:
                return None
            result = self._row_to_dict(document)
            result["review_items"] = [
                self._row_to_dict(row)
                for row in connection.execute(
                    "SELECT * FROM review_items WHERE document_id = ? ORDER BY created_at DESC",
                    (document_id,),
                ).fetchall()
            ]
            result["duplicate_candidates"] = [
                self._row_to_dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM duplicate_candidates
                    WHERE document_id = ? OR candidate_document_id = ?
                    ORDER BY updated_at DESC, id DESC
                    """,
                    (document_id, document_id),
                ).fetchall()
            ]
            result["document_groups"] = [
                self._document_group_with_members(connection, row)
                for row in connection.execute(
                    """
                    SELECT DISTINCT g.* FROM document_groups g
                    JOIN document_group_members m ON m.group_id = g.id
                    WHERE m.document_id = ?
                    ORDER BY g.updated_at DESC, g.id DESC
                    """,
                    (document_id,),
                ).fetchall()
            ]
            result["extracted_fields"] = [
                self._row_to_dict(row)
                for row in connection.execute(
                    "SELECT * FROM extracted_fields WHERE document_id = ? ORDER BY id ASC",
                    (document_id,),
                ).fetchall()
            ]
            result["routing_attempts"] = [
                self._row_to_dict(row)
                for row in connection.execute(
                    "SELECT * FROM routing_attempts WHERE document_id = ? ORDER BY created_at DESC",
                    (document_id,),
                ).fetchall()
            ]
            result["export_attempts"] = [
                self._row_to_dict(row)
                for row in connection.execute(
                    "SELECT * FROM export_attempts WHERE document_id = ? ORDER BY updated_at DESC, id DESC",
                    (document_id,),
                ).fetchall()
            ]
            result["reconciliation_matches"] = [
                self._row_to_dict(row)
                for row in connection.execute(
                    "SELECT * FROM reconciliation_matches WHERE document_id = ? ORDER BY created_at DESC",
                    (document_id,),
                ).fetchall()
            ]
            record = connection.execute(
                "SELECT * FROM bookkeeping_records WHERE document_id = ? LIMIT 1",
                (document_id,),
            ).fetchone()
            result["bookkeeping_record"] = (
                self._bookkeeping_record_with_line_items(connection, record)
                if record else None
            )
            result["review_corrections"] = [
                self._row_to_dict(row)
                for row in connection.execute(
                    "SELECT * FROM review_corrections WHERE document_id = ? ORDER BY created_at DESC",
                    (document_id,),
                ).fetchall()
            ]
            result["audit_events"] = [
                self._row_to_dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM audit_events
                    WHERE entity_type = 'bookkeeping_document' AND entity_id = ?
                    ORDER BY created_at DESC
                    """,
                    (str(document_id),),
                ).fetchall()
            ]
            return result

    def list_review_items(
        self,
        status: Optional[Any] = None,
        limit: int = 100,
        document_id: Optional[int] = None,
        offset: int = 0,
    ) -> list:
        limit = self._bounded_limit(limit)
        offset = max(0, self._int(offset, 0))
        query = "SELECT * FROM review_items"
        params = []
        where = []
        if status:
            if isinstance(status, Sequence) and not isinstance(status, str):
                statuses = [str(item) for item in status if item]
                if statuses:
                    placeholders = ", ".join("?" for _ in statuses)
                    where.append(f"status IN ({placeholders})")
                    params.extend(statuses)
            else:
                where.append("status = ?")
                params.append(status)
        if document_id is not None:
            where.append("document_id = ?")
            params.append(document_id)
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        query = f"{query} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend((limit, offset))
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_missing_receipt_review_items(
        self, limit: int = 100, before_id: Optional[int] = None, reconciliation_match_id: Optional[int] = None,
    ) -> list:
        query = "SELECT * FROM review_items WHERE document_id IS NULL AND reason = 'missing_receipt'"
        params = []
        if reconciliation_match_id is not None:
            query += f" AND ({MISSING_REVIEW_MATCH_REFERENCE_SQL}) = ?"
            params.append(reconciliation_match_id)
        if before_id is not None:
            query += " AND id < ?"
            params.append(int(before_id))
        query += " ORDER BY id DESC LIMIT ?"
        params.append(self._bounded_limit(limit))
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_review_work_item_page(
        self,
        status: Optional[Any] = None,
        limit: int = 100,
        offset: int = 0,
        document_id: Optional[int] = None,
    ) -> list:
        """Return complete document-level review groups for one bounded page."""
        limit = self._bounded_limit(limit)
        offset = max(0, self._int(offset, 0))
        where = []
        params = []
        self._append_status_filter(where, params, "r.status", status)
        if document_id is not None:
            where.append("r.document_id = ?")
            params.append(int(document_id))
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        group_key = (
            "CASE WHEN r.document_id IS NULL "
            "THEN 'review:' || CAST(r.id AS TEXT) "
            "ELSE 'document:' || CAST(r.document_id AS TEXT) END"
        )
        query = f"""
            WITH filtered AS (
                SELECT
                    r.*,
                    {group_key} AS review_group_key
                FROM review_items r
                {where_sql}
            ),
            page_groups AS (
                SELECT
                    review_group_key,
                    MAX(created_at) AS latest_created_at,
                    MAX(id) AS latest_id
                FROM filtered
                GROUP BY review_group_key
                ORDER BY latest_created_at DESC, latest_id DESC
                LIMIT ? OFFSET ?
            )
            SELECT
                filtered.id,
                filtered.document_id,
                filtered.reason,
                filtered.details,
                filtered.status,
                filtered.corrected_data_json,
                filtered.created_at,
                filtered.updated_at
            FROM filtered
            INNER JOIN page_groups
                ON page_groups.review_group_key = filtered.review_group_key
            ORDER BY
                page_groups.latest_created_at DESC,
                page_groups.latest_id DESC,
                filtered.created_at DESC,
                filtered.id DESC
        """
        with self._connection() as connection:
            rows = connection.execute(query, [*params, limit, offset]).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_review_summary_groups(
        self,
        status: Optional[Any] = None,
        document_id: Optional[int] = None,
    ) -> list:
        """Return one compact aggregate row per review work item."""
        where = []
        params = []
        self._append_status_filter(where, params, "r.status", status)
        if document_id is not None:
            where.append("r.document_id = ?")
            params.append(int(document_id))
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        group_key = (
            "CASE WHEN r.document_id IS NULL "
            "THEN 'review:' || CAST(r.id AS TEXT) "
            "ELSE 'document:' || CAST(r.document_id AS TEXT) END"
        )
        query = f"""
            SELECT
                {group_key} AS review_group_key,
                MAX(r.document_id) AS document_id,
                COUNT(*) AS review_count,
                MAX(CASE WHEN r.reason = 'duplicate_candidate' THEN 1 ELSE 0 END)
                    AS has_duplicate_candidate,
                MIN(r.created_at) AS oldest_created_at,
                MAX(r.created_at) AS newest_created_at,
                MAX(d.document_type) AS document_type,
                MAX(d.vendor_name) AS vendor_name,
                MAX(d.category) AS category,
                MAX(d.extracted_data_json) AS extracted_data_json,
                MAX(d.metadata_json) AS metadata_json
            FROM review_items r
            LEFT JOIN bookkeeping_documents d ON d.id = r.document_id
            {where_sql}
            GROUP BY {group_key}
            ORDER BY newest_created_at DESC, MAX(r.id) DESC
        """
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def count_review_work_item_groups(
        self,
        status: Optional[Any] = None,
        document_id: Optional[int] = None,
    ) -> int:
        where = []
        params = []
        self._append_status_filter(where, params, "r.status", status)
        if document_id is not None:
            where.append("r.document_id = ?")
            params.append(int(document_id))
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        group_key = (
            "CASE WHEN r.document_id IS NULL "
            "THEN 'review:' || CAST(r.id AS TEXT) "
            "ELSE 'document:' || CAST(r.document_id AS TEXT) END"
        )
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM (
                    SELECT 1
                    FROM review_items r
                    {where_sql}
                    GROUP BY {group_key}
                ) grouped_reviews
                """,
                params,
            ).fetchone()
        return int(row["total"] or 0) if row else 0

    def get_review_item(self, review_item_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM review_items WHERE id = ? LIMIT 1",
                (review_item_id,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def resolve_review_item(
        self,
        review_item_id: int,
        status: str = "resolved",
        resolution: Optional[str] = None,
        corrected_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        review_data = dict(corrected_data or {})
        if resolution:
            review_data["resolution"] = resolution
        self._update(
            "review_items",
            review_item_id,
            {
                "status": status,
                "corrected_data_json": self._json(review_data) if review_data else None,
                "updated_at": self._now(),
            },
        )

    def record_review_correction(self, payload: Dict[str, Any], preferred_id: Optional[int] = None) -> int:
        return self._insert(
            "review_corrections",
            {
                "id": preferred_id,
                "review_item_id": payload.get("reviewItemId"),
                "document_id": payload.get("documentId"),
                "original_data_json": self._json(payload.get("originalData")),
                "corrected_data_json": self._json(payload.get("correctedData")),
                "status": payload.get("status", "resolved"),
                "created_at": self._now(),
            },
        )

    def list_review_corrections(
        self,
        document_id: Optional[int] = None,
        limit: int = 100,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT * FROM review_corrections"
        params = []
        if document_id is not None:
            query = f"{query} WHERE document_id = ?"
            params.append(document_id)
        query = f"{query} ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_routing_attempts(
        self,
        status: Optional[Any] = None,
        target: Optional[str] = None,
        document_id: Optional[int] = None,
        bookkeeping_record_id: Optional[int] = None,
        limit: int = 100,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT * FROM routing_attempts"
        params = []
        where = []
        if status:
            if isinstance(status, Sequence) and not isinstance(status, str):
                statuses = [str(item) for item in status if item]
                if statuses:
                    placeholders = ", ".join("?" for _ in statuses)
                    where.append(f"status IN ({placeholders})")
                    params.extend(statuses)
            else:
                where.append("status = ?")
                params.append(status)
        if target:
            where.append("target = ?")
            params.append(target)
        if document_id is not None:
            where.append("document_id = ?")
            params.append(document_id)
        if bookkeeping_record_id is not None:
            where.append("(bookkeeping_record_id = ? OR metadata_json LIKE ? OR metadata_json LIKE ?)")
            params.extend([
                int(bookkeeping_record_id),
                f'%"bookkeepingRecordId": {int(bookkeeping_record_id)}%',
                f'%"bookkeeping_record_id": {int(bookkeeping_record_id)}%',
            ])
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        query = f"{query} ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_routing_attempts_without_export(
        self,
        status: Optional[Any] = None,
        limit: int = 100,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = """
            SELECT routing_attempts.*
            FROM routing_attempts
            WHERE NOT EXISTS (
                SELECT 1
                FROM export_attempts
                WHERE export_attempts.routing_attempt_id = routing_attempts.id
            )
        """
        params = []
        if status:
            if isinstance(status, Sequence) and not isinstance(status, str):
                statuses = [str(item) for item in status if item]
                if statuses:
                    placeholders = ", ".join("?" for _ in statuses)
                    query += f" AND routing_attempts.status IN ({placeholders})"
                    params.extend(statuses)
            else:
                query += " AND routing_attempts.status = ?"
                params.append(str(status))
        query += " ORDER BY routing_attempts.created_at DESC, routing_attempts.id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_reconciliation_match(self, reconciliation_match_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM reconciliation_matches WHERE id = ? LIMIT 1",
                (reconciliation_match_id,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def has_other_final_document_match(self, document_id: int, match_id: int) -> bool:
        with self._connection() as connection:
            return connection.execute(
                "SELECT 1 FROM reconciliation_matches WHERE document_id = ? AND id != ? "
                "AND status IN ('approved', 'reconciled', 'ignored') LIMIT 1",
                (document_id, match_id),
            ).fetchone() is not None

    def list_reconciliation_matches(
        self,
        status: Optional[Any] = None,
        document_id: Optional[int] = None,
        bank_transaction_id: Optional[str] = None,
        limit: int = 100,
        documentless_only: bool = False,
        bank_transaction_record_id: Optional[int] = None,
        ad_hoc_bank_only: bool = False,
        before_id: Optional[int] = None,
        keyset_order: bool = False,
        ad_hoc_account_identifier: Optional[str] = None,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT * FROM reconciliation_matches"
        params = []
        where = []
        if status:
            if isinstance(status, Sequence) and not isinstance(status, str):
                statuses = [str(item) for item in status if item]
                if statuses:
                    placeholders = ", ".join("?" for _ in statuses)
                    where.append(f"status IN ({placeholders})")
                    params.extend(statuses)
            else:
                where.append("status = ?")
                params.append(status)
        if document_id is not None:
            where.append("document_id = ?")
            params.append(document_id)
        elif documentless_only:
            where.append("document_id IS NULL")
        if bank_transaction_id:
            where.append("bank_transaction_id = ?")
            params.append(bank_transaction_id)
        if bank_transaction_record_id is not None:
            where.append(f"({RECONCILIATION_BANK_REFERENCE_SQL}) = ?")
            params.append(bank_transaction_record_id)
        elif ad_hoc_bank_only:
            where.append(f"({RECONCILIATION_BANK_REFERENCE_SQL}) IS NULL")
        if ad_hoc_account_identifier is not None:
            where.append(f"({RECONCILIATION_AD_HOC_ACCOUNT_SQL}) = ?")
            params.append(ad_hoc_account_identifier)
        if before_id is not None:
            where.append("id < ?")
            params.append(before_id)
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        ordering = "id DESC" if keyset_order else "created_at DESC, id DESC"
        query = f"{query} ORDER BY {ordering} LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def create_bank_statement_import(self, payload: Dict[str, Any], preferred_id: Optional[int] = None) -> int:
        now = self._now()
        return self._insert(
            "bank_statement_imports",
            {
                "id": preferred_id,
                "source": payload.get("source", "manual_import"),
                "account_identifier": payload.get("accountIdentifier") or payload.get("account_identifier") or "default",
                "filename": payload.get("filename"),
                "format": payload.get("format", "json"),
                "status": payload.get("status", "running"),
                "rows_seen": self._int(payload.get("rowsSeen") or payload.get("rows_seen"), 0),
                "rows_imported": self._int(payload.get("rowsImported") or payload.get("rows_imported"), 0),
                "duplicates": self._int(payload.get("duplicates"), 0),
                "metadata_json": self._json(self._redact_sensitive(payload.get("metadata"))),
                "created_at": now,
                "updated_at": now,
            },
        )

    def update_bank_statement_import(self, import_id: int, payload: Dict[str, Any]) -> None:
        self._update(
            "bank_statement_imports",
            import_id,
            {
                "source": payload.get("source"),
                "account_identifier": payload.get("accountIdentifier") or payload.get("account_identifier"),
                "filename": payload.get("filename"),
                "format": payload.get("format"),
                "status": payload.get("status"),
                "rows_seen": self._optional_int(self._first_present(payload.get("rowsSeen"), payload.get("rows_seen"))),
                "rows_imported": self._optional_int(self._first_present(payload.get("rowsImported"), payload.get("rows_imported"))),
                "duplicates": self._optional_int(payload.get("duplicates")),
                "metadata_json": self._json(self._redact_sensitive(payload.get("metadata"))),
                "updated_at": self._now(),
            },
        )

    def list_bank_statement_imports(
        self,
        account_identifier: Optional[str] = None,
        status: Optional[Any] = None,
        limit: int = 100,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT * FROM bank_statement_imports"
        params = []
        where = []
        if account_identifier:
            where.append("account_identifier = ?")
            params.append(account_identifier)
        if status:
            if isinstance(status, Sequence) and not isinstance(status, str):
                statuses = [str(item) for item in status if item]
                if statuses:
                    placeholders = ", ".join("?" for _ in statuses)
                    where.append(f"status IN ({placeholders})")
                    params.extend(statuses)
            else:
                where.append("status = ?")
                params.append(status)
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        query = f"{query} ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def upsert_bank_transaction(self, payload: Dict[str, Any], preferred_id: Optional[int] = None) -> int:
        account_identifier = str(
            payload.get("accountIdentifier") or payload.get("account_identifier") or "default"
        ).strip() or "default"
        transaction_id = str(payload.get("transactionId") or payload.get("transaction_id") or "").strip()
        if not transaction_id:
            raise ValueError("transactionId is required for a bank transaction")
        now = self._now()
        values = {
            "id": preferred_id,
            "import_id": payload.get("importId") or payload.get("import_id"),
            "account_identifier": account_identifier,
            "transaction_id": transaction_id,
            "transaction_date": self._date_text(
                self._first_present(
                    payload.get("transactionDate"),
                    payload.get("transaction_date"),
                    payload.get("date"),
                )
            ),
            "amount": self._float(self._first_present(payload.get("amount"), payload.get("transactionAmount"))),
            "currency": payload.get("currency") or "EUR",
            "description": payload.get("description"),
            "counterparty": payload.get("counterparty"),
            "status": payload.get("status", "imported"),
            "reconciliation_status": payload.get("reconciliationStatus") or payload.get("reconciliation_status") or "not_started",
            "duplicate_fingerprint": payload.get("duplicateFingerprint") or payload.get("duplicate_fingerprint"),
            "source": payload.get("source", "manual_import"),
            "metadata_json": self._json(self._redact_sensitive(payload.get("metadata"))),
            "created_at": now,
            "updated_at": now,
        }
        with self._connection() as connection:
            existing = connection.execute(
                """
                SELECT id FROM bank_transactions
                WHERE account_identifier = ? AND transaction_id = ?
                LIMIT 1
                """,
                (account_identifier, transaction_id),
            ).fetchone()
            if existing:
                transaction_record_id = int(existing["id"])
                update_values = dict(values)
                update_values.pop("id", None)
                update_values.pop("created_at", None)
                self._update_with_connection(connection, "bank_transactions", transaction_record_id, update_values)
                return transaction_record_id
            return self._insert_with_connection(connection, "bank_transactions", values)

    def update_bank_transaction(self, bank_transaction_id: int, payload: Dict[str, Any]) -> None:
        self._update(
            "bank_transactions",
            bank_transaction_id,
            {
                "import_id": payload.get("importId") or payload.get("import_id"),
                "account_identifier": payload.get("accountIdentifier") or payload.get("account_identifier"),
                "transaction_id": payload.get("transactionId") or payload.get("transaction_id"),
                "transaction_date": self._date_text(
                    self._first_present(
                        payload.get("transactionDate"),
                        payload.get("transaction_date"),
                        payload.get("date"),
                    )
                ),
                "amount": self._float(self._first_present(payload.get("amount"), payload.get("transactionAmount"))),
                "currency": payload.get("currency"),
                "description": payload.get("description"),
                "counterparty": payload.get("counterparty"),
                "status": payload.get("status"),
                "reconciliation_status": payload.get("reconciliationStatus") or payload.get("reconciliation_status"),
                "duplicate_fingerprint": payload.get("duplicateFingerprint") or payload.get("duplicate_fingerprint"),
                "source": payload.get("source"),
                "metadata_json": self._json(self._redact_sensitive(payload.get("metadata"))),
                "updated_at": self._now(),
            },
        )

    def get_bank_transaction(self, bank_transaction_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM bank_transactions WHERE id = ? LIMIT 1",
                (bank_transaction_id,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_bank_transaction_by_identity(
        self,
        account_identifier: str,
        transaction_id: str,
    ) -> Optional[Dict[str, Any]]:
        if not transaction_id:
            return None
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM bank_transactions
                WHERE account_identifier = ? AND transaction_id = ?
                LIMIT 1
                """,
                (account_identifier or "default", transaction_id),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_bank_transactions(
        self,
        account_identifier: Optional[str] = None,
        status: Optional[Any] = None,
        reconciliation_status: Optional[Any] = None,
        limit: int = 100,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT * FROM bank_transactions"
        params = []
        where = []
        if account_identifier:
            where.append("account_identifier = ?")
            params.append(account_identifier)
        if status:
            if isinstance(status, Sequence) and not isinstance(status, str):
                statuses = [str(item) for item in status if item]
                if statuses:
                    placeholders = ", ".join("?" for _ in statuses)
                    where.append(f"status IN ({placeholders})")
                    params.extend(statuses)
            else:
                where.append("status = ?")
                params.append(status)
        if reconciliation_status:
            if isinstance(reconciliation_status, Sequence) and not isinstance(reconciliation_status, str):
                statuses = [str(item) for item in reconciliation_status if item]
                if statuses:
                    placeholders = ", ".join("?" for _ in statuses)
                    where.append(f"reconciliation_status IN ({placeholders})")
                    params.extend(statuses)
            else:
                where.append("reconciliation_status = ?")
                params.append(reconciliation_status)
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        query = f"{query} ORDER BY transaction_date DESC, updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def upsert_vendor_category_rule(self, payload: Dict[str, Any]) -> int:
        vendor_name = str(payload.get("vendorName") or payload.get("vendor_name") or "").strip()
        category = str(payload.get("category") or "").strip()
        target_system = str(payload.get("targetSystem") or payload.get("target_system") or "none").strip() or "none"
        if not vendor_name or not category:
            raise ValueError("vendorName and category are required for a vendor category rule")
        normalized_vendor_name = self._normalize_text(vendor_name)
        now = self._now()
        confidence_score = self._float(payload.get("confidenceScore"))
        metadata = payload.get("metadata")
        source_document_id = payload.get("sourceDocumentId")
        status = str(payload.get("status", "learned") or "learned").strip()
        if status not in VENDOR_CATEGORY_RULE_STATUSES:
            raise ValueError(f"Unsupported vendor category rule status: {status}")

        with self._connection() as connection:
            existing = connection.execute(
                """
                SELECT * FROM vendor_category_rules
                WHERE normalized_vendor_name = ? AND category = ? AND target_system = ?
                LIMIT 1
                """,
                (normalized_vendor_name, category, target_system),
            ).fetchone()
            if existing:
                rule_id = int(existing["id"])
                existing_status = str(existing["status"] or "")
                effective_status = status
                if existing_status in GOVERNED_VENDOR_CATEGORY_RULE_STATUSES and status == "suggested":
                    effective_status = existing_status
                self._update_with_connection(
                    connection,
                    "vendor_category_rules",
                    rule_id,
                    {
                        "vendor_name": vendor_name,
                        "confidence_score": confidence_score,
                        "status": effective_status,
                        "source_document_id": source_document_id,
                        "usage_count": int(existing["usage_count"] or 0) + 1,
                        "metadata_json": self._json(metadata),
                        "updated_at": now,
                    },
                )
                return rule_id
            return self._insert_with_connection(
                connection,
                "vendor_category_rules",
                {
                    "normalized_vendor_name": normalized_vendor_name,
                    "vendor_name": vendor_name,
                    "category": category,
                    "target_system": target_system,
                    "confidence_score": confidence_score,
                    "status": status,
                    "source_document_id": source_document_id,
                    "usage_count": 1,
                    "metadata_json": self._json(metadata),
                    "created_at": now,
                    "updated_at": now,
                },
            )

    def get_vendor_category_rule(self, rule_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM vendor_category_rules WHERE id = ? LIMIT 1",
                (rule_id,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def update_vendor_category_rule_status(
        self,
        rule_id: int,
        status: str,
        resolution: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        status = str(status or "").strip()
        if status not in VENDOR_CATEGORY_RULE_STATUSES:
            raise ValueError(f"Unsupported vendor category rule status: {status}")
        now = self._now()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM vendor_category_rules WHERE id = ? LIMIT 1",
                (rule_id,),
            ).fetchone()
            if not row:
                return None
            rule = self._row_to_dict(row)
            metadata = rule.get("metadata") or {}
            history = list(metadata.get("statusHistory") or [])
            history.append({
                "from": rule.get("status"),
                "to": status,
                "resolution": resolution,
                "actor": actor,
                "changedAt": now,
            })
            metadata["statusHistory"] = history
            if resolution:
                metadata["lastResolution"] = resolution
            if actor:
                metadata["lastActor"] = actor
            self._update_with_connection(
                connection,
                "vendor_category_rules",
                rule_id,
                {
                    "status": status,
                    "metadata_json": self._json(metadata),
                    "updated_at": now,
                },
            )
            updated = connection.execute(
                "SELECT * FROM vendor_category_rules WHERE id = ? LIMIT 1",
                (rule_id,),
            ).fetchone()
        return self._row_to_dict(updated) if updated else None

    def list_vendor_category_rules(
        self,
        vendor_name: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list:
        limit = self._bounded_limit(limit)
        query = "SELECT * FROM vendor_category_rules"
        params = []
        where = []
        if vendor_name:
            where.append("normalized_vendor_name = ?")
            params.append(self._normalize_text(vendor_name))
        if status:
            where.append("status = ?")
            params.append(status)
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        query = f"{query} ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_audit_events(self, limit: int = 100) -> list:
        limit = self._bounded_limit(limit)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_documents_for_source_account(
        self,
        source_account_id: int,
        *,
        after_id: int = 0,
        limit: int = 500,
    ) -> list:
        bounded_limit = self._bounded_limit(limit)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM bookkeeping_documents
                WHERE source_account_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (int(source_account_id), max(0, int(after_id)), bounded_limit),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def find_audit_event(
        self,
        action: str,
        entity_type: str,
        entity_id: str,
    ) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM audit_events
                WHERE action = ? AND entity_type = ? AND entity_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (str(action), str(entity_type), str(entity_id)),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def find_latest_audit_event(
        self,
        action: str,
        entity_type: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        query = "SELECT * FROM audit_events WHERE action = ?"
        params = [str(action)]
        if entity_type:
            query += " AND entity_type = ?"
            params.append(str(entity_type))
        query += " ORDER BY created_at DESC, id DESC LIMIT 1"
        with self._connection() as connection:
            row = connection.execute(query, params).fetchone()
        return self._row_to_dict(row) if row else None

    def find_document_by_content_hash(
        self,
        content_sha256: str,
        exclude_source_document_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not content_sha256:
            return None
        query = """
            SELECT * FROM bookkeeping_documents
            WHERE content_sha256 = ?
        """
        params = [content_sha256]
        if exclude_source_document_id:
            query = f"{query} AND COALESCE(source_document_id, '') != ?"
            params.append(exclude_source_document_id)
        query = f"{query} ORDER BY id ASC LIMIT 1"
        with self._connection() as connection:
            row = connection.execute(query, params).fetchone()
        return self._row_to_dict(row) if row else None

    def repair_false_source_revisions(self, actor: str = "local_operator") -> Dict[str, Any]:
        """Remove unprocessed revision rows whose bytes equal their source parent."""

        repaired = []
        skipped = []
        with self._connection() as connection:
            revisions = connection.execute(
                """
                SELECT * FROM bookkeeping_documents
                WHERE source_document_id LIKE '%:revision:%'
                ORDER BY id ASC
                """
            ).fetchall()
            for row in revisions:
                child = self._row_to_dict(row)
                metadata = child.get("metadata") if isinstance(child.get("metadata"), dict) else {}
                source_revision = (
                    metadata.get("sourceRevision")
                    if isinstance(metadata.get("sourceRevision"), dict)
                    else {}
                )
                parent_id = source_revision.get("revisionOfDocumentId")
                parent = connection.execute(
                    "SELECT * FROM bookkeeping_documents WHERE id = ? LIMIT 1",
                    (parent_id,),
                ).fetchone() if parent_id else None
                reason = self._false_revision_repair_reason(connection, child, parent)
                if reason:
                    skipped.append({"documentId": child["id"], "reason": reason})
                    continue

                review_rows = connection.execute(
                    "SELECT id FROM review_items WHERE document_id = ?",
                    (child["id"],),
                ).fetchall()
                connection.execute("DELETE FROM review_items WHERE document_id = ?", (child["id"],))
                connection.execute("DELETE FROM bookkeeping_documents WHERE id = ?", (child["id"],))
                details = {
                    "actor": str(actor or "local_operator")[:200],
                    "removedDocumentId": child["id"],
                    "retainedDocumentId": int(parent["id"]),
                    "source": child.get("source"),
                    "sourceDocumentId": child.get("source_document_id"),
                    "contentSha256": child.get("content_sha256"),
                    "removedReviewItemIds": [int(item["id"]) for item in review_rows],
                    "localEvidenceDeleted": False,
                    "externalSubmission": "not_executed",
                }
                connection.execute(
                    """
                    INSERT INTO audit_events
                      (action, entity_type, entity_id, details_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        "local_ledger.false_source_revision_repaired",
                        "bookkeeping_document",
                        str(child["id"]),
                        self._json(self._redact_sensitive(details)),
                        self._now(),
                    ),
                )
                repaired.append(details)
        return {
            "success": True,
            "candidates": len(repaired) + len(skipped),
            "repaired": len(repaired),
            "skipped": skipped,
            "repairs": repaired,
            "externalSubmission": "not_executed",
        }

    def _insert(self, table: str, values: Dict[str, Any]) -> int:
        with self._connection() as connection:
            return self._insert_with_connection(connection, table, values)

    @staticmethod
    def _insert_with_connection(connection: sqlite3.Connection, table: str, values: Dict[str, Any]) -> int:
        values = {key: value for key, value in values.items() if value is not None}
        columns = ", ".join(values.keys())
        placeholders = ", ".join("?" for _ in values)
        cursor = connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )
        return int(values.get("id") or cursor.lastrowid)

    def _update(self, table: str, record_id: int, values: Dict[str, Any]) -> None:
        with self._connection() as connection:
            self._update_with_connection(connection, table, record_id, values)

    @staticmethod
    def _update_with_connection(
        connection: sqlite3.Connection,
        table: str,
        record_id: int,
        values: Dict[str, Any],
    ) -> None:
        values = {key: value for key, value in values.items() if value is not None}
        if not values:
            return
        assignments = ", ".join(f"{key} = ?" for key in values)
        connection.execute(
            f"UPDATE {table} SET {assignments} WHERE id = ?",
            (*values.values(), record_id),
        )

    @staticmethod
    def _existing_document_id(
        connection: sqlite3.Connection,
        source: str,
        source_document_id: Optional[str],
    ) -> Optional[int]:
        if not source_document_id:
            return None
        row = connection.execute(
            """
            SELECT id FROM bookkeeping_documents
            WHERE source = ? AND source_document_id = ?
            LIMIT 1
            """,
            (source, source_document_id),
        ).fetchone()
        return int(row["id"]) if row else None

    @staticmethod
    def _existing_bookkeeping_record_id(
        connection: sqlite3.Connection,
        document_id: Optional[int],
        bank_transaction_id: Optional[int],
    ) -> Optional[int]:
        if document_id is not None:
            row = connection.execute(
                "SELECT id FROM bookkeeping_records WHERE document_id = ? LIMIT 1",
                (document_id,),
            ).fetchone()
            if row:
                return int(row["id"])
        if bank_transaction_id is not None:
            row = connection.execute(
                "SELECT id FROM bookkeeping_records WHERE bank_transaction_id = ? LIMIT 1",
                (bank_transaction_id,),
            ).fetchone()
            if row:
                return int(row["id"])
        return None

    @staticmethod
    def _existing_export_attempt_id(
        connection: sqlite3.Connection,
        routing_attempt_id: Optional[int],
        operation_id: Optional[str],
    ) -> Optional[int]:
        if routing_attempt_id is not None:
            row = connection.execute(
                "SELECT id FROM export_attempts WHERE routing_attempt_id = ? LIMIT 1",
                (routing_attempt_id,),
            ).fetchone()
            if row:
                return int(row["id"])
        if operation_id:
            row = connection.execute(
                "SELECT id FROM export_attempts WHERE operation_id = ? LIMIT 1",
                (operation_id,),
            ).fetchone()
            if row:
                return int(row["id"])
        return None

    @staticmethod
    def _count(connection: sqlite3.Connection, table: str, where: Optional[str] = None) -> int:
        query = f"SELECT COUNT(*) AS count FROM {table}"
        if where:
            query = f"{query} WHERE {where}"
        row = connection.execute(query).fetchone()
        return int(row["count"] if row else 0)

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @classmethod
    def _backfill_document_content_hashes(cls, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT id, metadata_json
            FROM bookkeeping_documents
            WHERE content_sha256 IS NULL OR content_sha256 = ''
            """
        ).fetchall()
        for row in rows:
            metadata = cls._json_load(row["metadata_json"])
            content_hash = metadata.get("contentSha256") if isinstance(metadata, dict) else None
            if isinstance(content_hash, str) and re.fullmatch(r"[0-9a-fA-F]{64}", content_hash):
                connection.execute(
                    "UPDATE bookkeeping_documents SET content_sha256 = ? WHERE id = ?",
                    (content_hash.lower(), int(row["id"])),
                )

    @classmethod
    def _false_revision_repair_reason(
        cls,
        connection: sqlite3.Connection,
        child: Dict[str, Any],
        parent_row: Optional[sqlite3.Row],
    ) -> Optional[str]:
        if parent_row is None:
            return "parent_missing"
        parent = cls._row_to_dict(parent_row)
        child_hash = str(child.get("content_sha256") or "").lower()
        parent_hash = str(parent.get("content_sha256") or "").lower()
        expected_id = f"{parent.get('source_document_id')}:revision:{child_hash[:16]}"
        if not child_hash or child_hash != parent_hash:
            return "content_differs"
        if child.get("source") != parent.get("source") or child.get("source_document_id") != expected_id:
            return "source_identity_mismatch"
        if child.get("processing_status") != "needs_review" or child.get("ocr_text"):
            return "document_already_processed"
        if child.get("extracted_data"):
            return "document_already_processed"
        if child.get("duplicate_of_document_id") is not None:
            return "duplicate_relationship_present"

        reviews = connection.execute(
            "SELECT reason, status FROM review_items WHERE document_id = ?",
            (child["id"],),
        ).fetchall()
        if not reviews or any(
            review["reason"] != "source_revision_detected"
            or review["status"] not in {"pending", "in_review"}
            for review in reviews
        ):
            return "review_state_not_repairable"

        reference_checks = (
            ("bookkeeping_records", "document_id"),
            ("compliance_findings", "document_id"),
            ("document_group_members", "document_id"),
            ("duplicate_candidates", "document_id"),
            ("duplicate_candidates", "candidate_document_id"),
            ("export_attempts", "document_id"),
            ("extracted_fields", "document_id"),
            ("reconciliation_matches", "document_id"),
            ("retention_records", "document_id"),
            ("review_corrections", "document_id"),
            ("routing_attempts", "document_id"),
            ("bookkeeping_documents", "duplicate_of_document_id"),
        )
        for table, column in reference_checks:
            if connection.execute(
                f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1",
                (child["id"],),
            ).fetchone():
                return f"referenced_by_{table}"
        return None

    def _ensure_reconciliation_lookup_indexes(self, connection: sqlite3.Connection) -> None:
        # Keep expressions identical to lookup predicates so SQLite can use the indexes.
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_local_missing_review_match "
            f"ON review_items (({MISSING_REVIEW_MATCH_REFERENCE_SQL}), id DESC) "
            "WHERE document_id IS NULL AND reason = 'missing_receipt'"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_local_reconciliation_bank_record "
            f"ON reconciliation_matches (bank_transaction_id, ({RECONCILIATION_BANK_REFERENCE_SQL}), "
            "document_id, id DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_local_reconciliation_ad_hoc_account "
            f"ON reconciliation_matches (bank_transaction_id, ({RECONCILIATION_AD_HOC_ACCOUNT_SQL}), "
            "document_id, created_at DESC, id DESC) "
            f"WHERE ({RECONCILIATION_BANK_REFERENCE_SQL}) IS NULL"
        )

    def _ensure_review_item_schema(self, connection: sqlite3.Connection) -> None:
        duplicate_groups = connection.execute(
            """
            SELECT document_id, reason, MAX(id) AS keep_id, COUNT(*) AS item_count
            FROM review_items
            WHERE document_id IS NOT NULL
              AND status IN ('pending', 'in_review')
            GROUP BY document_id, reason
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        for group in duplicate_groups:
            superseded_rows = connection.execute(
                """
                SELECT id, corrected_data_json
                FROM review_items
                WHERE document_id = ? AND reason = ?
                  AND status IN ('pending', 'in_review') AND id != ?
                ORDER BY id ASC
                """,
                (group["document_id"], group["reason"], group["keep_id"]),
            ).fetchall()
            superseded_ids = []
            for row in superseded_rows:
                corrected_data = self._json_load(row["corrected_data_json"]) or {}
                if not isinstance(corrected_data, dict):
                    corrected_data = {"previousCorrectedData": corrected_data}
                corrected_data.update({
                    "resolution": "Superseded by the newest equivalent open review item.",
                    "supersededByReviewItemId": int(group["keep_id"]),
                })
                connection.execute(
                    """
                    UPDATE review_items
                    SET status = 'resolved', corrected_data_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (self._json(corrected_data), self._now(), row["id"]),
                )
                superseded_ids.append(int(row["id"]))
            if superseded_ids:
                connection.execute(
                    """
                    INSERT INTO audit_events
                      (action, entity_type, entity_id, details_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        "local_ledger.open_reviews_deduplicated",
                        "bookkeeping_document",
                        str(group["document_id"]),
                        self._json({
                            "reason": group["reason"],
                            "keptReviewItemId": int(group["keep_id"]),
                            "supersededReviewItemIds": superseded_ids,
                        }),
                        self._now(),
                    ),
                )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_local_review_open_document_reason
            ON review_items(document_id, reason)
            WHERE document_id IS NOT NULL AND status IN ('pending', 'in_review')
            """
        )

    def _ensure_export_attempt_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS export_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bookkeeping_record_id INTEGER,
                document_id INTEGER,
                routing_attempt_id INTEGER,
                workflow_run_id INTEGER,
                target_system TEXT NOT NULL DEFAULT 'waveapps',
                target_account TEXT,
                action_id TEXT,
                surface TEXT,
                operation_id TEXT,
                status TEXT NOT NULL DEFAULT 'approval_required',
                safety TEXT NOT NULL DEFAULT 'requires_confirmation',
                approval_required INTEGER NOT NULL DEFAULT 1,
                approved_at TEXT,
                approved_by TEXT,
                external_submission TEXT NOT NULL DEFAULT 'not_executed',
                submitted_at TEXT,
                external_id TEXT,
                message TEXT,
                payload_json TEXT,
                result_json TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(export_attempts)").fetchall()
        }
        required_columns = {
            "bookkeeping_record_id": "INTEGER",
            "document_id": "INTEGER",
            "routing_attempt_id": "INTEGER",
            "workflow_run_id": "INTEGER",
            "target_system": "TEXT NOT NULL DEFAULT 'waveapps'",
            "target_account": "TEXT",
            "action_id": "TEXT",
            "surface": "TEXT",
            "operation_id": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'approval_required'",
            "safety": "TEXT NOT NULL DEFAULT 'requires_confirmation'",
            "approval_required": "INTEGER NOT NULL DEFAULT 1",
            "approved_at": "TEXT",
            "approved_by": "TEXT",
            "external_submission": "TEXT NOT NULL DEFAULT 'not_executed'",
            "submitted_at": "TEXT",
            "external_id": "TEXT",
            "message": "TEXT",
            "payload_json": "TEXT",
            "result_json": "TEXT",
            "metadata_json": "TEXT",
            "created_at": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in required_columns.items():
            if column not in columns:
                connection.execute(f"ALTER TABLE export_attempts ADD COLUMN {column} {definition}")
        connection.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_local_exports_routing_unique
                ON export_attempts(routing_attempt_id)
                WHERE routing_attempt_id IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_local_exports_operation_unique
                ON export_attempts(operation_id)
                WHERE operation_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_local_exports_status
                ON export_attempts(status);
            CREATE INDEX IF NOT EXISTS idx_local_exports_external
                ON export_attempts(external_submission);
            CREATE INDEX IF NOT EXISTS idx_local_exports_target
                ON export_attempts(target_system);
            CREATE INDEX IF NOT EXISTS idx_local_exports_document
                ON export_attempts(document_id);
            """
        )

    def _ensure_routing_attempt_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS routing_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER,
                bookkeeping_record_id INTEGER,
                workflow_run_id INTEGER,
                target TEXT NOT NULL,
                status TEXT NOT NULL,
                external_id TEXT,
                message TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        self._ensure_column(
            connection,
            "routing_attempts",
            "bookkeeping_record_id",
            "INTEGER",
        )
        connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_local_routing_status
                ON routing_attempts(status);
            CREATE INDEX IF NOT EXISTS idx_local_routing_target
                ON routing_attempts(target);
            CREATE INDEX IF NOT EXISTS idx_local_routing_record
                ON routing_attempts(bookkeeping_record_id);
            """
        )

    def _ensure_bookkeeping_record_schema(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(bookkeeping_records)").fetchall()
        }
        required_columns = {
            "document_id": "INTEGER",
            "bank_transaction_id": "INTEGER",
            "source_type": "TEXT NOT NULL DEFAULT 'document'",
            "record_type": "TEXT NOT NULL DEFAULT 'expense'",
            "status": "TEXT NOT NULL DEFAULT 'draft'",
            "target_system": "TEXT NOT NULL DEFAULT 'waveapps'",
            "target_account": "TEXT",
            "vendor_name": "TEXT",
            "category": "TEXT",
            "record_date": "TEXT",
            "amount": "REAL",
            "vat_amount": "REAL",
            "currency": "TEXT NOT NULL DEFAULT 'EUR'",
            "description": "TEXT",
            "confidence_score": "REAL",
            "review_required": "INTEGER NOT NULL DEFAULT 0",
            "export_status": "TEXT NOT NULL DEFAULT 'not_started'",
            "reconciliation_status": "TEXT NOT NULL DEFAULT 'not_started'",
            "metadata_json": "TEXT",
            "created_at": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in required_columns.items():
            if column not in columns:
                connection.execute(f"ALTER TABLE bookkeeping_records ADD COLUMN {column} {definition}")
        connection.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_local_records_document_unique
                ON bookkeeping_records(document_id)
                WHERE document_id IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_local_records_bank_unique
                ON bookkeeping_records(bank_transaction_id)
                WHERE bank_transaction_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_local_records_status
                ON bookkeeping_records(status);
            CREATE INDEX IF NOT EXISTS idx_local_records_target
                ON bookkeeping_records(target_system);
            CREATE INDEX IF NOT EXISTS idx_local_records_export
                ON bookkeeping_records(export_status);
            CREATE INDEX IF NOT EXISTS idx_local_records_reconciliation
                ON bookkeeping_records(reconciliation_status);
            """
        )

    def _ensure_bookkeeping_record_line_item_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bookkeeping_record_line_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bookkeeping_record_id INTEGER NOT NULL,
                line_index INTEGER NOT NULL DEFAULT 0,
                item_name TEXT,
                description TEXT,
                quantity REAL,
                unit_price REAL,
                amount REAL,
                tax_amount REAL,
                tax_rate REAL,
                tax_code TEXT,
                category TEXT,
                account_name TEXT,
                source TEXT NOT NULL DEFAULT 'extraction',
                confidence_score REAL,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(bookkeeping_record_id, line_index),
                FOREIGN KEY(bookkeeping_record_id) REFERENCES bookkeeping_records(id) ON DELETE CASCADE
            )
            """
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(bookkeeping_record_line_items)").fetchall()
        }
        required_columns = {
            "bookkeeping_record_id": "INTEGER NOT NULL DEFAULT 0",
            "line_index": "INTEGER NOT NULL DEFAULT 0",
            "item_name": "TEXT",
            "description": "TEXT",
            "quantity": "REAL",
            "unit_price": "REAL",
            "amount": "REAL",
            "tax_amount": "REAL",
            "tax_rate": "REAL",
            "tax_code": "TEXT",
            "category": "TEXT",
            "account_name": "TEXT",
            "source": "TEXT NOT NULL DEFAULT 'extraction'",
            "confidence_score": "REAL",
            "metadata_json": "TEXT",
            "created_at": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in required_columns.items():
            if column not in columns:
                connection.execute(f"ALTER TABLE bookkeeping_record_line_items ADD COLUMN {column} {definition}")
        connection.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_local_record_lines_unique
                ON bookkeeping_record_line_items(bookkeeping_record_id, line_index);
            CREATE INDEX IF NOT EXISTS idx_local_record_lines_record
                ON bookkeeping_record_line_items(bookkeeping_record_id);
            CREATE INDEX IF NOT EXISTS idx_local_record_lines_account
                ON bookkeeping_record_line_items(account_name);
            CREATE INDEX IF NOT EXISTS idx_local_record_lines_tax
                ON bookkeeping_record_line_items(tax_code);
            """
        )

    @classmethod
    def _bookkeeping_record_values(
        cls,
        payload: Dict[str, Any],
        now: str,
        include_defaults: bool,
    ) -> Dict[str, Any]:
        default = (lambda value: value) if include_defaults else (lambda value: None)
        review_required = cls._payload_value(payload, "reviewRequired", "review_required")
        if review_required is None and include_defaults:
            review_required = 0
        return {
            "document_id": cls._optional_int(cls._payload_value(payload, "documentId", "document_id")),
            "bank_transaction_id": cls._optional_int(cls._payload_value(
                payload,
                "bankTransactionId",
                "bank_transaction_id",
            )),
            "source_type": cls._payload_value(payload, "sourceType", "source_type", default=default("document")),
            "record_type": cls._payload_value(payload, "recordType", "record_type", default=default("expense")),
            "status": cls._payload_value(payload, "status", default=default("draft")),
            "target_system": cls._payload_value(payload, "targetSystem", "target_system", default=default("waveapps")),
            "target_account": cls._payload_value(payload, "targetAccount", "target_account"),
            "vendor_name": cls._payload_value(payload, "vendorName", "vendor_name"),
            "category": cls._payload_value(payload, "category"),
            "record_date": cls._date_text(cls._payload_value(payload, "recordDate", "record_date", "transactionDate", "transaction_date")),
            "amount": cls._float(cls._payload_value(payload, "amount", "totalAmount", "total_amount")),
            "vat_amount": cls._float(cls._payload_value(payload, "vatAmount", "vat_amount")),
            "currency": cls._payload_value(payload, "currency", default=default("EUR")),
            "description": cls._payload_value(payload, "description"),
            "confidence_score": cls._float(cls._payload_value(payload, "confidenceScore", "confidence_score")),
            "review_required": cls._bool_int(review_required) if review_required is not None else None,
            "export_status": cls._payload_value(payload, "exportStatus", "export_status", default=default("not_started")),
            "reconciliation_status": cls._payload_value(
                payload,
                "reconciliationStatus",
                "reconciliation_status",
                default=default("not_started"),
            ),
            "metadata_json": cls._json(cls._redact_sensitive(cls._payload_value(payload, "metadata", "metadata_json"))),
        }

    @classmethod
    def _bookkeeping_line_item_values(
        cls,
        payload: Dict[str, Any],
        bookkeeping_record_id: int,
        line_index: int,
        now: str,
    ) -> Dict[str, Any]:
        return {
            "bookkeeping_record_id": bookkeeping_record_id,
            "line_index": cls._optional_int(cls._payload_value(payload, "lineIndex", "line_index")) or line_index,
            "item_name": cls._payload_value(payload, "itemName", "item_name", "name", "item"),
            "description": cls._payload_value(payload, "description"),
            "quantity": cls._float(cls._payload_value(payload, "quantity", "qty")),
            "unit_price": cls._float(cls._payload_value(payload, "unitPrice", "unit_price", "price")),
            "amount": cls._float(cls._payload_value(payload, "amount", "totalAmount", "total_amount")),
            "tax_amount": cls._float(cls._payload_value(
                payload,
                "taxAmount",
                "tax_amount",
                "vatAmount",
                "vat_amount",
            )),
            "tax_rate": cls._float(cls._payload_value(
                payload,
                "taxRate",
                "tax_rate",
                "vatRate",
                "vat_rate",
            )),
            "tax_code": cls._payload_value(payload, "taxCode", "tax_code", "salesTax", "sales_tax", "tax"),
            "category": cls._payload_value(payload, "category"),
            "account_name": cls._payload_value(payload, "accountName", "account_name", "account", "targetAccount"),
            "source": cls._payload_value(payload, "source", default="extraction"),
            "confidence_score": cls._float(cls._payload_value(payload, "confidenceScore", "confidence_score")),
            "metadata_json": cls._json(cls._redact_sensitive(cls._payload_value(payload, "metadata", "metadata_json"))),
            "created_at": now,
            "updated_at": now,
        }

    @classmethod
    def _export_attempt_values(
        cls,
        payload: Dict[str, Any],
        now: str,
        include_defaults: bool,
    ) -> Dict[str, Any]:
        default = (lambda value: value) if include_defaults else (lambda value: None)
        approval_required = cls._payload_value(payload, "approvalRequired", "approval_required")
        if approval_required is None and include_defaults:
            approval_required = 1
        return {
            "bookkeeping_record_id": cls._optional_int(cls._payload_value(
                payload,
                "bookkeepingRecordId",
                "bookkeeping_record_id",
            )),
            "document_id": cls._optional_int(cls._payload_value(payload, "documentId", "document_id")),
            "routing_attempt_id": cls._optional_int(cls._payload_value(
                payload,
                "routingAttemptId",
                "routing_attempt_id",
            )),
            "workflow_run_id": cls._optional_int(cls._payload_value(payload, "workflowRunId", "workflow_run_id")),
            "target_system": cls._payload_value(payload, "targetSystem", "target_system", default=default("waveapps")),
            "target_account": cls._payload_value(payload, "targetAccount", "target_account"),
            "action_id": cls._payload_value(payload, "actionId", "action_id"),
            "surface": cls._payload_value(payload, "surface"),
            "operation_id": cls._payload_value(payload, "operationId", "operation_id"),
            "status": cls._payload_value(payload, "status", default=default("approval_required")),
            "safety": cls._payload_value(payload, "safety", default=default("requires_confirmation")),
            "approval_required": cls._bool_int(approval_required) if approval_required is not None else None,
            "approved_at": cls._date_text(cls._payload_value(payload, "approvedAt", "approved_at")),
            "approved_by": cls._payload_value(payload, "approvedBy", "approved_by"),
            "external_submission": cls._payload_value(
                payload,
                "externalSubmission",
                "external_submission",
                default=default("not_executed"),
            ),
            "submitted_at": cls._date_text(cls._payload_value(payload, "submittedAt", "submitted_at")),
            "external_id": cls._payload_value(payload, "externalId", "external_id"),
            "message": cls._payload_value(payload, "message"),
            "payload_json": cls._json(cls._redact_sensitive(cls._payload_value(payload, "payload", "payload_json"))),
            "result_json": cls._json(cls._redact_sensitive(cls._payload_value(payload, "result", "result_json"))),
            "metadata_json": cls._json(cls._redact_sensitive(cls._payload_value(payload, "metadata", "metadata_json"))),
        }

    @staticmethod
    def _payload_value(payload: Dict[str, Any], *keys: str, default: Any = None) -> Any:
        for key in keys:
            if key in payload:
                return payload.get(key)
        return default

    @staticmethod
    def _append_status_filter(where: list, params: list, column: str, value: Optional[Any]) -> None:
        if not value:
            return
        if isinstance(value, Sequence) and not isinstance(value, str):
            statuses = [str(item) for item in value if item]
            if statuses:
                placeholders = ", ".join("?" for _ in statuses)
                where.append(f"{column} IN ({placeholders})")
                params.extend(statuses)
            return
        where.append(f"{column} = ?")
        params.append(str(value))

    @classmethod
    def _row_to_dict(cls, row: sqlite3.Row) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key in row.keys():
            value = row[key]
            if key.endswith("_json"):
                result[key[:-5]] = cls._json_load(value)
            else:
                result[key] = value
        return result

    @classmethod
    def _bookkeeping_record_with_line_items(
        cls,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> Dict[str, Any]:
        record = cls._row_to_dict(row)
        line_rows = connection.execute(
            """
            SELECT * FROM bookkeeping_record_line_items
            WHERE bookkeeping_record_id = ?
            ORDER BY line_index ASC, id ASC
            """,
            (record["id"],),
        ).fetchall()
        record["line_items"] = [cls._row_to_dict(line_row) for line_row in line_rows]
        record["line_item_count"] = len(record["line_items"])
        return record

    @classmethod
    def _bookkeeping_records_with_line_items(
        cls,
        connection: sqlite3.Connection,
        rows: Sequence[sqlite3.Row],
    ) -> list[Dict[str, Any]]:
        records = [cls._row_to_dict(row) for row in rows]
        if not records:
            return []

        records_by_id = {}
        for record in records:
            record["line_items"] = []
            record["line_item_count"] = 0
            records_by_id[int(record["id"])] = record

        record_ids = list(records_by_id)
        placeholders = ", ".join("?" for _ in record_ids)
        line_rows = connection.execute(
            f"""
            SELECT * FROM bookkeeping_record_line_items
            WHERE bookkeeping_record_id IN ({placeholders})
            ORDER BY bookkeeping_record_id ASC, line_index ASC, id ASC
            """,
            record_ids,
        ).fetchall()
        for row in line_rows:
            line_item = cls._row_to_dict(row)
            record = records_by_id.get(int(line_item["bookkeeping_record_id"]))
            if record is not None:
                record["line_items"].append(line_item)
                record["line_item_count"] += 1
        return records

    @classmethod
    def _document_group_with_members(
        cls,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> Dict[str, Any]:
        group = cls._row_to_dict(row)
        member_rows = connection.execute(
            """
            SELECT
                m.*,
                d.original_filename,
                d.source,
                d.source_document_id,
                d.processing_status,
                d.storage_path
            FROM document_group_members m
            LEFT JOIN bookkeeping_documents d ON d.id = m.document_id
            WHERE m.group_id = ?
            ORDER BY m.status ASC, m.sort_order ASC, m.id ASC
            """,
            (group["id"],),
        ).fetchall()
        members = []
        for member_row in member_rows:
            member = cls._row_to_dict(member_row)
            member["document"] = {
                "id": member.get("document_id"),
                "original_filename": member.pop("original_filename", None),
                "source": member.pop("source", None),
                "source_document_id": member.pop("source_document_id", None),
                "processing_status": member.pop("processing_status", None),
                "storage_path": member.pop("storage_path", None),
            }
            members.append(member)
        group["members"] = members
        group["member_count"] = len([member for member in members if member.get("status") == "active"])
        return group

    @staticmethod
    def _json(value: Any) -> Optional[str]:
        if value is None:
            return None
        return json.dumps(value, sort_keys=True, default=LocalOperationsLedger._json_default)

    @staticmethod
    def _json_load(value: Optional[str]) -> Any:
        if value is None or value == "":
            return None
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value

    @staticmethod
    def _json_default(value: Any) -> str:
        if isinstance(value, bytes):
            return f"<bytes:{len(value)}>"
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return str(value)

    @classmethod
    def _redact_sensitive(cls, value: Any) -> Any:
        secret_markers = (
            "token",
            "password",
            "passwd",
            "secret",
            "credential",
            "authorization",
            "api_key",
            "apikey",
            "cookie",
            "database_url",
            "connection_string",
            "dsn",
        )
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                key_text = str(key)
                if any(marker in key_text.lower() for marker in secret_markers):
                    result[key] = "<redacted>"
                else:
                    result[key] = cls._redact_sensitive(item)
            return result
        if isinstance(value, list):
            return [cls._redact_sensitive(item) for item in value]
        if isinstance(value, tuple):
            return [cls._redact_sensitive(item) for item in value]
        if isinstance(value, str):
            value = re.sub(
                r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+@",
                r"\1[REDACTED]@",
                value,
            )
            value = re.sub(
                r'''(?i)(["'](?:access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|api[_-]?key|apikey|secret|password|passwd|token|database[_-]?url|connection[_-]?string|dsn)["']\s*:\s*)(?:"[^"]*"|'[^']*'|[^,}\s]+)''',
                r'\1"[REDACTED]"',
                value,
            )
            value = re.sub(
                r"(?i)((?:access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|token|password|passwd|secret|(?:x[_-]?)?api[_-]?key|database[_-]?url|connection[_-]?string|dsn)\s*[:=]\s*)[^&,;\s]+",
                r"\1[REDACTED]",
                value,
            )
            value = re.sub(
                r"(?i)(Authorization\s*:\s*(?:Bearer|Basic)\s+)[^\s,;]+",
                r"\1[REDACTED]",
                value,
            )
            value = re.sub(
                r"(?i)((?:set-cookie|cookie)\s*:\s*)[^\r\n,]+",
                r"\1[REDACTED]",
                value,
            )
        return value

    @classmethod
    def _redact_error_message(cls, value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        return str(cls._redact_sensitive(str(value)))[:2000]

    @staticmethod
    def _float(value: Any) -> Optional[float]:
        if value is None or isinstance(value, bool) or value == "":
            return None
        try:
            number = float(value)
            return number if math.isfinite(number) else None
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _bool_int(value: Any) -> int:
        if isinstance(value, str):
            return 0 if value.strip().lower() in {"", "0", "false", "no", "off"} else 1
        return 1 if bool(value) else 0

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _nonnegative_optional_int(cls, value: Any) -> Optional[int]:
        parsed = cls._optional_int(value)
        return max(0, parsed) if parsed is not None else None

    @staticmethod
    def _first_present(*values: Any) -> Any:
        for value in values:
            if value is not None and value != "":
                return value
        return None

    @classmethod
    def _bounded_limit(cls, value: Any) -> int:
        parsed = cls._int(value, 100)
        return max(1, min(parsed, 500))

    @staticmethod
    def _date_text(value: Any) -> Optional[str]:
        if value is None or value == "":
            return None
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return str(value)

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
        if value in (None, ""):
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _public_runtime_lease(cls, lease: Dict[str, Any], now: datetime) -> Dict[str, Any]:
        expires_at = cls._parse_datetime(lease.get("expires_at"))
        return {
            "leaseName": lease.get("lease_name"),
            "active": bool(expires_at and expires_at > now),
            "acquiredAt": lease.get("acquired_at"),
            "heartbeatAt": lease.get("heartbeat_at"),
            "expiresAt": lease.get("expires_at"),
            "metadata": lease.get("metadata") or {},
        }

    @staticmethod
    def _normalize_text(value: str) -> str:
        return " ".join(str(value or "").strip().lower().split())

    @classmethod
    def _normalized_field_value(cls, value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        if isinstance(value, (dict, list)):
            return cls._json(value)
        return str(value)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _directory_label(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return text or fallback


def _directory_key(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _increment(container: Dict[str, int], key: str, amount: int = 1) -> None:
    container[key] = container.get(key, 0) + amount


def _latest(summary: Dict[str, Any], timestamp: Any) -> None:
    if timestamp and str(timestamp) > str(summary.get("latestActivityAt") or ""):
        summary["latestActivityAt"] = str(timestamp)


def _vendor_summary(summaries: Dict[str, Dict[str, Any]], vendor_name: str) -> Dict[str, Any]:
    key = _directory_key(vendor_name)
    if key not in summaries:
        summaries[key] = {
            "vendorName": vendor_name,
            "normalizedVendorName": key,
            "recordCount": 0,
            "documentIds": set(),
            "bankTransactionCount": 0,
            "amountByCurrency": {},
            "categories": {},
            "targetSystems": {},
            "recordStatuses": {},
            "documentStatuses": {},
            "exportStatuses": {},
            "reconciliationStatuses": {},
            "reviewRequiredCount": 0,
            "exportReadyCount": 0,
            "failedCount": 0,
            "ruleCount": 0,
            "approvedRuleCount": 0,
            "suggestedRuleCount": 0,
            "ruleStatuses": {},
            "rules": [],
            "latestActivityAt": "",
        }
    return summaries[key]


def _category_summary(summaries: Dict[str, Dict[str, Any]], category: str) -> Dict[str, Any]:
    key = _directory_key(category)
    if key not in summaries:
        summaries[key] = {
            "category": category,
            "normalizedCategory": key,
            "recordCount": 0,
            "documentIds": set(),
            "bankTransactionCount": 0,
            "amountByCurrency": {},
            "vendors": {},
            "targetSystems": {},
            "recordStatuses": {},
            "documentStatuses": {},
            "exportStatuses": {},
            "reconciliationStatuses": {},
            "reviewRequiredCount": 0,
            "exportReadyCount": 0,
            "failedCount": 0,
            "ruleCount": 0,
            "approvedRuleCount": 0,
            "suggestedRuleCount": 0,
            "ruleStatuses": {},
            "rules": [],
            "latestActivityAt": "",
        }
    return summaries[key]


def _finalize_directory_summaries(summaries: Any, limit: int) -> list:
    finalized = []
    for summary in summaries:
        item = dict(summary)
        document_ids = item.pop("documentIds", set())
        item["documentCount"] = len(document_ids)
        item["documentIds"] = sorted(document_ids)[:25]
        for key in (
            "categories",
            "vendors",
            "targetSystems",
            "recordStatuses",
            "documentStatuses",
            "exportStatuses",
            "reconciliationStatuses",
            "ruleStatuses",
        ):
            if key in item:
                item[key] = _ranked_counts(item[key])
        item["needsAttention"] = bool(item.get("reviewRequiredCount") or item.get("failedCount"))
        finalized.append(item)
    finalized.sort(
        key=lambda item: (
            int(item.get("needsAttention") or 0),
            int(item.get("reviewRequiredCount") or 0),
            str(item.get("latestActivityAt") or ""),
            int(item.get("recordCount") or 0) + int(item.get("documentCount") or 0),
        ),
        reverse=True,
    )
    return finalized[:limit]


def _ranked_counts(values: Dict[str, int]) -> list:
    return [
        {"value": key, "count": count}
        for key, count in sorted(values.items(), key=lambda item: (-item[1], item[0]))
    ]
