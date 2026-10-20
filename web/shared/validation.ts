/**
 * Shared Zod validation schemas — used by both server procedures and tests.
 * Centralizing schemas prevents drift between frontend validation and backend enforcement.
 */
import { z } from "zod";

// ── Common field schemas ────────────────────────────────────────

export const emailSchema = z
  .string()
  .email("Invalid email address")
  .max(320, "Email too long")
  .transform((v) => v.toLowerCase().trim());

export const nameSchema = z
  .string()
  .min(1, "Name is required")
  .max(100, "Name too long")
  .transform((v) => v.trim());

export const optionalNameSchema = z
  .string()
  .max(100, "Name too long")
  .optional()
  .transform((v) => v?.trim() || undefined);

// ── Waitlist schemas ────────────────────────────────────────────

export const waitlistJoinSchema = z.object({
  email: emailSchema,
  firstName: optionalNameSchema,
  lastName: optionalNameSchema,
});

// ── Contact form schemas ────────────────────────────────────────

export const contactSubmitSchema = z.object({
  firstName: nameSchema,
  lastName: nameSchema,
  email: emailSchema,
  subject: z
    .string()
    .min(1, "Subject is required")
    .max(100, "Subject too long")
    .transform((v) => v.trim()),
  message: z
    .string()
    .min(10, "Message must be at least 10 characters")
    .max(5000, "Message too long")
    .transform((v) => v.trim()),
});

// ── Blog post schemas ───────────────────────────────────────────

export const blogSlugSchema = z
  .string()
  .min(1)
  .max(255)
  .regex(/^[a-z0-9\-]+$/, "Slug must contain only lowercase letters, numbers, and hyphens");

export const blogCreateSchema = z.object({
  title: z.string().min(1, "Title is required").max(255),
  titleNl: z.string().max(255).optional(),
  slug: blogSlugSchema,
  excerpt: z.string().min(1, "Excerpt is required"),
  excerptNl: z.string().optional(),
  content: z.string().min(1, "Content is required"),
  contentNl: z.string().optional(),
  category: z.string().max(50).default("update"),
  coverImage: z.string().url("Invalid image URL").max(500).optional().or(z.literal("")),
  published: z.boolean().default(false),
  readTimeMinutes: z.number().int().min(1).max(60).default(3),
});

export const blogUpdateSchema = z.object({
  id: z.number().int().positive(),
  title: z.string().min(1).max(255).optional(),
  titleNl: z.string().max(255).optional(),
  slug: blogSlugSchema.optional(),
  excerpt: z.string().min(1).optional(),
  excerptNl: z.string().optional(),
  content: z.string().min(1).optional(),
  contentNl: z.string().optional(),
  category: z.string().max(50).optional(),
  coverImage: z.string().url().max(500).optional().or(z.literal("")),
  published: z.boolean().optional(),
  readTimeMinutes: z.number().int().min(1).max(60).optional(),
});

// ── Stripe schemas ──────────────────────────────────────────────

export const stripeCheckoutSchema = z.object({
  origin: z.string().url("Invalid origin URL"),
  productKey: z.string().min(1).max(50).default("payAsYouGo"),
});

export const stripePortalSchema = z.object({
  origin: z.string().url("Invalid origin URL"),
});

export const stripeVerifySessionSchema = z.object({
  sessionId: z
    .string()
    .min(1, "Session ID is required")
    .regex(/^cs_/, "Invalid session ID format"),
});

// ── Contact message status ──────────────────────────────────────

export const contactStatusSchema = z.enum(["new", "read", "replied", "archived"]);

export const contactUpdateStatusSchema = z.object({
  id: z.number().int().positive(),
  status: contactStatusSchema,
});

export const contactListSchema = z.object({
  status: contactStatusSchema.optional(),
}).optional();

// â”€â”€ FAB bookkeeping operations schemas â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

export const reviewStatusSchema = z.enum([
  "pending",
  "in_review",
  "approved",
  "rejected",
  "resolved",
]);

export const reviewQueueListSchema = z.object({
  status: reviewStatusSchema.optional(),
}).optional();

export const reviewItemUpdateSchema = z.object({
  id: z.number().int().positive(),
  status: reviewStatusSchema,
  resolution: z.string().max(2000).optional(),
});

const jsonObjectSchema = z.record(z.string(), z.unknown());

export const documentTypeSchema = z.enum([
  "receipt",
  "invoice",
  "order_confirmation",
  "bank_transaction",
  "statement",
  "unknown",
]);

