import { TRPCError } from "@trpc/server";
import type { TRPC_ERROR_CODE_KEY } from "@trpc/server/rpc";
import { ENV } from "./_core/env";
import { sanitizeExternalMessage } from "./lib/errorSanitizer";
import { readBoundedFabResponseBody } from "./lib/fabResponseBody";

type JsonRecord = Record<string, unknown>;

export type FabDataState = "live" | "stale" | "unavailable" | "error";

export type FabResourceState = {
  state: FabDataState;
  checkedAt: string;
  updatedAt: string | null;
  error: string | null;
};

export const FAB_OPERATOR_COMMAND_IDS = [
  "run_safe_cycle",
  "engage_emergency_stop",
  "clear_emergency_stop",
  "rescan_intake",
  "process_imported",
  "reprocess_incomplete",
  "reprocess_review_queue",
  "sync_sources",
  "run_due_recovery",
  "run_reconciliation",
  "refresh_notifications",
  "run_due_reports",
  "assess_compliance",
] as const;

export type FabOperatorCommandId = typeof FAB_OPERATOR_COMMAND_IDS[number];

export type FabControlCenter = {
  connection: {
    connected: boolean;
    status: string;
    endpoint: string;
    authConfigured: boolean;
    checkedAt: string;
    latencyMs: number | null;
    error: string | null;
  };
  metrics: {
    documents: number | null;
    pendingReview: number | null;
    pendingReviewDocuments: number | null;
    postingBlockedReviewDocuments: number | null;
    unreconciled: number | null;
    unreconciledDocuments: number | null;
    unreconciledBankTransactions: number | null;
    exceptions: number | null;
    failedDocuments: number | null;
  };
  decisionContext: {
    lastSafeCycleAt: string | null;
    latestWorkflowStatus: string | null;
    dataThroughDate: string | null;
    sourceCount: number | null;
    readySourceCount: number | null;
    latestSourceSyncAt: string | null;
    unreconciledAmountByCurrency: Record<string, number> | null;
    oldestReviewAgeHours: number | null;
    highPriorityExceptions: number | null;
    ledgerReadyForApproval: number | null;
  };
  health: JsonRecord;
  autonomy: JsonRecord;
  closeReadiness: JsonRecord;
  delivery: {
    status: JsonRecord;
    summary: JsonRecord;
    workOrders: JsonRecord[];
    count: number | null;
  };
  reviews: {
    workItems: JsonRecord[];
    categoryOptions: string[];
    summary: JsonRecord;
    pagination: JsonRecord;
  };
  gmailAuthorization: JsonRecord;
  driveAuthorization: JsonRecord;
  waveSetup: JsonRecord;
  waveReceiptExecutor: JsonRecord;
  cloudAccess: JsonRecord;
  banking: {
    recentImports: JsonRecord[];
  };
  exceptions: JsonRecord[];
  exceptionSummary: JsonRecord;
  connections: JsonRecord[];
  workflows: JsonRecord[];
  recovery: JsonRecord;
  backups: {
    backups: JsonRecord[];
    schedule: JsonRecord;
    restorePolicy: JsonRecord;
    verificationMode: string | null;
  };
  reporting: {
    scheduleStatus: JsonRecord;
    reportRuns: JsonRecord[];
    externalSubmission: string | null;
  };
  compliance: {
    summary: JsonRecord;
    assessments: JsonRecord[];
    statutoryStatus: string | null;
    filingStatus: string | null;
    externalFiling: string | null;
  };
  notifications: JsonRecord[];
  reconciliation: JsonRecord[];
  activity: JsonRecord[];
  hai: {
    status: JsonRecord;
    manifest: JsonRecord;
  };
  resourceStates: Record<FabResourceKey, FabResourceState>;
  partialErrors: Array<{ resource: FabResourceKey; error: string; state: FabDataState; updatedAt: string | null }>;
};

const DEFAULT_FAB_LOCAL_API_URL = "http://127.0.0.1:5001";
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "::1", "[::1]"]);
const MAX_CONCURRENT_READS = 8;
const FAB_HTTP_ERROR_CODES: Partial<Record<number, TRPC_ERROR_CODE_KEY>> = {
  400: "BAD_REQUEST",
  401: "UNAUTHORIZED",
  403: "FORBIDDEN",
  404: "NOT_FOUND",
  405: "METHOD_NOT_SUPPORTED",
  409: "CONFLICT",
  413: "PAYLOAD_TOO_LARGE",
  422: "UNPROCESSABLE_CONTENT",
  429: "TOO_MANY_REQUESTS",
  503: "SERVICE_UNAVAILABLE",
};
// Start the costliest independent reads first so the bounded worker pool does
// not leave a long-running request at the end of the dashboard refresh.
const READ_PATHS = {
  backups: "/api/backups?limit=5&verify=false",
  autonomy: "/api/autonomy/plan?limit=25",
  reviewQueue: "/api/review?status=open&limit=25&offset=0&view=summary",
  exceptions: "/api/exceptions?limit=25&includeEntities=true",
  driveWaveWorkOrders: "/api/drive-wave/work-orders?limit=200&itemsLimit=25&view=summary",
  health: "/api/health",
  closeReadiness: "/api/close-readiness",
  reportRuns: "/api/report-runs?limit=5",
  compliance: "/api/compliance/assessments?limit=5",
  recovery: "/api/workflows/recovery?limit=10",
  liveness: "/api/live",
  waveSetup: "/api/wave/setup",
  driveWaveStatus: "/api/drive-wave/status",
  workflows: "/api/workflows?limit=10",
  masterLedger: "/api/master-ledger?limit=250&summaryOnly=true",
  metrics: "/api/dashboard",
  notifications: "/api/notifications?limit=10",
  settings: "/api/settings",
  bankTransactions: "/api/bank-transactions?limit=250",
  reconciliation: "/api/reconciliation?limit=10",
  activity: "/api/audit?limit=12",
  sources: "/api/sources?limit=50",
  waveReceiptExecutor: "/api/wave/receipt-executor/status",
  sourceReadiness: "/api/sources/readiness",
  driveAuthorization: "/api/connectors/google-drive/authorization",
  haiStatus: "/api/hai/status",
  haiManifest: "/api/hai/manifest",
  cloudStatus: "/api/cloud/status",
  gmailAuthorization: "/api/connectors/gmail/authorization",
} as const;

export type FabResourceKey = keyof typeof READ_PATHS;

const READ_TIMEOUT_MS: Partial<Record<FabResourceKey, number>> = {
  health: 12_000,
  autonomy: 15_000,
  backups: 12_000,
  driveWaveWorkOrders: 12_000,
  cloudStatus: 3_000,
};
const CONTROL_CENTER_BATCH_PATH = "/api/control-center/resources";
const CONTROL_CENTER_DIRECT_RESOURCES = new Set<FabResourceKey>([
  "backups",
  "cloudStatus",
]);

