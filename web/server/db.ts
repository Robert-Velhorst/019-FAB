import { eq, sql, desc, and, gte, lte, isNull } from "drizzle-orm";
import { drizzle } from "drizzle-orm/mysql2";
import {
  InsertUser,
  users,
  waitlist,
  InsertWaitlistEntry,
  contactMessages,
  InsertContactMessage,
  blogPosts,
  InsertBlogPost,
  bookkeepingDocuments,
  InsertBookkeepingDocument,
  reviewItems,
  InsertReviewItem,
  workflowRuns,
  InsertWorkflowRun,
  routingAttempts,
  InsertRoutingAttempt,
  reconciliationMatches,
  InsertReconciliationMatch,
  auditEvents,
  InsertAuditEvent,
} from "../drizzle/schema";
import { ENV } from './_core/env';
import { createLogger } from "./lib/logger";

const log = createLogger("Database");

let _db: ReturnType<typeof drizzle> | null = null;

type FabDatabase = NonNullable<Awaited<ReturnType<typeof getDb>>>;
type FabTransaction = Parameters<Parameters<FabDatabase["transaction"]>[0]>[0];
type AuditEventTemplate = Omit<InsertAuditEvent, "entityId"> & Pick<Partial<InsertAuditEvent>, "entityId">;

function auditEventForEntity(event: AuditEventTemplate, id: number): InsertAuditEvent {
  return { ...event, entityId: event.entityId ?? String(id) };
}

async function withAuditTransaction<T>(
  operation: (tx: FabTransaction) => Promise<{ result: T; audit: InsertAuditEvent | null }>,
): Promise<T> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  return db.transaction(async (tx) => {
    const { result, audit } = await operation(tx);
    if (audit) await tx.insert(auditEvents).values(audit);
    return result;
  });
}

// Lazily create the drizzle instance so local tooling can run without a DB.
export async function getDb() {
  if (!_db && process.env.DATABASE_URL) {
    try {
      _db = drizzle({
        connection: {
          uri: process.env.DATABASE_URL,
          waitForConnections: true,
          connectionLimit: 10,
          maxIdle: 5,
          idleTimeout: 60_000,
          queueLimit: 50,
          connectTimeout: 10_000,
        },
      });
    } catch (error) {
      log.warn(
        "Failed to connect",
        {},
        error instanceof Error ? error : new Error(String(error)),
      );
      _db = null;
    }
  }
  return _db;
}

export async function closeDb(): Promise<void> {
  const db = _db;
  _db = null;
  if (!db) return;

  await new Promise<void>((resolve, reject) => {
    db.$client.end((error) => error ? reject(error) : resolve());
  });
}

export async function upsertUser(user: InsertUser): Promise<void> {
  if (!user.openId) {
    throw new Error("User openId is required for upsert");
  }

  const db = await getDb();
  if (!db) {
    log.warn("Cannot upsert user: database not available");
    return;
  }

  try {
    const values: InsertUser = {
      openId: user.openId,
    };
    const updateSet: Record<string, unknown> = {};

    const textFields = ["name", "email", "loginMethod"] as const;
    type TextField = (typeof textFields)[number];

    const assignNullable = (field: TextField) => {
      const value = user[field];
      if (value === undefined) return;
      const normalized = value ?? null;
      values[field] = normalized;
      updateSet[field] = normalized;
    };

    textFields.forEach(assignNullable);

    if (user.lastSignedIn !== undefined) {
      values.lastSignedIn = user.lastSignedIn;
      updateSet.lastSignedIn = user.lastSignedIn;
    }
    if (user.role !== undefined) {
      values.role = user.role;
      updateSet.role = user.role;
    } else if (user.openId === ENV.ownerOpenId) {
      values.role = 'admin';
      updateSet.role = 'admin';
    }

    if (!values.lastSignedIn) {
      values.lastSignedIn = new Date();
    }

    if (Object.keys(updateSet).length === 0) {
      updateSet.lastSignedIn = new Date();
    }

    await db.insert(users).values(values).onDuplicateKeyUpdate({
      set: updateSet,
    });
  } catch (error) {
    log.error(
      "Failed to upsert user",
      {},
      error instanceof Error ? error : new Error(String(error)),
    );
    throw error;
  }
}