export const processingStatusSchema = z.enum([
  "imported",
  "processing",
  "extracted",
  "validated",
  "needs_review",
  "approved",
  "routed",
  "reconciled",
  "failed",
  "archived",
]);

export const workflowStatusSchema = z.enum([
  "queued",
  "running",
  "completed",
  "completed_with_review",
  "failed",
  "cancelled",
]);

export const routeTargetSchema = z.enum([
  "mijngeldzaken",
  "waveapps_business",
  "waveapps_personal",
  "manual_review",
  "none",
]);

export const routeStatusSchema = z.enum([
  "pending",
  "submitted",
  "success",
  "failed",
  "skipped",
  "requires_review",
]);

export const reconciliationStatusSchema = z.enum([
  "matched",
  "unmatched",
  "partial",
  "review",
]);

export const bookkeepingDocumentRegisterSchema = z.object({
  source: z.string().min(1).max(64),
  sourceDocumentId: z.string().max(255).optional(),
  originalFilename: z.string().min(1).max(500),
  mimeType: z.string().max(120).optional(),
  storagePath: z.string().max(1000).optional(),
  documentType: documentTypeSchema.default("unknown"),
  processingStatus: processingStatusSchema.default("imported"),
  duplicateFingerprint: z.string().max(128).optional(),
  duplicateOfDocumentId: z.number().int().positive().optional(),
  vendorName: z.string().max(255).optional(),
  category: z.string().max(255).optional(),
  transactionDate: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).optional(),
  totalAmount: z.number().finite().optional(),
  vatAmount: z.number().finite().optional(),
  confidenceScore: z.number().min(0).max(1).optional(),
  ocrText: z.string().optional(),
  extractedData: jsonObjectSchema.optional(),
  metadata: jsonObjectSchema.optional(),
});

const bookkeepingDocumentUpdateFieldsSchema = bookkeepingDocumentRegisterSchema
  .omit({ documentType: true, processingStatus: true })
  .partial()
  .extend({
    sourceDocumentId: z.string().max(255).nullable().optional(),
    mimeType: z.string().max(120).nullable().optional(),
    storagePath: z.string().max(1000).nullable().optional(),
    duplicateFingerprint: z.string().max(128).nullable().optional(),
    duplicateOfDocumentId: z.number().int().positive().nullable().optional(),
    vendorName: z.string().max(255).nullable().optional(),
    category: z.string().max(255).nullable().optional(),
    transactionDate: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).nullable().optional(),
    totalAmount: z.number().finite().nullable().optional(),
    vatAmount: z.number().finite().nullable().optional(),
    confidenceScore: z.number().min(0).max(1).nullable().optional(),
    ocrText: z.string().nullable().optional(),
    extractedData: jsonObjectSchema.nullable().optional(),
    metadata: jsonObjectSchema.nullable().optional(),
    documentType: documentTypeSchema.optional(),
    processingStatus: processingStatusSchema.optional(),
  });

export const bookkeepingDocumentUpdateSchema = bookkeepingDocumentUpdateFieldsSchema.extend({
  id: z.number().int().positive(),
});

export const reviewItemCreateSchema = z.object({
  documentId: z.number().int().positive().optional(),
  reason: z.string().min(1).max(120),
  details: z.string().max(10000).optional(),
  status: reviewStatusSchema.default("pending"),
  correctedData: jsonObjectSchema.optional(),
});

export const workflowRunCreateSchema = z.object({
  status: workflowStatusSchema.default("queued"),
  triggerSource: z.string().min(1).max(100).default("manual"),
  metadata: jsonObjectSchema.optional(),
});

export const workflowRunUpdateSchema = z.object({
  id: z.number().int().positive(),
  status: workflowStatusSchema.optional(),
  documentsImported: z.number().int().min(0).optional(),
  documentsProcessed: z.number().int().min(0).optional(),
  documentsNeedingReview: z.number().int().min(0).optional(),
  errorMessage: z.string().max(10000).nullable().optional(),
  startedAt: z.union([z.null(), z.coerce.date()]).optional(),
  finishedAt: z.union([z.null(), z.coerce.date()]).optional(),
});

export const routingAttemptCreateSchema = z.object({
  documentId: z.number().int().positive().optional(),
  bookkeepingRecordId: z.number().int().positive().optional(),
  workflowRunId: z.number().int().positive().optional(),
  target: routeTargetSchema,
  status: routeStatusSchema.default("pending"),
  externalId: z.string().max(255).optional(),
  message: z.string().max(10000).optional(),
  metadata: jsonObjectSchema.optional(),
}).refine((value) => value.documentId || value.bookkeepingRecordId, {
  message: "documentId or bookkeepingRecordId is required",
});

