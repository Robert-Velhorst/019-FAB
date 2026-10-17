import csv
import hashlib
import io
import json
import os
import re
import tempfile
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.tz import gettz

from src.operations.local_ledger import LocalOperationsLedger
from src.utils.csv_safety import neutralize_csv_row


REPORT_VERSION = "fab-local-financial-report-v1"
REPORT_TYPES = {"overview", "profit_and_loss", "vat", "cash_flow", "expenses"}
REPORT_BASES = {"accrual", "cash"}
EXCLUDED_RECORD_STATUSES = {"duplicate", "failed", "ignored", "rejected"}
FINAL_RECONCILIATION_STATUSES = {"approved", "reconciled"}
LINKED_BANK_RECONCILIATION_STATUSES = FINAL_RECONCILIATION_STATUSES | {"candidate", "needs_review"}
REPORT_RECORD_LIMIT = 500
CENT = Decimal("0.01")
SCHEDULED_REPORT_FORMAT = "fab-scheduled-financial-report-v1"
SCHEDULE_FREQUENCIES = {"daily", "weekly", "monthly"}
SCHEDULE_PERIOD_MODES = {
    "previous_month",
    "current_month_to_date",
    "current_year_to_date",
    "previous_quarter",
}
SCHEDULE_ARTIFACT_FORMATS = {"json", "csv"}


class LocalFinancialReportingService:
    """Build provisional, reconciliation-aware reports from FAB's normalized ledger."""

    def __init__(self, ledger: LocalOperationsLedger, config: Optional[Dict[str, Any]] = None):
        self.ledger = ledger
        self.config = config or {}

    def generate(
        self,
        report_type: str = "overview",
        basis: str = "accrual",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        target_system: Optional[str] = None,
        include_rows: bool = False,
    ) -> Dict[str, Any]:
        report_type = _report_type(report_type)
        basis = _basis(basis)
        from_date, to_date = _period(from_date, to_date)
        target_system = str(target_system or "").strip() or None

        records = self.ledger.list_bookkeeping_records(
            target_system=target_system,
            from_date=from_date,
            to_date=to_date,
            limit=REPORT_RECORD_LIMIT,
        )
        scoped_count = self.ledger.count_bookkeeping_records(
            target_system=target_system,
            from_date=from_date,
            to_date=to_date,
        )
        undated_count = self.ledger.count_undated_bookkeeping_records(
            target_system=target_system,
            source_type="bank_transaction" if basis == "cash" else None,
            excluded_statuses=sorted(EXCLUDED_RECORD_STATUSES),
        )
        evaluated = [_evaluate_record(record, basis) for record in records]
        included = [item for item in evaluated if item["included"]]
        excluded = [item for item in evaluated if not item["included"]]
        reporting_rows = [item["row"] for item in included]
        bank_rows = [
            _normalized_row(record)
            for record in records
            if str(record.get("source_type") or "") == "bank_transaction"
            and str(record.get("status") or "") not in EXCLUDED_RECORD_STATUSES
            and record.get("amount") is not None
        ]

        reports = {
            "profitAndLoss": _profit_and_loss(reporting_rows),
            "vat": _vat_report(reporting_rows),
            "cashFlow": _cash_flow(bank_rows),
            "expenses": _expense_breakdowns(reporting_rows),
        }
        blockers = _quality_blockers(
            reporting_rows,
            scoped_count=scoped_count,
            loaded_count=len(records),
            undated_count=undated_count,
        )
        selected = _selected_report(report_type, reports)
        payload: Dict[str, Any] = {
            "success": True,
            "reportVersion": REPORT_VERSION,
            "reportType": report_type,
            "generatedAt": _now(),
            "period": {"fromDate": from_date, "toDate": to_date},
            "basis": basis,
            "targetSystem": target_system,
            "currencyPolicy": "separate_totals_per_currency",
            "statutoryStatus": "provisional",
            "externalSubmission": "not_executed",
            "summary": {
                "scopedRecordCount": scoped_count,
                "loadedRecordCount": len(records),
                "includedRecordCount": len(reporting_rows),
                "excludedRecordCount": len(excluded),
                "undatedRecordCount": undated_count,
                "truncated": scoped_count > len(records),
                "readiness": "ready" if not blockers else "needs_review",
                "blockers": blockers,
                "excludedReasons": _reason_counts(excluded),
            },
            "report": selected,
        }
        if report_type == "overview":
            payload["reports"] = reports
        if include_rows:
            payload["rows"] = reporting_rows
            payload["excludedRows"] = [
                {**item["row"], "exclusionReason": item["reason"]}
                for item in excluded
            ]
        return payload

    def csv_artifact(
        self,
        report_type: str = "overview",
        basis: str = "accrual",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        target_system: Optional[str] = None,
    ) -> Dict[str, Any]:
        report = self.generate(
            report_type=report_type,
            basis=basis,
            from_date=from_date,
            to_date=to_date,
            target_system=target_system,
            include_rows=True,
        )
        columns = [
            "recordId",
            "recordDate",
            "sourceType",
            "recordType",
            "vendorName",
            "category",
            "targetSystem",
            "targetAccount",
            "currency",
            "grossAmount",
            "vatAmount",
            "netAmount",
            "status",
            "reconciliationStatus",
            "reviewRequired",
        ]
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(neutralize_csv_row(row) for row in (report.get("rows") or []))
        return {
            "success": True,
            "status": "prepared",
            "filename": f"fab-{report['reportType']}-{report['period']['fromDate']}-{report['period']['toDate']}.csv",
            "contentType": "text/csv",
            "content": output.getvalue(),
            "rowCount": len(report.get("rows") or []),
            "report": report,
            "externalSubmission": "not_executed",
        }

    def record_generation_audit(self, report: Dict[str, Any], actor: str = "fab_local_reporting") -> int:
        return self.ledger.record_audit_event({
            "action": "local_reporting.report_generated",
            "entityType": "financial_report",
            "details": {
                "actor": actor,
                "reportVersion": report.get("reportVersion"),
                "reportType": report.get("reportType"),
                "basis": report.get("basis"),
                "period": report.get("period"),
                "targetSystem": report.get("targetSystem"),
                "includedRecordCount": (report.get("summary") or {}).get("includedRecordCount"),
                "readiness": (report.get("summary") or {}).get("readiness"),
                "externalSubmission": "not_executed",
            },
        })