const resourceCache = new Map<FabResourceKey, { value: JsonRecord; updatedAt: string }>();
// The operator UI polls once per minute. Keep one immutable verified snapshot
// just beyond that interval so normal polling does not rebuild 29 resources.
// Mutations and explicit refreshes invalidate this snapshot immediately.
const CONTROL_CENTER_CACHE_TTL_MS = 65_000;
let controlCenterSnapshot: { value: FabControlCenter; expiresAt: number } | null = null;
let controlCenterInFlight: Promise<FabControlCenter> | null = null;
let controlCenterCacheGeneration = 0;

const COMMAND_PATHS: Record<FabOperatorCommandId, { path: string; body: JsonRecord; method?: "POST" | "DELETE" }> = {
  run_safe_cycle: { path: "/api/autonomy/run", body: { limit: 25, includeWavePlan: true, includeWaveSync: true, includeConnectorSync: true } },
  engage_emergency_stop: { path: "/api/autonomy/emergency-stop", body: { reason: "Operator stopped autonomous processing from the FAB dashboard." } },
  clear_emergency_stop: { path: "/api/autonomy/emergency-stop", method: "DELETE", body: {} },
  rescan_intake: { path: "/api/intake/rescan", body: {} },
  process_imported: { path: "/api/documents/process-imported", body: { limit: 25 } },
  reprocess_incomplete: { path: "/api/documents/reprocess-incomplete", body: { limit: 25 } },
  reprocess_review_queue: { path: "/api/documents/reprocess-review-queue", body: { limit: 25 } },
  sync_sources: { path: "/api/sources/sync", body: {} },
  run_due_recovery: { path: "/api/workflows/recovery/run-due", body: { limit: 5 } },
  run_reconciliation: { path: "/api/reconciliation/run", body: { limit: 100 } },
  refresh_notifications: { path: "/api/notifications/refresh", body: {} },
  run_due_reports: { path: "/api/report-runs/run-due", body: {} },
  assess_compliance: { path: "/api/compliance/assessments", body: {} },
};

export function getFabLocalApiBaseUrl(
  rawUrl = ENV.fabLocalApiUrl,
  insecureHosts: readonly string[] = ENV.fabLocalApiInsecureHosts,
): URL {
  const parsed = new URL((rawUrl || DEFAULT_FAB_LOCAL_API_URL).trim().replace(/\/+$/, ""));
  if (!new Set(["http:", "https:"]).has(parsed.protocol)) {
    throw new Error("FAB_LOCAL_API_URL must use http or https");
  }
  if (parsed.username || parsed.password) {
    throw new Error("FAB_LOCAL_API_URL must not contain credentials");
  }
  const hostname = parsed.hostname.toLowerCase();
  if (
    parsed.protocol !== "https:"
    && !LOOPBACK_HOSTS.has(hostname)
    && !insecureHosts.includes(hostname)
  ) {
    throw new Error("Non-loopback FAB_LOCAL_API_URL values must use https");
  }
  return parsed;
}

export function getFabBrowserApiBaseUrl(
  rawUrl = ENV.fabLocalApiPublicUrl,
): URL {
  const parsed = new URL((rawUrl || DEFAULT_FAB_LOCAL_API_URL).trim().replace(/\/+$/, ""));
  if (!new Set(["http:", "https:"]).has(parsed.protocol)) {
    throw new Error("FAB_LOCAL_API_PUBLIC_URL must use http or https");
  }
  if (parsed.username || parsed.password) {
    throw new Error("FAB_LOCAL_API_PUBLIC_URL must not contain credentials");
  }
  if (parsed.pathname !== "/" || parsed.search || parsed.hash) {
    throw new Error("FAB_LOCAL_API_PUBLIC_URL must be an origin without a path, query, or fragment");
  }
  if (parsed.protocol !== "https:" && !LOOPBACK_HOSTS.has(parsed.hostname.toLowerCase())) {
    throw new Error("Non-loopback FAB_LOCAL_API_PUBLIC_URL values must use https");
  }
  return parsed;
}

export async function fabLocalRequest(
  path: string,
  init: RequestInit = {},
  options: { baseUrl?: string; token?: string; timeoutMs?: number } = {},
): Promise<JsonRecord> {
  const baseUrl = getFabLocalApiBaseUrl(options.baseUrl);
  const target = new URL(path, `${baseUrl.toString().replace(/\/$/, "")}/`);
  if (target.origin !== baseUrl.origin) {
    throw new Error("FAB local API path escaped the configured origin");
  }
  const token = options.token ?? ENV.fabLocalApiToken;
  const headers = new Headers(init.headers);
  headers.set("accept", "application/json");
  if (init.body) headers.set("content-type", "application/json");
  if (token) headers.set("authorization", `Bearer ${token}`);

  const timeoutMs = options.timeoutMs ?? 8_000;
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs <= 0 || timeoutMs > 120_000) {
    throw new Error("Invalid FAB local API request timeout");
  }
  if (init.signal?.aborted) throw new Error("FAB local API request cancelled");
  const controller = new AbortController();
  const cancel = () => controller.abort();
  init.signal?.addEventListener("abort", cancel, { once: true });
  let timedOut = false;
  const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
  const method = String(init.method || "GET").toUpperCase();
  const isMutation = method !== "GET" && method !== "HEAD";

  try {
    // A failed readback does not prove that a submitted mutation did not run.
    if (isMutation) invalidateFabControlCenterSnapshot();
    const response = await fetch(target, { ...init, headers, signal: controller.signal, redirect: "manual" });
    if (response.status >= 300 && response.status < 400) {
      throw new Error("FAB local API redirects are not permitted");
    }
    const bytes = await readBoundedFabResponseBody(response, 8 * 1024 * 1024);
    let body: JsonRecord;
    try {
      const parsed: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error();
      body = parsed as JsonRecord;
    } catch {
      throw new Error(response.ok
        ? "FAB local API did not return a valid JSON object"
        : `FAB local API returned ${response.status}`);
    }
    if (!response.ok) {
      const message = sanitizeExternalMessage(
        stringValue(body.error) || `FAB local API returned ${response.status}`,
      );
      const code = FAB_HTTP_ERROR_CODES[response.status];
      if (code) throw new TRPCError({ code, message });
      throw new Error(message);
    }
    return body;
  } catch (error) {
    if (controller.signal.aborted) {
      throw new Error(timedOut ? "FAB local API request timed out" : "FAB local API request cancelled");
    }
    throw error;
  } finally {
    if (isMutation) invalidateFabControlCenterSnapshot();
    clearTimeout(timeout);
    init.signal?.removeEventListener("abort", cancel);
    controller.abort();
  }
}

