import { COOKIE_NAME } from "@shared/const";
import crypto from "node:crypto";
import { TRPCError } from "@trpc/server";
import {
  waitlistJoinSchema,
  contactSubmitSchema,
  contactListSchema,
  contactUpdateStatusSchema,
  blogCreateSchema,
  blogUpdateSchema,
  blogSlugSchema,
  reviewQueueListSchema,
  reviewItemUpdateSchema,
  bookkeepingDocumentRegisterSchema,
  reviewItemCreateSchema,
  workflowRunCreateSchema,
  workflowRunUpdateSchema,
  routingAttemptCreateSchema,
  auditEventListSchema,
  waveActionExecuteSchema,
  waveActionPlanSchema,
  mijngeldzakenActionExecuteSchema,
  mijngeldzakenActionPlanSchema,
  automationCapabilityPlanSchema,
  automationWorkflowPlanSchema,
  automationWorkflowQueueSchema,
  automationWorkflowOperationUpdateSchema,
  automationWorkflowOperationClaimSchema,
  automationWorkflowExecutorCycleSchema,
  automationWorkflowExecutorLoopSchema,
  automationWorkflowDraftArtifactSchema,
  automationWorkflowMasterLedgerSchema,
} from "@shared/validation";
import { getSessionCookieOptions } from "./_core/cookies";
import { systemRouter } from "./_core/systemRouter";
import { stripeRouter } from "./routers/stripe";
import { publicProcedure, router, adminProcedure } from "./_core/trpc";
import {
  addToWaitlist,
  getWaitlistCount,
  getWaitlistEntries,
  getWaitlistStats,
  addContactMessage,
  getContactMessages,
  getContactMessageCount,
  updateContactMessageStatus,
  createBlogPost,
  updateBlogPost,
  deleteBlogPost,
  getBlogPostBySlug,
  getBlogPostById,
  getPublishedBlogPosts,
  getAllBlogPosts,
  getBlogPostCount,
  getBookkeepingOverview,
  createBookkeepingDocumentWithAudit,
  addReviewItemWithAudit,
  createWorkflowRunWithAudit,
  updateWorkflowRunWithAudit,
  createRoutingAttemptWithAudit,
  recordAuditEvent,
  getReviewQueue,
  getRecentWorkflowRuns,
  getWorkflowRunById,
  getRecentReconciliationMatches,
  getRecentAuditEvents,
  updateReviewItemStatus,
} from "./db";
import { notifyOwner } from "./_core/notification";
import { sanitizeText, sanitizeRichContent, sanitizeSlug } from "./lib/sanitize";
import { createLogger } from "./lib/logger";
import { findWaveAction, findWaveSurface, getWaveParitySummary, getWaveSurfaceCatalog } from "@shared/waveSurface";
import {
  buildMijngeldzakenMasterLedgerDraft,
  findMijngeldzakenAction,
  getMijngeldzakenParitySummary,
  getMijngeldzakenSurfaceCatalog,
  planMijngeldzakenAction,
} from "@shared/mijngeldzakenSurface";
import {
  getAutonomousBookkeeperPlaybook,
  getAutomationPlaybookSummary,
  planAutomationCapability,
  planAutomationWorkflow,
} from "@shared/bookkeeperAutomation";
import {
  buildAutomationWorkflowMasterLedgerProjection,
  buildAutomationWorkflowMasterLedgerCsv,
} from "@shared/masterLedgerProjection";
import { fabRouter } from "./fabRouter";
import { z } from "zod";

const log = createLogger("Router");
type WorkflowRunStatusValue = "queued" | "running" | "completed" | "completed_with_review";

function buildWaveOperationId(actionId: string, payload: Record<string, unknown>) {
  return `wave:${crypto
    .createHash("sha256")
    .update(`${actionId}:${JSON.stringify(payload, Object.keys(payload).sort())}`)
    .digest("hex")}`;
}