class LocalScheduledReportService:
    """Persist due report runs and checksum-bound local artifacts."""

    def __init__(self, ledger: LocalOperationsLedger, config: Optional[Dict[str, Any]] = None):
        self.ledger = ledger
        self.config = config or {}
        self.report_dir = self._report_dir()
        os.makedirs(self.report_dir, exist_ok=True)

    def schedule_status(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        schedule = _schedule_config(self.config)
        if not schedule["enabled"]:
            return {
                "enabled": False,
                "status": "disabled",
                "schedule": schedule,
                "externalSubmission": "not_executed",
            }
        slot = _schedule_slot(schedule, now=now)
        existing = self.ledger.get_financial_report_run_by_slot(
            schedule["scheduleId"],
            slot["scheduleSlot"],
        )
        retry_at = _parse_datetime((existing or {}).get("next_retry_at"))
        now_value = _aware_utc(now)
        due = existing is None or (
            existing.get("status") == "failed"
            and (retry_at is None or retry_at <= now_value)
        )
        return {
            "enabled": True,
            "status": "due" if due else _schedule_existing_status(existing),
            "due": due,
            "schedule": schedule,
            "slot": slot,
            "existingReportRun": existing,
            "reportDir": self.report_dir,
            "externalSubmission": "not_executed",
        }

    def run_due(
        self,
        now: Optional[datetime] = None,
        actor: str = "fab_local_worker",
    ) -> Dict[str, Any]:
        status = self.schedule_status(now=now)
        if not status.get("enabled"):
            return {
                "success": True,
                "status": "disabled",
                "scheduleStatus": status,
                "externalSubmission": "not_executed",
            }
        schedule = status["schedule"]
        slot = status["slot"]
        claimed = self.ledger.claim_financial_report_run({
            "scheduleId": schedule["scheduleId"],
            "scheduleSlot": slot["scheduleSlot"],
            "reportType": schedule["reportType"],
            "basis": schedule["basis"],
            "periodFrom": slot["period"]["fromDate"],
            "periodTo": slot["period"]["toDate"],
            "targetSystem": schedule.get("targetSystem"),
            "scheduledFor": slot["scheduledFor"],
            "startedAt": _iso_utc(_aware_utc(now)),
            "metadata": {
                "actor": actor,
                "frequency": schedule["frequency"],
                "periodMode": schedule["periodMode"],
                "timezone": schedule["timezone"],
                "formats": schedule["formats"],
                "nextDueAt": slot["nextDueAt"],
            },
        })
        if not claimed.get("acquired"):
            report_run = claimed.get("reportRun") or {}
            return {
                "success": True,
                "status": (
                    "retry_deferred"
                    if claimed.get("status") == "retry_deferred"
                    else "already_generated"
                    if str(report_run.get("status") or "").startswith("prepared")
                    else "already_running"
                ),
                "reportRun": report_run,
                "scheduleStatus": self.schedule_status(now=now),
                "externalSubmission": "not_executed",
            }

        report_run = claimed["reportRun"]
        report_run_id = int(report_run["id"])
        created_paths = []
        try:
            reporting = LocalFinancialReportingService(self.ledger, self.config)
            report = reporting.generate(
                report_type=schedule["reportType"],
                basis=schedule["basis"],
                from_date=slot["period"]["fromDate"],
                to_date=slot["period"]["toDate"],
                target_system=schedule.get("targetSystem"),
                include_rows=True,
            )
            artifact_values: Dict[str, Any] = {}
            safe_stem = (
                f"fab-report-{_safe_name(schedule['scheduleId'])}-"
                f"{_safe_name(slot['scheduleSlot'])}-run-{report_run_id}"
            )
            if "json" in schedule["formats"]:
                json_payload = {
                    "format": SCHEDULED_REPORT_FORMAT,
                    "createdAt": _now(),
                    "schedule": {
                        "scheduleId": schedule["scheduleId"],
                        "scheduleSlot": slot["scheduleSlot"],
                        "scheduledFor": slot["scheduledFor"],
                        "nextDueAt": slot["nextDueAt"],
                        "frequency": schedule["frequency"],
                        "periodMode": schedule["periodMode"],
                        "timezone": schedule["timezone"],
                    },
                    "report": report,
                    "safety": {
                        "containsSecrets": False,
                        "statutoryStatus": "provisional",
                        "externalSubmission": "not_executed",
                    },
                }
                json_text = json.dumps(json_payload, sort_keys=True, indent=2, default=str) + "\n"
                json_path = self._write_artifact(f"{safe_stem}.json", json_text)
                created_paths.append(json_path)
                artifact_values.update({
                    "jsonPath": json_path,
                    "jsonSha256": _sha256_file(json_path),
                    "jsonBytes": os.path.getsize(json_path),
                })
            if "csv" in schedule["formats"]:
                csv_artifact = reporting.csv_artifact(
                    report_type=schedule["reportType"],
                    basis=schedule["basis"],
                    from_date=slot["period"]["fromDate"],
                    to_date=slot["period"]["toDate"],
                    target_system=schedule.get("targetSystem"),
                )
                csv_path = self._write_artifact(f"{safe_stem}.csv", csv_artifact["content"])
                created_paths.append(csv_path)
                artifact_values.update({
                    "csvPath": csv_path,
                    "csvSha256": _sha256_file(csv_path),
                    "csvBytes": os.path.getsize(csv_path),
                })

            blockers = (report.get("summary") or {}).get("blockers") or []
            run_status = "prepared" if not blockers else "prepared_needs_review"
            completed = self.ledger.complete_financial_report_run(report_run_id, {
                "status": run_status,
                "readiness": (report.get("summary") or {}).get("readiness"),
                "rowCount": (report.get("summary") or {}).get("includedRecordCount", 0),
                "blockerCount": len(blockers),
                **artifact_values,
                "metadata": {
                    "reportVersion": report.get("reportVersion"),
                    "statutoryStatus": report.get("statutoryStatus"),
                    "currencyPolicy": report.get("currencyPolicy"),
                    "blockers": blockers,
                },
            })
            self.ledger.record_audit_event({
                "action": "local_reporting.scheduled_report_prepared",
                "entityType": "financial_report_run",
                "entityId": str(report_run_id),
                "details": {
                    "actor": actor,
                    "scheduleId": schedule["scheduleId"],
                    "scheduleSlot": slot["scheduleSlot"],
                    "status": run_status,
                    "readiness": completed.get("readiness") if completed else None,
                    "rowCount": completed.get("row_count") if completed else 0,
                    "blockerCount": completed.get("blocker_count") if completed else 0,
                    "artifactChecksums": {
                        "json": artifact_values.get("jsonSha256"),
                        "csv": artifact_values.get("csvSha256"),
                    },
                    "externalSubmission": "not_executed",
                },
            })
            return {
                "success": True,
                "status": run_status,
                "reportRun": completed,
                "scheduleStatus": self.schedule_status(now=now),
                "externalSubmission": "not_executed",
            }
        except Exception as exc:
            for path in created_paths:
                try:
                    os.remove(path)
                except OSError:
                    pass
            retry_at = _aware_utc(now) + timedelta(hours=schedule["retryHours"])
            failed = self.ledger.complete_financial_report_run(report_run_id, {
                "status": "failed",
                "readiness": "failed",
                "nextRetryAt": _iso_utc(retry_at),
                "errorMessage": str(exc),
                "metadata": {"failureType": type(exc).__name__},
            })
            self.ledger.record_audit_event({
                "action": "local_reporting.scheduled_report_failed",
                "entityType": "financial_report_run",
                "entityId": str(report_run_id),
                "details": {
                    "actor": actor,
                    "scheduleId": schedule["scheduleId"],
                    "scheduleSlot": slot["scheduleSlot"],
                    "error": str(exc),
                    "nextRetryAt": _iso_utc(retry_at),
                    "externalSubmission": "not_executed",
                },
            })
            return {
                "success": False,
                "status": "failed",
                "error": str(exc),
                "reportRun": failed,
                "externalSubmission": "not_executed",
            }

    def inspect_run(self, report_run_id: int) -> Dict[str, Any]:
        report_run = self.ledger.get_financial_report_run(report_run_id)
        if not report_run:
            return {"success": False, "status": "not_found", "error": "Financial report run not found"}
        artifacts = {}
        errors = []
        for format_name in ("json", "csv"):
            path = report_run.get(f"{format_name}_path")
            expected = report_run.get(f"{format_name}_sha256")
            if not path:
                continue
            try:
                resolved = self._resolve_artifact_path(path)
                actual = _sha256_file(resolved)
                valid = bool(expected and actual == expected)
                artifacts[format_name] = {
                    "path": resolved,
                    "filename": os.path.basename(resolved),
                    "sha256": actual,
                    "expectedSha256": expected,
                    "sizeBytes": os.path.getsize(resolved),
                    "valid": valid,
                }
                if not valid:
                    errors.append(f"{format_name}_checksum_mismatch")
            except (OSError, ValueError) as exc:
                artifacts[format_name] = {"path": path, "valid": False, "error": str(exc)}
                errors.append(f"{format_name}_artifact_invalid")
        return {
            "success": not errors,
            "status": "valid" if not errors else "invalid",
            "reportRun": report_run,
            "artifacts": artifacts,
            "errors": errors,
            "externalSubmission": "not_executed",
        }

    def read_artifact(self, report_run_id: int, format_name: str) -> Dict[str, Any]:
        format_name = str(format_name or "json").strip().lower()
        if format_name not in SCHEDULE_ARTIFACT_FORMATS:
            raise ValueError(f"Unsupported scheduled report artifact format: {format_name}")
        inspected = self.inspect_run(report_run_id)
        if not inspected.get("success"):
            raise ValueError("Scheduled report artifact integrity check failed")
        artifact = (inspected.get("artifacts") or {}).get(format_name)
        if not artifact:
            raise ValueError(f"Scheduled report has no {format_name} artifact")
        with open(artifact["path"], "r", encoding="utf-8") as handle:
            content = handle.read()
        return {
            "success": True,
            "format": format_name,
            "filename": artifact["filename"],
            "contentType": "application/json" if format_name == "json" else "text/csv",
            "content": content,
            "sha256": artifact["sha256"],
            "externalSubmission": "not_executed",
        }

    def _write_artifact(self, filename: str, content: str) -> str:
        path = self._resolve_artifact_path(filename)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=self.report_dir,
            prefix=f".{os.path.basename(path)}.",
            suffix=".tmp",
            delete=False,
        )
        temp_path = handle.name
        try:
            with handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        except Exception:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise
        return path

    def _report_dir(self) -> str:
        value = _config_value(
            self.config,
            "fab_local_report_dir",
            "operations_report_dir",
            "report_export_dir",
        )
        if not value:
            value = os.path.join(os.path.dirname(os.path.abspath(self.ledger.path)), "reports")
        return os.path.realpath(os.path.abspath(os.path.expanduser(str(value))))

    def _resolve_artifact_path(self, path: str) -> str:
        candidate = os.path.expanduser(str(path or ""))
        if not candidate:
            raise ValueError("Scheduled report artifact path is required")
        if not os.path.isabs(candidate):
            candidate = os.path.join(self.report_dir, candidate)
        candidate = os.path.realpath(os.path.abspath(candidate))
        report_dir = os.path.normcase(os.path.realpath(os.path.abspath(self.report_dir)))
        try:
            common_path = os.path.commonpath([os.path.normcase(candidate), report_dir])
        except ValueError as exc:
            raise ValueError("Scheduled report artifact must stay inside the configured report directory") from exc
        if common_path != report_dir:
            raise ValueError("Scheduled report artifact must stay inside the configured report directory")
        if os.path.isdir(candidate):
            raise ValueError("Scheduled report artifact path cannot be a directory")
        return candidate