async function settleIndividualFabReads(
  entries: Array<[FabResourceKey, string]>,
): Promise<Array<PromiseSettledResult<JsonRecord>>> {
  const results = new Array<PromiseSettledResult<JsonRecord>>(entries.length);
  let nextIndex = 0;
  const workerCount = Math.min(MAX_CONCURRENT_READS, entries.length);
  await Promise.all(Array.from({ length: workerCount }, async () => {
    while (nextIndex < entries.length) {
      const index = nextIndex;
      nextIndex += 1;
      try {
        const [resource, path] = entries[index];
        results[index] = {
          status: "fulfilled",
          value: await fabLocalRequest(path, {}, { timeoutMs: READ_TIMEOUT_MS[resource] }),
        };
      } catch (reason) {
        results[index] = { status: "rejected", reason };
      }
    }
  }));
  return results;
}

function parseFabBatchResults(
  body: JsonRecord,
  entries: Array<[FabResourceKey, string]>,
): Array<PromiseSettledResult<JsonRecord>> | null {
  if (Number(body.version) !== 1) return null;
  const resources = asRecord(body.resources);
  if (!resources) return null;
  const results: Array<PromiseSettledResult<JsonRecord>> = [];
  for (const [resource] of entries) {
    const envelope = asRecord(resources[resource]);
    if (!envelope || typeof envelope.ok !== "boolean") return null;
    if (envelope.ok) {
      const value = asRecord(envelope.value);
      if (!value) return null;
      results.push({ status: "fulfilled", value });
    } else {
      results.push({
        status: "rejected",
        reason: new Error(stringValue(envelope.error) || "FAB resource could not be read"),
      });
    }
  }
  return results;
}

async function settleFabReads(
  entries: Array<[FabResourceKey, string]>,
): Promise<Array<PromiseSettledResult<JsonRecord>>> {
  const directEntries = entries.filter(([resource]) => CONTROL_CENTER_DIRECT_RESOURCES.has(resource));
  const batchEntries = entries.filter(([resource]) => !CONTROL_CENTER_DIRECT_RESOURCES.has(resource));
  const [batchOutcome, directResults] = await Promise.all([
    fabLocalRequest(CONTROL_CENTER_BATCH_PATH, {}, { timeoutMs: 12_000 }).then(
      (value): PromiseSettledResult<JsonRecord> => ({ status: "fulfilled", value }),
      (reason): PromiseSettledResult<JsonRecord> => ({ status: "rejected", reason }),
    ),
    settleIndividualFabReads(directEntries),
  ]);

  let batchResults = batchOutcome.status === "fulfilled"
    ? parseFabBatchResults(batchOutcome.value, batchEntries)
    : null;
  if (!batchResults) {
    const unsupportedBatch = batchOutcome.status === "rejected"
      && batchOutcome.reason instanceof TRPCError
      && ["NOT_FOUND", "METHOD_NOT_SUPPORTED"].includes(batchOutcome.reason.code);
    if (batchOutcome.status === "rejected" && !unsupportedBatch) {
      // Authentication, throttling and transport failures are not evidence of a legacy API.
      batchResults = batchEntries.map(() => ({ status: "rejected", reason: batchOutcome.reason }));
    } else {
      batchResults = await settleIndividualFabReads(batchEntries);
    }
  }

  const byResource = new Map<FabResourceKey, PromiseSettledResult<JsonRecord>>();
  directEntries.forEach(([resource], index) => byResource.set(resource, directResults[index]));
  batchEntries.forEach(([resource], index) => byResource.set(resource, batchResults[index]));
  return entries.map(([resource]) => byResource.get(resource) ?? {
    status: "rejected",
    reason: new Error("FAB resource result is missing"),
  });
}

export async function getFabControlCenter(): Promise<FabControlCenter> {
  const now = Date.now();
  if (controlCenterSnapshot && controlCenterSnapshot.expiresAt > now) {
    return controlCenterSnapshot.value;
  }
  if (controlCenterInFlight) return controlCenterInFlight;

  const generation = controlCenterCacheGeneration;
  const request = buildFabControlCenter(generation);
  controlCenterInFlight = request;
  try {
    const value = await request;
    if (value.connection.connected && generation === controlCenterCacheGeneration) {
      controlCenterSnapshot = {
        value,
        expiresAt: Date.now() + CONTROL_CENTER_CACHE_TTL_MS,
      };
    }
    return value;
  } finally {
    if (controlCenterInFlight === request) controlCenterInFlight = null;
  }
}

export async function refreshFabControlCenter(): Promise<FabControlCenter> {
  invalidateFabControlCenterSnapshot();
  return getFabControlCenter();
}

