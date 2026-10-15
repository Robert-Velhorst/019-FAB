export type FabRecord = Record<string, unknown>;
export type FabPanelState = "loading" | "live" | "stale" | "unavailable" | "empty" | "error";
export type FabResourceState = {
  state: FabPanelState;
  checkedAt?: string | null;
  updatedAt?: string | null;
  error?: string | null;
};
export type FabCommandId =
  | "run_safe_cycle"
  | "engage_emergency_stop"
  | "clear_emergency_stop"
  | "rescan_intake"
  | "process_imported"
  | "reprocess_incomplete"
  | "reprocess_review_queue"
  | "sync_sources"
  | "run_due_recovery"
  | "run_reconciliation"
  | "refresh_notifications"
  | "run_due_reports"
  | "assess_compliance";

export function asRecord(value: unknown): FabRecord {
  return value && typeof value === "object" && !Array.isArray(value) ? value as FabRecord : {};
}

export function records(value: unknown): FabRecord[] {
  return Array.isArray(value)
    ? value.flatMap((item) => Object.keys(asRecord(item)).length ? [asRecord(item)] : [])
    : [];
}

export function text(value: unknown, fallback = "-"): string {
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}

export function count(value: unknown): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

export function bool(value: unknown): boolean {
  return value === true || value === 1 || value === "true" || value === "ready" || value === "ok";
}

export function humanize(value: unknown): string {
  const raw = text(value, "Unknown");
  return raw.replace(/[._-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function commandOutcome(value: unknown): "success" | "attention" | "failure" {
  const result = asRecord(value);
  const nested = asRecord(result.result);
  if (result.success === false || nested.success === false) return "failure";

  const status = text(result.status, text(nested.status, "")).toLowerCase();
  if (["failed", "failure", "error", "blocked", "unsupported", "invalid", "cancelled", "canceled"].includes(status)) {
    return "failure";
  }
  if ([
    "already_running",
    "already_generated",
    "attention_required",
    "completed_with_errors",
    "deferred",
    "disabled",
    "needs_review",
    "prepared_needs_review",
    "retry_deferred",
  ].includes(status)) {
    return "attention";
  }
  return "success";
}

export function commandDetails(value: unknown): FabRecord {
  const result = asRecord(value);
  return { ...result, ...asRecord(result.result) };
}

export function compactHumanize(value: unknown): string {
  const raw = text(value, "Unknown");
  return raw.replace(/[._-]+/g, " ").replace(/^\w/, (letter) => letter.toUpperCase());
}

export function timeAgo(value: unknown, locale = "en"): string {
  const raw = text(value, "");
  const timestamp = Date.parse(raw);
  if (!raw || Number.isNaN(timestamp)) return "Not recorded";
  const seconds = Math.round((timestamp - Date.now()) / 1_000);
  const formatter = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  const ranges: Array<[Intl.RelativeTimeFormatUnit, number]> = [
    ["year", 31_536_000],
    ["month", 2_592_000],
    ["day", 86_400],
    ["hour", 3_600],
    ["minute", 60],
  ];
  for (const [unit, divisor] of ranges) {
    if (Math.abs(seconds) >= divisor) return formatter.format(Math.round(seconds / divisor), unit);
  }
  return formatter.format(seconds, "second");
}

export function exactDateTime(value: unknown, locale = "en-NL"): string {
  const raw = text(value, "");
  const timestamp = Date.parse(raw);
  if (!raw || Number.isNaN(timestamp)) return "Not recorded";
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(new Date(timestamp));
}

export function durationBetween(startValue: unknown, endValue: unknown): string {
  const start = Date.parse(text(startValue, ""));
  const end = Date.parse(text(endValue, ""));
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return "Not recorded";
  const milliseconds = end - start;
  if (milliseconds < 1_000) return `${milliseconds} ms`;
  if (milliseconds < 60_000) return `${(milliseconds / 1_000).toFixed(milliseconds < 10_000 ? 1 : 0)} s`;
  return `${Math.floor(milliseconds / 60_000)}m ${Math.round((milliseconds % 60_000) / 1_000)}s`;
}

export function panelState(value: unknown, itemCount?: number): FabPanelState {
  const state = text(asRecord(value).state, "unavailable").toLowerCase() as FabPanelState;
  if (state === "live" && itemCount === 0) return "empty";
  if (["loading", "live", "stale", "unavailable", "empty", "error"].includes(state)) return state;
  return "unavailable";
}

export function statusTone(value: unknown): "good" | "warn" | "bad" | "neutral" | "info" {
  const status = text(value, "").toLowerCase();
  if (["ready", "ready_to_archive", "ok", "healthy", "completed", "connected", "reconciled", "idle", "current", "valid", "complete", "created", "active"].includes(status)) return "good";
  if (["blocked", "blocked_by_review", "source_file_unavailable", "failed", "error", "disconnected", "high", "unavailable", "invalid", "invalid_state", "invalid_runtime", "wrong_business", "incompatible"].includes(status)) return "bad";
  if (["attention", "needs_attention", "needs_auth", "needs_authorization", "needs_receipt_executor", "authentication_required", "not_connected", "not_running", "stale", "needs_processing", "needs_review", "refresh_wave_readback", "medium", "due", "deferred", "prepared_disabled", "not_configured", "supervision_required", "incomplete", "legacy_ledger_only"].includes(status)) return "warn";
  if (["running", "syncing", "candidate", "pending", "manifest_valid", "locate_or_create_transaction", "upload_and_verify_attachment"].includes(status)) return "info";
  return "neutral";
}

export function matchesSearch(item: FabRecord, search: string): boolean {
  const normalized = search.trim().toLowerCase();
  if (!normalized) return true;
  return JSON.stringify(item).toLowerCase().includes(normalized);
}
