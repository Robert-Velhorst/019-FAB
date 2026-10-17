import type { Application, NextFunction, Request, RequestHandler, Response } from "express";
import crypto from "crypto";
import { z, ZodError } from "zod";
import {
  bookkeepingDocumentRegisterSchema,
  bookkeepingDocumentUpdateSchema,
  reviewItemCreateSchema,
  reconciliationMatchCreateSchema,
  routingAttemptCreateSchema,
  workflowRunCreateSchema,
  workflowRunUpdateSchema,
  auditEventCreateSchema,
} from "../shared/validation";
import { ENV } from "./_core/env";
import { createLogger } from "./lib/logger";
import { sanitizeText } from "./lib/sanitize";
import {
  addReviewItemWithAudit,
  createBookkeepingDocumentWithAudit,
  createReconciliationMatchWithAudit,
  createRoutingAttemptWithAudit,
  createWorkflowRunWithAudit,
  recordAuditEvent,
  updateBookkeepingDocumentWithAudit,
  updateWorkflowRunWithAudit,
} from "./db";

const log = createLogger("FabOperations");
const REQUEST_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const MAX_OPERATIONS_AUTH_HEADER_CHARS = 8_192;

function getRequestId(res: Response): string {
  return typeof res.locals.fabRequestId === "string"
    ? res.locals.fabRequestId
    : "unknown";
}

function respondServiceError(
  res: Response,
  status: number,
  errorCode: string,
  message: string,
  details?: Record<string, unknown>,
) {
  res.status(status).json({
    success: false,
    status: "error",
    errorCode,
    message,
    requestId: getRequestId(res),
    ...details,
  });
}

function assignFabRequestId(req: Request, res: Response, next: NextFunction) {
  const candidate = req.header("x-request-id")?.trim() || "";
  const requestId = REQUEST_ID_PATTERN.test(candidate)
    ? candidate
    : crypto.randomUUID();
  res.locals.fabRequestId = requestId;
  res.setHeader("X-Request-ID", requestId);
  next();
}

function readToken(req: Request): string {
  const authorization = req.header("authorization") || "";
  if (authorization.length > MAX_OPERATIONS_AUTH_HEADER_CHARS) return "";
  const separator = authorization.indexOf(" ");
  const scheme = separator > 0 ? authorization.slice(0, separator) : "";
  const bearer = scheme.toLowerCase() === "bearer"
    ? authorization.slice(separator + 1).trim()
    : "";
  const tokenHeader = req.header("x-fab-operations-token") || "";
  return bearer || (tokenHeader.length <= MAX_OPERATIONS_AUTH_HEADER_CHARS ? tokenHeader : "");
}

export function isValidOperationsToken(candidate: string, expected: string): boolean {
  if (!candidate || !expected) return false;

  const candidateBuffer = Buffer.from(candidate);
  const expectedBuffer = Buffer.from(expected);
  if (candidateBuffer.length !== expectedBuffer.length) return false;

  return crypto.timingSafeEqual(candidateBuffer, expectedBuffer);
}

export function requireFabOperationsToken(req: Request, res: Response, next: NextFunction) {
  if (!ENV.fabOperationsServiceToken) {
    respondServiceError(
      res,
      503,
      "service_not_configured",
      "FAB operations service authentication is not configured.",
    );
    return;
  }

  if (!isValidOperationsToken(readToken(req), ENV.fabOperationsServiceToken)) {
    respondServiceError(
      res,
      401,
      "authentication_failed",
      "FAB operations authentication failed.",
    );
    return;
  }

  next();
}

function handleServiceError(res: Response, err: unknown) {
  if (err instanceof ZodError) {
    respondServiceError(
      res,
      400,
      "validation_failed",
      "The FAB operations request is invalid.",
      { issues: err.issues },
    );
    return;
  }

  log.error("Operations request failed", {
    requestId: getRequestId(res),
    errorName: err instanceof Error ? err.name : "UnknownError",
    errorCode:
      err && typeof err === "object" && "code" in err
        ? String(err.code).slice(0, 80)
        : undefined,
  });
  respondServiceError(
    res,
    500,
    "operation_failed",
    "The FAB operations request failed.",
  );
}