async function buildFabControlCenter(generation: number): Promise<FabControlCenter> {
  const checkedAt = new Date().toISOString();
  const startedAt = Date.now();
  let endpoint = DEFAULT_FAB_LOCAL_API_URL;
  try {
    endpoint = getFabBrowserApiBaseUrl().toString().replace(/\/$/, "");
  } catch (error) {
    return disconnectedControlCenter(
      endpoint,
      checkedAt,
      error instanceof Error ? error.message : "Invalid FAB public API configuration",
    );
  }
  try {
    getFabLocalApiBaseUrl();
  } catch (error) {
    return disconnectedControlCenter(
      endpoint,
      checkedAt,
      error instanceof Error ? error.message : "Invalid FAB local API configuration",
    );
  }

  const entries = Object.entries(READ_PATHS) as Array<[FabResourceKey, string]>;
  const settled = await settleFabReads(entries);
  if (generation !== controlCenterCacheGeneration) {
    throw new TRPCError({ code: "CONFLICT", message: "FAB dashboard changed while loading; refresh to read current data." });
  }
  const resources: Partial<Record<FabResourceKey, JsonRecord>> = {};
  const resourceStates = {} as Record<FabResourceKey, FabResourceState>;
  const partialErrors: FabControlCenter["partialErrors"] = [];
  settled.forEach((result, index) => {
    const resource = entries[index][0];
    if (result.status === "fulfilled") {
      resources[resource] = result.value;
      resourceCache.set(resource, { value: result.value, updatedAt: checkedAt });
      resourceStates[resource] = { state: "live", checkedAt, updatedAt: checkedAt, error: null };
      return;
    }
    const error = sanitizeExternalMessage(
      result.reason instanceof Error ? result.reason.message : result.reason,
      500,
      "Request failed",
    );
    const cached = resourceCache.get(resource);
    if (cached) {
      resources[resource] = cached.value;
      resourceStates[resource] = { state: "stale", checkedAt, updatedAt: cached.updatedAt, error };
    } else {
      resourceStates[resource] = { state: "error", checkedAt, updatedAt: null, error };
    }
    partialErrors.push({
      resource,
      error,
      state: resourceStates[resource].state,
      updatedAt: resourceStates[resource].updatedAt,
    });
  });

  const connected = resourceStates.liveness.state === "live";

  const metrics = resources.metrics || {};
  const reviewSummary = asRecord(resources.reviewQueue?.summary) || {};
  const exceptionsPayload = resources.exceptions || {};
  const settings = resources.settings || {};
  const sourceReadiness = resources.sourceReadiness || {};
  const waveSetup = resources.waveSetup || {};
  const waveReceiptExecutor = resources.waveReceiptExecutor || {};
  const cloudAccess = resources.cloudStatus || {};
  const registeredSources = arrayValue(resources.sources?.sources);
  const workflowRuns = arrayValue(resources.workflows?.workflowRuns);
  const reviewWorkItems = arrayValue(resources.reviewQueue?.workItems);
  const bankTransactions = arrayValue(resources.bankTransactions?.bankTransactions);
  const unreconciledBankTransactions = bankTransactions.filter((item) => ![
    "approved",
    "reconciled",
    "ignored",
  ].includes(stringValue(item.reconciliation_status || item.reconciliationStatus).toLowerCase()));
  const bankStatementImports = arrayValue(resources.bankTransactions?.bankStatementImports);
  const haiAllowedCommandIds = stringArray(resources.haiStatus?.allowedCommandIds);
  const sourceConnections = arrayValue(settings.sources).map((source) => {
    const sourceId = stringValue(source.id);
    const syncPlan = arrayValue(sourceReadiness.sources).find((item) => stringValue(item.source) === sourceId);
    const account = registeredSources.find((item) => {
      const sourceType = stringValue(item.source_type || item.sourceType);
      return sourceType === sourceId || sourceType === sourceId.replace("waveapps_", "waveapps");
    });
    const baseConnection = {
      ...source,
      canSync: Boolean(syncPlan?.canSync),
      enabled: syncPlan ? Boolean(syncPlan.enabled) : Boolean(source.configured),
      mode: syncPlan?.mode || source.mode || null,
      scannerProfile: syncPlan?.scannerProfile || null,
      connectorProfile: syncPlan?.connectorProfile || null,
      nextAction: syncPlan?.nextAction || null,
      lastSyncAt: account?.last_sync_at || account?.updated_at || null,
      accountStatus: account?.status || null,
    };
    if (sourceId !== "waveapps_business") return baseConnection;
    const setupStatus = stringValue(waveSetup.status, stringValue(source.status, "not_configured"));
    return {
      ...baseConnection,
      status: setupStatus,
      ready: waveSetup.ready === true,
      configured: waveSetup.accessTokenConfigured === true && Boolean(waveSetup.businessId),
      details: waveSetup.ready === true
        ? "Wave business and account mappings were verified from the live chart of accounts."
        : "Connect Wave, validate the business, and map the posting accounts.",
      nextAction: stringValue(asRecord(waveSetup.activation)?.nextAction, waveSetupNextAction(setupStatus)),
    };
  });
  const latestSafeCycle = workflowRuns.find((workflow) => (
    stringValue(workflow.status).toLowerCase() === "completed"
      && ["local_autonomous_cycle", "autonomy_run"].includes(
        stringValue(workflow.trigger_source, stringValue(workflow.triggerSource)).toLowerCase(),
      )
  ));

  return {
    connection: {
      connected,
      status: connected ? stringValue(resources.health?.status, "connected") : "disconnected",
      endpoint,
      authConfigured: Boolean(ENV.fabLocalApiToken),
      checkedAt,
      latencyMs: connected ? Date.now() - startedAt : null,
      error: connected ? null : resourceStates.liveness.error || "FAB local API is unavailable",
    },
    metrics: {
      documents: nullableNumber(metrics.documents),
      pendingReview: nullableNumber(metrics.pending_review),
      pendingReviewDocuments: nullableNumber(reviewSummary.documents),
      postingBlockedReviewDocuments: nullableNumber(reviewSummary.postingBlockedDocuments),
      unreconciled: sumNullable(metrics.unreconciled_bank_transactions, metrics.unreconciled_documents),
      unreconciledDocuments: nullableNumber(metrics.unreconciled_documents),
      unreconciledBankTransactions: nullableNumber(metrics.unreconciled_bank_transactions),
      exceptions: nullableNumber(asRecord(exceptionsPayload.summary)?.total),
      failedDocuments: nullableNumber(metrics.failed_documents),
    },
    decisionContext: {
      lastSafeCycleAt: latestSafeCycle
        ? nullableString(latestSafeCycle.finished_at || latestSafeCycle.finishedAt || latestSafeCycle.updated_at || latestSafeCycle.updatedAt)
        : null,
      latestWorkflowStatus: workflowRuns.length ? nullableString(workflowRuns[0].status) : null,
      dataThroughDate: resourceStates.masterLedger.state === "live" || resourceStates.masterLedger.state === "stale"
        ? nullableString(resources.masterLedger?.dataThroughDate)
        : null,
      sourceCount: resourceStates.sources.state === "live" || resourceStates.sources.state === "stale"
        ? registeredSources.length
        : null,
      readySourceCount: resourceStates.sources.state === "live" || resourceStates.sources.state === "stale"
        ? registeredSources.filter((source) => ["connected", "ready", "ok"].includes(
          stringValue(source.status).toLowerCase(),
        )).length
        : null,
      latestSourceSyncAt: resourceStates.sources.state === "live" || resourceStates.sources.state === "stale"
        ? latestDate(registeredSources.map((source) => source.last_sync_at || source.lastSyncAt || source.updated_at || source.updatedAt))
        : null,
      unreconciledAmountByCurrency: resourceStates.bankTransactions.state === "live" || resourceStates.bankTransactions.state === "stale"
        ? amountByCurrency(unreconciledBankTransactions)
        : null,
      oldestReviewAgeHours: resourceStates.reviewQueue.state === "live" || resourceStates.reviewQueue.state === "stale"
        ? reviewSummaryAgeHours(reviewSummary, checkedAt) ?? oldestReviewAgeHours(reviewWorkItems, checkedAt)
        : null,
      highPriorityExceptions: resourceStates.exceptions.state === "live" || resourceStates.exceptions.state === "stale"
        ? nullableNumber(asRecord(asRecord(exceptionsPayload.summary)?.bySeverity)?.high)
        : null,
      ledgerReadyForApproval: resourceStates.masterLedger.state === "live" || resourceStates.masterLedger.state === "stale"
        ? nullableNumber(asRecord(resources.masterLedger?.summary)?.readyForApproval)
        : null,
    },
    health: resources.health || {},
    autonomy: resources.autonomy || {},
    closeReadiness: resources.closeReadiness || {},
    delivery: {
      status: resources.driveWaveStatus || {},
      summary: asRecord(resources.driveWaveWorkOrders?.summary) || {},
      workOrders: arrayValue(resources.driveWaveWorkOrders?.workOrders).map(projectDeliveryWorkOrder),
      count: nullableNumber(resources.driveWaveWorkOrders?.count),
    },
    reviews: projectReviewQueue(resources.reviewQueue),
    gmailAuthorization: resources.gmailAuthorization || {},
    driveAuthorization: resources.driveAuthorization || {},
    waveSetup,
    waveReceiptExecutor,
    cloudAccess,
    banking: {
      recentImports: bankStatementImports.slice(0, 5).map((item) => ({
        ...selectFields(item, [
          "account_identifier",
          "created_at",
          "duplicates",
          "filename",
          "format",
          "id",
          "rows_imported",
          "rows_seen",
          "status",
          "updated_at",
        ]),
        identity_conflicts: nullableNumber((asRecord(item.metadata) || {}).identityConflicts) ?? 0,
      })),
    },
    exceptions: arrayValue(exceptionsPayload.exceptions),
    exceptionSummary: asRecord(exceptionsPayload.summary) || {},
    connections: [
      ...sourceConnections,
      {
        id: "wave_receipt_executor",
        label: "Wave receipt session",
        status: stringValue(waveReceiptExecutor.status, "not_connected"),
        configured: waveReceiptExecutor.enabled === true,
        ready: waveReceiptExecutor.ready === true,
        details: waveReceiptExecutor.ready === true
          ? "A fresh user-owned browser executor can upload and read receipts back into FAB verification."
          : "Non-secret HAI/browser bridge for Wave receipt upload and exact attachment readback.",
        nextAction: stringValue(
          waveReceiptExecutor.nextAction,
          "Connect a supervised HAI or browser executor using the local FAB manifest.",
        ),
        lastSyncAt: waveReceiptExecutor.lastSeenAt || null,
        missingCapabilities: stringArray(waveReceiptExecutor.missingCapabilities),
      },
      {
        id: "hai",
        label: "HAI connector",
        status: stringValue(resources.haiStatus?.status, "unavailable"),
        configured: Boolean(resources.haiStatus?.enabled),
        ready: resources.haiStatus?.status === "ready",
        details: resources.haiStatus?.status === "ready"
          ? `Governed machine control is enabled for ${haiAllowedCommandIds.length} allowlisted commands.`
          : "Governed machine-control contract for safe local FAB commands.",
        allowedCommandIds: haiAllowedCommandIds,
      },
      {
        id: "ngrok_cloud",
        label: "FAB cloud access",
        status: stringValue(cloudAccess.status, "not_running"),
        configured: cloudAccess.configured === true,
        ready: cloudAccess.active === true,
        details: cloudAccess.active === true
          ? "Authenticated HTTPS access is verified for the FAB API and HAI manifest."
          : "Project-owned ngrok access for the authenticated FAB API and HAI connector.",
        nextAction: stringValue(
          cloudAccess.nextAction,
          "Run Start-FAB-Ngrok.cmd after configuring a dedicated FAB endpoint.",
        ),
        lastSyncAt: cloudAccess.verifiedAt || null,
        publicUrl: cloudAccess.publicUrl || null,
        authMode: cloudAccess.authMode || null,
      },
    ],
    workflows: workflowRuns,
    recovery: resources.recovery || {},
    backups: projectBackups(resources.backups),
    reporting: projectReporting(resources.reportRuns),
    compliance: projectCompliance(resources.compliance),
    notifications: arrayValue(resources.notifications?.notifications),
    reconciliation: arrayValue(resources.reconciliation?.reconciliationMatches),
    activity: arrayValue(resources.activity?.auditEvents),
    hai: {
      status: resources.haiStatus || {},
      manifest: resources.haiManifest || {},
    },
    resourceStates,
    partialErrors,
  };
}