def _schedule_config(config: Dict[str, Any]) -> Dict[str, Any]:
    enabled = _bool_config(config, "report_schedule_enabled", "worker_generate_scheduled_reports", default=True)
    frequency = str(_config_value(config, "report_schedule_frequency") or "monthly").strip().lower()
    if frequency not in SCHEDULE_FREQUENCIES:
        raise ValueError(f"Unsupported report schedule frequency: {frequency}")
    period_mode = str(_config_value(config, "report_schedule_period_mode") or "previous_month").strip().lower()
    if period_mode not in SCHEDULE_PERIOD_MODES:
        raise ValueError(f"Unsupported report schedule period mode: {period_mode}")
    report_type = _report_type(_config_value(config, "report_schedule_report_type") or "overview")
    basis = _basis(_config_value(config, "report_schedule_basis") or "accrual")
    timezone_name = str(_config_value(config, "report_schedule_timezone") or "Europe/Amsterdam").strip()
    _load_timezone(timezone_name)
    formats = _list_config(_config_value(config, "report_schedule_formats") or "json,csv")
    formats = list(dict.fromkeys(item.lower() for item in formats))
    unsupported_formats = [item for item in formats if item not in SCHEDULE_ARTIFACT_FORMATS]
    if unsupported_formats or not formats:
        raise ValueError(f"Unsupported report schedule formats: {unsupported_formats or formats}")
    schedule_id = str(_config_value(config, "report_schedule_id") or "default-financial-report").strip()
    return {
        "enabled": enabled,
        "scheduleId": schedule_id or "default-financial-report",
        "frequency": frequency,
        "weekday": _bounded_int(_config_value(config, "report_schedule_weekday"), 0, 0, 6),
        "monthDay": _bounded_int(_config_value(config, "report_schedule_month_day"), 1, 1, 28),
        "hour": _bounded_int(_config_value(config, "report_schedule_hour"), 6, 0, 23),
        "minute": _bounded_int(_config_value(config, "report_schedule_minute"), 0, 0, 59),
        "timezone": timezone_name,
        "periodMode": period_mode,
        "reportType": report_type,
        "basis": basis,
        "targetSystem": str(_config_value(config, "report_schedule_target_system") or "").strip() or None,
        "formats": formats,
        "retryHours": _bounded_float(_config_value(config, "report_schedule_retry_hours"), 6.0, 0.1, 168.0),
        "externalSubmission": "not_executed",
    }