export const reconciliationMatchCreateSchema = z.object({
  documentId: z.number().int().positive().optional(),
  bankTransactionId: z.string().min(1).max(255),
  status: reconciliationStatusSchema.default("review"),
  confidenceScore: z.number().min(0).max(1).optional(),
  amountDifference: z.number().finite().optional(),
  matchedAt: z.coerce.date().optional(),
  metadata: jsonObjectSchema.optional(),
});

export const auditEventCreateSchema = z.object({
  action: z.string().min(1).max(120),
  entityType: z.string().min(1).max(80),
  entityId: z.string().max(120).optional(),
  details: jsonObjectSchema.optional(),
});

export const auditEventListSchema = z.object({
  limit: z.number().int().min(1).max(100).default(20),
}).optional();

export const waveActionPlanSchema = z.object({
  surfaceId: z.string().min(1).max(120),
  actionId: z.string().min(1).max(120),
  payload: jsonObjectSchema.default({}),
  allowWrite: z.boolean().default(false),
});

export const waveActionExecuteSchema = waveActionPlanSchema.extend({
  mode: z.enum(["dry_run", "queue"]).default("dry_run"),
  confirmed: z.boolean().default(false),
  actor: z.string().min(1).max(120).default("fab_admin"),
  idempotencyKey: z.string().max(255).optional(),
});

export const mijngeldzakenActionPlanSchema = waveActionPlanSchema;
export const mijngeldzakenActionExecuteSchema = waveActionExecuteSchema;

export const automationCapabilityPlanSchema = z.object({
  capabilityId: z.string().min(1).max(120),
  availableSignals: z.array(z.string().min(1).max(120)).default([]),
  confidence: z.number().min(0).max(1).optional(),
  approvals: z.array(z.string().min(1).max(200)).default([]),
});

export const automationWorkflowPlanSchema = z.object({
  workflowId: z.enum(["daily_reconciliation_run", "period_close_pack", "mijngeldzaken_master_ledger_sync"]),
  fromDate: z.string().min(1).max(40),
  toDate: z.string().min(1).max(40),
  asOfDate: z.string().min(1).max(40).optional(),
  accountOption: z.string().min(1).max(120).optional(),
  accountName: z.string().min(1).max(255).optional(),
  contactOption: z.string().min(1).max(120).optional(),
  contactName: z.string().min(1).max(255).optional(),
  cashMode: z.string().min(1).max(80).optional(),
  includeExports: z.boolean().default(true),
  availableSignals: z.array(z.string().min(1).max(120)).default([]),
  confidence: z.number().min(0).max(1).optional(),
  approvals: z.array(z.string().min(1).max(200)).default([]),
});

export const automationWorkflowQueueSchema = automationWorkflowPlanSchema.extend({
  mode: z.enum(["dry_run", "queue"]).default("dry_run"),
  confirmed: z.boolean().default(false),
  actor: z.string().min(1).max(120).default("fab_admin"),
  idempotencyKey: z.string().max(255).optional(),
});

export const automationWorkflowOperationUpdateSchema = z.object({
  workflowRunId: z.number().int().positive(),
  operationId: z.string().min(1).max(255),
  status: z.enum(["pending", "running", "succeeded", "failed", "blocked", "skipped"]),
  actor: z.string().min(1).max(120).default("fab_executor"),
  message: z.string().max(1000).optional(),
  externalId: z.string().max(255).optional(),
  evidence: jsonObjectSchema.default({}),
});

export const automationWorkflowOperationClaimSchema = z.object({
  workflowRunId: z.number().int().positive(),
  actor: z.string().min(1).max(120).default("fab_executor"),
  leaseSeconds: z.number().int().min(30).max(3600).default(300),
});

export const automationWorkflowExecutorCycleSchema = z.object({
  workflowRunId: z.number().int().positive(),
  actor: z.string().min(1).max(120).default("fab_executor"),
  leaseSeconds: z.number().int().min(30).max(3600).default(300),
});

export const automationWorkflowExecutorLoopSchema = automationWorkflowExecutorCycleSchema.extend({
  maxSteps: z.number().int().min(1).max(100).default(25),
});

export const automationWorkflowDraftArtifactSchema = z.object({
  workflowRunId: z.number().int().positive(),
  operationId: z.string().min(1).max(255),
  format: z.enum(["json", "csv"]).default("json"),
});

export const automationWorkflowMasterLedgerSchema = z.object({
  workflowRunId: z.number().int().positive().optional(),
  targetSystem: z.enum(["waveapps", "mijngeldzaken"]).optional(),
  audit: z.boolean().default(false),
});