export function resetFabControlCenterCacheForTests() {
  resourceCache.clear();
  invalidateFabControlCenterSnapshot();
}

function invalidateFabControlCenterSnapshot() {
  controlCenterSnapshot = null;
  controlCenterInFlight = null;
  controlCenterCacheGeneration += 1;
}

export async function runFabOperatorCommand(
  commandId: FabOperatorCommandId,
  actor: string,
  payload: JsonRecord = {},
): Promise<JsonRecord> {
  const command = COMMAND_PATHS[commandId];
  const safeActor = actor.trim().slice(0, 200) || "fab_dashboard";
  return fabLocalRequest(command.path, {
    method: command.method || "POST",
    body: JSON.stringify({ ...command.body, ...payload, actor: safeActor }),
  });
}

export async function createFabBackup(actor: string): Promise<JsonRecord> {
  const response = await fabLocalRequest("/api/backups", {
    method: "POST",
    body: JSON.stringify({
      actor: actor.trim().slice(0, 200) || "fab_dashboard:local_operator",
      note: "Created from the FAB operator dashboard.",
      requireCompleteSourceEvidence: true,
    }),
  }, { timeoutMs: 120_000 });
  const manifest = selectFields(response.manifest, [
    "createdAt",
    "format",
    "ledgerBytes",
    "ledgerSha256",
  ]);
  manifest.sourceEvidence = selectFields(asRecord(response.manifest)?.sourceEvidence, [
    "coverageStatus",
    "gapCount",
    "includedBytes",
    "includedDocuments",
    "includedFiles",
    "totalDocuments",
  ]);
  return {
    ...selectFields(response, ["backupFilename", "status", "success"]),
    manifest,
  };
}

export async function createFabSupportBundle(actor: string): Promise<JsonRecord> {
  const response = await fabLocalRequest("/api/support-bundles", {
    method: "POST",
    body: JSON.stringify({
      actor: actor.trim().slice(0, 200) || "fab_dashboard:local_operator",
      note: "Created from the FAB operator dashboard.",
    }),
  }, { timeoutMs: 120_000 });
  return selectFields(response, [
    "bundleFilename",
    "externalSubmission",
    "privacy",
    "sha256",
    "sizeBytes",
    "status",
    "success",
  ]);
}

export async function uploadFabIntakeFile(input: {
  filename: string;
  mimeType?: string;
  contentBase64: string;
}): Promise<JsonRecord> {
  return fabLocalRequest("/api/intake/upload", {
    method: "POST",
    body: JSON.stringify(input),
  }, { timeoutMs: 20_000 });
}