def _schedule_slot(schedule: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    zone = _load_timezone(schedule["timezone"])
    local_now = _aware_utc(now).astimezone(zone)
    due_local = _latest_due_local(schedule, local_now)
    next_due_local = _next_due_local(schedule, due_local)
    period_from, period_to = _scheduled_period(schedule["periodMode"], due_local.date(), local_now.date())
    if schedule["frequency"] == "daily":
        schedule_slot = f"daily:{due_local.date().isoformat()}"
    elif schedule["frequency"] == "weekly":
        iso_year, iso_week, _ = due_local.date().isocalendar()
        schedule_slot = f"weekly:{iso_year}-W{iso_week:02d}"
    else:
        schedule_slot = f"monthly:{due_local.year}-{due_local.month:02d}"
    return {
        "scheduleSlot": schedule_slot,
        "scheduledFor": _iso_utc(due_local.astimezone(timezone.utc)),
        "nextDueAt": _iso_utc(next_due_local.astimezone(timezone.utc)),
        "period": {"fromDate": period_from.isoformat(), "toDate": period_to.isoformat()},
    }


def _latest_due_local(schedule: Dict[str, Any], local_now: datetime) -> datetime:
    scheduled_time = time(schedule["hour"], schedule["minute"])
    frequency = schedule["frequency"]
    if frequency == "daily":
        due = datetime.combine(local_now.date(), scheduled_time, tzinfo=local_now.tzinfo)
        return due if local_now >= due else due - timedelta(days=1)
    if frequency == "weekly":
        week_start = local_now.date() - timedelta(days=local_now.weekday())
        due_date = week_start + timedelta(days=schedule["weekday"])
        due = datetime.combine(due_date, scheduled_time, tzinfo=local_now.tzinfo)
        return due if local_now >= due else due - timedelta(days=7)
    due_date = date(local_now.year, local_now.month, schedule["monthDay"])
    due = datetime.combine(due_date, scheduled_time, tzinfo=local_now.tzinfo)
    if local_now >= due:
        return due
    previous_month = _month_shift(due_date, -1)
    return datetime.combine(
        previous_month.replace(day=min(schedule["monthDay"], monthrange(previous_month.year, previous_month.month)[1])),
        scheduled_time,
        tzinfo=local_now.tzinfo,
    )


def _next_due_local(schedule: Dict[str, Any], due_local: datetime) -> datetime:
    if schedule["frequency"] == "daily":
        return due_local + timedelta(days=1)
    if schedule["frequency"] == "weekly":
        return due_local + timedelta(days=7)
    next_month = _month_shift(due_local.date(), 1)
    next_date = next_month.replace(
        day=min(schedule["monthDay"], monthrange(next_month.year, next_month.month)[1])
    )
    return datetime.combine(next_date, due_local.timetz().replace(tzinfo=None), tzinfo=due_local.tzinfo)


def _scheduled_period(period_mode: str, due_date: date, local_today: date) -> tuple[date, date]:
    if period_mode == "previous_month":
        previous = _month_shift(due_date.replace(day=1), -1)
        return previous, previous.replace(day=monthrange(previous.year, previous.month)[1])
    if period_mode == "current_month_to_date":
        return local_today.replace(day=1), local_today
    if period_mode == "current_year_to_date":
        return date(local_today.year, 1, 1), local_today
    quarter = (due_date.month - 1) // 3
    previous_quarter_end_month = quarter * 3
    if previous_quarter_end_month == 0:
        year = due_date.year - 1
        previous_quarter_end_month = 12
    else:
        year = due_date.year
    start_month = previous_quarter_end_month - 2
    return (
        date(year, start_month, 1),
        date(year, previous_quarter_end_month, monthrange(year, previous_quarter_end_month)[1]),
    )


def _month_shift(value: date, months: int) -> date:
    month_index = value.year * 12 + (value.month - 1) + months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def _schedule_existing_status(report_run: Optional[Dict[str, Any]]) -> str:
    if not report_run:
        return "due"
    status = str(report_run.get("status") or "unknown")
    if status.startswith("prepared"):
        return "current"
    if status == "running":
        return "running"
    if status == "failed":
        return "retry_deferred"
    return status


def _load_timezone(timezone_name: str):
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        fallback = gettz(timezone_name)
        if fallback is None:
            raise ValueError(f"Unknown report schedule timezone: {timezone_name}")
        return fallback


def _config_value(config: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = config.get(key)
        if value not in (None, ""):
            return value
    for key in keys:
        if "_" not in key:
            continue
        section, option = key.split("_", 1)
        values = config.get(section)
        if isinstance(values, dict):
            value = values.get(option)
            if value not in (None, ""):
                return value
    return None


def _list_config(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = str(value or "").replace(";", ",").split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def _bool_config(config: Dict[str, Any], *keys: str, default: bool) -> bool:
    value = _config_value(config, *keys)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _aware_utc(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def _safe_name(value: Any) -> str:
    text = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "").strip())
    return text.strip("-._") or "report"


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _evaluate_record(record: Dict[str, Any], basis: str) -> Dict[str, Any]:
    row = _normalized_row(record)
    status = str(record.get("status") or "")
    source_type = str(record.get("source_type") or "")
    reconciliation_status = str(record.get("reconciliation_status") or "")
    reason = None
    if status in EXCLUDED_RECORD_STATUSES:
        reason = f"record_status_{status}"
    elif record.get("amount") is None:
        reason = "missing_amount"
    elif basis == "cash" and source_type != "bank_transaction":
        reason = "not_cash_evidence"
    elif basis == "accrual" and source_type == "bank_transaction" and reconciliation_status in LINKED_BANK_RECONCILIATION_STATUSES:
        reason = (
            "reconciled_bank_evidence"
            if reconciliation_status in FINAL_RECONCILIATION_STATUSES
            else "pending_reconciliation_bank_evidence"
        )
    return {"included": reason is None, "reason": reason, "row": row}


def _normalized_row(record: Dict[str, Any]) -> Dict[str, Any]:
    gross = _money(record.get("amount"))
    raw_vat = _money(record.get("vat_amount"))
    record_type = str(record.get("record_type") or "expense").lower()
    is_income = record_type in {"income", "revenue", "sales_invoice"}
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    is_credit_note = str(metadata.get("documentType") or "").strip().lower() == "credit_note"
    absolute_gross = abs(gross)
    absolute_vat = abs(raw_vat)
    reporting_direction = Decimal("-1") if is_credit_note else Decimal("1")
    reporting_gross = absolute_gross * reporting_direction
    reporting_vat = absolute_vat * reporting_direction
    reporting_net = (absolute_gross - absolute_vat) * reporting_direction
    return {
        "recordId": record.get("id"),
        "recordDate": record.get("record_date"),
        "sourceType": record.get("source_type"),
        "recordType": "income" if is_income else "expense",
        "vendorName": record.get("vendor_name") or "Unknown",
        "category": record.get("category") or "Unassigned",
        "targetSystem": record.get("target_system"),
        "targetAccount": record.get("target_account"),
        "currency": str(record.get("currency") or "EUR").upper(),
        "grossAmount": _number(reporting_gross),
        "signedAmount": _number(gross),
        "vatAmount": _number(reporting_vat),
        "netAmount": _number(reporting_net),
        "status": record.get("status"),
        "reconciliationStatus": record.get("reconciliation_status"),
        "reviewRequired": bool(record.get("review_required")),
    }


def _profit_and_loss(rows: list[Dict[str, Any]]) -> Dict[str, Any]:
    totals: Dict[str, Dict[str, Decimal]] = {}
    for row in rows:
        currency = row["currency"]
        bucket = totals.setdefault(currency, {
            "revenueGross": Decimal("0"),
            "revenueVat": Decimal("0"),
            "revenueNet": Decimal("0"),
            "expensesGross": Decimal("0"),
            "expensesVat": Decimal("0"),
            "expensesNet": Decimal("0"),
        })
        prefix = "revenue" if row["recordType"] == "income" else "expenses"
        bucket[f"{prefix}Gross"] += _money(row["grossAmount"])
        bucket[f"{prefix}Vat"] += _money(row["vatAmount"])
        bucket[f"{prefix}Net"] += _money(row["netAmount"])
    by_currency = []
    for currency in sorted(totals):
        bucket = totals[currency]
        by_currency.append({
            "currency": currency,
            **{key: _number(value) for key, value in bucket.items()},
            "netResult": _number(bucket["revenueNet"] - bucket["expensesNet"]),
        })
    return {"byCurrency": by_currency}


def _vat_report(rows: list[Dict[str, Any]]) -> Dict[str, Any]:
    totals: Dict[str, Dict[str, Decimal]] = {}
    for row in rows:
        bucket = totals.setdefault(row["currency"], {"outputVat": Decimal("0"), "inputVat": Decimal("0")})
        key = "outputVat" if row["recordType"] == "income" else "inputVat"
        bucket[key] += _money(row["vatAmount"])
    return {
        "byCurrency": [
            {
                "currency": currency,
                "outputVat": _number(values["outputVat"]),
                "inputVat": _number(values["inputVat"]),
                "netVatPayable": _number(values["outputVat"] - values["inputVat"]),
            }
            for currency, values in sorted(totals.items())
        ],
        "filingStatus": "not_filed",
    }


def _cash_flow(rows: list[Dict[str, Any]]) -> Dict[str, Any]:
    totals: Dict[str, Dict[str, Decimal]] = {}
    for row in rows:
        signed = _money(row["signedAmount"])
        bucket = totals.setdefault(row["currency"], {"inflow": Decimal("0"), "outflow": Decimal("0"), "netMovement": Decimal("0")})
        if signed >= 0:
            bucket["inflow"] += signed
        else:
            bucket["outflow"] += abs(signed)
        bucket["netMovement"] += signed
    return {
        "byCurrency": [
            {"currency": currency, **{key: _number(value) for key, value in values.items()}}
            for currency, values in sorted(totals.items())
        ],
        "source": "bank_transaction_records",
    }


def _expense_breakdowns(rows: list[Dict[str, Any]]) -> Dict[str, Any]:
    expense_rows = [row for row in rows if row["recordType"] == "expense"]
    return {
        "byCategory": _group_expenses(expense_rows, "category", "category"),
        "byVendor": _group_expenses(expense_rows, "vendorName", "vendorName"),
    }


def _group_expenses(rows: list[Dict[str, Any]], source_key: str, output_key: str) -> list[Dict[str, Any]]:
    totals: Dict[tuple[str, str], Dict[str, Decimal]] = {}
    counts: Dict[tuple[str, str], int] = {}
    for row in rows:
        key = (str(row.get(source_key) or "Unknown"), row["currency"])
        bucket = totals.setdefault(key, {"grossAmount": Decimal("0"), "vatAmount": Decimal("0"), "netAmount": Decimal("0")})
        for amount_key in bucket:
            bucket[amount_key] += _money(row[amount_key])
        counts[key] = counts.get(key, 0) + 1
    result = [
        {
            output_key: label,
            "currency": currency,
            "recordCount": counts[(label, currency)],
            **{key: _number(value) for key, value in values.items()},
        }
        for (label, currency), values in totals.items()
    ]
    return sorted(result, key=lambda item: (-float(item["grossAmount"]), item[output_key], item["currency"]))


def _quality_blockers(
    rows: list[Dict[str, Any]],
    scoped_count: int,
    loaded_count: int,
    undated_count: int,
) -> list[Dict[str, Any]]:
    blockers = []
    if scoped_count > loaded_count:
        blockers.append({"code": "record_limit_reached", "count": scoped_count - loaded_count})
    if undated_count:
        blockers.append({"code": "undated_records_outside_period", "count": undated_count})
    checks = {
        "review_required": lambda row: row["reviewRequired"],
        "unassigned_category": lambda row: row["category"] == "Unassigned",
        "unmapped_target_account": lambda row: not row.get("targetAccount"),
        "unreconciled_record": lambda row: str(row.get("reconciliationStatus") or "") not in FINAL_RECONCILIATION_STATUSES,
        "vat_exceeds_gross": lambda row: _money(row["vatAmount"]) > _money(row["grossAmount"]),
    }
    for code, predicate in checks.items():
        count = sum(1 for row in rows if predicate(row))
        if count:
            blockers.append({"code": code, "count": count})
    return blockers


def _reason_counts(excluded: list[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in excluded:
        reason = str(item.get("reason") or "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _selected_report(report_type: str, reports: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "overview": reports,
        "profit_and_loss": reports["profitAndLoss"],
        "vat": reports["vat"],
        "cash_flow": reports["cashFlow"],
        "expenses": reports["expenses"],
    }[report_type]


def _report_type(value: Any) -> str:
    normalized = str(value or "overview").strip().lower().replace("-", "_")
    if normalized not in REPORT_TYPES:
        raise ValueError(f"Unsupported report type: {normalized}")
    return normalized


def _basis(value: Any) -> str:
    normalized = str(value or "accrual").strip().lower()
    if normalized not in REPORT_BASES:
        raise ValueError(f"Unsupported reporting basis: {normalized}")
    return normalized


def _period(from_date: Optional[str], to_date: Optional[str]) -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    start = _date(from_date, date(today.year, 1, 1))
    end = _date(to_date, today)
    if start > end:
        raise ValueError("fromDate cannot be later than toDate")
    return start.isoformat(), end.isoformat()


def _date(value: Optional[str], default: date) -> date:
    if value in (None, ""):
        return default
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"Invalid ISO date: {value}") from exc


def _money(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _number(value: Decimal) -> float:
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
