import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { and, eq, sql } from "drizzle-orm";
import { randomUUID } from "node:crypto";
import {
  auditEvents,
  bookkeepingDocuments,
  reviewItems,
  workflowRuns,
} from "../drizzle/schema";
import { appRouter } from "./routers";
import type { TrpcContext } from "./_core/context";

const integrationUrl = process.env.FAB_MYSQL_INTEGRATION_URL;
const previousDatabaseUrl = process.env.DATABASE_URL;
let dbModule: typeof import("./db");

function assertDisposableLoopbackDatabase(value: string): void {
  const parsed = new URL(value);
  const databaseName = parsed.pathname.replace(/^\//, "");
  if (
    parsed.protocol !== "mysql:" ||
    !["127.0.0.1", "localhost"].includes(parsed.hostname) ||
    databaseName !== "fab_test"
  ) {
    throw new Error(
      "MySQL integration tests only permit a loopback database named fab_test"
    );
  }
}

describe.skipIf(!integrationUrl)("MySQL database integration", () => {
  beforeAll(async () => {
    assertDisposableLoopbackDatabase(integrationUrl!);
    process.env.DATABASE_URL = integrationUrl;
    dbModule = await import("./db");
    const db = await dbModule.getDb();
    if (!db)
      throw new Error(
        "The disposable MySQL integration database is unavailable"
      );
    await db.execute(sql`SELECT 1`);
  });

  afterAll(async () => {
    if (dbModule) await dbModule.closeDb();
    if (previousDatabaseUrl === undefined) delete process.env.DATABASE_URL;
    else process.env.DATABASE_URL = previousDatabaseUrl;
  });

  it("keeps source registration idempotent and commits its audit event", async () => {
    const sourceDocumentId = `mysql-integration-${randomUUID()}`;
    const first = await dbModule.createBookkeepingDocumentWithAudit(
      {
        source: "mysql_integration_test",
        sourceDocumentId,
        originalFilename: "synthetic-receipt.pdf",
        mimeType: "application/pdf",
        documentType: "receipt",
        processingStatus: "needs_review",
        vendorName: "Original reviewed vendor",
        totalAmount: "12.34",
      },
      {
        actorUserId: null,
        action: "mysql_integration.document.register",
        entityType: "bookkeeping_document",
        details: { synthetic: true, pass: 1 },
      }
    );

    const retry = await dbModule.createBookkeepingDocumentWithAudit(
      {
        source: "mysql_integration_test",
        sourceDocumentId,
        originalFilename: "renamed-retry.pdf",
        documentType: "invoice",
        processingStatus: "imported",
        vendorName: "Retry must not overwrite review",
        totalAmount: "99.99",
      },
      {
        actorUserId: null,
        action: "mysql_integration.document.register",
        entityType: "bookkeeping_document",
        details: { synthetic: true, pass: 2 },
      }
    );

    const db = (await dbModule.getDb())!;
    const [stored] = await db
      .select()
      .from(bookkeepingDocuments)
      .where(eq(bookkeepingDocuments.id, first.id))
      .limit(1);
    const events = await db
      .select()
      .from(auditEvents)
      .where(
        and(
          eq(auditEvents.entityType, "bookkeeping_document"),
          eq(auditEvents.entityId, String(first.id)),
          eq(auditEvents.action, "mysql_integration.document.register")
        )
      );

    expect(retry.id).toBe(first.id);
    expect(stored?.originalFilename).toBe("synthetic-receipt.pdf");
    expect(stored?.documentType).toBe("receipt");
    expect(stored?.processingStatus).toBe("needs_review");
    expect(stored?.vendorName).toBe("Original reviewed vendor");
    expect(String(stored?.totalAmount)).toBe("12.34");
    expect(events).toHaveLength(2);
  });

  it("routes an authenticated dashboard registration through tRPC into MySQL with audit evidence", async () => {
    const now = new Date();
    const caller = appRouter.createCaller({
      user: {
        id: 91001,
        openId: `mysql-integration-admin-${randomUUID()}`,
        email: "integration-admin@example.invalid",
        name: "Synthetic integration admin",
        loginMethod: "integration_test",
        role: "admin",
        createdAt: now,
        updatedAt: now,
        lastSignedIn: now,
        stripeCustomerId: null,
      },
      req: {} as TrpcContext["req"],
      res: {} as TrpcContext["res"],
    });
    const sourceDocumentId = `mysql-trpc-${randomUUID()}`;
    const created = await caller.bookkeeping.registerDocument({
      source: "mysql_integration_test",
      sourceDocumentId,
      originalFilename: "synthetic-dashboard-receipt.pdf",
      mimeType: "application/pdf",
      documentType: "receipt",
      processingStatus: "needs_review",
      vendorName: "Synthetic dashboard vendor",
      totalAmount: 18.75,
      metadata: { synthetic: true },
    });

    const db = (await dbModule.getDb())!;
    const [stored] = await db
      .select()
      .from(bookkeepingDocuments)
      .where(eq(bookkeepingDocuments.id, created.id))
      .limit(1);
    const events = await db
      .select()
      .from(auditEvents)
      .where(
        and(
          eq(auditEvents.entityType, "bookkeeping_document"),
          eq(auditEvents.entityId, String(created.id)),
          eq(auditEvents.action, "document.register")
        )
      );

    expect(stored?.sourceDocumentId).toBe(sourceDocumentId);
    expect(stored?.vendorName).toBe("Synthetic dashboard vendor");
    expect(String(stored?.totalAmount)).toBe("18.75");
    expect(events).toHaveLength(1);
    expect(events[0]?.actorUserId).toBe(91001);
  });

  it("enforces workflow metadata compare-and-swap and audits only the winning update", async () => {
    const initialMetadata = { synthetic: true, revision: 1 };
    const updatedMetadata = { synthetic: true, revision: 2 };
    const workflow = await dbModule.createWorkflowRunWithAudit(
      {
        triggerSource: "mysql_integration_test",
        metadata: initialMetadata,
      },
      {
        actorUserId: null,
        action: "mysql_integration.workflow.create",
        entityType: "workflow_run",
        details: { synthetic: true },
      }
    );

    await expect(
      dbModule.updateWorkflowRunWithAudit(
        workflow.id,
        {
          status: "running",
          metadata: updatedMetadata,
        },
        {
          actorUserId: null,
          action: "mysql_integration.workflow.update",
          entityType: "workflow_run",
          details: { synthetic: true, revision: 2 },
        },
        { expectedMetadata: initialMetadata }
      )
    ).resolves.toBe(true);

    await expect(
      dbModule.updateWorkflowRunWithAudit(
        workflow.id,
        {
          status: "failed",
          errorMessage: "stale compare-and-swap must not apply",
        },
        {
          actorUserId: null,
          action: "mysql_integration.workflow.stale_update",
          entityType: "workflow_run",
          details: { synthetic: true },
        },
        { expectedMetadata: initialMetadata }
      )
    ).resolves.toBe(false);

    const db = (await dbModule.getDb())!;
    const [stored] = await db
      .select()
      .from(workflowRuns)
      .where(eq(workflowRuns.id, workflow.id))
      .limit(1);
    const events = await db
      .select()
      .from(auditEvents)
      .where(
        and(
          eq(auditEvents.entityType, "workflow_run"),
          eq(auditEvents.entityId, String(workflow.id))
        )
      );

    expect(stored?.status).toBe("running");
    expect(stored?.errorMessage).toBeNull();
    expect(stored?.metadata).toEqual(updatedMetadata);
    expect(events).toHaveLength(2);
    expect(events.map(event => event.action)).toEqual(
      expect.arrayContaining([
        "mysql_integration.workflow.create",
        "mysql_integration.workflow.update",
      ])
    );
  });

  it("commits review status and audit event together", async () => {
    const document = await dbModule.createBookkeepingDocument({
      source: "mysql_integration_test",
      sourceDocumentId: `mysql-review-document-${randomUUID()}`,
      originalFilename: "synthetic-review.pdf",
      documentType: "receipt",
      processingStatus: "needs_review",
    });
    const review = await dbModule.addReviewItem({
      documentId: document.id,
      reason: "mysql_integration_test",
      details: "Synthetic integration row",
    });

    await expect(
      dbModule.updateReviewItemStatus(
        review.id,
        "resolved",
        "Synthetic integration resolution"
      )
    ).resolves.toBe(true);

    const db = (await dbModule.getDb())!;
    const [stored] = await db
      .select()
      .from(reviewItems)
      .where(eq(reviewItems.id, review.id))
      .limit(1);
    const events = await db
      .select()
      .from(auditEvents)
      .where(
        and(
          eq(auditEvents.entityType, "review_item"),
          eq(auditEvents.entityId, String(review.id)),
          eq(auditEvents.action, "review_item.update_status")
        )
      );

    expect(stored?.status).toBe("resolved");
    expect(stored?.resolution).toBe("Synthetic integration resolution");
    expect(stored?.resolvedAt).toBeInstanceOf(Date);
    expect(events).toHaveLength(1);
  });

  it("rolls back document creation when its audit insert fails", async () => {
    const seededAudit = await dbModule.recordAuditEvent({
      actorUserId: null,
      action: "mysql_integration.rollback_seed",
      entityType: "mysql_integration_test",
      entityId: randomUUID(),
      details: { synthetic: true },
    });
    expect(seededAudit.id).not.toBeNull();

    const sourceDocumentId = `mysql-rollback-${randomUUID()}`;
    await expect(
      dbModule.createBookkeepingDocumentWithAudit(
        {
          source: "mysql_integration_test",
          sourceDocumentId,
          originalFilename: "must-rollback.pdf",
          documentType: "receipt",
          processingStatus: "imported",
        },
        {
          id: seededAudit.id!,
          actorUserId: null,
          action: "mysql_integration.rollback_collision",
          entityType: "bookkeeping_document",
          details: { synthetic: true },
        }
      )
    ).rejects.toMatchObject({ cause: { code: "ER_DUP_ENTRY" } });

    const db = (await dbModule.getDb())!;
    const rows = await db
      .select({ id: bookkeepingDocuments.id })
      .from(bookkeepingDocuments)
      .where(
        and(
          eq(bookkeepingDocuments.source, "mysql_integration_test"),
          eq(bookkeepingDocuments.sourceDocumentId, sourceDocumentId)
        )
      )
      .limit(1);
    expect(rows).toHaveLength(0);
  });
});