export function registerFabOperationsRoutes(app: Application, limiter?: RequestHandler) {
  const middleware = limiter
    ? [assignFabRequestId, limiter, requireFabOperationsToken]
    : [assignFabRequestId, requireFabOperationsToken];
  app.use("/api/fab/operations", ...middleware);

  app.post("/api/fab/operations/workflow-runs", async (req, res) => {
    try {
      const input = workflowRunCreateSchema.parse(req.body);
      const result = await createWorkflowRunWithAudit({
        status: input.status,
        triggerSource: sanitizeText(input.triggerSource),
        metadata: input.metadata || null,
      }, {
        actorUserId: null,
        action: "service.workflow_run.create",
        entityType: "workflow_run",
        details: { status: input.status, triggerSource: sanitizeText(input.triggerSource) },
      });
      res.json(result);
    } catch (err) {
      handleServiceError(res, err);
    }
  });

  app.patch("/api/fab/operations/workflow-runs/:id", async (req, res) => {
    try {
      const input = workflowRunUpdateSchema.parse({ ...req.body, id: Number(req.params.id) });
      const { id, ...data } = input;
      if (Object.keys(data).length === 0) {
        respondServiceError(res, 400, "empty_update", "At least one workflow run field must be provided.");
        return;
      }
      if (!await updateWorkflowRunWithAudit(id, data, {
        actorUserId: null,
        action: "service.workflow_run.update",
        entityType: "workflow_run",
        details: { changedFields: Object.keys(data).sort() },
      })) {
        respondServiceError(res, 404, "workflow_run_not_found", "Workflow run was not found.");
        return;
      }
      res.json({ success: true });
    } catch (err) {
      handleServiceError(res, err);
    }
  });

  app.post("/api/fab/operations/documents", async (req, res) => {
    try {
      const input = bookkeepingDocumentRegisterSchema.parse(req.body);
      const result = await createBookkeepingDocumentWithAudit({
        source: sanitizeText(input.source),
        sourceDocumentId: input.sourceDocumentId ? sanitizeText(input.sourceDocumentId) : null,
        originalFilename: sanitizeText(input.originalFilename),
        mimeType: input.mimeType ? sanitizeText(input.mimeType) : null,
        storagePath: input.storagePath ? sanitizeText(input.storagePath) : null,
        documentType: input.documentType,
        processingStatus: input.processingStatus,
        duplicateFingerprint: input.duplicateFingerprint ? sanitizeText(input.duplicateFingerprint) : null,
        duplicateOfDocumentId: input.duplicateOfDocumentId || null,
        vendorName: input.vendorName ? sanitizeText(input.vendorName) : null,
        category: input.category ? sanitizeText(input.category) : null,
        transactionDate: input.transactionDate || null,
        totalAmount: input.totalAmount?.toFixed(2),
        vatAmount: input.vatAmount?.toFixed(2),
        confidenceScore: input.confidenceScore?.toFixed(4),
        ocrText: input.ocrText || null,
        extractedData: input.extractedData || null,
        metadata: input.metadata || null,
      }, {
        actorUserId: null,
        action: "service.document.register",
        entityType: "bookkeeping_document",
        details: { source: input.source },
      });
      res.json(result);
    } catch (err) {
      handleServiceError(res, err);
    }
  });

  app.patch("/api/fab/operations/documents/:id", async (req, res) => {
    try {
      const input = bookkeepingDocumentUpdateSchema.parse({ ...req.body, id: Number(req.params.id) });
      const { id, ...data } = input;
      if (Object.keys(data).length === 0) {
        respondServiceError(res, 400, "empty_update", "At least one document field must be provided.");
        return;
      }
      const updated = await updateBookkeepingDocumentWithAudit(id, {
        source: data.source ? sanitizeText(data.source) : undefined,
        sourceDocumentId: data.sourceDocumentId === undefined || data.sourceDocumentId === null
          ? data.sourceDocumentId
          : sanitizeText(data.sourceDocumentId),
        originalFilename: data.originalFilename ? sanitizeText(data.originalFilename) : undefined,
        mimeType: data.mimeType === undefined || data.mimeType === null
          ? data.mimeType
          : sanitizeText(data.mimeType),
        storagePath: data.storagePath === undefined || data.storagePath === null
          ? data.storagePath
          : sanitizeText(data.storagePath),
        documentType: data.documentType,
        processingStatus: data.processingStatus,
        duplicateFingerprint: data.duplicateFingerprint === undefined || data.duplicateFingerprint === null
          ? data.duplicateFingerprint
          : sanitizeText(data.duplicateFingerprint),
        duplicateOfDocumentId: data.duplicateOfDocumentId,
        vendorName: data.vendorName === undefined || data.vendorName === null
          ? data.vendorName
          : sanitizeText(data.vendorName),
        category: data.category === undefined || data.category === null
          ? data.category
          : sanitizeText(data.category),
        transactionDate: data.transactionDate,
        totalAmount: data.totalAmount === undefined || data.totalAmount === null
          ? data.totalAmount
          : data.totalAmount.toFixed(2),
        vatAmount: data.vatAmount === undefined || data.vatAmount === null
          ? data.vatAmount
          : data.vatAmount.toFixed(2),
        confidenceScore: data.confidenceScore === undefined || data.confidenceScore === null
          ? data.confidenceScore
          : data.confidenceScore.toFixed(4),
        ocrText: data.ocrText,
        extractedData: data.extractedData,
        metadata: data.metadata,
      }, {
        actorUserId: null,
        action: "service.document.update",
        entityType: "bookkeeping_document",
        details: { changedFields: Object.keys(data).sort() },
      });
      if (!updated) {
        respondServiceError(res, 404, "bookkeeping_document_not_found", "Bookkeeping document was not found.");
        return;
      }
      res.json({ success: true });
    } catch (err) {
      handleServiceError(res, err);
    }
  });

  app.post("/api/fab/operations/review-items", async (req, res) => {
    try {
      const input = reviewItemCreateSchema.parse(req.body);
      const result = await addReviewItemWithAudit({
        documentId: input.documentId || null,
        reason: sanitizeText(input.reason),
        details: input.details ? sanitizeText(input.details) : null,
        status: input.status,
        correctedData: input.correctedData || null,
      }, {
        actorUserId: null,
        action: "service.review_item.create",
        entityType: "review_item",
        details: { documentId: input.documentId || null, status: input.status },
      });
      res.json(result);
    } catch (err) {
      handleServiceError(res, err);
    }
  });

  app.post("/api/fab/operations/routing-attempts", async (req, res) => {
    try {
      const input = routingAttemptCreateSchema.parse(req.body);
      const result = await createRoutingAttemptWithAudit({
        documentId: input.documentId || null,
        bookkeepingRecordId: input.bookkeepingRecordId || null,
        workflowRunId: input.workflowRunId || null,
        target: input.target,
        status: input.status,
        externalId: input.externalId ? sanitizeText(input.externalId) : null,
        message: input.message ? sanitizeText(input.message) : null,
        metadata: input.metadata || null,
      }, {
        actorUserId: null,
        action: "service.routing_attempt.create",
        entityType: "routing_attempt",
        details: {
          documentId: input.documentId || null,
          bookkeepingRecordId: input.bookkeepingRecordId || null,
          target: input.target,
          status: input.status,
        },
      });
      res.json(result);
    } catch (err) {
      handleServiceError(res, err);
    }
  });

  app.post("/api/fab/operations/reconciliation-matches", async (req, res) => {
    try {
      const input = reconciliationMatchCreateSchema.parse(req.body);
      const result = await createReconciliationMatchWithAudit({
        documentId: input.documentId || null,
        bankTransactionId: sanitizeText(input.bankTransactionId),
        status: input.status,
        confidenceScore: input.confidenceScore?.toFixed(4),
        amountDifference: input.amountDifference?.toFixed(2),
        matchedAt: input.matchedAt || (input.status === "matched" ? new Date() : null),
        metadata: input.metadata || null,
      }, {
        actorUserId: null,
        action: "service.reconciliation_match.create",
        entityType: "reconciliation_match",
        details: { documentId: input.documentId || null, status: input.status },
      });
      res.json(result);
    } catch (err) {
      handleServiceError(res, err);
    }
  });

  app.post("/api/fab/operations/audit-events", async (req, res) => {
    try {
      const input = auditEventCreateSchema.parse(req.body);
      const result = await recordAuditEvent({
        actorUserId: null,
        action: sanitizeText(input.action),
        entityType: sanitizeText(input.entityType),
        entityId: input.entityId ? sanitizeText(input.entityId) : null,
        details: input.details || null,
      });
      res.json(result);
    } catch (err) {
      handleServiceError(res, err);
    }
  });
}