export async function getUserByOpenId(openId: string) {
  const db = await getDb();
  if (!db) {
    log.warn("Cannot get user: database not available");
    return undefined;
  }

  const result = await db.select().from(users).where(eq(users.openId, openId)).limit(1);

  return result.length > 0 ? result[0] : undefined;
}

// ── Waitlist ──────────────────────────────────────────────────

export async function addToWaitlist(entry: InsertWaitlistEntry): Promise<{ success: boolean; duplicate: boolean }> {
  const db = await getDb();
  if (!db) {
    throw new Error("Database not available");
  }

  try {
    await db.insert(waitlist).values(entry);
    return { success: true, duplicate: false };
  } catch (error: any) {
    // MySQL duplicate key error code
    if (error?.code === "ER_DUP_ENTRY" || error?.errno === 1062) {
      return { success: false, duplicate: true };
    }
    throw error;
  }
}

export async function getWaitlistCount(): Promise<number> {
  const db = await getDb();
  if (!db) return 0;

  const result = await db.select({ count: sql<number>`count(*)` }).from(waitlist);
  return result[0]?.count ?? 0;
}

export async function getWaitlistEntries(opts?: { situation?: string; from?: Date; to?: Date }) {
  const db = await getDb();
  if (!db) return [];

  return db.select().from(waitlist).orderBy(desc(waitlist.createdAt));
}

// ── Contact Messages ─────────────────────────────────────────

export async function addContactMessage(msg: InsertContactMessage): Promise<{ success: boolean }> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  await db.insert(contactMessages).values(msg);
  return { success: true };
}

export async function getContactMessages(opts?: { status?: string }) {
  const db = await getDb();
  if (!db) return [];

  if (opts?.status) {
    return db.select().from(contactMessages)
      .where(eq(contactMessages.status, opts.status as any))
      .orderBy(desc(contactMessages.createdAt));
  }
  return db.select().from(contactMessages).orderBy(desc(contactMessages.createdAt));
}

export async function getContactMessageCount(): Promise<number> {
  const db = await getDb();
  if (!db) return 0;

  const result = await db.select({ count: sql<number>`count(*)` }).from(contactMessages);
  return result[0]?.count ?? 0;
}

export async function updateContactMessageStatus(id: number, status: "new" | "read" | "replied" | "archived"): Promise<void> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  await db.update(contactMessages).set({ status }).where(eq(contactMessages.id, id));
}

// ── Waitlist Stats ───────────────────────────────────────────

export async function getWaitlistStats() {
  const db = await getDb();
  if (!db) return { total: 0, daily: [] as { date: string; count: number }[] };

  const total = await getWaitlistCount();
  const daily = await db.select({
    date: sql<string>`DATE(createdAt)`,
    count: sql<number>`count(*)`,
  }).from(waitlist).groupBy(sql`DATE(createdAt)`).orderBy(sql`DATE(createdAt)`);

  return { total, daily };
}

// ── Blog Posts ─────────────────────────────────────────────────────

export async function createBlogPost(post: InsertBlogPost): Promise<{ id: number }> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  const result = await db.insert(blogPosts).values(post);
  return { id: Number(result[0].insertId) };
}

export async function updateBlogPost(id: number, data: Partial<InsertBlogPost>): Promise<void> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  await db.update(blogPosts).set(data).where(eq(blogPosts.id, id));
}

export async function deleteBlogPost(id: number): Promise<void> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  await db.delete(blogPosts).where(eq(blogPosts.id, id));
}

export async function getBlogPostBySlug(slug: string) {
  const db = await getDb();
  if (!db) return undefined;

  const result = await db.select().from(blogPosts).where(eq(blogPosts.slug, slug)).limit(1);
  return result.length > 0 ? result[0] : undefined;
}

export async function getBlogPostById(id: number) {
  const db = await getDb();
  if (!db) return undefined;

  const result = await db.select().from(blogPosts).where(eq(blogPosts.id, id)).limit(1);
  return result.length > 0 ? result[0] : undefined;
}

export async function getPublishedBlogPosts(opts?: { category?: string; limit?: number }) {
  const db = await getDb();
  if (!db) return [];

  let query = db.select().from(blogPosts)
    .where(eq(blogPosts.published, true))
    .orderBy(desc(blogPosts.publishedAt));

  if (opts?.limit) {
    query = query.limit(opts.limit) as any;
  }

  return query;
}

