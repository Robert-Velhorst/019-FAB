import { afterEach, describe, expect, it, vi } from "vitest";
import { createServer } from "node:http";
import { once } from "node:events";
import type { AddressInfo } from "node:net";
import {
  createFabBackup,
  createFabSupportBundle,
  fabLocalRequest,
  getFabBrowserApiBaseUrl,
  getFabControlCenter,
  getFabLocalApiBaseUrl,
  getFabReviewPage,
  importFabBankStatement,
  refreshFabControlCenter,
  resetFabControlCenterCacheForTests,
  resolveFabReviewItem,
  runFabOperatorCommand,
  saveFabWaveSetup,
  startFabGmailAuthorization,
  startFabGoogleDriveAuthorization,
  uploadFabGmailCredentials,
  uploadFabGoogleDriveCredentials,
  uploadFabIntakeFile,
  validateFabWaveSetup,
} from "./fabLocalGateway";

afterEach(() => {
  resetFabControlCenterCacheForTests();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("FAB local API gateway", () => {
  it.each([
    [400, "BAD_REQUEST"], [401, "UNAUTHORIZED"], [403, "FORBIDDEN"],
    [404, "NOT_FOUND"], [405, "METHOD_NOT_SUPPORTED"], [409, "CONFLICT"], [413, "PAYLOAD_TOO_LARGE"],
    [422, "UNPROCESSABLE_CONTENT"], [429, "TOO_MANY_REQUESTS"], [503, "SERVICE_UNAVAILABLE"],
  ])("preserves backend rejection %s as %s without retrying", async (status, code) => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ error: "Synthetic rejection" }), { status: Number(status) }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(fabLocalRequest("/api/test", { method: "POST", body: "{}" }))
      .rejects.toMatchObject({ code, message: "Synthetic rejection" });
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("preserves a rejected review as a conflict without replaying it", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      success: false, status: "stale_evidence", error: "Reconciliation evidence changed; refresh before approval.",
    }), { status: 409 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(resolveFabReviewItem({ reviewItemId: 1, status: "approved", resolution: "Synthetic approval" }))
      .rejects.toMatchObject({ code: "CONFLICT", message: "Reconciliation evidence changed; refresh before approval." });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("sanitizes classified errors and uses HTTP status rather than a body-supplied code", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      code: "UNAUTHORIZED", error: "token=synthetic-private-token " + "x".repeat(700),
    }), { status: 400 })));
    try {
      await fabLocalRequest("/api/test");
      throw new Error("Expected rejection");
    } catch (error) {
      expect(error).toMatchObject({ code: "BAD_REQUEST" });
      const message = (error as Error).message;
      expect(message).not.toContain("synthetic-private-token");
      expect(message).toContain("[REDACTED]");
      expect(message.length).toBeLessThanOrEqual(500);
    }
  });

  it("preserves rejection classification over real HTTP without replaying a mutation", async () => {
    let submissions = 0;
    const server = createServer((req, res) => {
      submissions += 1;
      req.resume();
      res.writeHead(413, { "content-type": "application/json" });
      res.end('{"error":"Synthetic upload exceeds the limit"}');
    });
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    try {
      await expect(fabLocalRequest("/api/test", { method: "POST", body: "{}" }, {
        baseUrl: `http://127.0.0.1:${(server.address() as AddressInfo).port}`,
      })).rejects.toMatchObject({ code: "PAYLOAD_TOO_LARGE", message: "Synthetic upload exceeds the limit" });
      expect(submissions).toBe(1);
    } finally {
      await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections(); });
    }
  });

  it("does not submit a pre-cancelled request", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(fabLocalRequest("/api/test", { signal: AbortSignal.abort() })).rejects.toThrow("cancelled");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([0, -1, NaN, 120_001])("rejects invalid timeout %s before submission", async timeoutMs => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(fabLocalRequest("/api/test", {}, { timeoutMs })).rejects.toThrow("Invalid");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("propagates caller cancellation during a body read", async () => {
    const caller = new AbortController();
    vi.stubGlobal("fetch", vi.fn(async (_url, init) => {
      const body = new ReadableStream({ start(controller) {
        init.signal.addEventListener("abort", () => controller.error(new DOMException("Aborted", "AbortError")));
      } });
      queueMicrotask(() => caller.abort());
      return new Response(body);
    }));
    await expect(fabLocalRequest("/api/test", { signal: caller.signal })).rejects.toThrow("cancelled");
  });

  it("rejects oversized JSON and invalid UTF-8 instead of returning success", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", {
      headers: { "content-length": String(8 * 1024 * 1024 + 1) },
    })));
    await expect(fabLocalRequest("/api/test")).rejects.toThrow("size limit");
    vi.stubGlobal("fetch", vi.fn(async () => new Response(new Uint8Array([123, 34, 120, 34, 58, 34, 255, 34, 125]))));
    await expect(fabLocalRequest("/api/test")).rejects.toThrow("JSON object");
  });

  it("does not replay a mutation or credentials through a real redirect", async () => {
    const forwarded: string[] = [];
    const server = createServer((req, res) => {
      if (req.url === "/start") {
        res.writeHead(307, { location: "/destination" });
        res.end();
      } else {
        forwarded.push(req.headers.authorization || "");
        req.resume();
        res.writeHead(200, { "content-type": "application/json" });
        res.end('{"success":true}');
      }
    });
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    try {
      await expect(fabLocalRequest("/start", { method: "POST", body: '{"synthetic":true}', redirect: "follow" }, {
        baseUrl: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, token: "synthetic-token",
      })).rejects.toThrow(/redirect/i);
      expect(forwarded).toEqual([]);
    } finally {
      await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections(); });
    }
  });

  it.each(["not-json", "null", "[]", '"text"'])("rejects malformed successful payload %j", async body => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(body, { status: 200 })));
    await expect(fabLocalRequest("/api/test")).rejects.toThrow(/JSON object/i);
  });

  it("does not swallow a timeout while reading the response", async () => {
    vi.stubGlobal("fetch", vi.fn(async (_url, init) => ({
      ok: true, status: 200, headers: new Headers(),
      json: async () => { throw new DOMException("Aborted", "AbortError"); },
      body: new ReadableStream({ start(controller) {
        init.signal.addEventListener("abort", () => controller.error(new DOMException("Aborted", "AbortError")));
      } }),
    })));
    await expect(fabLocalRequest("/api/test", {}, { timeoutMs: 10 })).rejects.toThrow("timed out");
  });

  it("times out a real HTTP body that never finishes", async () => {
    let bodyStarted = false;
    const server = createServer((_req, res) => {
      bodyStarted = true;
      res.writeHead(200, { "content-type": "application/json" });
      res.write('{"unfinished":');
    });
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    try {
      await expect(fabLocalRequest("/api/test", {}, {
        baseUrl: `http://127.0.0.1:${(server.address() as AddressInfo).port}`,
        timeoutMs: 250,
      })).rejects.toThrow("timed out");
      expect(bodyStarted).toBe(true);
    } finally {
      await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections(); });
    }
  });

  it("rejects insecure non-loopback endpoints", () => {
    expect(() => getFabLocalApiBaseUrl("http://accounting.example.test"))
      .toThrow("must use https");
    expect(getFabLocalApiBaseUrl("http://127.0.0.1:5001").hostname).toBe("127.0.0.1");
    expect(getFabLocalApiBaseUrl("http://api:5001", ["api"]).hostname).toBe("api");
    expect(() => getFabLocalApiBaseUrl("http://worker:5001", ["api"]))
      .toThrow("must use https");
  });

  it("only exposes clean browser-reachable API origins", () => {
    expect(getFabBrowserApiBaseUrl("http://127.0.0.1:5511").origin)
      .toBe("http://127.0.0.1:5511");
    expect(getFabBrowserApiBaseUrl("https://fab.example.test/").origin)
      .toBe("https://fab.example.test");
    expect(() => getFabBrowserApiBaseUrl("http://api:5001"))
      .toThrow("must use https");
    expect(() => getFabBrowserApiBaseUrl("https://operator:secret@fab.example.test"))
      .toThrow("must not contain credentials");
    expect(() => getFabBrowserApiBaseUrl("https://fab.example.test/private"))
      .toThrow("without a path");
    expect(() => getFabBrowserApiBaseUrl("https://fab.example.test?token=secret"))
      .toThrow("without a path");
    expect(() => getFabBrowserApiBaseUrl("https://fab.example.test#private"))
      .toThrow("without a path");
  });

  it("keeps the local API token server-side", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(new Headers(init?.headers).get("authorization")).toBe("Bearer private-token");
      return new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await fabLocalRequest("/api/health", {}, {
      baseUrl: "http://127.0.0.1:5001",
      token: "private-token",
    });

    expect(result).toEqual({ status: "ok" });
    expect(JSON.stringify(result)).not.toContain("private-token");
  });

  it("redacts secrets from local API error responses", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      error: "Wave rejected access_token=response-secret Authorization: Bearer bearer-secret " + "x".repeat(1_000),
    }), {
      status: 502,
      headers: { "content-type": "application/json" },
    })));

    await expect(fabLocalRequest("/api/test", {}, {
      baseUrl: "http://127.0.0.1:5001",
      token: "request-secret",
    })).rejects.toSatisfy((error: unknown) => {
      const message = error instanceof Error ? error.message : String(error);
      expect(message).toContain("[REDACTED]");
      expect(message.length).toBeLessThanOrEqual(500);
      expect(message).not.toContain("response-secret");
      expect(message).not.toContain("bearer-secret");
      expect(message).not.toContain("request-secret");
      return true;
    });
  });

  it("loads a bounded review page and projects only dashboard fields", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      expect(url.pathname).toBe("/api/review");
      expect(url.searchParams.get("status")).toBe("open");
      expect(url.searchParams.get("offset")).toBe("0");
      expect(url.searchParams.get("limit")).toBe("100");
      expect(url.searchParams.get("view")).toBe("summary");
      expect(url.searchParams.get("includeSummary")).toBe("false");
      expect(url.searchParams.get("includeCategoryOptions")).toBe("false");
      return new Response(JSON.stringify({
        workItems: [{
          id: "document-7",
          documentId: 7,
          document: { filename: "receipt.pdf", privateMetadata: "omit" },
          reviewItems: [{ id: 9, reason: "manual_review", correctedData: { secret: true } }],
        }],
        categoryOptions: ["Office Supplies"],
        summary: {
          workItems: 119,
          documents: 118,
          postingBlockedDocuments: 117,
          oldestReviewCreatedAt: "2026-01-01T00:00:00Z",
          privateSummary: "omit",
        },
        pagination: {
          scope: "work_items",
          offset: 0,
          limit: 100,
          returned: 1,
          total: 119,
          hasMore: true,
          nextOffset: 100,
          privateCursor: "omit",
        },
      }), { status: 200, headers: { "content-type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await getFabReviewPage({ offset: -25, limit: 500 });

    expect(result.pagination).toEqual({
      scope: "work_items",
      offset: 0,
      limit: 100,
      returned: 1,
      total: 119,
      hasMore: true,
      nextOffset: 100,
    });
    expect(result.summary).toMatchObject({
      workItems: 119,
      documents: 118,
      postingBlockedDocuments: 117,
      oldestReviewCreatedAt: "2026-01-01T00:00:00Z",
    });
    expect(result.workItems[0]).toMatchObject({
      id: "document-7",
      documentId: 7,
      document: { filename: "receipt.pdf" },
      reviewItems: [{ id: 9, reason: "manual_review" }],
    });
    expect(JSON.stringify(result)).not.toContain("private");
    expect(JSON.stringify(result)).not.toContain("secret");
  });

  it("creates a sanitized support bundle through the local API", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.method).toBe("POST");
      expect(JSON.parse(String(init?.body))).toMatchObject({
        actor: "operator-16",
        note: "Created from the FAB operator dashboard.",
      });
      return new Response(JSON.stringify({
        success: true,
        status: "created",
        bundleFilename: "fab-support-20260808.zip",
        bundlePath: "C:\\private\\fab-support-20260808.zip",
        sha256: "b".repeat(64),
        privacy: { containsCredentials: false },
      }), { status: 201, headers: { "content-type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await createFabSupportBundle("operator-16");

    expect(result.bundleFilename).toBe("fab-support-20260808.zip");
    expect(result.bundlePath).toBeUndefined();
    expect(result.privacy).toEqual({ containsCredentials: false });
  });

  it("aggregates authoritative ledger state into the control center", async () => {
    const fixtures: Record<string, unknown> = {
      "/api/live": { status: "ok", authRequired: true },
      "/api/health": { status: "attention", operations: { status: "attention" } },
      "/api/dashboard": {
        documents: 18,
        pending_review: 4,
        unreconciled_bank_transactions: 3,
        unreconciled_documents: 2,
        failed_documents: 1,
      },
      "/api/autonomy/plan": { status: "ready", actions: [] },
      "/api/exceptions": {
        summary: { total: 2, bySeverity: { high: 1, medium: 1, low: 0 } },
        exceptions: [{ id: "exception-1", severity: "high" }],
      },
      "/api/settings": {
        sources: [
          { id: "google_drive", label: "Google Drive", status: "ready", configured: true },
          { id: "freshdesk", label: "Freshdesk", status: "ready", configured: true },
          { id: "waveapps_business", label: "Wave - Noodzakelijk Online", status: "attention", configured: false },
        ],
      },
      "/api/sources/readiness": {
        sources: [
          { source: "google_drive", enabled: true, canSync: true, nextAction: "Sync the approved folder." },
          {
            source: "freshdesk",
            enabled: true,
            canSync: true,
            mode: "financial_ticket_read_only",
            connectorProfile: {
              enabled: true,
              profileId: "scan_to_folder_v1",
              ticketMutation: "not_executed",
            },
          },
        ],
      },
      "/api/sources": { sources: [{ source_type: "google_drive", status: "connected", updated_at: "2026-07-15T08:00:00Z" }] },
      "/api/workflows": {
        workflowRuns: [{
          id: 10,
          status: "completed",
          trigger_source: "local_autonomous_cycle",
          finished_at: "2026-07-25T09:15:00Z",
        }],
      },
      "/api/workflows/recovery": { status: "due", dueCount: 1, candidates: [{ workflowRunId: 9 }] },
      "/api/backups": {
        backupDir: "C:\\private\\backups",
        restoreConfirmationPhrase: "RESTORE FAB LOCAL LEDGER",
        fullRestoreConfirmationPhrase: "RESTORE FAB LEDGER AND SOURCE EVIDENCE",
        restorePolicy: {
          status: "maintenance_required",
          maintenanceMode: false,
          ledgerRestoreSupported: true,
          sourceEvidenceRestoreSupported: true,
          sourceRestoreRoot: "C:\\private\\restored-source-evidence",
          workerMustBeStopped: true,
          nextAction: "Stop FAB before recovery.",
          externalSubmission: "not_executed",
        },
        verificationMode: "manifest_only",
        schedule: {
          status: "current",
          due: false,
          intervalHours: 24,
          lastSuccessfulAt: "2026-07-25T08:00:00Z",
          nextDueAt: "2026-07-26T08:00:00Z",
          latestBackupFilename: "fab-recovery-package_20260725.zip",
          latestLedgerSha256: "a".repeat(64),
          sourceEvidenceStatus: "complete",
          sourceEvidenceDocuments: 18,
          sourceEvidenceFiles: 18,
          sourceEvidenceBytes: 4096,
          sourceEvidenceGaps: 0,
          integrityVerification: "manifest_only",
        },
        backups: [{
          backupFilename: "fab-recovery-package_20260725.zip",
          backupPath: "C:\\private\\backups\\fab-recovery-package_20260725.zip",
          status: "valid",
          createdAt: "2026-07-25T08:00:00Z",
          ledgerBytes: 2048,
          ledgerSha256: "a".repeat(64),
          sizeBytes: 6144,
          format: "fab-recovery-package-v2",
          sourceEvidenceStatus: "complete",
          sourceEvidenceDocuments: 18,
          sourceEvidenceFiles: 18,
          sourceEvidenceBytes: 4096,
          sourceEvidenceGaps: 0,
        }],
      },
      "/api/report-runs": {
        scheduleStatus: {
          enabled: true,
          status: "current",
          due: false,
          reportDir: "C:\\private\\reports",
          schedule: {
            scheduleId: "quarterly-overview",
            reportType: "overview",
            basis: "accrual",
            frequency: "quarterly",
            periodMode: "previous_quarter",
            timezone: "Europe/Amsterdam",
            formats: ["json", "csv"],
          },
          slot: {
            scheduleSlot: "2026-Q2",
            scheduledFor: "2026-07-01T06:00:00Z",
            nextDueAt: "2026-10-01T06:00:00Z",
            period: {
              fromDate: "2026-04-01",
              toDate: "2026-06-30",
              privatePeriodEvidence: "omit-this",
            },
          },
        },
        reportRuns: [{
          id: 14,
          schedule_id: "quarterly-overview",
          schedule_slot: "2026-Q2",
          report_type: "overview",
          basis: "accrual",
          period_from: "2026-04-01",
          period_to: "2026-06-30",
          status: "prepared",
          readiness: "ready",
          row_count: 42,
          blocker_count: 0,
          json_path: "C:\\private\\reports\\quarterly.json",
          csv_path: "C:\\private\\reports\\quarterly.csv",
          json_sha256: "c".repeat(64),
          csv_sha256: "d".repeat(64),
          external_submission: "not_executed",
          metadata: { privateReportMetadata: "omit-this" },
          finished_at: "2026-07-01T06:00:02Z",
        }],
        externalSubmission: "not_executed",
      },
      "/api/compliance/assessments": {
        summary: {
          assessmentCount: 3,
          openFindings: 2,
          blockingFindings: 1,
          attentionFindings: 1,
          retentionRecords: 18,
          statutoryStatus: "provisional",
          filingStatus: "not_filed",
          externalFiling: "not_executed",
        },
        assessments: [{
          id: 8,
          period_from: "2026-04-01",
          period_to: "2026-06-30",
          basis: "accrual",
          status: "blocked",
          record_count: 42,
          finding_count: 2,
          blocking_count: 1,
          attention_count: 1,
          source_checksum: "e".repeat(64),
          statutory_status: "provisional",
          external_filing: "not_executed",
          metadata: { privateComplianceEvidence: "omit-this" },
          created_at: "2026-07-01T06:05:00Z",
        }],
        statutoryStatus: "provisional",
        filingStatus: "not_filed",
        externalFiling: "not_executed",
      },
      "/api/notifications": { notifications: [{ id: 4, severity: "medium" }] },
      "/api/reconciliation": { reconciliationMatches: [{ id: 3, status: "needs_review" }] },
      "/api/audit": { auditEvents: [{ id: 2, action: "local_api.source.upsert" }] },
      "/api/close-readiness": { status: "blocked", canClose: false, blockingCount: 2 },
      "/api/hai/status": { status: "ready", enabled: true, allowedCommandIds: ["run_safe_cycle", "refresh_notifications"] },
      "/api/hai/manifest": { version: "fab-hai-connector-v1", commands: [] },
      "/api/cloud/status": {
        service: "fab-ngrok-cloud-access",
        status: "active",
        active: true,
        configured: true,
        publicUrl: "https://fab.example.test",
        authMode: "bearer_token",
        haiManifestUrl: "https://fab.example.test/api/hai/manifest",
        verifiedAt: "2026-07-25T09:30:00Z",
        externalSubmission: "not_executed",
      },
      "/api/drive-wave/status": { status: "ready", archiveEnabled: true, driveTokenPresent: true },
      "/api/drive-wave/work-orders": {
        count: 1,
        summary: { needsAttachmentVerification: 1, readyToArchive: 0 },
        workOrders: [{
          workOrderId: "drive-wave-7-abcd",
          documentId: 7,
          stage: "upload_and_verify_attachment",
          actionRequired: "Verify the attachment.",
          source: {
            filename: "receipt.pdf",
            mimeType: "application/pdf",
            provider: "google_drive",
            sha256: "abc123",
            attachmentId: "private-provider-attachment",
            localPath: "C:\\private\\receipt.pdf",
          },
          wave: {
            externalTransactionId: null,
            targetSystem: "waveapps_business",
            expectedFields: { vendor: "Example" },
          },
          archivePlan: {
            canArchive: false,
            reasons: ["wave_attachment_evidence_missing"],
            evidenceDigest: "private-evidence-digest",
          },
          reviews: { blocking: 1, open: 1, reasons: ["manual_review_category"] },
          browserExecution: { transactionListUrl: "https://example.test/private" },
          evidence: { template: { businessId: "private-business-id" } },
        }],
      },
      "/api/connectors/google-drive/authorization": {
        status: "ready_to_authorize",
        credentialsPresent: true,
        tokenPresent: false,
        folderConfigured: true,
      },
      "/api/connectors/gmail/authorization": {
        status: "ready_to_authorize",
        credentialsPresent: true,
        tokenPresent: false,
        scannerMode: true,
        trustedSenders: ["eprintcenter@hp8.us"],
      },
      "/api/wave/setup": {
        status: "needs_mapping",
        ready: false,
        targetSystem: "waveapps_business",
        businessId: "business-1",
        accessTokenConfigured: true,
        accounts: [{ id: "account-1", name: "Current Account" }],
        mapping: { verified: false },
      },
      "/api/wave/receipt-executor/status": {
        status: "not_connected",
        enabled: true,
        ready: false,
        nextAction: "Connect a supervised HAI or browser executor using the local FAB manifest.",
        missingCapabilities: ["receipt_upload", "receipt_download"],
      },
      "/api/review": {
        summary: {
          reviewItems: 3,
          documents: 2,
          postingBlockedDocuments: 1,
          postingBlockedReviewItems: 2,
          evidenceOnlyDocuments: 1,
          evidenceOnlyReviewItems: 1,
          duplicateCandidates: 0,
          workItems: 2,
          oldestReviewCreatedAt: "2026-01-01T00:00:00Z",
          newestReviewCreatedAt: "2026-07-24T09:00:00Z",
        },
        pagination: {
          scope: "work_items",
          offset: 0,
          limit: 50,
          returned: 1,
          total: 2,
          hasMore: true,
          nextOffset: 1,
          previousOffset: null,
        },
        categoryOptions: ["Operations | Office Supplies"],
        workItems: [{
          id: "document-7",
          documentId: 7,
          reasons: ["manual_review_category"],
          reviewPath: "/documents/7",
          document: {
            filename: "receipt.pdf",
            vendorName: "Example",
            category: "Manual Review",
            categorySuggestion: {
              category: "Office Supplies",
              confidenceScore: 0.97,
              rationale: "Exact vendor match.",
              privateModelTrace: "omit-this",
            },
            ocrExcerpt: "Source evidence",
            privateInternalMetadata: { raw: true },
          },
          duplicateCandidates: [{
            id: 11,
            candidateDocumentId: 6,
            matchType: "fuzzy_document_match",
            confidenceScore: 0.96,
            matchedIdentityFields: ["vendor", "date", "amount", "transaction_reference"],
            conflictingIdentityFields: [],
            comparableFields: 4,
            similarityScore: 1,
            currentIdentity: {
              vendor: "example",
              date: "2026 07 01",
              amount: "42.00",
              transactionReference: "tx1234",
              privateReference: "omit-this",
            },
            candidateIdentity: {
              vendor: "example",
              date: "2026 07 01",
              amount: "42.00",
              transactionReference: "tx1234",
            },
            document: {
              filename: "possible-duplicate.pdf",
              vendorName: "Example",
              transactionDate: "2026-07-01",
              totalAmount: 42,
              ocrExcerpt: "omit duplicate OCR",
            },
            evidence: { duplicateFingerprint: "omit-this" },
          }],
          reviewItems: [{
            id: 9,
            reason: "manual_review_category",
            details: "Verify category.",
            createdAt: "2026-07-24T09:00:00Z",
            correctedData: { private: true },
          }],
        }],
      },
      "/api/master-ledger": {
        summary: { readyForApproval: 2 },
        dataThroughDate: "2026-07-19",
        rowsOmitted: 2,
      },
      "/api/bank-transactions": {
        bankTransactions: [
          { id: 90, amount: -125.5, currency: "EUR", description: "Private bank line" },
          { id: 91, amount: 20, currency: "EUR", description: "Private bank line" },
          { id: 92, amount: 8.25, currency: "GBP", description: "Private bank line" },
          { id: 93, amount: -999, currency: "EUR", reconciliation_status: "reconciled", description: "Private bank line" },
        ],
      },
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      const fixture = fixtures[url.pathname];
      return new Response(JSON.stringify(fixture ?? {}), {
        status: fixture ? 200 : 404,
        headers: { "content-type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await getFabControlCenter();

    expect(result.connection.connected).toBe(true);
    expect(result.connection.endpoint).toBe("http://127.0.0.1:5001");
    expect(result.metrics).toMatchObject({
      documents: 18,
      pendingReview: 4,
      pendingReviewDocuments: 2,
      postingBlockedReviewDocuments: 1,
      unreconciled: 5,
      exceptions: 2,
    });
    expect(result.resourceStates.metrics.state).toBe("live");
    expect(result.decisionContext).toMatchObject({
      lastSafeCycleAt: "2026-07-25T09:15:00Z",
      latestWorkflowStatus: "completed",
      dataThroughDate: "2026-07-19",
      sourceCount: 1,
      readySourceCount: 1,
      latestSourceSyncAt: "2026-07-15T08:00:00Z",
      unreconciledAmountByCurrency: { EUR: 145.5, GBP: 8.25 },
      highPriorityExceptions: 1,
      ledgerReadyForApproval: 2,
    });
    expect(result.decisionContext.oldestReviewAgeHours).toBeGreaterThan(1_000);
    expect(result.connections).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: "google_drive", canSync: true, nextAction: "Sync the approved folder." }),
      expect.objectContaining({
        id: "freshdesk",
        mode: "financial_ticket_read_only",
        connectorProfile: expect.objectContaining({
          profileId: "scan_to_folder_v1",
          ticketMutation: "not_executed",
        }),
      }),
      expect.objectContaining({
        id: "waveapps_business",
        status: "needs_mapping",
        configured: true,
        ready: false,
        nextAction: "Map the verified funding account and every FAB category currently in use.",
      }),
      expect.objectContaining({
        id: "wave_receipt_executor",
        status: "not_connected",
        configured: true,
        ready: false,
        missingCapabilities: ["receipt_upload", "receipt_download"],
      }),
      expect.objectContaining({
        id: "hai",
        status: "ready",
        allowedCommandIds: ["run_safe_cycle", "refresh_notifications"],
        details: "Governed machine control is enabled for 2 allowlisted commands.",
      }),
      expect.objectContaining({
        id: "ngrok_cloud",
        status: "active",
        configured: true,
        ready: true,
        publicUrl: "https://fab.example.test",
        authMode: "bearer_token",
      }),
    ]));
    expect(result.recovery).toMatchObject({ dueCount: 1 });
    expect(result.backups).toMatchObject({
      verificationMode: "manifest_only",
      restorePolicy: {
        status: "maintenance_required",
        maintenanceMode: false,
        ledgerRestoreSupported: true,
        sourceEvidenceRestoreSupported: true,
        workerMustBeStopped: true,
        externalSubmission: "not_executed",
      },
      schedule: {
        status: "current",
        integrityVerification: "manifest_only",
        sourceEvidenceStatus: "complete",
        sourceEvidenceDocuments: 18,
      },
      backups: [expect.objectContaining({
        backupFilename: "fab-recovery-package_20260725.zip",
        status: "valid",
        sourceEvidenceFiles: 18,
      })],
    });
    expect(result.reporting).toMatchObject({
      scheduleStatus: {
        enabled: true,
        status: "current",
        due: false,
        schedule: { scheduleId: "quarterly-overview", reportType: "overview" },
        slot: { scheduleSlot: "2026-Q2", nextDueAt: "2026-10-01T06:00:00Z" },
      },
      reportRuns: [expect.objectContaining({
        id: 14,
        status: "prepared",
        rowCount: 42,
        hasJsonArtifact: true,
        hasCsvArtifact: true,
        jsonSha256: "c".repeat(64),
      })],
      externalSubmission: "not_executed",
    });
    expect(result.compliance).toMatchObject({
      summary: {
        assessmentCount: 3,
        openFindings: 2,
        blockingFindings: 1,
        retentionRecords: 18,
      },
      assessments: [expect.objectContaining({
        id: 8,
        status: "blocked",
        recordCount: 42,
        sourceChecksum: "e".repeat(64),
      })],
      statutoryStatus: "provisional",
      filingStatus: "not_filed",
      externalFiling: "not_executed",
    });
    expect(result.delivery).toMatchObject({
      count: 1,
      summary: { needsAttachmentVerification: 1 },
      workOrders: [expect.objectContaining({
        documentId: 7,
        source: {
          filename: "receipt.pdf",
          mimeType: "application/pdf",
          provider: "google_drive",
          sha256: "abc123",
        },
        archivePlan: {
          canArchive: false,
          reasons: ["wave_attachment_evidence_missing"],
        },
      })],
    });
    expect(result.reviews).toMatchObject({
      summary: {
        reviewItems: 3,
        documents: 2,
        postingBlockedDocuments: 1,
        evidenceOnlyDocuments: 1,
      },
      categoryOptions: ["Operations | Office Supplies"],
      pagination: {
        scope: "work_items",
        offset: 0,
        limit: 50,
        returned: 1,
        total: 2,
        hasMore: true,
        nextOffset: 1,
        previousOffset: null,
      },
      workItems: [expect.objectContaining({
        documentId: 7,
        document: expect.objectContaining({
          filename: "receipt.pdf",
          vendorName: "Example",
          ocrExcerpt: "Source evidence",
          categorySuggestion: {
            category: "Office Supplies",
            confidenceScore: 0.97,
            rationale: "Exact vendor match.",
          },
        }),
        duplicateCandidates: [
          expect.objectContaining({
            id: 11,
            candidateDocumentId: 6,
            matchType: "fuzzy_document_match",
            matchedIdentityFields: ["vendor", "date", "amount", "transaction_reference"],
            currentIdentity: {
              vendor: "example",
              date: "2026 07 01",
              amount: "42.00",
              transactionReference: "tx1234",
            },
            document: {
              filename: "possible-duplicate.pdf",
              vendorName: "Example",
              transactionDate: "2026-07-01",
              totalAmount: 42,
            },
          }),
        ],
        reviewItems: [
          expect.objectContaining({
            id: 9,
            reason: "manual_review_category",
            details: "Verify category.",
          }),
        ],
      })],
    });
    expect(result.driveAuthorization).toMatchObject({
      status: "ready_to_authorize",
      credentialsPresent: true,
      tokenPresent: false,
    });
    expect(result.gmailAuthorization).toMatchObject({
      status: "ready_to_authorize",
      scannerMode: true,
      trustedSenders: ["eprintcenter@hp8.us"],
    });
    expect(result.waveSetup).toMatchObject({
      status: "needs_mapping",
      accessTokenConfigured: true,
      businessId: "business-1",
    });
    expect(result.waveReceiptExecutor).toMatchObject({
      status: "not_connected",
      enabled: true,
      ready: false,
    });
    expect(result.cloudAccess).toMatchObject({
      status: "active",
      active: true,
      externalSubmission: "not_executed",
    });
    expect(fetchMock.mock.calls.some(([input]) => {
      const url = new URL(String(input));
      return url.pathname === "/api/review"
        && url.searchParams.get("limit") === "25"
        && url.searchParams.get("offset") === "0"
        && url.searchParams.get("view") === "summary";
    })).toBe(true);
    expect(fetchMock.mock.calls.some(([input]) => {
      const url = new URL(String(input));
      return url.pathname === "/api/drive-wave/work-orders"
        && url.searchParams.get("limit") === "200"
        && url.searchParams.get("itemsLimit") === "25"
        && url.searchParams.get("view") === "summary";
    })).toBe(true);
    const serialized = JSON.stringify(result);
    expect(serialized).not.toContain("private-token");
    expect(serialized).not.toContain("private-provider-attachment");
    expect(serialized).not.toContain("C:\\private\\receipt.pdf");
    expect(serialized).not.toContain("private-evidence-digest");
    expect(serialized).not.toContain("private-business-id");
    expect(serialized).not.toContain("privateModelTrace");
    expect(serialized).not.toContain("privateReference");
    expect(serialized).not.toContain("privateInternalMetadata");
    expect(serialized).not.toContain("omit duplicate OCR");
    expect(serialized).not.toContain("duplicateFingerprint");
    expect(serialized).not.toContain("correctedData");
    expect(serialized).not.toContain("Private ledger vendor");
    expect(serialized).not.toContain("Private latest vendor");
    expect(serialized).not.toContain("Private bank line");
    expect(serialized).not.toContain("C:\\private\\backups");
    expect(serialized).not.toContain("C:\\private\\reports");
    expect(serialized).not.toContain("privateReportMetadata");
    expect(serialized).not.toContain("privatePeriodEvidence");
    expect(serialized).not.toContain("privateComplianceEvidence");
    expect(serialized).not.toContain("RESTORE FAB LOCAL LEDGER");
    expect(serialized).not.toContain("RESTORE FAB LEDGER AND SOURCE EVIDENCE");
    expect(serialized).not.toContain("C:\\private\\restored-source-evidence");
  });

  it("does not turn unavailable resources into reassuring zeroes", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => {
      throw new Error("local API offline");
    }));

    const result = await getFabControlCenter();

    expect(result.connection.connected).toBe(false);
    expect(result.metrics).toMatchObject({
      documents: null,
      pendingReview: null,
      pendingReviewDocuments: null,
      postingBlockedReviewDocuments: null,
      unreconciled: null,
      exceptions: null,
    });
    expect(result.decisionContext).toMatchObject({
      lastSafeCycleAt: null,
      dataThroughDate: null,
      sourceCount: null,
      readySourceCount: null,
      latestSourceSyncAt: null,
      unreconciledAmountByCurrency: null,
      ledgerReadyForApproval: null,
    });
    expect(result.resourceStates.metrics).toMatchObject({ state: "error", updatedAt: null });
    expect(result.resourceStates.exceptions.state).toBe("error");
  });

  it.each([401, 403, 429, 503, "offline"])("does not amplify a failed batch %s into individual reads", async status => {
    const paths: string[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      paths.push(new URL(String(input)).pathname);
      if (status === "offline") throw new Error("Synthetic backend offline");
      return new Response(JSON.stringify({ error: "Synthetic batch rejected" }), { status: Number(status) });
    });
    vi.stubGlobal("fetch", fetchMock);
    const result = await getFabControlCenter();
    expect(paths.sort()).toEqual(["/api/backups", "/api/cloud/status", "/api/control-center/resources"].sort());
    expect(result.connection.connected).toBe(false);
    expect(result.metrics.documents).toBeNull();
    expect(result.resourceStates.metrics).toMatchObject({ state: "error", updatedAt: null });
    expect(result.resourceStates.liveness.state).toBe("error");
  });

  it.each([404, 405])("retains bounded individual compatibility reads for unsupported batch %s", async status => {
    const paths: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input)).pathname;
      paths.push(path);
      return new Response(JSON.stringify(path === "/api/control-center/resources"
        ? { error: "Synthetic unsupported batch" } : { status: "ok" }),
      { status: path === "/api/control-center/resources" ? status : 200 });
    }));
    const result = await getFabControlCenter();
    expect(result.connection.connected).toBe(true);
    expect(paths).toContain("/api/live");
    expect(paths.filter(path => path === "/api/control-center/resources")).toHaveLength(1);
    expect(paths.length).toBe(30);
  });

  it("does not fan out after a real HTTP service-unavailable response", async () => {
    const paths: string[] = [];
    const nativeFetch = globalThis.fetch;
    const server = createServer((req, res) => {
      paths.push(new URL(req.url || "/", "http://127.0.0.1").pathname);
      req.resume();
      res.writeHead(503, { "content-type": "application/json" });
      res.end('{"error":"Synthetic service unavailable"}');
    });
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    try {
      const base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
      vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
        const target = new URL(String(input));
        return nativeFetch(new URL(target.pathname + target.search, base), {
          ...init, headers: { accept: "application/json" },
        });
      });
      const result = await getFabControlCenter();
      expect(paths.sort()).toEqual(["/api/backups", "/api/cloud/status", "/api/control-center/resources"].sort());
      expect(result.connection.connected).toBe(false);
      expect(result.metrics.documents).toBeNull();
    } finally {
      await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections(); });
    }
  });

  it("bounds control-center reads so the local ledger is not flooded", async () => {
    let activeRequests = 0;
    let maximumActiveRequests = 0;
    const fetchMock = vi.fn(async () => {
      activeRequests += 1;
      maximumActiveRequests = Math.max(maximumActiveRequests, activeRequests);
      await new Promise((resolve) => setTimeout(resolve, 5));
      activeRequests -= 1;
      return new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await getFabControlCenter();
    const deliveryRequest = fetchMock.mock.calls.find(([input]) => (
      new URL(String(input)).pathname === "/api/drive-wave/work-orders"
    ));
    const masterLedgerRequest = fetchMock.mock.calls.find(([input]) => (
      new URL(String(input)).pathname === "/api/master-ledger"
    ));

    expect(result.connection.connected).toBe(true);
    expect(deliveryRequest).toBeDefined();
    expect(new URL(String(deliveryRequest?.[0])).searchParams.get("view")).toBe("summary");
    expect(masterLedgerRequest).toBeDefined();
    expect(new URL(String(masterLedgerRequest?.[0])).searchParams.get("summaryOnly")).toBe("true");
    expect(fetchMock.mock.calls.length).toBeGreaterThan(20);
    expect(maximumActiveRequests).toBeLessThanOrEqual(8);
  });

  it("batches ordinary control-center resources into one snapshot read", async () => {
    const batchedResources = [
      "autonomy", "reviewQueue", "exceptions", "closeReadiness", "driveWaveStatus", "driveWaveWorkOrders",
      "health", "reportRuns", "compliance", "recovery", "liveness", "waveSetup",
      "workflows", "masterLedger", "metrics", "notifications", "settings",
      "bankTransactions", "reconciliation", "activity", "sources",
      "waveReceiptExecutor", "sourceReadiness", "driveAuthorization",
      "haiStatus", "haiManifest", "gmailAuthorization",
    ];
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input)).pathname;
      const body = path === "/api/control-center/resources"
        ? {
            version: 1,
            resources: Object.fromEntries(batchedResources.map((resource) => [
              resource,
              { ok: true, value: { status: "ok" } },
            ])),
          }
        : { status: "ok" };
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await getFabControlCenter();
    const requestedPaths = fetchMock.mock.calls.map(([input]) => new URL(String(input)).pathname);

    expect(result.connection.connected).toBe(true);
    expect(requestedPaths).toContain("/api/control-center/resources");
    expect(requestedPaths).not.toContain("/api/dashboard");
    expect(requestedPaths).not.toContain("/api/health");
    expect(requestedPaths).not.toContain("/api/autonomy/plan");
    expect(requestedPaths).not.toContain("/api/drive-wave/work-orders");
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("coalesces duplicate snapshots and invalidates them after a mutation", async () => {
    const fetchMock = vi.fn(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2));
      return new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const [first, second] = await Promise.all([
      getFabControlCenter(),
      getFabControlCenter(),
    ]);
    const firstSnapshotCalls = fetchMock.mock.calls.length;
    const cached = await getFabControlCenter();

    expect(first).toBe(second);
    expect(cached).toBe(first);
    expect(firstSnapshotCalls).toBeGreaterThan(20);
    expect(fetchMock).toHaveBeenCalledTimes(firstSnapshotCalls);

    await runFabOperatorCommand("refresh_notifications", "operator-cache-test");
    expect(fetchMock).toHaveBeenCalledTimes(firstSnapshotCalls + 1);
    await getFabControlCenter();
    expect(fetchMock.mock.calls.length).toBeGreaterThan(firstSnapshotCalls + 20);
  });

  it("invalidates a snapshot after an uncertain mutation without retrying it", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") return new Response("not-json");
      return new Response(JSON.stringify({ status: "ok" }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const first = await getFabControlCenter();
    const firstCalls = fetchMock.mock.calls.length;
    await expect(fabLocalRequest("/api/test", { method: "POST", body: "{}" })).rejects.toThrow("JSON object");
    const refreshed = await getFabControlCenter();
    expect(refreshed).not.toBe(first);
    expect(fetchMock.mock.calls.length).toBeGreaterThan(firstCalls + 20);
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  });

  it("rejects an obsolete refresh without overwriting newer stale-fallback evidence", async () => {
    let phase = 1;
    let release!: (response: Response) => void;
    let started!: () => void;
    const oldMetrics = new Promise<Response>(resolve => { release = resolve; });
    const metricsStarted = new Promise<void>(resolve => { started = resolve; });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input)).pathname;
      if (path === "/api/dashboard" && phase === 1) { started(); return oldMetrics; }
      if (phase === 3) return new Response('{"error":"Synthetic unavailable"}', { status: 503 });
      return new Response(JSON.stringify(path === "/api/dashboard" ? { documents: 22 } : {}));
    }));
    const obsolete = getFabControlCenter().then(value => value, error => error);
    try {
      await metricsStarted;
      phase = 2;
      const fresh = await refreshFabControlCenter();
      expect(fresh.metrics.documents).toBe(22);
      release(new Response('{"documents":11}'));
      expect(await obsolete).toMatchObject({ code: "CONFLICT" });
      phase = 3;
      const stale = await refreshFabControlCenter();
      expect(stale.metrics.documents).toBe(22);
      expect(stale.resourceStates.metrics.state).toBe("stale");
    } finally {
      release(new Response('{"documents":11}'));
      await obsolete;
    }
  });

  it.each(["{}", "not-json"])("invalidates reads cached during mutation completion %s", async body => {
    let documents = 11;
    let release!: (response: Response) => void;
    let started!: () => void;
    const pendingMutation = new Promise<Response>(resolve => { release = resolve; });
    const mutationStarted = new Promise<void>(resolve => { started = resolve; });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") { started(); return pendingMutation; }
      return new Response(JSON.stringify(new URL(String(input)).pathname === "/api/dashboard" ? { documents } : {}));
    });
    vi.stubGlobal("fetch", fetchMock);
    const mutation = fabLocalRequest("/api/test", { method: "POST", body: "{}" }).then(value => value, error => error);
    try {
      await mutationStarted;
      const during = await getFabControlCenter();
      expect(during.metrics.documents).toBe(11);
      documents = 22;
      release(new Response(body));
      const outcome = await mutation;
      if (body === "not-json") expect(outcome).toBeInstanceOf(Error);
      else expect(outcome).toEqual({});
      const after = await getFabControlCenter();
      expect(after.metrics.documents).toBe(22);
      expect(after).not.toBe(during);
      expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
    } finally {
      release(new Response(body));
      await mutation;
    }
  });

  it("refreshes a snapshot created during a pending real HTTP mutation", async () => {
    let documents = 11;
    let submissions = 0;
    let finishMutation = () => {};
    let started!: () => void;
    const mutationStarted = new Promise<void>(resolve => { started = resolve; });
    const nativeFetch = globalThis.fetch;
    const server = createServer((req, res) => {
      req.resume();
      res.writeHead(200, { "content-type": "application/json" });
      if (req.method === "POST") {
        submissions += 1;
        finishMutation = () => {
          if (!res.writableEnded) { documents = 22; res.end("{}"); }
        };
        started();
      } else {
        res.end(JSON.stringify(new URL(req.url || "/", "http://127.0.0.1").pathname === "/api/dashboard" ? { documents } : {}));
      }
    });
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    let mutation: Promise<unknown> | undefined;
    try {
      const base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
      vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
        const target = new URL(String(input));
        return nativeFetch(new URL(target.pathname + target.search, base), {
          ...init, headers: { accept: "application/json" },
        });
      });
      mutation = fabLocalRequest("/api/test", { method: "POST", body: "{}" });
      await mutationStarted;
      expect((await getFabControlCenter()).metrics.documents).toBe(11);
      finishMutation();
      await mutation;
      expect((await getFabControlCenter()).metrics.documents).toBe(22);
      expect(submissions).toBe(1);
    } finally {
      finishMutation();
      await mutation?.catch(() => {});
      await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections(); });
    }
  });

  it("keeps automatic polling cached while explicit refresh reloads every resource", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ status: "ok" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);

    const first = await getFabControlCenter();
    const firstSnapshotCalls = fetchMock.mock.calls.length;
    const polled = await getFabControlCenter();

    expect(polled).toBe(first);
    expect(fetchMock).toHaveBeenCalledTimes(firstSnapshotCalls);

    const refreshed = await refreshFabControlCenter();

    expect(refreshed).not.toBe(first);
    expect(fetchMock.mock.calls.length).toBeGreaterThan(firstSnapshotCalls + 20);
  });

  it("retains last valid resource data as visibly stale after a partial failure", async () => {
    let failMetrics = false;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input)).pathname;
      if (failMetrics && ["/api/dashboard", "/api/exceptions"].includes(path)) {
        return new Response(JSON.stringify({ error: "resource unavailable" }), {
          status: 503,
          headers: { "content-type": "application/json" },
        });
      }
      const value = path === "/api/dashboard"
        ? { documents: 7, pending_review: 3, unreconciled_bank_transactions: 2, unreconciled_documents: 1, failed_documents: 0 }
        : path === "/api/exceptions"
          ? { summary: { total: 3 }, exceptions: [{ id: "held-exception" }] }
          : path === "/api/health"
            ? { status: "ok" }
            : {};
      return new Response(JSON.stringify(value), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }));

    const live = await getFabControlCenter();
    failMetrics = true;
    await runFabOperatorCommand("refresh_notifications", "operator-stale-test");
    const stale = await getFabControlCenter();

    expect(live.metrics.pendingReview).toBe(3);
    expect(stale.connection.connected).toBe(true);
    expect(stale.metrics).toMatchObject({ documents: 7, pendingReview: 3, unreconciled: 3, exceptions: 3 });
    expect(stale.exceptions).toEqual([{ id: "held-exception" }]);
    expect(stale.resourceStates.metrics).toMatchObject({ state: "stale", error: "resource unavailable" });
    expect(stale.resourceStates.exceptions.state).toBe("stale");
    expect(stale.partialErrors).toEqual(expect.arrayContaining([
      expect.objectContaining({ resource: "metrics", state: "stale" }),
      expect.objectContaining({ resource: "exceptions", state: "stale" }),
    ]));
  });

  it("maps dashboard commands only to fixed local API paths", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => new Response(
      JSON.stringify({ status: "completed", path: new URL(String(input)).pathname, body: JSON.parse(String(init?.body)) }),
      { status: 200, headers: { "content-type": "application/json" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    const result = await runFabOperatorCommand("run_due_recovery", "operator-12", { limit: 2 });

    expect(result).toMatchObject({
      status: "completed",
      path: "/api/workflows/recovery/run-due",
      body: { limit: 2, actor: "operator-12" },
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("creates only a source-complete backup through the fixed local endpoint", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => new Response(
      JSON.stringify({
        success: true,
        status: "created",
        backupFilename: "fab-recovery-package.zip",
        backupPath: "C:\\private\\backups\\fab-recovery-package.zip",
        manifest: {
          format: "fab-recovery-package-v2",
          createdAt: "2026-07-25T10:00:00Z",
          ledgerBytes: 2048,
          ledgerSha256: "b".repeat(64),
          configSummary: { backupDir: "C:\\private\\backups" },
          sourceEvidence: {
            coverageStatus: "complete",
            totalDocuments: 18,
            includedDocuments: 18,
            includedFiles: 18,
            includedBytes: 4096,
            gapCount: 0,
            entries: [{ originalFilename: "private.pdf" }],
          },
        },
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    const result = await createFabBackup("operator-15");

    expect(result).toMatchObject({
      success: true,
      status: "created",
      backupFilename: "fab-recovery-package.zip",
      manifest: {
        format: "fab-recovery-package-v2",
        sourceEvidence: {
          coverageStatus: "complete",
          includedDocuments: 18,
          gapCount: 0,
        },
      },
    });
    const [input, init] = fetchMock.mock.calls[0];
    expect(new URL(String(input)).pathname).toBe("/api/backups");
    expect(JSON.parse(String(init?.body))).toEqual({
      actor: "operator-15",
      note: "Created from the FAB operator dashboard.",
      requireCompleteSourceEvidence: true,
    });
    expect(JSON.stringify(result)).not.toContain("C:\\private\\backups");
    expect(JSON.stringify(result)).not.toContain("private.pdf");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("maps unread-scan recovery to its bounded local endpoint", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => new Response(
      JSON.stringify({ status: "completed", path: new URL(String(input)).pathname, body: JSON.parse(String(init?.body)) }),
      { status: 200, headers: { "content-type": "application/json" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    const result = await runFabOperatorCommand("reprocess_incomplete", "operator-13", { limit: 10 });

    expect(result).toMatchObject({
      status: "completed",
      path: "/api/documents/reprocess-incomplete",
      body: { limit: 10, actor: "operator-13" },
    });
  });

  it("maps review reassessment to its backed-up local endpoint", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => new Response(
      JSON.stringify({ status: "completed", path: new URL(String(input)).pathname, body: JSON.parse(String(init?.body)) }),
      { status: 200, headers: { "content-type": "application/json" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    const result = await runFabOperatorCommand("reprocess_review_queue", "operator-14", { limit: 20 });

    expect(result).toMatchObject({
      status: "completed",
      path: "/api/documents/reprocess-review-queue",
      body: { limit: 20, actor: "operator-14" },
    });
  });

  it("uploads intake files only through the fixed local API path", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => new Response(
      JSON.stringify({
        status: "registered",
        path: new URL(String(input)).pathname,
        body: JSON.parse(String(init?.body)),
      }),
      { status: 201, headers: { "content-type": "application/json" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    const result = await uploadFabIntakeFile({
      filename: "receipt.pdf",
      mimeType: "application/pdf",
      contentBase64: "cmVjZWlwdA==",
    });

    expect(result).toMatchObject({
      status: "registered",
      path: "/api/intake/upload",
      body: { filename: "receipt.pdf", mimeType: "application/pdf" },
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("imports a bounded bank statement with a server-owned source and actor", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => new Response(
      JSON.stringify({
        success: true,
        status: "completed",
        bankStatementImportId: 81,
        accountIdentifier: "NL00FAB0123456789",
        filename: "statement.csv",
        format: "csv",
        rowsSeen: 3,
        rowsImported: 2,
        duplicates: 1,
        identityConflicts: 1,
        identityConflictRows: [{ row: 3, transactionId: "bank-3", bankTransactionId: 3 }],
        identityConflictsTruncated: false,
        skipped: 0,
        bankTransactionIds: [1, 2, 3],
        internalMetadata: "must-not-cross-the-web-boundary",
        externalSubmission: "not_executed",
        path: new URL(String(input)).pathname,
        request: JSON.parse(String(init?.body)),
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    const result = await importFabBankStatement({
      filename: "statement.csv",
      format: "csv",
      accountIdentifier: "NL00FAB0123456789",
      contentBase64: "YSxiLGM=",
      actor: "fab_dashboard:9",
    });

    expect(result).toMatchObject({
      success: true,
      status: "completed",
      rowsSeen: 3,
      rowsImported: 2,
      duplicates: 1,
      identityConflicts: 1,
      identityConflictRows: [{ row: 3, transactionId: "bank-3", bankTransactionId: 3 }],
      identityConflictsTruncated: false,
      externalSubmission: "not_executed",
    });
    expect(result).not.toHaveProperty("bankTransactionIds");
    expect(result).not.toHaveProperty("internalMetadata");
    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(String(init?.body))).toEqual({
      filename: "statement.csv",
      format: "csv",
      accountIdentifier: "NL00FAB0123456789",
      contentBase64: "YSxiLGM=",
      source: "dashboard_bank_statement",
      actor: "fab_dashboard:9",
    });
  });

  it("resolves a review only through its fixed local API record path", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => new Response(
      JSON.stringify({
        success: true,
        path: new URL(String(input)).pathname,
        body: JSON.parse(String(init?.body)),
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    const result = await resolveFabReviewItem({
      reviewItemId: 42,
      status: "approved",
      resolution: "Verified against the source receipt.",
      corrections: {
        category: "Operations | Office Supplies",
        totalAmount: 42.5,
        duplicateCandidateId: 7,
      },
    });

    expect(result).toMatchObject({
      success: true,
      path: "/api/review/42/resolve",
      body: {
        status: "approved",
        resolution: "Verified against the source receipt.",
        corrections: {
          category: "Operations | Office Supplies",
          totalAmount: 42.5,
          duplicateCandidateId: 7,
        },
        learnRule: true,
        applyToMatchingVendor: false,
      },
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("installs Drive credentials and starts only the fixed authorization workflow", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => new Response(
      JSON.stringify({
        success: true,
        path: new URL(String(input)).pathname,
        body: JSON.parse(String(init?.body)),
      }),
      { status: 202, headers: { "content-type": "application/json" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    const installed = await uploadFabGoogleDriveCredentials({
      filename: "drive-client.json",
      contentBase64: "e30=",
      replace: false,
      actor: "fab_dashboard:7",
    });
    const started = await startFabGoogleDriveAuthorization("fab_dashboard:7");

    expect(installed).toMatchObject({
      path: "/api/connectors/google-drive/credentials",
      body: {
        filename: "drive-client.json",
        replace: false,
        actor: "fab_dashboard:7",
      },
    });
    expect(started).toMatchObject({
      path: "/api/connectors/google-drive/authorization/start",
      body: { actor: "fab_dashboard:7" },
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("installs Gmail credentials and starts only the read-only authorization workflow", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => new Response(
      JSON.stringify({
        success: true,
        path: new URL(String(input)).pathname,
        body: JSON.parse(String(init?.body)),
      }),
      { status: 202, headers: { "content-type": "application/json" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    const installed = await uploadFabGmailCredentials({
      filename: "gmail-client.json",
      contentBase64: "e30=",
      replace: false,
      actor: "fab_dashboard:7",
    });
    const started = await startFabGmailAuthorization("fab_dashboard:7");

    expect(installed).toMatchObject({
      path: "/api/connectors/gmail/credentials",
      body: {
        filename: "gmail-client.json",
        replace: false,
        actor: "fab_dashboard:7",
      },
    });
    expect(started).toMatchObject({
      path: "/api/connectors/gmail/authorization/start",
      body: { actor: "fab_dashboard:7" },
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("stores and validates Wave setup only through fixed local API paths", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const requestBody = JSON.parse(String(init?.body));
      const publicBody = { ...requestBody };
      delete publicBody.accessToken;
      return new Response(JSON.stringify({
        success: true,
        path: new URL(String(input)).pathname,
        body: publicBody,
        accessTokenConfigured: true,
      }), { status: 200, headers: { "content-type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchMock);

    const saved = await saveFabWaveSetup({
      targetSystem: "waveapps_business",
      accessToken: "user-owned-wave-token",
      businessId: "business-1",
      actor: "fab_dashboard:7",
    });
    const validated = await validateFabWaveSetup("waveapps_business");

    expect(saved).toMatchObject({
      path: "/api/wave/setup",
      body: {
        targetSystem: "waveapps_business",
        businessId: "business-1",
        actor: "fab_dashboard:7",
      },
      accessTokenConfigured: true,
    });
    expect(validated).toMatchObject({
      path: "/api/wave/setup/validate",
      body: { targetSystem: "waveapps_business" },
    });
    expect(JSON.stringify(saved)).not.toContain("user-owned-wave-token");
    expect(String(fetchMock.mock.calls[0]?.[1]?.body)).toContain("user-owned-wave-token");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