export async function uploadFabGoogleDriveCredentials(input: {
  filename: string;
  contentBase64: string;
  replace?: boolean;
  actor: string;
}): Promise<JsonRecord> {
  return fabLocalRequest("/api/connectors/google-drive/credentials", {
    method: "POST",
    body: JSON.stringify({
      filename: input.filename,
      contentBase64: input.contentBase64,
      replace: input.replace ?? false,
      actor: input.actor.trim().slice(0, 200) || "fab_dashboard:local_operator",
    }),
  }, { timeoutMs: 20_000 });
}

export async function uploadFabGmailCredentials(input: {
  filename: string;
  contentBase64: string;
  replace?: boolean;
  actor: string;
}): Promise<JsonRecord> {
  return fabLocalRequest("/api/connectors/gmail/credentials", {
    method: "POST",
    body: JSON.stringify({
      filename: input.filename,
      contentBase64: input.contentBase64,
      replace: input.replace ?? false,
      actor: input.actor.trim().slice(0, 200) || "fab_dashboard:local_operator",
    }),
  }, { timeoutMs: 20_000 });
}

export async function startFabGmailAuthorization(actor: string): Promise<JsonRecord> {
  return fabLocalRequest("/api/connectors/gmail/authorization/start", {
    method: "POST",
    body: JSON.stringify({
      actor: actor.trim().slice(0, 200) || "fab_dashboard:local_operator",
    }),
  });
}

export async function startFabGoogleDriveAuthorization(actor: string): Promise<JsonRecord> {
  return fabLocalRequest("/api/connectors/google-drive/authorization/start", {
    method: "POST",
    body: JSON.stringify({
      actor: actor.trim().slice(0, 200) || "fab_dashboard:local_operator",
    }),
  });
}

export async function saveFabWaveSetup(input: {
  targetSystem?: "waveapps_business" | "waveapps_personal";
  accessToken?: string;
  businessId?: string;
  anchorAccountId?: string;
  defaultCategoryAccountId?: string;
  categoryAccountIds?: Record<string, string>;
  clearAccessToken?: boolean;
  actor: string;
}): Promise<JsonRecord> {
  return fabLocalRequest("/api/wave/setup", {
    method: "PUT",
    body: JSON.stringify({
      ...input,
      actor: input.actor.trim().slice(0, 200) || "fab_dashboard:local_operator",
    }),
  });
}

export async function validateFabWaveSetup(
  targetSystem: "waveapps_business" | "waveapps_personal" = "waveapps_business",
): Promise<JsonRecord> {
  return fabLocalRequest("/api/wave/setup/validate", {
    method: "POST",
    body: JSON.stringify({ targetSystem }),
  }, { timeoutMs: 20_000 });
}

export async function importFabBankStatement(input: {
  filename: string;
  format: "csv" | "json" | "camt" | "mt940";
  accountIdentifier: string;
  contentBase64: string;
  actor: string;
}): Promise<JsonRecord> {
  const response = await fabLocalRequest("/api/bank-transactions/import", {
    method: "POST",
    body: JSON.stringify({
      filename: input.filename,
      format: input.format,
      accountIdentifier: input.accountIdentifier.trim().slice(0, 200) || "default",
      contentBase64: input.contentBase64,
      source: "dashboard_bank_statement",
      actor: input.actor.trim().slice(0, 200) || "fab_dashboard:local_operator",
    }),
  }, { timeoutMs: 20_000 });
  return selectFields(response, [
    "accountIdentifier",
    "bankStatementImportId",
    "duplicates",
    "externalSubmission",
    "filename",
    "format",
    "identityConflictRows",
    "identityConflicts",
    "identityConflictsTruncated",
    "rowsImported",
    "rowsSeen",
    "skipped",
    "status",
    "success",
  ]);
}

export async function getFabReviewPage(input: {
  offset: number;
  limit?: number;
}): Promise<FabControlCenter["reviews"]> {
  const offset = Math.max(0, Math.min(Math.trunc(input.offset), 10_000_000));
  const limit = Math.max(1, Math.min(Math.trunc(input.limit ?? 50), 100));
  const payload = await fabLocalRequest(
    `/api/review?status=open&limit=${limit}&offset=${offset}&view=summary&includeSummary=false&includeCategoryOptions=false`,
  );
  return projectReviewQueue(payload);
}

export async function resolveFabReviewItem(input: {
  reviewItemId: number;
  status: "approved" | "rejected" | "resolved" | "ignored";
  resolution: string;
  corrections?: {
    vendorName?: string;
    category?: string;
    transactionDate?: string;
    totalAmount?: number;
    vatAmount?: number;
    targetSystem?: string;
    duplicateOfDocumentId?: number;
    duplicateCandidateId?: number;
    documentType?: "receipt" | "vendor_invoice" | "credit_note" | "order_confirmation" | "estimate" | "bank_statement" | "insurance_policy" | "government_correspondence";
  };
  learnRule?: boolean;
  applyToMatchingVendor?: boolean;
}): Promise<JsonRecord> {
  return fabLocalRequest(`/api/review/${input.reviewItemId}/resolve`, {
    method: "POST",
    body: JSON.stringify({
      status: input.status,
      resolution: input.resolution,
      corrections: input.corrections || {},
      learnRule: input.learnRule ?? true,
      applyToMatchingVendor: input.applyToMatchingVendor ?? false,
    }),
  });
}

function disconnectedControlCenter(endpoint: string, checkedAt: string, error: string): FabControlCenter {
  const resourceStates = Object.fromEntries(
    (Object.keys(READ_PATHS) as FabResourceKey[]).map((resource) => [
      resource,
      { state: "unavailable", checkedAt, updatedAt: null, error },
    ]),
  ) as Record<FabResourceKey, FabResourceState>;
  return {
    connection: {
      connected: false,
      status: "disconnected",
      endpoint,
      authConfigured: Boolean(ENV.fabLocalApiToken),
      checkedAt,
      latencyMs: null,
      error,
    },
    metrics: {
      documents: null,
      pendingReview: null,
      pendingReviewDocuments: null,
      postingBlockedReviewDocuments: null,
      unreconciled: null,
      unreconciledDocuments: null,
      unreconciledBankTransactions: null,
      exceptions: null,
      failedDocuments: null,
    },
    decisionContext: {
      lastSafeCycleAt: null,
      latestWorkflowStatus: null,
      dataThroughDate: null,
      sourceCount: null,
      readySourceCount: null,
      latestSourceSyncAt: null,
      unreconciledAmountByCurrency: null,
      oldestReviewAgeHours: null,
      highPriorityExceptions: null,
      ledgerReadyForApproval: null,
    },
    health: {},
    autonomy: {},
    closeReadiness: {},
    delivery: { status: {}, summary: {}, workOrders: [], count: null },
    reviews: { workItems: [], categoryOptions: [], summary: {}, pagination: {} },
    gmailAuthorization: {},
    driveAuthorization: {},
    waveSetup: {},
    waveReceiptExecutor: {},
    cloudAccess: {},
    banking: { recentImports: [] },
    exceptions: [],
    exceptionSummary: {},
    connections: [],
    workflows: [],
    recovery: {},
    backups: { backups: [], schedule: {}, restorePolicy: {}, verificationMode: null },
    reporting: { scheduleStatus: {}, reportRuns: [], externalSubmission: null },
    compliance: {
      summary: {},
      assessments: [],
      statutoryStatus: null,
      filingStatus: null,
      externalFiling: null,
    },
    notifications: [],
    reconciliation: [],
    activity: [],
    hai: { status: {}, manifest: {} },
    resourceStates,
    partialErrors: [],
  };
}