export async function getAllBlogPosts() {
  const db = await getDb();
  if (!db) return [];

  return db.select().from(blogPosts).orderBy(desc(blogPosts.createdAt));
}

export async function getBlogPostCount(): Promise<number> {
  const db = await getDb();
  if (!db) return 0;

  const result = await db.select({ count: sql<number>`count(*)` }).from(blogPosts);
  return result[0]?.count ?? 0;
}

// â”€â”€ FAB Bookkeeping Operations â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

export async function createBookkeepingDocument(document: InsertBookkeepingDocument): Promise<{ id: number }> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  if (document.sourceDocumentId) {
    await db
      .insert(bookkeepingDocuments)
      .values(document)
      .onDuplicateKeyUpdate({
        set: { sourceDocumentId: document.sourceDocumentId },
      });

    const registered = await db
      .select({ id: bookkeepingDocuments.id })
      .from(bookkeepingDocuments)
      .where(
        and(
          eq(bookkeepingDocuments.source, document.source),
          eq(bookkeepingDocuments.sourceDocumentId, document.sourceDocumentId)
        )
      )
      .limit(1);

    if (!registered[0]) throw new Error("Bookkeeping document registration failed");
    return { id: registered[0].id };
  }

  const result = await db.insert(bookkeepingDocuments).values(document);
  return { id: Number(result[0].insertId) };
}

export async function createBookkeepingDocumentWithAudit(
  document: InsertBookkeepingDocument,
  event: AuditEventTemplate,
): Promise<{ id: number }> {
  return withAuditTransaction(async (tx) => {
    let id: number;
    if (document.sourceDocumentId) {
      await tx
        .insert(bookkeepingDocuments)
        .values(document)
        .onDuplicateKeyUpdate({ set: { sourceDocumentId: document.sourceDocumentId } });
      const registered = await tx
        .select({ id: bookkeepingDocuments.id })
        .from(bookkeepingDocuments)
        .where(and(
          eq(bookkeepingDocuments.source, document.source),
          eq(bookkeepingDocuments.sourceDocumentId, document.sourceDocumentId),
        ))
        .limit(1);
      if (!registered[0]) throw new Error("Bookkeeping document registration failed");
      id = registered[0].id;
    } else {
      const inserted = await tx.insert(bookkeepingDocuments).values(document);
      id = Number(inserted[0].insertId);
    }
    return {
      result: { id },
      audit: auditEventForEntity(event, id),
    };
  });
}

export async function updateBookkeepingDocument(
  id: number,
  document: Partial<InsertBookkeepingDocument>
): Promise<boolean> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  return db.transaction(async (tx) => {
    const [result] = await tx
      .update(bookkeepingDocuments)
      .set(document)
      .where(eq(bookkeepingDocuments.id, id));
    if (result.affectedRows > 0) return true;

    const existing = await tx
      .select({ id: bookkeepingDocuments.id })
      .from(bookkeepingDocuments)
      .where(eq(bookkeepingDocuments.id, id))
      .limit(1)
      .for("update");
    return Boolean(existing[0]);
  });
}

export async function updateBookkeepingDocumentWithAudit(
  id: number,
  document: Partial<InsertBookkeepingDocument>,
  event: AuditEventTemplate,
): Promise<boolean> {
  return withAuditTransaction(async (tx) => {
    const [result] = await tx
      .update(bookkeepingDocuments)
      .set(document)
      .where(eq(bookkeepingDocuments.id, id));
    if (result.affectedRows > 0) {
      return { result: true, audit: auditEventForEntity(event, id) };
    }

    const existing = await tx
      .select({ id: bookkeepingDocuments.id })
      .from(bookkeepingDocuments)
      .where(eq(bookkeepingDocuments.id, id))
      .limit(1)
      .for("update");
    return {
      result: Boolean(existing[0]),
      audit: null,
    };
  });
}

export async function addReviewItem(item: InsertReviewItem): Promise<{ id: number }> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  const result = await db.insert(reviewItems).values(item);
  return { id: Number(result[0].insertId) };
}

export async function addReviewItemWithAudit(
  item: InsertReviewItem,
  event: AuditEventTemplate,
): Promise<{ id: number }> {
  return withAuditTransaction(async (tx) => {
    const inserted = await tx.insert(reviewItems).values(item);
    const id = Number(inserted[0].insertId);
    return {
      result: { id },
      audit: auditEventForEntity(event, id),
    };
  });
}