function buildMijngeldzakenOperationId(actionId: string, payload: Record<string, unknown>) {
  return `mijngeldzaken:${crypto
    .createHash("sha256")
    .update(`${actionId}:${JSON.stringify(payload, Object.keys(payload).sort())}`)
    .digest("hex")}`;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function stringValue(value: unknown, fallback = "") {
  return typeof value === "string" ? value : fallback;
}

function booleanValue(value: unknown) {
  return value === true;
}

function stringArrayValue(value: unknown) {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function operationSummaries(value: unknown) {
  if (!Array.isArray(value)) return [];

  return value.flatMap((item) => {
    const operation = asRecord(item);
    if (!operation) return [];
    const targetSystem = stringValue(operation.targetSystem, "waveapps");
    const safety = stringValue(operation.safety);
    const actionId = stringValue(operation.actionId);
    const surfaceId = stringValue(operation.surfaceId);
    const derivedDraft =
      targetSystem === "mijngeldzaken" && safety === "safe_draft"
        ? asRecord(operation.masterLedgerDraft) || buildMijngeldzakenMasterLedgerDraft({
            actionId,
            surfaceId,
            payload: asRecord(operation.payload) || {},
            sourceProof: {
              workflowId: operation.workflowId,
              stepId: operation.stepId,
              operationId: operation.operationId,
            },
          })
        : undefined;

    return [{
      operationId: stringValue(operation.operationId),
      targetSystem,
      stepId: stringValue(operation.stepId),
      surfaceId,
      actionId,
      mode: stringValue(operation.mode),
      safety,
      status: stringValue(operation.status, "pending"),
      message: stringValue(operation.message),
      externalId: stringValue(operation.externalId),
      actor: stringValue(operation.actor),
      claimedAt: stringValue(operation.claimedAt),
      leaseExpiresAt: stringValue(operation.leaseExpiresAt),
      updatedAt: stringValue(operation.updatedAt),
      masterLedgerChecksum: stringValue(operation.masterLedgerChecksum) || stringValue(asRecord(derivedDraft)?.checksum),
      masterLedgerDraftType: stringValue(asRecord(derivedDraft)?.draftType),
    }];
  });
}

function masterLedgerProjectionForRun(run: { id: number; metadata: unknown }) {
  const metadata = asRecord(run.metadata);
  const workflowId = stringValue(metadata?.workflowId);
  return buildAutomationWorkflowMasterLedgerProjection({
    workflowRunId: run.id,
    workflowId,
    operations: metadata ? metadataOperations(metadata) : [],
  });
}

function targetBreakdown(operations: Array<{ targetSystem: string }>) {
  return operations.reduce<Record<string, number>>((acc, operation) => {
    const target = operation.targetSystem || "waveapps";
    acc[target] = (acc[target] || 0) + 1;
    return acc;
  }, {});
}

function metadataOperations(metadata: Record<string, unknown>) {
  return Array.isArray(metadata.operations)
    ? metadata.operations.flatMap((item) => {
        const operation = asRecord(item);
        return operation ? [operation] : [];
      })
    : [];
}

function resolveWorkflowOperationState(operations: Array<Record<string, unknown>>) {
  const statuses = operations.map((operation) => stringValue(operation.status, "pending"));
  const failedOrBlocked = statuses.some((status) => status === "failed" || status === "blocked");
  const allTerminal = statuses.every((status) => ["succeeded", "failed", "blocked", "skipped"].includes(status));
  const anyRunning = statuses.some((status) => status === "running");
  const nextWorkflowStatus: WorkflowRunStatusValue =
    failedOrBlocked
      ? "completed_with_review"
      : allTerminal
        ? "completed"
        : anyRunning
          ? "running"
          : "queued";
  const reviewCount = statuses.filter((status) => status === "failed" || status === "blocked").length;

  return {
    allTerminal,
    failedOrBlocked,
    nextWorkflowStatus,
    reviewCount,
  };
}

function findClaimableOperationIndex(operations: Array<Record<string, unknown>>, now: Date) {
  return operations.findIndex((operation) => {
    const status = stringValue(operation.status, "pending");
    if (status === "pending") return true;
    if (status !== "running") return false;

    const leaseExpiresAt = stringValue(operation.leaseExpiresAt);
    return leaseExpiresAt ? Date.parse(leaseExpiresAt) <= now.getTime() : false;
  });
}

function completePolicyGatedOperation(operation: Record<string, unknown>, completedAt: Date) {
  const targetSystem = stringValue(operation.targetSystem, "waveapps");
  const action =
    targetSystem === "mijngeldzaken"
      ? findMijngeldzakenAction(stringValue(operation.actionId))
      : findWaveAction(stringValue(operation.actionId));
  const supported = Boolean(action && action.surfaceId === stringValue(operation.surfaceId));
  const canAutoComplete = supported && ["read_only", "safe_draft"].includes(action!.safety);
  const isSafeDraft = supported && action!.safety === "safe_draft";
  const targetLabel = targetSystem === "mijngeldzaken" ? "MijnGeldzaken" : "Wave";
  const masterLedgerDraft =
    targetSystem === "mijngeldzaken" && isSafeDraft
      ? buildMijngeldzakenMasterLedgerDraft({
          actionId: stringValue(operation.actionId),
          surfaceId: stringValue(operation.surfaceId),
          payload: asRecord(operation.payload) || {},
          sourceProof: {
            workflowId: operation.workflowId,
            stepId: operation.stepId,
            operationId: operation.operationId,
          },
        })
      : undefined;
  const resultStatus = canAutoComplete ? "succeeded" : "blocked";
  const resultMessage = canAutoComplete
    ? isSafeDraft
      ? `${targetLabel} safe-draft operation prepared locally; no external submission was performed.`
      : `Read-only ${targetLabel} operation completed by the policy-gated executor cycle.`
    : supported
      ? `${targetLabel} action safety '${action!.safety}' requires a dedicated write/credential executor or review.`
      : `${targetLabel} action is not supported by the FAB action catalog.`;

  return {
    action,
    canAutoComplete,
    resultStatus,
    resultMessage,
    operation: {
      ...operation,
      status: resultStatus,
      message: resultMessage,
      externalId: canAutoComplete
        ? `${isSafeDraft ? "fab-draft" : "fab-readonly"}:${stringValue(operation.operationId)}`
        : null,
      evidence: {
        executorMode: "policy_gated_cycle",
        targetSystem,
        externalSubmission: "not_executed",
        draftPrepared: isSafeDraft,
        masterLedgerDraft,
        masterLedgerChecksum: masterLedgerDraft?.checksum,
        actionId: stringValue(operation.actionId),
        surfaceId: stringValue(operation.surfaceId),
        safety: action?.safety ?? "unsupported",
        result: resultStatus,
      },
      masterLedgerDraft,
      masterLedgerChecksum: masterLedgerDraft?.checksum,
      updatedAt: completedAt.toISOString(),
      completedAt: completedAt.toISOString(),
    } as Record<string, unknown>,
  };
}

function buildAutomationWorkflowDraftArtifact(operation: Record<string, unknown>, format: "json" | "csv") {
  const targetSystem = stringValue(operation.targetSystem, "waveapps");
  if (targetSystem !== "mijngeldzaken") {
    return {
      status: "unsupported_target" as const,
      message: "Only MijnGeldzaken workflow operations expose master-ledger draft artifacts.",
    };
  }

  const storedDraft = asRecord(operation.masterLedgerDraft);
  const draft = storedDraft || buildMijngeldzakenMasterLedgerDraft({
    actionId: stringValue(operation.actionId),
    surfaceId: stringValue(operation.surfaceId),
    payload: asRecord(operation.payload) || {},
    sourceProof: {
      workflowId: operation.workflowId,
      stepId: operation.stepId,
      operationId: operation.operationId,
    },
  });

  if (!draft) {
    return {
      status: "no_artifact" as const,
      message: "Workflow operation does not have a master-ledger draft artifact.",
    };
  }

  const checksum = stringValue(draft.checksum);
  const draftType = stringValue(draft.draftType);
  const filenameBase = `fab-mijngeldzaken-${stringValue(operation.operationId, "draft").replace(/[^a-zA-Z0-9_-]/g, "-")}`;
  if (format === "json") {
    return {
      status: "prepared" as const,
      artifact: {
        format,
        contentType: "application/json",
        filename: `${filenameBase}.json`,
        checksum,
        draftType,
        externalSubmission: "not_executed",
        content: draft,
      },
    };
  }

  if (draftType !== "transaction_import") {
    return {
      status: "unsupported_format" as const,
      message: "CSV artifacts are only available for MijnGeldzaken transaction-import drafts.",
      supportedFormats: ["json"],
    };
  }

  return {
    status: "prepared" as const,
    artifact: {
      format,
      contentType: "text/csv",
      filename: `${filenameBase}.csv`,
      checksum,
      draftType,
      externalSubmission: "not_executed",
      content: mijngeldzakenDraftCsv(draft),
    },
  };
}

function mijngeldzakenDraftCsv(draft: Record<string, unknown>) {
  const row = asRecord(draft.importRow) || {};
  const columns = Array.isArray(draft.columns) ? draft.columns.map((column) => String(column)) : Object.keys(row);
  return `${columns.map(csvCell).join(",")}\n${columns.map((column) => csvCell(row[column])).join(",")}\n`;
}

function csvCell(value: unknown) {
  const text = value === null || value === undefined ? "" : String(value);
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

async function createAutonomousWorkflowReviewItem(params: {
  actorUserId: number;
  workflowRunId: number;
  workflowId: string;
  message: string;
  source: string;
  operation?: Record<string, unknown> | null;
  missingSignals?: string[];
  missingFields?: Array<{ stepId: string; actionId: string; fields: string[] }>;
}) {
  const result = await addReviewItemWithAudit({
    documentId: null,
    reason: "autonomous_wave_workflow_blocked",
    details: sanitizeText(params.message),
    status: "pending",
    correctedData: {
      workflowRunId: params.workflowRunId,
      workflowId: params.workflowId,
      source: params.source,
      operationId: params.operation ? stringValue(params.operation.operationId) : null,
      stepId: params.operation ? stringValue(params.operation.stepId) : null,
      surfaceId: params.operation ? stringValue(params.operation.surfaceId) : null,
      actionId: params.operation ? stringValue(params.operation.actionId) : null,
      safety: params.operation ? stringValue(params.operation.safety) : null,
      missingSignals: params.missingSignals || [],
      missingFields: params.missingFields || [],
    },
  }, {
    actorUserId: params.actorUserId,
    action: "review_item.create_autonomous_exception",
    entityType: "review_item",
    details: {
      workflowRunId: params.workflowRunId,
      workflowId: params.workflowId,
      source: params.source,
      operationId: params.operation ? stringValue(params.operation.operationId) : null,
    },
  });

  return result;
}

export const appRouter = router({
  system: systemRouter,
  stripe: stripeRouter,

  fab: fabRouter,

  auth: router({
    me: publicProcedure.query(opts => opts.ctx.user),
    logout: publicProcedure.mutation(({ ctx }) => {
      const cookieOptions = getSessionCookieOptions(ctx.req);
      ctx.res.clearCookie(COOKIE_NAME, { ...cookieOptions, maxAge: -1 });
      return { success: true } as const;
    }),
  }),

  waitlist: router({
    join: publicProcedure
      .input(waitlistJoinSchema)
      .mutation(async ({ input }) => {
        // Sanitize all text inputs to prevent stored XSS
        const sanitizedEmail = sanitizeText(input.email).toLowerCase();
        const sanitizedFirst = input.firstName ? sanitizeText(input.firstName) : null;
        const sanitizedLast = input.lastName ? sanitizeText(input.lastName) : null;

        const result = await addToWaitlist({
          email: sanitizedEmail,
          firstName: sanitizedFirst,
          lastName: sanitizedLast,
          source: "website",
        });

        if (result.duplicate) {
          return { success: true, message: "already_registered" } as const;
        }

        const count = await getWaitlistCount();
        log.info("New waitlist signup", { email: sanitizedEmail, total: count });

        await notifyOwner({
          title: `New waitlist signup: ${sanitizedEmail}`,
          content: `${sanitizedFirst || ""} ${sanitizedLast || ""} (${sanitizedEmail}) just joined the FAB waitlist.\n\nTotal signups: ${count}.`,
        }).catch((err) => {
          log.warn("Failed to notify owner about waitlist signup", {}, err instanceof Error ? err : undefined);
        });

        return { success: true, message: "registered" } as const;
      }),

    count: publicProcedure.query(async () => {
      const count = await getWaitlistCount();
      return { count };
    }),

    list: adminProcedure.query(async () => {
      return getWaitlistEntries();
    }),

    stats: adminProcedure.query(async () => {
      return getWaitlistStats();
    }),
  }),

  contact: router({
    submit: publicProcedure
      .input(contactSubmitSchema)
      .mutation(async ({ input }) => {
        // Sanitize all text inputs
        const sanitized = {
          firstName: sanitizeText(input.firstName),
          lastName: sanitizeText(input.lastName),
          email: sanitizeText(input.email).toLowerCase(),
          subject: sanitizeText(input.subject),
          message: sanitizeText(input.message),
        };

        await addContactMessage(sanitized);

        log.info("New contact message", {
          from: sanitized.email,
          subject: sanitized.subject,
        });

        await notifyOwner({
          title: `New contact message from ${sanitized.firstName} ${sanitized.lastName}`,
          content: `From: ${sanitized.firstName} ${sanitized.lastName} (${sanitized.email})\nSubject: ${sanitized.subject}\n\n${sanitized.message}`,
        }).catch((err) => {
          log.warn("Failed to notify owner about contact message", {}, err instanceof Error ? err : undefined);
        });

        return { success: true } as const;
      }),

    list: adminProcedure
      .input(contactListSchema)
      .query(async ({ input }) => {
        return getContactMessages(input);
      }),

    count: adminProcedure.query(async () => {
      const count = await getContactMessageCount();
      return { count };
    }),

    updateStatus: adminProcedure
      .input(contactUpdateStatusSchema)
      .mutation(async ({ input }) => {
        await updateContactMessageStatus(input.id, input.status);
        return { success: true } as const;
      }),
  }),

  blog: router({
    published: publicProcedure
      .input(
        z.object({
          category: z.string().max(50).optional(),
          limit: z.number().int().min(1).max(50).optional(),
        }).optional()
      )
      .query(async ({ input }) => {
        return getPublishedBlogPosts(input);
      }),

    bySlug: publicProcedure
      .input(z.object({ slug: blogSlugSchema }))
      .query(async ({ input }) => {
        const post = await getBlogPostBySlug(input.slug);
        if (!post || !post.published) return null;
        return post;
      }),

    list: adminProcedure.query(async () => {
      return getAllBlogPosts();
    }),

    byId: adminProcedure
      .input(z.object({ id: z.number().int().positive() }))
      .query(async ({ input }) => {
        return getBlogPostById(input.id);
      }),

    count: adminProcedure.query(async () => {
      const count = await getBlogPostCount();
      return { count };
    }),

    create: adminProcedure
      .input(blogCreateSchema)
      .mutation(async ({ input, ctx }) => {
        // Sanitize content fields to prevent stored XSS
        const sanitized = {
          title: sanitizeText(input.title),
          titleNl: input.titleNl ? sanitizeText(input.titleNl) : null,
          slug: sanitizeSlug(input.slug),
          excerpt: sanitizeText(input.excerpt),
          excerptNl: input.excerptNl ? sanitizeText(input.excerptNl) : null,
          content: sanitizeRichContent(input.content),
          contentNl: input.contentNl ? sanitizeRichContent(input.contentNl) : null,
          category: sanitizeText(input.category),
          coverImage: input.coverImage || null,
          published: input.published,
          readTimeMinutes: input.readTimeMinutes,
          authorId: ctx.user.id,
          publishedAt: input.published ? new Date() : null,
        };

        const result = await createBlogPost(sanitized);
        log.info("Blog post created", { id: result.id, slug: sanitized.slug });
        return result;
      }),

    update: adminProcedure
      .input(blogUpdateSchema)
      .mutation(async ({ input }) => {
        const { id, ...rawData } = input;

        // Sanitize any provided text fields
        const data: Record<string, unknown> = {};
        if (rawData.title !== undefined) data.title = sanitizeText(rawData.title);
        if (rawData.titleNl !== undefined) data.titleNl = rawData.titleNl ? sanitizeText(rawData.titleNl) : null;
        if (rawData.slug !== undefined) data.slug = sanitizeSlug(rawData.slug);
        if (rawData.excerpt !== undefined) data.excerpt = sanitizeText(rawData.excerpt);
        if (rawData.excerptNl !== undefined) data.excerptNl = rawData.excerptNl ? sanitizeText(rawData.excerptNl) : null;
        if (rawData.content !== undefined) data.content = sanitizeRichContent(rawData.content);
        if (rawData.contentNl !== undefined) data.contentNl = rawData.contentNl ? sanitizeRichContent(rawData.contentNl) : null;
        if (rawData.category !== undefined) data.category = sanitizeText(rawData.category);
        if (rawData.coverImage !== undefined) data.coverImage = rawData.coverImage || null;
        if (rawData.published !== undefined) data.published = rawData.published;
        if (rawData.readTimeMinutes !== undefined) data.readTimeMinutes = rawData.readTimeMinutes;

        // If publishing for the first time, set publishedAt
        if (data.published === true) {
          const existing = await getBlogPostById(id);
          if (existing && !existing.publishedAt) {
            data.publishedAt = new Date();
          }
        }

        await updateBlogPost(id, data);
        log.info("Blog post updated", { id });
        return { success: true } as const;
      }),

    delete: adminProcedure
      .input(z.object({ id: z.number().int().positive() }))
      .mutation(async ({ input }) => {
        await deleteBlogPost(input.id);
        log.info("Blog post deleted", { id: input.id });
        return { success: true } as const;
      }),
  }),

  bookkeeping: router({
    overview: adminProcedure.query(async () => {
      return getBookkeepingOverview();
    }),

    waveSurface: adminProcedure.query(() => {
      return getWaveSurfaceCatalog();
    }),

    waveParity: adminProcedure.query(() => {
      return getWaveParitySummary();
    }),

    mijngeldzakenSurface: adminProcedure.query(() => {
      return getMijngeldzakenSurfaceCatalog();
    }),

    mijngeldzakenParity: adminProcedure.query(() => {
      return getMijngeldzakenParitySummary();
    }),

    planMijngeldzakenAction: adminProcedure
      .input(mijngeldzakenActionPlanSchema)
      .query(({ input }) => {
        return planMijngeldzakenAction(input);
      }),

    executeMijngeldzakenAction: adminProcedure
      .input(mijngeldzakenActionExecuteSchema)
      .mutation(async ({ input, ctx }) => {
        const action = findMijngeldzakenAction(input.actionId);

        if (!action || action.surfaceId !== input.surfaceId) {
          return {
            status: "unsupported" as const,
            message: "MijnGeldzaken action is not in the FAB action catalog.",
            operation: null,
          };
        }

        const missingFields = action.requiredFields.filter((field) => input.payload[field] === undefined);
        const operation = {
          operationId: input.idempotencyKey || buildMijngeldzakenOperationId(action.id, input.payload),
          targetSystem: "mijngeldzaken",
          surfaceId: action.surfaceId,
          actionId: action.id,
          mode: action.mode,
          safety: action.safety,
          payload: input.payload,
          actor: input.actor,
          createdByUserId: ctx.user.id,
          createdAt: new Date().toISOString(),
        };

        if (missingFields.length) {
          return {
            status: "needs_review" as const,
            message: "MijnGeldzaken action cannot run until required fields are present.",
            missingFields,
            operation,
          };
        }

        if (action.safety === "requires_credentials") {
          return {
            status: "blocked_requires_credentials" as const,
            message: "MijnGeldzaken action requires user-owned sign-in or stored credential authorization.",
            missingFields: [],
            operation,
          };
        }

        if (action.safety === "requires_confirmation" && !input.confirmed) {
          return {
            status: "blocked_requires_confirmation" as const,
            message: "MijnGeldzaken action changes external state and needs explicit confirmation.",
            missingFields: [],
            operation,
          };
        }

        await recordAuditEvent({
          actorUserId: ctx.user.id,
          action: input.mode === "dry_run" ? "mijngeldzaken_action.dry_run" : "mijngeldzaken_action.queue",
          entityType: "mijngeldzaken_action",
          entityId: operation.operationId,
          details: {
            targetSystem: "mijngeldzaken",
            surfaceId: action.surfaceId,
            actionId: action.id,
            safety: action.safety,
            mode: input.mode,
          },
        });

        return {
          status: input.mode === "dry_run" ? "planned" as const : "queued" as const,
          message:
            input.mode === "dry_run"
              ? "MijnGeldzaken action passed policy checks in dry-run mode."
              : "MijnGeldzaken action is queued for an API or browser executor.",
          missingFields: [],
          operation,
        };
      }),

    automationPlaybook: adminProcedure.query(() => {
      return getAutonomousBookkeeperPlaybook();
    }),

    automationParity: adminProcedure.query(() => {
      return getAutomationPlaybookSummary();
    }),

    planAutomationCapability: adminProcedure
      .input(automationCapabilityPlanSchema)
      .query(({ input }) => {
        return planAutomationCapability(input);
      }),

    planAutomationWorkflow: adminProcedure
      .input(automationWorkflowPlanSchema)
      .query(({ input }) => {
        return planAutomationWorkflow(input);
      }),

    queueAutomationWorkflow: adminProcedure
      .input(automationWorkflowQueueSchema)
      .mutation(async ({ input, ctx }) => {
        const plan = planAutomationWorkflow(input);

        const missingFields: Array<{ stepId: string; actionId: string; fields: string[] }> = [];
        const operations = plan.steps.map((step) => {
          const targetSystem = step.targetSystem ?? "waveapps";
          const waveSurface = targetSystem === "waveapps" ? findWaveSurface(step.surfaceId) : undefined;
          const action =
            targetSystem === "mijngeldzaken"
              ? findMijngeldzakenAction(step.actionId)
              : findWaveAction(step.actionId);
          const resolvedSurfaceId = waveSurface?.id ?? step.surfaceId;
          const isSupported = Boolean(action && action.surfaceId === resolvedSurfaceId);
          const fields = isSupported ? action!.requiredFields.filter((field) => step.payload[field] === undefined) : [];

          if (fields.length) {
            missingFields.push({ stepId: step.id, actionId: step.actionId, fields });
          }

          return {
            operationId:
              input.idempotencyKey ? `${input.idempotencyKey}:${step.id}` :
              targetSystem === "mijngeldzaken"
                ? buildMijngeldzakenOperationId(`${input.workflowId}:${step.id}:${step.actionId}`, step.payload)
                : buildWaveOperationId(`${input.workflowId}:${step.id}:${step.actionId}`, step.payload),
            workflowId: input.workflowId,
            targetSystem,
            stepId: step.id,
            surfaceId: resolvedSurfaceId,
            actionId: action?.id ?? step.actionId,
            mode: isSupported ? action!.mode : "read",
            safety: isSupported ? action!.safety : "unsupported",
            payload: step.payload,
            actor: input.actor,
            createdByUserId: ctx.user.id,
            createdAt: new Date().toISOString(),
          };
        });

        const unsupportedActions = operations.filter((operation) => operation.safety === "unsupported");
        const credentialActions = operations.filter((operation) => operation.safety === "requires_credentials");
        const confirmationActions = operations.filter((operation) => operation.safety === "requires_confirmation");

        async function persistRun(params: {
          status: "queued" | "completed" | "completed_with_review" | "failed";
          message: string;
          blockingActions: typeof operations;
          auditAction: string;
        }) {
          const now = new Date();
          const planReviewCount = plan.status === "ready" ? 0 : plan.missingSignals.length + plan.reviewGates.length;
          const documentsNeedingReview =
            missingFields.length +
            params.blockingActions.length +
            planReviewCount;
          const workflowRun = await createWorkflowRunWithAudit({
            status: params.status,
            triggerSource: `automation:${input.workflowId}`,
            documentsImported: 0,
            documentsProcessed: operations.length,
            documentsNeedingReview,
            errorMessage:
              params.status === "completed_with_review" || params.status === "failed" ? params.message : null,
            startedAt: now,
            finishedAt: params.status === "queued" ? null : now,
            metadata: {
              workflowId: input.workflowId,
              mode: input.mode,
              actor: input.actor,
              planStatus: plan.status,
              canRunAutonomously: plan.canRunAutonomously,
              requiredSignals: plan.requiredSignals,
              missingSignals: plan.missingSignals,
              reviewGates: plan.reviewGates,
              missingFields,
              blockingActions: params.blockingActions.map((operation) => ({
                operationId: operation.operationId,
                stepId: operation.stepId,
                targetSystem: operation.targetSystem,
                surfaceId: operation.surfaceId,
                actionId: operation.actionId,
                safety: operation.safety,
              })),
              operations: operations.map((operation) => ({
                operationId: operation.operationId,
                stepId: operation.stepId,
                targetSystem: operation.targetSystem,
                surfaceId: operation.surfaceId,
                actionId: operation.actionId,
                mode: operation.mode,
                safety: operation.safety,
                status:
                  params.blockingActions.some((blocked) => blocked.operationId === operation.operationId)
                    ? "blocked"
                    : input.mode === "dry_run"
                      ? "succeeded"
                      : "pending",
                payload: operation.payload,
              })),
            },
          }, {
            actorUserId: ctx.user.id,
            action: params.auditAction,
            entityType: "automation_workflow",
            details: {
              workflowId: input.workflowId,
              mode: input.mode,
              status: plan.status,
              canRunAutonomously: plan.canRunAutonomously,
              steps: operations.map((operation) => ({
                operationId: operation.operationId,
                stepId: operation.stepId,
                targetSystem: operation.targetSystem,
                surfaceId: operation.surfaceId,
                actionId: operation.actionId,
                safety: operation.safety,
              })),
            },
          });

          if (documentsNeedingReview > 0) {
            await createAutonomousWorkflowReviewItem({
              actorUserId: ctx.user.id,
              workflowRunId: workflowRun.id,
              workflowId: input.workflowId,
              message: params.message,
              source: params.auditAction,
              operation: params.blockingActions[0] || null,
              missingSignals: plan.missingSignals,
              missingFields,
            });
          }

          return workflowRun.id;
        }

        if (unsupportedActions.length) {
          const message = "Workflow contains downstream actions that are not in the FAB action catalog.";
          const workflowRunId = await persistRun({
            status: "failed",
            message,
            blockingActions: unsupportedActions,
            auditAction: "automation_workflow.blocked",
          });

          return {
            status: "unsupported" as const,
            message,
            workflowRunId,
            plan,
            missingFields,
            operations,
            blockingActions: unsupportedActions,
          };
        }

        if (missingFields.length) {
          const message = "Workflow cannot run until required downstream action fields are present.";
          const workflowRunId = await persistRun({
            status: "completed_with_review",
            message,
            blockingActions: [],
            auditAction: "automation_workflow.blocked",
          });

          return {
            status: "needs_review" as const,
            message,
            workflowRunId,
            plan,
            missingFields,
            operations,
            blockingActions: [],
          };
        }

        if (credentialActions.length) {
          const message = "Workflow contains actions that require external credentials or provider authorization.";
          const workflowRunId = await persistRun({
            status: "completed_with_review",
            message,
            blockingActions: credentialActions,
            auditAction: "automation_workflow.blocked",
          });

          return {
            status: "blocked_requires_credentials" as const,
            message,
            workflowRunId,
            plan,
            missingFields,
            operations,
            blockingActions: credentialActions,
          };
        }

        if (confirmationActions.length && !input.confirmed) {
          const message = "Workflow contains external state changes and needs explicit confirmation.";
          const workflowRunId = await persistRun({
            status: "completed_with_review",
            message,
            blockingActions: confirmationActions,
            auditAction: "automation_workflow.blocked",
          });

          return {
            status: "blocked_requires_confirmation" as const,
            message,
            workflowRunId,
            plan,
            missingFields,
            operations,
            blockingActions: confirmationActions,
          };
        }

        if (plan.status === "needs_signals") {
          const workflowRunId = await persistRun({
            status: "completed_with_review",
            message: plan.nextAction,
            blockingActions: [],
            auditAction: "automation_workflow.blocked",
          });

          return {
            status: "needs_signals" as const,
            message: plan.nextAction,
            workflowRunId,
            plan,
            missingFields,
            operations,
            blockingActions: [],
          };
        }

        if (plan.status === "blocked_by_review" && !input.confirmed) {
          const workflowRunId = await persistRun({
            status: "completed_with_review",
            message: plan.nextAction,
            blockingActions: [],
            auditAction: "automation_workflow.blocked",
          });

          return {
            status: "blocked_by_review" as const,
            message: plan.nextAction,
            workflowRunId,
            plan,
            missingFields,
            operations,
            blockingActions: [],
          };
        }

        const message =
          input.mode === "dry_run"
            ? "Workflow passed downstream policy checks in dry-run mode."
            : "Workflow is queued for the autonomous downstream executor.";
        const workflowRunId = await persistRun({
          status: input.mode === "dry_run" ? "completed" : "queued",
          message,
          blockingActions: [],
          auditAction: input.mode === "dry_run" ? "automation_workflow.dry_run" : "automation_workflow.queue",
        });

        return {
          status: input.mode === "dry_run" ? "planned" as const : "queued" as const,
          message,
          workflowRunId,
          plan,
          missingFields,
          operations,
          blockingActions: [],
        };
      }),

    claimAutomationWorkflowOperation: adminProcedure
      .input(automationWorkflowOperationClaimSchema)
      .mutation(async ({ input, ctx }) => {
        const run = await getWorkflowRunById(input.workflowRunId);
        const metadata = asRecord(run?.metadata);

        if (!run || !metadata || !stringValue(metadata.workflowId) || !run.triggerSource.startsWith("automation:")) {
          return {
            status: "not_found" as const,
            message: "Autonomous workflow run was not found.",
            workflowRun: null,
            operation: null,
          };
        }

        if (!["queued", "running"].includes(run.status)) {
          return {
            status: "not_claimable" as const,
            message: "Autonomous workflow run is not queued or running.",
            workflowRun: run,
            operation: null,
          };
        }

        const operations = metadataOperations(metadata);
        const now = new Date();
        const operationIndex = findClaimableOperationIndex(operations, now);

        if (operationIndex < 0) {
          return {
            status: "no_pending_operation" as const,
            message: "No pending autonomous Wave operation is available to claim.",
            workflowRun: run,
            operation: null,
          };
        }

        const leaseExpiresAt = new Date(now.getTime() + input.leaseSeconds * 1000);
        const claimedOperation: Record<string, unknown> = {
          ...operations[operationIndex],
          status: "running",
          actor: input.actor,
          claimedAt: now.toISOString(),
          leaseExpiresAt: leaseExpiresAt.toISOString(),
          updatedAt: now.toISOString(),
        };
        const updatedOperations = operations.map((operation, index) =>
          index === operationIndex ? claimedOperation : operation
        );
        const updatedMetadata = {
          ...metadata,
          operations: updatedOperations,
          lastOperationClaim: {
            operationId: stringValue(claimedOperation.operationId),
            actor: input.actor,
            claimedAt: now.toISOString(),
            leaseExpiresAt: leaseExpiresAt.toISOString(),
          },
        };

        const updated = await updateWorkflowRunWithAudit(run.id, {
          status: "running",
          finishedAt: null,
          errorMessage: null,
          metadata: updatedMetadata,
        }, {
          actorUserId: ctx.user.id,
          action: "automation_workflow.operation_claim",
          entityType: "automation_workflow_operation",
          entityId: stringValue(claimedOperation.operationId),
          details: {
            workflowRunId: run.id,
            workflowId: stringValue(metadata.workflowId),
            operationId: stringValue(claimedOperation.operationId),
            actor: input.actor,
            leaseExpiresAt: leaseExpiresAt.toISOString(),
          },
        }, { expectedMetadata: metadata });
        if (!updated) throw new TRPCError({ code: "CONFLICT", message: "Workflow changed while this operation was being claimed. Refresh and retry." });

        return {
          status: "claimed" as const,
          message: "Autonomous Wave operation claimed for execution.",
          workflowRun: {
            id: run.id,
            status: "running" as const,
          },
          operation: claimedOperation,
        };
      }),

    runAutomationWorkflowExecutorCycle: adminProcedure
      .input(automationWorkflowExecutorCycleSchema)
      .mutation(async ({ input, ctx }) => {
        const run = await getWorkflowRunById(input.workflowRunId);
        const metadata = asRecord(run?.metadata);

        if (!run || !metadata || !stringValue(metadata.workflowId) || !run.triggerSource.startsWith("automation:")) {
          return {
            status: "not_found" as const,
            message: "Autonomous workflow run was not found.",
            workflowRun: null,
            operation: null,
          };
        }

        if (!["queued", "running"].includes(run.status)) {
          return {
            status: "not_claimable" as const,
            message: "Autonomous workflow run is not queued or running.",
            workflowRun: run,
            operation: null,
          };
        }

        const operations = metadataOperations(metadata);
        const now = new Date();
        const operationIndex = findClaimableOperationIndex(operations, now);

        if (operationIndex < 0) {
          return {
            status: "idle" as const,
            message: "No pending autonomous Wave operation is available to execute.",
            workflowRun: run,
            operation: null,
          };
        }

        const leaseExpiresAt = new Date(now.getTime() + input.leaseSeconds * 1000);
        const claimedOperation: Record<string, unknown> = {
          ...operations[operationIndex],
          status: "running",
          actor: input.actor,
          claimedAt: now.toISOString(),
          leaseExpiresAt: leaseExpiresAt.toISOString(),
          updatedAt: now.toISOString(),
        };
        const completedAt = new Date();
        const { action, canAutoComplete, resultStatus, resultMessage, operation: completedOperation } =
          completePolicyGatedOperation(claimedOperation, completedAt);
        const updatedOperations = operations.map((operation, index) =>
          index === operationIndex ? completedOperation : operation
        );
        const { allTerminal, failedOrBlocked, nextWorkflowStatus, reviewCount } =
          resolveWorkflowOperationState(updatedOperations);
        const updatedMetadata = {
          ...metadata,
          operations: updatedOperations,
          lastOperationClaim: {
            operationId: stringValue(claimedOperation.operationId),
            actor: input.actor,
            claimedAt: now.toISOString(),
            leaseExpiresAt: leaseExpiresAt.toISOString(),
          },
          lastOperationUpdate: {
            operationId: stringValue(completedOperation.operationId),
            status: resultStatus,
            actor: input.actor,
            updatedAt: completedAt.toISOString(),
          },
        };

        const updated = await updateWorkflowRunWithAudit(run.id, {
          status: nextWorkflowStatus,
          documentsNeedingReview: reviewCount,
          finishedAt: allTerminal || failedOrBlocked ? completedAt : null,
          errorMessage:
            failedOrBlocked
              ? resultMessage
              : null,
          metadata: updatedMetadata,
        }, {
          actorUserId: ctx.user.id,
          action: "automation_workflow.executor_cycle",
          entityType: "automation_workflow_operation",
          entityId: stringValue(completedOperation.operationId),
          details: {
            workflowRunId: run.id,
            workflowId: stringValue(metadata.workflowId),
            operationId: stringValue(completedOperation.operationId),
            actionId: stringValue(completedOperation.actionId),
            actor: input.actor,
            status: resultStatus,
            safety: action?.safety ?? "unsupported",
            message: resultMessage,
          },
        }, { expectedMetadata: metadata });
        if (!updated) throw new TRPCError({ code: "CONFLICT", message: "Workflow changed while this operation was executing. Refresh before retrying." });

        if (failedOrBlocked) {
          await createAutonomousWorkflowReviewItem({
            actorUserId: ctx.user.id,
            workflowRunId: run.id,
            workflowId: stringValue(metadata.workflowId),
            message: resultMessage,
            source: "automation_workflow.executor_cycle",
            operation: completedOperation,
          });
        }

        return {
          status: canAutoComplete ? "executed" as const : "blocked" as const,
          message: resultMessage,
          workflowRun: {
            id: run.id,
            status: nextWorkflowStatus,
            documentsNeedingReview: reviewCount,
          },
          operation: completedOperation,
        };
      }),

    runAutomationWorkflowExecutorLoop: adminProcedure
      .input(automationWorkflowExecutorLoopSchema)
      .mutation(async ({ input, ctx }) => {
        const run = await getWorkflowRunById(input.workflowRunId);
        const metadata = asRecord(run?.metadata);

        if (!run || !metadata || !stringValue(metadata.workflowId) || !run.triggerSource.startsWith("automation:")) {
          return {
            status: "not_found" as const,
            message: "Autonomous workflow run was not found.",
            workflowRun: null,
            operations: [],
          };
        }

        if (!["queued", "running"].includes(run.status)) {
          return {
            status: "not_claimable" as const,
            message: "Autonomous workflow run is not queued or running.",
            workflowRun: run,
            operations: [],
          };
        }

        let operations = metadataOperations(metadata);
        const completedOperations: Array<Record<string, unknown>> = [];
        let loopStatus: "executed" | "blocked" | "idle" | "limit_reached" = "idle";
        let loopMessage = "No pending autonomous Wave operation is available to execute.";

        for (let step = 0; step < input.maxSteps; step += 1) {
          const now = new Date();
          const operationIndex = findClaimableOperationIndex(operations, now);

          if (operationIndex < 0) {
            loopStatus = completedOperations.length ? "executed" : "idle";
            loopMessage = completedOperations.length
              ? "Executor loop completed all currently claimable safe operations."
              : "No pending autonomous Wave operation is available to execute.";
            break;
          }

          const leaseExpiresAt = new Date(now.getTime() + input.leaseSeconds * 1000);
          const claimedOperation: Record<string, unknown> = {
            ...operations[operationIndex],
            status: "running",
            actor: input.actor,
            claimedAt: now.toISOString(),
            leaseExpiresAt: leaseExpiresAt.toISOString(),
            updatedAt: now.toISOString(),
          };
          const completedAt = new Date();
          const { canAutoComplete, resultStatus, resultMessage, operation: completedOperation } =
            completePolicyGatedOperation(claimedOperation, completedAt);

          operations = operations.map((operation, index) =>
            index === operationIndex ? completedOperation : operation
          );
          completedOperations.push(completedOperation);

          if (!canAutoComplete) {
            loopStatus = "blocked";
            loopMessage = resultMessage;
            break;
          }

          loopStatus = "executed";
          loopMessage = "Executor loop completed safe read-only Wave operations.";
        }

        if (loopStatus === "executed" && completedOperations.length >= input.maxSteps) {
          const nextIndex = findClaimableOperationIndex(operations, new Date());
          if (nextIndex >= 0) {
            loopStatus = "limit_reached";
            loopMessage = "Executor loop reached its max step limit before all operations were completed.";
          }
        }

        if (!completedOperations.length) {
          return {
            status: loopStatus,
            message: loopMessage,
            workflowRun: run,
            operations: [],
          };
        }

        const completedAt = new Date();
        const { allTerminal, failedOrBlocked, nextWorkflowStatus, reviewCount } =
          resolveWorkflowOperationState(operations);
        const updatedMetadata = {
          ...metadata,
          operations,
          lastExecutorLoop: {
            actor: input.actor,
            status: loopStatus,
            operationCount: completedOperations.length,
            updatedAt: completedAt.toISOString(),
            operations: completedOperations.map((operation) => ({
              operationId: stringValue(operation.operationId),
              status: stringValue(operation.status),
              actionId: stringValue(operation.actionId),
              surfaceId: stringValue(operation.surfaceId),
            })),
          },
        };

        const updated = await updateWorkflowRunWithAudit(run.id, {
          status: nextWorkflowStatus,
          documentsNeedingReview: reviewCount,
          finishedAt: allTerminal || failedOrBlocked ? completedAt : null,
          errorMessage:
            failedOrBlocked
              ? loopMessage
              : null,
          metadata: updatedMetadata,
        }, {
          actorUserId: ctx.user.id,
          action: "automation_workflow.executor_loop",
          entityType: "automation_workflow",
          details: {
            workflowRunId: run.id,
            workflowId: stringValue(metadata.workflowId),
            actor: input.actor,
            status: loopStatus,
            operationCount: completedOperations.length,
            operations: completedOperations.map((operation) => ({
              operationId: stringValue(operation.operationId),
              actionId: stringValue(operation.actionId),
              surfaceId: stringValue(operation.surfaceId),
              status: stringValue(operation.status),
            })),
          },
        }, { expectedMetadata: metadata });
        if (!updated) throw new TRPCError({ code: "CONFLICT", message: "Workflow changed while the executor loop was running. Refresh before retrying." });

        if (failedOrBlocked) {
          const blockedOperation =
            completedOperations.find((operation) => stringValue(operation.status) === "blocked") ||
            completedOperations[completedOperations.length - 1];
          await createAutonomousWorkflowReviewItem({
            actorUserId: ctx.user.id,
            workflowRunId: run.id,
            workflowId: stringValue(metadata.workflowId),
            message: loopMessage,
            source: "automation_workflow.executor_loop",
            operation: blockedOperation,
          });
        }

        return {
          status: loopStatus,
          message: loopMessage,
          workflowRun: {
            id: run.id,
            status: nextWorkflowStatus,
            documentsNeedingReview: reviewCount,
          },
          operations: completedOperations,
        };
      }),

    updateAutomationWorkflowOperation: adminProcedure
      .input(automationWorkflowOperationUpdateSchema)
      .mutation(async ({ input, ctx }) => {
        const run = await getWorkflowRunById(input.workflowRunId);
        const metadata = asRecord(run?.metadata);

        if (!run || !metadata || !stringValue(metadata.workflowId) || !run.triggerSource.startsWith("automation:")) {
          return {
            status: "not_found" as const,
            message: "Autonomous workflow run was not found.",
            workflowRun: null,
            operation: null,
          };
        }

        const operations = metadataOperations(metadata);
        const operationIndex = operations.findIndex((operation) => operation.operationId === input.operationId);

        if (operationIndex < 0) {
          return {
            status: "operation_not_found" as const,
            message: "Wave operation was not found on this autonomous workflow run.",
            workflowRun: run,
            operation: null,
          };
        }

        const now = new Date();
        const updatedOperation = {
          ...operations[operationIndex],
          status: input.status,
          message: input.message || null,
          externalId: input.externalId || null,
          evidence: input.evidence,
          actor: input.actor,
          updatedAt: now.toISOString(),
        };
        const updatedOperations = operations.map((operation, index) =>
          index === operationIndex ? updatedOperation : operation
        );
        const { allTerminal, failedOrBlocked, nextWorkflowStatus, reviewCount } =
          resolveWorkflowOperationState(updatedOperations);
        const updatedMetadata = {
          ...metadata,
          operations: updatedOperations,
          lastOperationUpdate: {
            operationId: input.operationId,
            status: input.status,
            actor: input.actor,
            updatedAt: now.toISOString(),
          },
        };

        const updated = await updateWorkflowRunWithAudit(run.id, {
          status: nextWorkflowStatus,
          documentsNeedingReview: reviewCount,
          finishedAt: allTerminal || failedOrBlocked ? now : null,
          errorMessage:
            failedOrBlocked
              ? input.message || "Autonomous Wave operation requires review."
              : null,
          metadata: updatedMetadata,
        }, {
          actorUserId: ctx.user.id,
          action: "automation_workflow.operation_update",
          entityType: "automation_workflow_operation",
          entityId: input.operationId,
          details: {
            workflowRunId: run.id,
            workflowId: stringValue(metadata.workflowId),
            operationId: input.operationId,
            status: input.status,
            message: input.message || null,
            externalId: input.externalId || null,
          },
        }, { expectedMetadata: metadata });
        if (!updated) throw new TRPCError({ code: "CONFLICT", message: "Workflow changed while this operation was being updated. Refresh and retry." });

        if (failedOrBlocked) {
          await createAutonomousWorkflowReviewItem({
            actorUserId: ctx.user.id,
            workflowRunId: run.id,
            workflowId: stringValue(metadata.workflowId),
            message: input.message || "Autonomous Wave operation requires review.",
            source: "automation_workflow.operation_update",
            operation: updatedOperation,
          });
        }

        return {
          status: "updated" as const,
          message: "Autonomous Wave operation status updated.",
          workflowRun: {
            id: run.id,
            status: nextWorkflowStatus,
            documentsNeedingReview: reviewCount,
          },
          operation: updatedOperation,
        };
      }),

    planWaveAction: adminProcedure
      .input(waveActionPlanSchema)
      .query(({ input }) => {
        const surface = findWaveSurface(input.surfaceId);
        const action = findWaveAction(input.actionId);

        if (!surface || !action || action.surfaceId !== surface.id) {
          return {
            status: "unsupported" as const,
            surface,
            action,
            missingFields: [],
            canRunAutonomously: false,
            requiresConfirmation: true,
            message: "Wave action is not in the FAB action catalog.",
          };
        }

        const missingFields = action.requiredFields.filter((field) => input.payload[field] === undefined);
        const requiresConfirmation = ["requires_confirmation", "requires_credentials"].includes(action.safety);
        const canRunAutonomously =
          missingFields.length === 0 &&
          (action.safety === "read_only" || action.safety === "safe_draft" || input.allowWrite);

        return {
          status: missingFields.length ? "needs_fields" as const : "planned" as const,
          surface,
          action,
          missingFields,
          canRunAutonomously,
          requiresConfirmation,
          message: canRunAutonomously
            ? `FAB can plan ${action.label} against Wave ${surface.label}.`
            : `FAB can prepare ${action.label}, but execution requires review or confirmation.`,
        };
      }),

    executeWaveAction: adminProcedure
      .input(waveActionExecuteSchema)
      .mutation(async ({ input, ctx }) => {
        const surface = findWaveSurface(input.surfaceId);
        const action = findWaveAction(input.actionId);

        if (!surface || !action || action.surfaceId !== surface.id) {
          return {
            status: "unsupported" as const,
            message: "Wave action is not in the FAB action catalog.",
            operation: null,
          };
        }

        const missingFields = action.requiredFields.filter((field) => input.payload[field] === undefined);
        const operation = {
          operationId: input.idempotencyKey || buildWaveOperationId(action.id, input.payload),
          surfaceId: surface.id,
          actionId: action.id,
          mode: action.mode,
          safety: action.safety,
          payload: input.payload,
          actor: input.actor,
          createdByUserId: ctx.user.id,
          createdAt: new Date().toISOString(),
        };

        if (missingFields.length) {
          return {
            status: "needs_review" as const,
            message: "Wave action cannot run until required fields are present.",
            missingFields,
            operation,
          };
        }

        if (action.safety === "requires_credentials") {
          return {
            status: "blocked_requires_credentials" as const,
            message: "Wave action requires external credentials or provider authorization.",
            missingFields: [],
            operation,
          };
        }

        if (action.safety === "requires_confirmation" && !input.confirmed) {
          return {
            status: "blocked_requires_confirmation" as const,
            message: "Wave action changes external state and needs explicit confirmation.",
            missingFields: [],
            operation,
          };
        }

        await recordAuditEvent({
          actorUserId: ctx.user.id,
          action: input.mode === "dry_run" ? "wave_action.dry_run" : "wave_action.queue",
          entityType: "wave_action",
          entityId: operation.operationId,
          details: {
            surfaceId: surface.id,
            actionId: action.id,
            safety: action.safety,
            mode: input.mode,
          },
        });

        return {
          status: input.mode === "dry_run" ? "planned" as const : "queued" as const,
          message:
            input.mode === "dry_run"
              ? "Wave action passed policy checks in dry-run mode."
              : "Wave action is queued for a Wave API or browser executor.",
          missingFields: [],
          operation,
        };
      }),

    reviewQueue: adminProcedure
      .input(reviewQueueListSchema)
      .query(async ({ input }) => {
        return getReviewQueue(input);
      }),

    workflowRuns: adminProcedure.query(async () => {
      return getRecentWorkflowRuns(10);
    }),

    autonomousWorkflowRuns: adminProcedure.query(async () => {
      const runs = await getRecentWorkflowRuns(25);

      return runs.flatMap((run) => {
        const metadata = asRecord(run.metadata);
        const workflowId = stringValue(metadata?.workflowId);
        if (!metadata || !workflowId || !run.triggerSource.startsWith("automation:")) return [];

        const operations = operationSummaries(metadata.operations);
        const blockingActions = operationSummaries(metadata.blockingActions);
        const targets = targetBreakdown(operations);
        const masterLedger = masterLedgerProjectionForRun(run);

        return [{
          id: run.id,
          status: run.status,
          triggerSource: run.triggerSource,
          workflowId,
          mode: stringValue(metadata.mode, "unknown"),
          planStatus: stringValue(metadata.planStatus, "unknown"),
          canRunAutonomously: booleanValue(metadata.canRunAutonomously),
          documentsProcessed: run.documentsProcessed,
          documentsNeedingReview: run.documentsNeedingReview,
          operationCount: operations.length,
          targetBreakdown: targets,
          targetSystems: Object.keys(targets).sort(),
          waveOperationCount: targets.waveapps || 0,
          mijngeldzakenOperationCount: targets.mijngeldzaken || 0,
          masterLedger: {
            ledgerChecksum: masterLedger.ledgerChecksum,
            totalRows: masterLedger.summary.totalRows,
            blockedRows: masterLedger.summary.blockedRows,
            readyForDraft: masterLedger.summary.readyForDraft,
            readyForApproval: masterLedger.summary.readyForApproval,
            readyForExternalExecution: masterLedger.summary.readyForExternalExecution,
            downstreamStatuses: masterLedger.summary.downstreamStatuses,
            byTargetSystem: masterLedger.summary.byTargetSystem,
            externalSubmission: masterLedger.externalSubmission,
          },
          operations: operations.slice(0, 10),
          blockingActions,
          missingSignals: stringArrayValue(metadata.missingSignals),
          reviewGates: stringArrayValue(metadata.reviewGates),
          createdAt: run.createdAt,
          finishedAt: run.finishedAt,
        }];
      });
    }),

    automationWorkflowMasterLedger: adminProcedure
      .input(automationWorkflowMasterLedgerSchema.optional())
      .query(async ({ input, ctx }) => {
        const requestedRunId = input?.workflowRunId;
        const runs = requestedRunId
          ? await (async () => {
              const run = await getWorkflowRunById(requestedRunId);
              return run ? [run] : [];
            })()
          : await getRecentWorkflowRuns(25);
        const automationRuns = runs.filter((run) => {
          const metadata = asRecord(run.metadata);
          return Boolean(metadata && stringValue(metadata.workflowId) && run.triggerSource.startsWith("automation:"));
        });
        const operations = automationRuns.flatMap((run) => {
          const metadata = asRecord(run.metadata);
          return metadataOperations(metadata || {}).map((operation) => ({
            ...operation,
            workflowRunId: run.id,
            workflowId: stringValue(operation.workflowId, stringValue(metadata?.workflowId)),
          }));
        });
        const projection = buildAutomationWorkflowMasterLedgerProjection({
          workflowRunId: requestedRunId,
          workflowId: requestedRunId ? stringValue(asRecord(automationRuns[0]?.metadata)?.workflowId) : undefined,
          targetSystem: input?.targetSystem,
          operations,
        });
        const csv = buildAutomationWorkflowMasterLedgerCsv(projection);

        if (input?.audit) {
          await recordAuditEvent({
            actorUserId: ctx.user.id,
            action: "automation_workflow.master_ledger_projection_prepared",
            entityType: "automation_workflow",
            entityId: requestedRunId ? String(requestedRunId) : "aggregate",
            details: {
              workflowRunId: requestedRunId || null,
              targetSystem: input.targetSystem || null,
              ledgerChecksum: projection.ledgerChecksum,
              totalRows: projection.summary.totalRows,
              blockedRows: projection.summary.blockedRows,
              externalSubmission: "not_executed",
            },
          });
        }

        return {
          ...projection,
          csvArtifact: {
            format: "csv" as const,
            contentType: "text/csv",
            filename: requestedRunId
              ? `fab-web-master-ledger-run-${requestedRunId}.csv`
              : "fab-web-master-ledger.csv",
            checksum: projection.ledgerChecksum,
            externalSubmission: "not_executed",
            content: csv,
          },
        };
      }),

    automationWorkflowDraftArtifact: adminProcedure
      .input(automationWorkflowDraftArtifactSchema)
      .query(async ({ input, ctx }) => {
        const workflowRun = await getWorkflowRunById(input.workflowRunId);
        if (!workflowRun) {
          return {
            status: "not_found" as const,
            message: "Workflow run not found.",
          };
        }

        const metadata = asRecord(workflowRun.metadata);
        const operation = metadata ? metadataOperations(metadata).find((item) => stringValue(item.operationId) === input.operationId) : undefined;
        if (!operation) {
          return {
            status: "operation_not_found" as const,
            message: "Workflow operation not found.",
          };
        }

        const artifactResult = buildAutomationWorkflowDraftArtifact(operation, input.format);
        if (artifactResult.status === "prepared") {
          await recordAuditEvent({
            actorUserId: ctx.user.id,
            action: "automation_workflow.draft_artifact_prepared",
            entityType: "automation_workflow_operation",
            entityId: input.operationId,
            details: {
              workflowRunId: input.workflowRunId,
              workflowId: stringValue(operation.workflowId),
              targetSystem: stringValue(operation.targetSystem, "mijngeldzaken"),
              actionId: stringValue(operation.actionId),
              format: artifactResult.artifact.format,
              filename: artifactResult.artifact.filename,
              checksum: artifactResult.artifact.checksum,
              draftType: artifactResult.artifact.draftType,
              externalSubmission: "not_executed",
            },
          });
        }

        return {
          workflowRunId: input.workflowRunId,
          operationId: input.operationId,
          ...artifactResult,
        };
      }),

    reconciliationMatches: adminProcedure.query(async () => {
      return getRecentReconciliationMatches(10);
    }),

    auditEvents: adminProcedure
      .input(auditEventListSchema)
      .query(async ({ input }) => {
        return getRecentAuditEvents(input?.limit ?? 20);
      }),

    createWorkflowRun: adminProcedure
      .input(workflowRunCreateSchema)
      .mutation(async ({ input, ctx }) => {
        const result = await createWorkflowRunWithAudit({
          status: input.status,
          triggerSource: sanitizeText(input.triggerSource),
          metadata: input.metadata || null,
        }, {
          actorUserId: ctx.user.id,
          action: "workflow_run.create",
          entityType: "workflow_run",
          details: {
            status: input.status,
            triggerSource: sanitizeText(input.triggerSource),
          },
        });
        return result;
      }),

    updateWorkflowRun: adminProcedure
      .input(workflowRunUpdateSchema)
      .mutation(async ({ input, ctx }) => {
        const { id, ...data } = input;
        if (Object.keys(data).length === 0) {
          throw new TRPCError({ code: "BAD_REQUEST", message: "At least one workflow run field must be provided." });
        }
        if (!await updateWorkflowRunWithAudit(id, data, {
          actorUserId: ctx.user.id,
          action: "workflow_run.update",
          entityType: "workflow_run",
          details: { changedFields: Object.keys(data).sort() },
        })) {
          throw new TRPCError({ code: "NOT_FOUND", message: "Workflow run was not found." });
        }
        return { success: true } as const;
      }),

    registerDocument: adminProcedure
      .input(bookkeepingDocumentRegisterSchema)
      .mutation(async ({ input, ctx }) => {
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
          actorUserId: ctx.user.id,
          action: "document.register",
          entityType: "bookkeeping_document",
          details: { source: input.source },
        });
        return result;
      }),

    createReviewItem: adminProcedure
      .input(reviewItemCreateSchema)
      .mutation(async ({ input, ctx }) => {
        const result = await addReviewItemWithAudit({
          documentId: input.documentId || null,
          reason: sanitizeText(input.reason),
          details: input.details ? sanitizeText(input.details) : null,
          status: input.status,
          correctedData: input.correctedData || null,
        }, {
          actorUserId: ctx.user.id,
          action: "review_item.create",
          entityType: "review_item",
          details: { documentId: input.documentId || null, status: input.status },
        });
        return result;
      }),

    createRoutingAttempt: adminProcedure
      .input(routingAttemptCreateSchema)
      .mutation(async ({ input, ctx }) => {
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
          actorUserId: ctx.user.id,
          action: "routing_attempt.create",
          entityType: "routing_attempt",
          details: {
            documentId: input.documentId || null,
            bookkeepingRecordId: input.bookkeepingRecordId || null,
            target: input.target,
            status: input.status,
          },
        });
        return result;
      }),

    updateReviewStatus: adminProcedure
      .input(reviewItemUpdateSchema)
      .mutation(async ({ input, ctx }) => {
        const updated = await updateReviewItemStatus(input.id, input.status, input.resolution, ctx.user.id);
        if (!updated) {
          throw new TRPCError({ code: "NOT_FOUND", message: "Review item was not found." });
        }
        log.info("Review item status updated", {
          id: input.id,
          status: input.status,
          actor: ctx.user.id,
        });
        return { success: true } as const;
      }),
  }),
});

export type AppRouter = typeof appRouter;
