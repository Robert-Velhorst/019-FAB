import express from "express";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { isValidOperationsToken, registerFabOperationsRoutes } from "./fabOperations";
import {
  createRoutingAttemptWithAudit,
  recordAuditEvent,
  updateBookkeepingDocumentWithAudit,
  updateWorkflowRunWithAudit,
} from "./db";

vi.mock("./_core/env", () => ({
  ENV: {
    fabOperationsServiceToken: "secret-token",
  },
}));

vi.mock("./db", () => ({
  createRoutingAttemptWithAudit: vi.fn().mockResolvedValue({ id: 45 }),
  recordAuditEvent: vi.fn().mockResolvedValue({ id: 99 }),
  updateBookkeepingDocumentWithAudit: vi.fn().mockResolvedValue(true),
  updateWorkflowRunWithAudit: vi.fn().mockResolvedValue(true),
  createWorkflowRunWithAudit: vi.fn().mockResolvedValue({ id: 34 }),
  createBookkeepingDocumentWithAudit: vi.fn().mockResolvedValue({ id: 12 }),
  addReviewItemWithAudit: vi.fn().mockResolvedValue({ id: 23 }),
  createReconciliationMatchWithAudit: vi.fn().mockResolvedValue({ id: 56 }),
}));

beforeEach(() => {
  vi.clearAllMocks();
});