export async function createWorkflowRun(run: InsertWorkflowRun): Promise<{ id: number }> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  const result = await db.insert(workflowRuns).values(run);
  return { id: Number(result[0].insertId) };
}

export async function createWorkflowRunWithAudit(
  run: InsertWorkflowRun,
  event: AuditEventTemplate,
): Promise<{ id: number }> {
  return withAuditTransaction(async (tx) => {
    const inserted = await tx.insert(workflowRuns).values(run);
    const id = Number(inserted[0].insertId);
    return {
      result: { id },
      audit: auditEventForEntity(event, id),
    };
  });
}

export async function updateWorkflowRun(
  id: number,
  data: Partial<InsertWorkflowRun>
): Promise<boolean> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  return db.transaction(async (tx) => {
    const [result] = await tx
      .update(workflowRuns)
      .set(data)
      .where(eq(workflowRuns.id, id));
    if (result.affectedRows > 0) return true;

    const existing = await tx
      .select({ id: workflowRuns.id })
      .from(workflowRuns)
      .where(eq(workflowRuns.id, id))
      .limit(1)
      .for("update");
    return Boolean(existing[0]);
  });
}

export async function updateWorkflowRunWithAudit(
  id: number,
  data: Partial<InsertWorkflowRun>,
  event: AuditEventTemplate,
  options: { expectedMetadata?: Record<string, unknown> | null } = {},
): Promise<boolean> {
  return withAuditTransaction(async (tx) => {
    const where = Object.prototype.hasOwnProperty.call(options, "expectedMetadata")
      ? and(
          eq(workflowRuns.id, id),
          options.expectedMetadata === null
            ? isNull(workflowRuns.metadata)
            : sql`JSON_CONTAINS(${workflowRuns.metadata}, CAST(${JSON.stringify(options.expectedMetadata)} AS JSON))
                AND JSON_CONTAINS(CAST(${JSON.stringify(options.expectedMetadata)} AS JSON), ${workflowRuns.metadata})`,
        )
      : eq(workflowRuns.id, id);
    const [result] = await tx
      .update(workflowRuns)
      .set(data)
      .where(where);
    if (result.affectedRows > 0) {
      return { result: true, audit: { ...event, entityId: String(id) } };
    }

    const existing = await tx
      .select({ id: workflowRuns.id })
      .from(workflowRuns)
      .where(where)
      .limit(1)
      .for("update");
    return {
      result: Boolean(existing[0]),
      audit: null,
    };
  });
}

export async function createRoutingAttempt(attempt: InsertRoutingAttempt): Promise<{ id: number }> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  const result = await db.insert(routingAttempts).values(attempt);
  return { id: Number(result[0].insertId) };
}

export async function createRoutingAttemptWithAudit(
  attempt: InsertRoutingAttempt,
  event: Omit<InsertAuditEvent, "entityId">,
): Promise<{ id: number }> {
  return withAuditTransaction(async (tx) => {
    const inserted = await tx.insert(routingAttempts).values(attempt);
    const id = Number(inserted[0].insertId);
    return {
      result: { id },
      audit: auditEventForEntity(event, id),
    };
  });
}

export async function createReconciliationMatch(match: InsertReconciliationMatch): Promise<{ id: number }> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  const result = await db.insert(reconciliationMatches).values(match);
  return { id: Number(result[0].insertId) };
}

export async function createReconciliationMatchWithAudit(
  match: InsertReconciliationMatch,
  event: AuditEventTemplate,
): Promise<{ id: number }> {
  return withAuditTransaction(async (tx) => {
    const inserted = await tx.insert(reconciliationMatches).values(match);
    const id = Number(inserted[0].insertId);
    return {
      result: { id },
      audit: auditEventForEntity(event, id),
    };
  });
}

export async function getRecentReconciliationMatches(limit = 10) {
  const db = await getDb();
  if (!db) return [];

  const rows = await db
    .select({
      match: reconciliationMatches,
      document: bookkeepingDocuments,
    })
    .from(reconciliationMatches)
    .leftJoin(bookkeepingDocuments, eq(reconciliationMatches.documentId, bookkeepingDocuments.id))
    .orderBy(desc(reconciliationMatches.createdAt))
    .limit(limit);

  return rows.map((row) => ({
    ...row.match,
    document: row.document,
  }));
}