function asRecord(value: unknown): JsonRecord | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as JsonRecord : null;
}

function arrayValue(value: unknown): JsonRecord[] {
  return Array.isArray(value) ? value.flatMap((item) => asRecord(item) ? [asRecord(item)!] : []) : [];
}

function projectDeliveryWorkOrder(value: JsonRecord): JsonRecord {
  const source = selectFields(value.source, ["filename", "mimeType", "provider", "sha256"]);
  const wave = selectFields(value.wave, ["externalTransactionId", "targetSystem"]);
  const archivePlan = selectFields(value.archivePlan, [
    "canArchive",
    "evidenceVerified",
    "externalSubmission",
    "reasons",
    "retentionStatus",
    "status",
  ]);
  const reviews = selectFields(value.reviews, ["blocking", "open", "reasons"]);
  return {
    ...selectFields(value, [
      "actionRequired",
      "documentId",
      "externalSubmission",
      "stage",
      "status",
      "success",
      "workOrderId",
      "workOrderVersion",
    ]),
    source,
    wave,
    archivePlan,
    reviews,
  };
}

function projectBackups(value: unknown): FabControlCenter["backups"] {
  const payload = asRecord(value) || {};
  return {
    verificationMode: stringValue(payload.verificationMode) || null,
    restorePolicy: selectFields(payload.restorePolicy, [
      "externalSubmission",
      "ledgerRestoreSupported",
      "maintenanceMode",
      "nextAction",
      "sourceEvidenceRestoreSupported",
      "status",
      "workerMustBeStopped",
    ]),
    backups: arrayValue(payload.backups).map((backup) => selectFields(backup, [
      "backupFilename",
      "createdAt",
      "format",
      "ledgerBytes",
      "ledgerSha256",
      "sizeBytes",
      "sourceEvidenceBytes",
      "sourceEvidenceDocuments",
      "sourceEvidenceFiles",
      "sourceEvidenceGaps",
      "sourceEvidenceStatus",
      "status",
    ])),
    schedule: selectFields(payload.schedule, [
      "due",
      "intervalHours",
      "invalidBackupCount",
      "integrityVerification",
      "lastSuccessfulAt",
      "latestBackupFilename",
      "latestLedgerSha256",
      "nextDueAt",
      "reason",
      "requireCompleteSourceEvidence",
      "sourceEvidenceBytes",
      "sourceEvidenceDocuments",
      "sourceEvidenceFiles",
      "sourceEvidenceGaps",
      "sourceEvidenceStatus",
      "status",
    ]),
  };
}

function projectReporting(value: unknown): FabControlCenter["reporting"] {
  const payload = asRecord(value) || {};
  const scheduleStatus = asRecord(payload.scheduleStatus) || {};
  const schedule = selectFields(scheduleStatus.schedule, [
    "basis",
    "formats",
    "frequency",
    "periodMode",
    "reportType",
    "scheduleId",
    "targetSystem",
    "timezone",
  ]);
  const slot = selectFields(scheduleStatus.slot, [
    "nextDueAt",
    "period",
    "scheduledFor",
    "scheduleSlot",
  ]);
  if (slot.period) slot.period = selectFields(slot.period, ["fromDate", "toDate"]);
  const existingReportRun = projectReportRun(asRecord(scheduleStatus.existingReportRun) || {});
  const compactScheduleStatus: JsonRecord = {
    ...selectFields(scheduleStatus, ["due", "enabled", "externalSubmission", "status"]),
    schedule,
    slot,
  };
  if (Object.keys(existingReportRun).length) compactScheduleStatus.existingReportRun = existingReportRun;
  return {
    scheduleStatus: compactScheduleStatus,
    reportRuns: arrayValue(payload.reportRuns).map(projectReportRun),
    externalSubmission: nullableString(payload.externalSubmission),
  };
}

function projectReportRun(run: JsonRecord): JsonRecord {
  if (!Object.keys(run).length) return {};
  return {
    id: run.id,
    scheduleId: run.schedule_id ?? run.scheduleId,
    scheduleSlot: run.schedule_slot ?? run.scheduleSlot,
    reportType: run.report_type ?? run.reportType,
    basis: run.basis,
    periodFrom: run.period_from ?? run.periodFrom,
    periodTo: run.period_to ?? run.periodTo,
    targetSystem: run.target_system ?? run.targetSystem,
    scheduledFor: run.scheduled_for ?? run.scheduledFor,
    status: run.status,
    readiness: run.readiness,
    rowCount: run.row_count ?? run.rowCount,
    blockerCount: run.blocker_count ?? run.blockerCount,
    attemptCount: run.attempt_count ?? run.attemptCount,
    externalSubmission: run.external_submission ?? run.externalSubmission,
    jsonSha256: run.json_sha256 ?? run.jsonSha256,
    jsonBytes: run.json_bytes ?? run.jsonBytes,
    csvSha256: run.csv_sha256 ?? run.csvSha256,
    csvBytes: run.csv_bytes ?? run.csvBytes,
    hasJsonArtifact: Boolean(run.json_path ?? run.jsonPath),
    hasCsvArtifact: Boolean(run.csv_path ?? run.csvPath),
    createdAt: run.created_at ?? run.createdAt,
    updatedAt: run.updated_at ?? run.updatedAt,
    finishedAt: run.finished_at ?? run.finishedAt,
    nextRetryAt: run.next_retry_at ?? run.nextRetryAt,
  };
}

function projectCompliance(value: unknown): FabControlCenter["compliance"] {
  const payload = asRecord(value) || {};
  return {
    summary: selectFields(payload.summary, [
      "assessmentCount",
      "attentionFindings",
      "blockingFindings",
      "externalFiling",
      "filingStatus",
      "openFindings",
      "retentionRecords",
      "statutoryStatus",
    ]),
    assessments: arrayValue(payload.assessments).map(projectComplianceAssessment),
    statutoryStatus: nullableString(payload.statutoryStatus),
    filingStatus: nullableString(payload.filingStatus),
    externalFiling: nullableString(payload.externalFiling),
  };
}