describe("FAB operations service auth", () => {
  it("validates service tokens without accepting empty or partial values", () => {
    expect(isValidOperationsToken("secret-token", "secret-token")).toBe(true);
    expect(isValidOperationsToken("", "secret-token")).toBe(false);
    expect(isValidOperationsToken("secret", "secret-token")).toBe(false);
    expect(isValidOperationsToken("wrong-token", "secret-token")).toBe(false);
  });

  it("accepts a bounded Bearer header and rejects oversized authorization input", async () => {
    const app = express();
    app.use(express.json());
    registerFabOperationsRoutes(app);
    const server = app.listen(0);
    try {
      const address = server.address();
      if (!address || typeof address === "string") throw new Error("Expected TCP test server address");
      const baseUrl = `http://127.0.0.1:${address.port}/api/fab/operations/routing-attempts`;
      const valid = await fetch(baseUrl, {
        method: "POST",
        headers: { authorization: "Bearer    secret-token", "content-type": "application/json" },
        body: "{}",
      });
      const oversized = await fetch(baseUrl, {
        method: "POST",
        headers: { authorization: `Bearer ${" ".repeat(8_200)}secret-token`, "content-type": "application/json" },
        body: "{}",
      });
      expect(valid.status).toBe(400);
      expect(oversized.status).toBe(401);
    } finally {
      await new Promise<void>((resolve, reject) => {
        server.close(error => error ? reject(error) : resolve());
        server.closeAllConnections();
      });
    }
  });

  it("returns a stable error envelope and request ID when authentication fails", async () => {
    const app = express();
    app.use(express.json());
    registerFabOperationsRoutes(app);
    const server = app.listen(0);
    try {
      const address = server.address();
      if (!address || typeof address === "string") {
        throw new Error("Expected TCP test server address");
      }

      const response = await fetch(
        `http://127.0.0.1:${address.port}/api/fab/operations/routing-attempts`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: "{}",
        },
      );
      const requestId = response.headers.get("x-request-id");

      expect(response.status).toBe(401);
      expect(requestId).toMatch(/^[0-9a-f-]{36}$/i);
      await expect(response.json()).resolves.toEqual({
        success: false,
        status: "error",
        errorCode: "authentication_failed",
        message: "FAB operations authentication failed.",
        requestId,
      });
    } finally {
      await new Promise<void>((resolve, reject) => {
        server.close((error) => error ? reject(error) : resolve());
      });
    }
  });

  it("records bookkeeping-record routing attempts through the service endpoint", async () => {
    const app = express();
    app.use(express.json());
    registerFabOperationsRoutes(app);
    const server = app.listen(0);
    try {
      const address = server.address();
      if (!address || typeof address === "string") {
        throw new Error("Expected TCP test server address");
      }

      const response = await fetch(`http://127.0.0.1:${address.port}/api/fab/operations/routing-attempts`, {
        method: "POST",
        headers: {
          authorization: "Bearer secret-token",
          "content-type": "application/json",
          "x-request-id": "hai-routing-acceptance-1",
        },
        body: JSON.stringify({
          bookkeepingRecordId: 98,
          workflowRunId: 34,
          target: "waveapps_business",
          status: "submitted",
          externalId: "wave-bank-expense-1",
          message: "Bank transaction draft.",
        }),
      });

      expect(response.status).toBe(200);
      expect(response.headers.get("x-request-id")).toBe("hai-routing-acceptance-1");
      await expect(response.json()).resolves.toEqual({ id: 45 });
      expect(createRoutingAttemptWithAudit).toHaveBeenCalledWith(
        expect.objectContaining({
          documentId: null,
          bookkeepingRecordId: 98,
          workflowRunId: 34,
          target: "waveapps_business",
          status: "submitted",
          externalId: "wave-bank-expense-1",
          message: "Bank transaction draft.",
        }),
        expect.objectContaining({
          action: "service.routing_attempt.create",
          details: expect.objectContaining({
            documentId: null,
            bookkeepingRecordId: 98,
          }),
        }),
      );
      expect(recordAuditEvent).not.toHaveBeenCalled();
    } finally {
      await new Promise<void>((resolve, reject) => {
        server.close((error) => {
          if (error) reject(error);
          else resolve();
        });
      });
    }
  });

  it("returns not found and skips audit events when update targets do not exist", async () => {
    vi.mocked(updateBookkeepingDocumentWithAudit).mockResolvedValueOnce(false);
    vi.mocked(updateWorkflowRunWithAudit).mockResolvedValueOnce(false);
    const app = express();
    app.use(express.json());
    registerFabOperationsRoutes(app);
    const server = app.listen(0);
    try {
      const address = server.address();
      if (!address || typeof address === "string") {
        throw new Error("Expected TCP test server address");
      }
      const headers = {
        authorization: "Bearer secret-token",
        "content-type": "application/json",
      };
      const [documentResponse, workflowResponse] = await Promise.all([
        fetch(`http://127.0.0.1:${address.port}/api/fab/operations/documents/404`, {
          method: "PATCH",
          headers,
          body: JSON.stringify({ processingStatus: "failed" }),
        }),
        fetch(`http://127.0.0.1:${address.port}/api/fab/operations/workflow-runs/405`, {
          method: "PATCH",
          headers,
          body: JSON.stringify({ status: "failed" }),
        }),
      ]);

      expect(documentResponse.status).toBe(404);
      await expect(documentResponse.json()).resolves.toMatchObject({
        errorCode: "bookkeeping_document_not_found",
      });
      expect(workflowResponse.status).toBe(404);
      await expect(workflowResponse.json()).resolves.toMatchObject({
        errorCode: "workflow_run_not_found",
      });
      expect(recordAuditEvent).not.toHaveBeenCalled();
    } finally {
      await new Promise<void>((resolve, reject) => {
        server.close((error) => error ? reject(error) : resolve());
        server.closeAllConnections();
      });
    }
  });

  it("rejects empty document and workflow updates before reaching the database", async () => {
    const app = express();
    app.use(express.json());
    registerFabOperationsRoutes(app);
    const server = app.listen(0);
    try {
      const address = server.address();
      if (!address || typeof address === "string") {
        throw new Error("Expected TCP test server address");
      }
      const headers = {
        authorization: "Bearer secret-token",
        "content-type": "application/json",
      };
      const [documentResponse, workflowResponse] = await Promise.all([
        fetch(`http://127.0.0.1:${address.port}/api/fab/operations/documents/404`, {
          method: "PATCH",
          headers,
          body: "{}",
        }),
        fetch(`http://127.0.0.1:${address.port}/api/fab/operations/workflow-runs/405`, {
          method: "PATCH",
          headers,
          body: "{}",
        }),
      ]);

      expect(documentResponse.status).toBe(400);
      await expect(documentResponse.json()).resolves.toMatchObject({ errorCode: "empty_update" });
      expect(workflowResponse.status).toBe(400);
      await expect(workflowResponse.json()).resolves.toMatchObject({ errorCode: "empty_update" });
      expect(updateBookkeepingDocumentWithAudit).not.toHaveBeenCalled();
      expect(updateWorkflowRunWithAudit).not.toHaveBeenCalled();
      expect(recordAuditEvent).not.toHaveBeenCalled();
    } finally {
      await new Promise<void>((resolve, reject) => {
        server.close((error) => error ? reject(error) : resolve());
        server.closeAllConnections();
      });
    }
  });

  it("preserves explicit nulls when clearing nullable document fields", async () => {
    const app = express();
    app.use(express.json());
    registerFabOperationsRoutes(app);
    const server = app.listen(0);
    try {
      const address = server.address();
      if (!address || typeof address === "string") {
        throw new Error("Expected TCP test server address");
      }
      const response = await fetch(`http://127.0.0.1:${address.port}/api/fab/operations/documents/41`, {
        method: "PATCH",
        headers: {
          authorization: "Bearer secret-token",
          "content-type": "application/json",
        },
        body: JSON.stringify({
          storagePath: null,
          duplicateOfDocumentId: null,
          totalAmount: 0,
          category: "",
        }),
      });

      expect(response.status).toBe(200);
      expect(updateBookkeepingDocumentWithAudit).toHaveBeenCalledWith(
        41,
        expect.objectContaining({
          storagePath: null,
          duplicateOfDocumentId: null,
          totalAmount: "0.00",
          category: "",
        }),
        expect.objectContaining({ details: { changedFields: ["category", "duplicateOfDocumentId", "storagePath", "totalAmount"] } }),
      );
    } finally {
      await new Promise<void>((resolve, reject) => {
        server.close((error) => error ? reject(error) : resolve());
        server.closeAllConnections();
      });
    }
  });

  it("accepts JSON timestamps and explicit nulls for nullable workflow fields", async () => {
    const app = express();
    app.use(express.json());
    registerFabOperationsRoutes(app);
    const server = app.listen(0);
    try {
      const address = server.address();
      if (!address || typeof address === "string") {
        throw new Error("Expected TCP test server address");
      }
      const response = await fetch(`http://127.0.0.1:${address.port}/api/fab/operations/workflow-runs/42`, {
        method: "PATCH",
        headers: {
          authorization: "Bearer secret-token",
          "content-type": "application/json",
        },
        body: JSON.stringify({
          errorMessage: null,
          startedAt: null,
          finishedAt: "2026-10-14T20:00:00.000Z",
        }),
      });

      expect(response.status).toBe(200);
      const update = vi.mocked(updateWorkflowRunWithAudit).mock.calls[0]?.[1];
      expect(update).toMatchObject({ errorMessage: null, startedAt: null });
      expect(update?.finishedAt).toBeInstanceOf(Date);
      expect(update?.finishedAt?.toISOString()).toBe("2026-10-14T20:00:00.000Z");
    } finally {
      await new Promise<void>((resolve, reject) => {
        server.close((error) => error ? reject(error) : resolve());
        server.closeAllConnections();
      });
    }
  });

  it("records changed fields without copying financial or secret payloads into audit details", async () => {
    const app = express();
    app.use(express.json());
    registerFabOperationsRoutes(app);
    const server = app.listen(0);
    try {
      const address = server.address();
      if (!address || typeof address === "string") {
        throw new Error("Expected TCP test server address");
      }
      const headers = {
        authorization: "Bearer secret-token",
        "content-type": "application/json",
      };
      const [documentResponse, workflowResponse] = await Promise.all([
        fetch(`http://127.0.0.1:${address.port}/api/fab/operations/documents/41`, {
          method: "PATCH",
          headers,
          body: JSON.stringify({
            processingStatus: "validated",
            ocrText: "BANK-ACCOUNT-12345678",
            extractedData: { accountNumber: "NL00BANK0123456789" },
            metadata: { accessToken: "private-provider-token" },
          }),
        }),
        fetch(`http://127.0.0.1:${address.port}/api/fab/operations/workflow-runs/42`, {
          method: "PATCH",
          headers,
          body: JSON.stringify({ errorMessage: "DATABASE_URL=private-db-secret" }),
        }),
      ]);

      expect(documentResponse.status).toBe(200);
      expect(workflowResponse.status).toBe(200);
      const auditDetails = [
        vi.mocked(updateBookkeepingDocumentWithAudit).mock.calls[0]?.[2].details,
        vi.mocked(updateWorkflowRunWithAudit).mock.calls[0]?.[2].details,
      ];
      expect(auditDetails).toContainEqual({
        changedFields: ["extractedData", "metadata", "ocrText", "processingStatus"],
      });
      expect(auditDetails).toContainEqual({ changedFields: ["errorMessage"] });
      expect(JSON.stringify(auditDetails)).not.toContain("BANK-ACCOUNT-12345678");
      expect(JSON.stringify(auditDetails)).not.toContain("NL00BANK0123456789");
      expect(JSON.stringify(auditDetails)).not.toContain("private-provider-token");
      expect(JSON.stringify(auditDetails)).not.toContain("private-db-secret");
      expect(recordAuditEvent).not.toHaveBeenCalled();
    } finally {
      await new Promise<void>((resolve, reject) => {
        server.close((error) => error ? reject(error) : resolve());
        server.closeAllConnections();
      });
    }
  });

  it("does not expose unexpected database errors to HAI callers", async () => {
    vi.mocked(createRoutingAttemptWithAudit).mockRejectedValueOnce(
      new Error("DATABASE_URL=postgres://operator-secret@localhost/fab"),
    );
    const app = express();
    app.use(express.json());
    registerFabOperationsRoutes(app);
    const server = app.listen(0);
    try {
      const address = server.address();
      if (!address || typeof address === "string") {
        throw new Error("Expected TCP test server address");
      }

      const response = await fetch(
        `http://127.0.0.1:${address.port}/api/fab/operations/routing-attempts`,
        {
          method: "POST",
          headers: {
            authorization: "Bearer secret-token",
            "content-type": "application/json",
            "x-request-id": "hai-failure-acceptance-1",
          },
          body: JSON.stringify({
            bookkeepingRecordId: 98,
            target: "waveapps_business",
            status: "submitted",
          }),
        },
      );
      const responseBody = JSON.stringify(await response.json());

      expect(response.status).toBe(500);
      expect(responseBody).toContain("operation_failed");
      expect(responseBody).toContain("hai-failure-acceptance-1");
      expect(responseBody).not.toContain("DATABASE_URL");
      expect(responseBody).not.toContain("operator-secret");
    } finally {
      await new Promise<void>((resolve, reject) => {
        server.close((error) => error ? reject(error) : resolve());
      });
    }
  });
});