export async function recordAuditEvent(event: InsertAuditEvent): Promise<{ id: number | null }> {
  const db = await getDb();
  if (!db) return { id: null };

  const result = await db.insert(auditEvents).values(event);
  return { id: Number(result[0].insertId) };
}

export async function getRecentAuditEvents(limit = 20) {
  const db = await getDb();
  if (!db) return [];

  return db.select().from(auditEvents).orderBy(desc(auditEvents.createdAt)).limit(limit);
}

export async function getBookkeepingOverview() {
  const db = await getDb();
  if (!db) {
    return {
      documents: 0,
      needsReview: 0,
      routed: 0,
      failed: 0,
      pendingReviews: 0,
      activeWorkflowRuns: 0,
    };
  }

  const [documents] = await db.select({ count: sql<number>`count(*)` }).from(bookkeepingDocuments);
  const [needsReview] = await db
    .select({ count: sql<number>`count(*)` })
    .from(bookkeepingDocuments)
    .where(eq(bookkeepingDocuments.processingStatus, "needs_review"));
  const [routed] = await db
    .select({ count: sql<number>`count(*)` })
    .from(bookkeepingDocuments)
    .where(eq(bookkeepingDocuments.processingStatus, "routed"));
  const [failed] = await db
    .select({ count: sql<number>`count(*)` })
    .from(bookkeepingDocuments)
    .where(eq(bookkeepingDocuments.processingStatus, "failed"));
  const [pendingReviews] = await db
    .select({ count: sql<number>`count(*)` })
    .from(reviewItems)
    .where(eq(reviewItems.status, "pending"));
  const [activeWorkflowRuns] = await db
    .select({ count: sql<number>`count(*)` })
    .from(workflowRuns)
    .where(sql`${workflowRuns.status} in ('queued', 'running')`);

  return {
    documents: documents?.count ?? 0,
    needsReview: needsReview?.count ?? 0,
    routed: routed?.count ?? 0,
    failed: failed?.count ?? 0,
    pendingReviews: pendingReviews?.count ?? 0,
    activeWorkflowRuns: activeWorkflowRuns?.count ?? 0,
  };
}

export async function getRecentWorkflowRuns(limit = 10) {
  const db = await getDb();
  if (!db) return [];

  return db.select().from(workflowRuns).orderBy(desc(workflowRuns.createdAt)).limit(limit);
}

export async function getWorkflowRunById(id: number) {
  const db = await getDb();
  if (!db) return null;

  const rows = await db.select().from(workflowRuns).where(eq(workflowRuns.id, id)).limit(1);
  return rows[0] ?? null;
}

export async function getReviewQueue(opts?: { status?: "pending" | "in_review" | "approved" | "rejected" | "resolved" }) {
  const db = await getDb();
  if (!db) return [];

  const baseQuery = db
    .select({
      review: reviewItems,
      document: bookkeepingDocuments,
    })
    .from(reviewItems)
    .leftJoin(bookkeepingDocuments, eq(reviewItems.documentId, bookkeepingDocuments.id));

  const rows = opts?.status
    ? await baseQuery.where(eq(reviewItems.status, opts.status)).orderBy(desc(reviewItems.createdAt))
    : await baseQuery.orderBy(desc(reviewItems.createdAt));

  return rows.map((row) => ({
    ...row.review,
    document: row.document,
  }));
}

export async function updateReviewItemStatus(
  id: number,
  status: "pending" | "in_review" | "approved" | "rejected" | "resolved",
  resolution?: string,
  actorUserId?: number
): Promise<boolean> {
  const db = await getDb();
  if (!db) throw new Error("Database not available");

  const resolvedAt = ["approved", "rejected", "resolved"].includes(status) ? new Date() : null;
  return db.transaction(async (tx) => {
    const existing = await tx
      .select({ id: reviewItems.id })
      .from(reviewItems)
      .where(eq(reviewItems.id, id))
      .limit(1)
      .for("update");
    if (!existing[0]) return false;

    await tx
      .update(reviewItems)
      .set({ status, resolution: resolution || null, resolvedAt })
      .where(eq(reviewItems.id, id));

    await tx.insert(auditEvents).values({
      actorUserId: actorUserId || null,
      action: "review_item.update_status",
      entityType: "review_item",
      entityId: String(id),
      details: { status, resolution: resolution || null },
    });
    return true;
  });
}