function projectComplianceAssessment(assessment: JsonRecord): JsonRecord {
  return {
    id: assessment.id,
    periodFrom: assessment.period_from ?? assessment.periodFrom,
    periodTo: assessment.period_to ?? assessment.periodTo,
    basis: assessment.basis,
    targetSystem: assessment.target_system ?? assessment.targetSystem,
    status: assessment.status,
    recordCount: assessment.record_count ?? assessment.recordCount,
    findingCount: assessment.finding_count ?? assessment.findingCount,
    blockingCount: assessment.blocking_count ?? assessment.blockingCount,
    attentionCount: assessment.attention_count ?? assessment.attentionCount,
    sourceChecksum: assessment.source_checksum ?? assessment.sourceChecksum,
    statutoryStatus: assessment.statutory_status ?? assessment.statutoryStatus,
    externalFiling: assessment.external_filing ?? assessment.externalFiling,
    createdAt: assessment.created_at ?? assessment.createdAt,
    updatedAt: assessment.updated_at ?? assessment.updatedAt,
  };
}

function projectReviewWorkItem(value: JsonRecord): JsonRecord {
  const document = selectFields(value.document, [
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
  ]);
  const categorySuggestion = selectFields(
    asRecord(value.document)?.categorySuggestion,
    ["category", "confidenceScore", "matchPolicy", "rationale", "source"],
  );
  if (Object.keys(categorySuggestion).length) {
    document.categorySuggestion = categorySuggestion;
  }
  const duplicateCandidates = arrayValue(value.duplicateCandidates).map((candidate) => ({
    ...selectFields(candidate, [
      "candidateDocumentId",
      "confidenceScore",
      "conflictingIdentityFields",
      "comparableFields",
      "id",
      "matchType",
      "matchedIdentityFields",
      "reason",
      "similarityScore",
    ]),
    currentIdentity: selectFields(candidate.currentIdentity, [
      "amount",
      "date",
      "invoiceNumber",
      "orderNumber",
      "postingPolarity",
      "receiptNumber",
      "tax",
      "transactionReference",
      "vendor",
    ]),
    candidateIdentity: selectFields(candidate.candidateIdentity, [
      "amount",
      "date",
      "invoiceNumber",
      "orderNumber",
      "postingPolarity",
      "receiptNumber",
      "tax",
      "transactionReference",
      "vendor",
    ]),
    document: selectFields(candidate.document, [
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
    ]),
  }));
  const reviewItems = arrayValue(value.reviewItems).map((item) => selectFields(
    item,
    ["createdAt", "details", "id", "reason", "status", "updatedAt"],
  ));
  return {
    ...selectFields(value, [
      "documentId",
      "id",
      "reasons",
      "reviewPath",
      "status",
    ]),
    document,
    duplicateCandidates,
    reviewItems,
  };
}

function projectReviewQueue(value: unknown): FabControlCenter["reviews"] {
  const payload = asRecord(value) || {};
  return {
    workItems: arrayValue(payload.workItems).map(projectReviewWorkItem),
    categoryOptions: stringArray(payload.categoryOptions),
    summary: selectFields(payload.summary, [
      "categorySuggestions",
      "documents",
      "duplicateCandidates",
      "evidenceOnlyDocuments",
      "evidenceOnlyReviewItems",
      "newestReviewCreatedAt",
      "oldestReviewCreatedAt",
      "postingBlockedDocuments",
      "postingBlockedReviewItems",
      "reviewItems",
      "workItems",
    ]),
    pagination: selectFields(payload.pagination, [
      "hasMore",
      "limit",
      "nextOffset",
      "offset",
      "previousOffset",
      "returned",
      "scope",
      "total",
    ]),
  };
}

function selectFields(value: unknown, fields: string[]): JsonRecord {
  const source = asRecord(value);
  if (!source) return {};
  return Object.fromEntries(
    fields
      .filter((field) => Object.prototype.hasOwnProperty.call(source, field))
      .map((field) => [field, source[field]]),
  );
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function stringValue(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function nullableString(value: unknown): string | null {
  const result = stringValue(value).trim();
  return result || null;
}

function latestDate(values: unknown[]): string | null {
  const valid = values
    .flatMap((value) => {
      const raw = nullableString(value);
      const timestamp = raw ? Date.parse(raw) : Number.NaN;
      return raw && !Number.isNaN(timestamp) ? [{ raw, timestamp }] : [];
    })
    .sort((left, right) => right.timestamp - left.timestamp);
  return valid[0]?.raw || null;
}

function amountByCurrency(transactions: JsonRecord[]): Record<string, number> {
  return transactions.reduce<Record<string, number>>((totals, transaction) => {
    const amount = nullableNumber(transaction.amount);
    if (amount === null) return totals;
    const currency = stringValue(transaction.currency, "UNKNOWN").toUpperCase();
    totals[currency] = Number(((totals[currency] || 0) + Math.abs(amount)).toFixed(2));
    return totals;
  }, {});
}

function oldestReviewAgeHours(workItems: JsonRecord[], checkedAt: string): number | null {
  const timestamps = workItems.flatMap((item) => arrayValue(item.reviewItems))
    .flatMap((review) => {
      const raw = nullableString(review.createdAt || review.created_at);
      const timestamp = raw ? Date.parse(raw) : Number.NaN;
      return Number.isNaN(timestamp) ? [] : [timestamp];
    });
  if (!timestamps.length) return null;
  return Math.max(0, Math.round((Date.parse(checkedAt) - Math.min(...timestamps)) / 3_600_000));
}

function reviewSummaryAgeHours(summary: JsonRecord, checkedAt: string): number | null {
  const oldest = nullableString(summary.oldestReviewCreatedAt);
  if (!oldest) return null;
  const oldestTimestamp = Date.parse(oldest);
  const checkedTimestamp = Date.parse(checkedAt);
  if (Number.isNaN(oldestTimestamp) || Number.isNaN(checkedTimestamp)) return null;
  return Math.max(0, Number(((checkedTimestamp - oldestTimestamp) / 3_600_000).toFixed(1)));
}

function waveSetupNextAction(status: string): string {
  if (status === "needs_token") return "Add the user-owned Wave access token.";
  if (status === "needs_business_id") return "Select the Wave business to operate.";
  if (status === "needs_validation") return "Validate the Wave business and load its chart of accounts.";
  if (status === "needs_mapping") return "Map the verified funding account and every FAB category currently in use.";
  if (status === "ready") return "Wave is ready for governed bookkeeping operations.";
  return "Review the Wave connection setup.";
}

function nullableNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function sumNullable(left: unknown, right: unknown): number | null {
  const leftNumber = nullableNumber(left);
  const rightNumber = nullableNumber(right);
  return leftNumber === null || rightNumber === null ? null : leftNumber + rightNumber;
}
