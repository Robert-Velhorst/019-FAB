import {
  boolean,
  date,
  decimal,
  index,
  int,
  json,
  mysqlEnum,
  mysqlTable,
  text,
  timestamp,
  uniqueIndex,
  varchar,
} from "drizzle-orm/mysql-core";

/**
 * Core user table backing auth flow.
 * Indexes on email and stripeCustomerId for lookup performance.
 */
export const users = mysqlTable(
  "users",
  {
    id: int("id").autoincrement().primaryKey(),
    openId: varchar("openId", { length: 64 }).notNull().unique(),
    name: text("name"),
    email: varchar("email", { length: 320 }),
    loginMethod: varchar("loginMethod", { length: 64 }),
    role: mysqlEnum("role", ["user", "admin"]).default("user").notNull(),
    createdAt: timestamp("createdAt").defaultNow().notNull(),
    updatedAt: timestamp("updatedAt").defaultNow().onUpdateNow().notNull(),
    lastSignedIn: timestamp("lastSignedIn").defaultNow().notNull(),
    stripeCustomerId: varchar("stripeCustomerId", { length: 255 }),
  },
  (table) => [
    index("idx_users_email").on(table.email),
    index("idx_users_stripe_customer").on(table.stripeCustomerId),
    index("idx_users_role").on(table.role),
  ]
);

export type User = typeof users.$inferSelect;
export type InsertUser = typeof users.$inferInsert;

export const oauthSessions = mysqlTable(
  "oauth_sessions",
  {
    tokenHash: varchar("tokenHash", { length: 64 }).primaryKey(),
    openId: varchar("openId", { length: 64 }).notNull(),
    expiresAt: timestamp("expiresAt").notNull(),
    revokedAt: timestamp("revokedAt"),
    createdAt: timestamp("createdAt").defaultNow().notNull(),
  },
  (table) => [index("idx_oauth_sessions_expiry").on(table.expiresAt)]
);

/**
 * Waitlist signups.
 * Unique constraint on email prevents duplicates.
 * Index on createdAt for admin listing sorted by date.
 */
export const waitlist = mysqlTable(
  "waitlist",
  {
    id: int("id").autoincrement().primaryKey(),
    email: varchar("email", { length: 320 }).notNull().unique(),
    firstName: varchar("firstName", { length: 100 }),
    lastName: varchar("lastName", { length: 100 }),
    source: varchar("source", { length: 50 }).default("website"),
    createdAt: timestamp("createdAt").defaultNow().notNull(),
  },
  (table) => [
    index("idx_waitlist_created").on(table.createdAt),
  ]
);

export type WaitlistEntry = typeof waitlist.$inferSelect;
export type InsertWaitlistEntry = typeof waitlist.$inferInsert;

/**
 * Contact form messages.
 * Indexes on status (for admin filtering) and createdAt (for sorting).
 */
export const contactMessages = mysqlTable(
  "contact_messages",
  {
    id: int("id").autoincrement().primaryKey(),
    firstName: varchar("firstName", { length: 100 }).notNull(),
    lastName: varchar("lastName", { length: 100 }).notNull(),
    email: varchar("email", { length: 320 }).notNull(),
    subject: varchar("subject", { length: 100 }).notNull(),
    message: text("message").notNull(),
    status: mysqlEnum("status", ["new", "read", "replied", "archived"])
      .default("new")
      .notNull(),
    createdAt: timestamp("createdAt").defaultNow().notNull(),
  },
  (table) => [
    index("idx_contact_status").on(table.status),
    index("idx_contact_created").on(table.createdAt),
    index("idx_contact_email").on(table.email),
  ]
);

export type ContactMessage = typeof contactMessages.$inferSelect;
export type InsertContactMessage = typeof contactMessages.$inferInsert;

/**
 * Blog posts.
 * Unique constraint on slug for URL routing.
 * Composite index on (published, publishedAt) for efficient public listing.
 * Index on category for filtered queries.
 */
export const blogPosts = mysqlTable(
  "blog_posts",
  {
    id: int("id").autoincrement().primaryKey(),
    title: varchar("title", { length: 255 }).notNull(),
    titleNl: varchar("titleNl", { length: 255 }),
    slug: varchar("slug", { length: 255 }).notNull().unique(),
    excerpt: text("excerpt").notNull(),
    excerptNl: text("excerptNl"),
    content: text("content").notNull(),
    contentNl: text("contentNl"),
    category: varchar("category", { length: 50 }).default("update").notNull(),
    coverImage: varchar("coverImage", { length: 500 }),
    published: boolean("published").default(false).notNull(),
    authorId: int("authorId"),
    readTimeMinutes: int("readTimeMinutes").default(3),
    createdAt: timestamp("createdAt").defaultNow().notNull(),
    updatedAt: timestamp("updatedAt").defaultNow().onUpdateNow().notNull(),
    publishedAt: timestamp("publishedAt"),
  },
  (table) => [
    index("idx_blog_published_date").on(table.published, table.publishedAt),
    index("idx_blog_category").on(table.category),
    index("idx_blog_author").on(table.authorId),
  ]
);

export type BlogPost = typeof blogPosts.$inferSelect;
export type InsertBlogPost = typeof blogPosts.$inferInsert;

export const documentTypeEnum = mysqlEnum("documentType", [
  "receipt",
  "invoice",
  "order_confirmation",
  "bank_transaction",
  "statement",
  "unknown",
]);

export const processingStatusEnum = mysqlEnum("processingStatus", [
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

export const reviewStatusEnum = mysqlEnum("status", [
  "pending",
  "in_review",
  "approved",
  "rejected",
  "resolved",
]);

export const workflowStatusEnum = mysqlEnum("status", [
  "queued",
  "running",
  "completed",
  "completed_with_review",
  "failed",
  "cancelled",
]);

export const routeTargetEnum = mysqlEnum("routeTarget", [
  "mijngeldzaken",
  "waveapps_business",
  "waveapps_personal",
  "manual_review",
  "none",
]);

const routingTargetEnum = mysqlEnum("target", [
  "mijngeldzaken",
  "waveapps_business",
  "waveapps_personal",
  "manual_review",
  "none",
]);

export const routeStatusEnum = mysqlEnum("status", [
  "pending",
  "submitted",
  "success",
  "failed",
  "skipped",
  "requires_review",
]);

/**
 * Financial documents imported into FAB from Drive, Gmail, Photos, Freshdesk,
 * mobile capture, or later integrations.
 */
export const bookkeepingDocuments = mysqlTable(
  "bookkeeping_documents",
  {
    id: int("id").autoincrement().primaryKey(),
    source: varchar("source", { length: 64 }).notNull(),
    sourceDocumentId: varchar("sourceDocumentId", { length: 255 }),
    originalFilename: varchar("originalFilename", { length: 500 }).notNull(),
    mimeType: varchar("mimeType", { length: 120 }),
    storagePath: varchar("storagePath", { length: 1000 }),
    documentType: documentTypeEnum.default("unknown").notNull(),
    processingStatus: processingStatusEnum.default("imported").notNull(),
    duplicateFingerprint: varchar("duplicateFingerprint", { length: 128 }),
    duplicateOfDocumentId: int("duplicateOfDocumentId"),
    vendorName: varchar("vendorName", { length: 255 }),
    category: varchar("category", { length: 255 }),
    transactionDate: date("transactionDate", { mode: "string" }),
    totalAmount: decimal("totalAmount", { precision: 12, scale: 2 }),
    vatAmount: decimal("vatAmount", { precision: 12, scale: 2 }),
    confidenceScore: decimal("confidenceScore", { precision: 5, scale: 4 }),
    ocrText: text("ocrText"),
    extractedData: json("extractedData").$type<Record<string, unknown>>(),
    metadata: json("metadata").$type<Record<string, unknown>>(),
    createdAt: timestamp("createdAt").defaultNow().notNull(),
    updatedAt: timestamp("updatedAt").defaultNow().onUpdateNow().notNull(),
  },
  (table) => [
    index("idx_bookkeeping_docs_status").on(table.processingStatus),
    uniqueIndex("uq_bookkeeping_docs_source_document").on(table.source, table.sourceDocumentId),
    index("idx_bookkeeping_docs_vendor").on(table.vendorName),
    index("idx_bookkeeping_docs_category").on(table.category),
    index("idx_bookkeeping_docs_date").on(table.transactionDate),
    index("idx_bookkeeping_docs_fingerprint").on(table.duplicateFingerprint),
  ]
);

export type BookkeepingDocument = typeof bookkeepingDocuments.$inferSelect;
export type InsertBookkeepingDocument = typeof bookkeepingDocuments.$inferInsert;

export const vendors = mysqlTable(
  "vendors",
  {
    id: int("id").autoincrement().primaryKey(),
    name: varchar("name", { length: 255 }).notNull().unique(),
    normalizedName: varchar("normalizedName", { length: 255 }).notNull(),
    defaultCategory: varchar("defaultCategory", { length: 255 }),
    aliases: json("aliases").$type<string[]>(),
    metadata: json("metadata").$type<Record<string, unknown>>(),
    createdAt: timestamp("createdAt").defaultNow().notNull(),
    updatedAt: timestamp("updatedAt").defaultNow().onUpdateNow().notNull(),
  },
  (table) => [
    index("idx_vendors_normalized").on(table.normalizedName),
    index("idx_vendors_category").on(table.defaultCategory),
  ]
);

export type Vendor = typeof vendors.$inferSelect;
export type InsertVendor = typeof vendors.$inferInsert;

export const categories = mysqlTable(
  "categories",
  {
    id: int("id").autoincrement().primaryKey(),
    name: varchar("name", { length: 255 }).notNull(),
    parentId: int("parentId"),
    routeTarget: routeTargetEnum.default("manual_review").notNull(),
    rules: json("rules").$type<Record<string, unknown>>(),
    createdAt: timestamp("createdAt").defaultNow().notNull(),
    updatedAt: timestamp("updatedAt").defaultNow().onUpdateNow().notNull(),
  },
  (table) => [
    index("idx_categories_parent").on(table.parentId),
    index("idx_categories_route").on(table.routeTarget),
  ]
);

export type Category = typeof categories.$inferSelect;
export type InsertCategory = typeof categories.$inferInsert;

export const reviewItems = mysqlTable(
  "review_items",
  {
    id: int("id").autoincrement().primaryKey(),
    documentId: int("documentId"),
    reason: varchar("reason", { length: 120 }).notNull(),
    details: text("details"),
    status: reviewStatusEnum.default("pending").notNull(),
    assignedToUserId: int("assignedToUserId"),
    resolution: text("resolution"),
    correctedData: json("correctedData").$type<Record<string, unknown>>(),
    createdAt: timestamp("createdAt").defaultNow().notNull(),
    updatedAt: timestamp("updatedAt").defaultNow().onUpdateNow().notNull(),
    resolvedAt: timestamp("resolvedAt"),
  },
  (table) => [
    index("idx_review_status").on(table.status),
    index("idx_review_document").on(table.documentId),
    index("idx_review_assignee").on(table.assignedToUserId),
    index("idx_review_created").on(table.createdAt),
  ]
);

export type ReviewItem = typeof reviewItems.$inferSelect;
export type InsertReviewItem = typeof reviewItems.$inferInsert;

export const workflowRuns = mysqlTable(
  "workflow_runs",
  {
    id: int("id").autoincrement().primaryKey(),
    status: workflowStatusEnum.default("queued").notNull(),
    triggerSource: varchar("triggerSource", { length: 100 }).default("manual").notNull(),
    documentsImported: int("documentsImported").default(0).notNull(),
    documentsProcessed: int("documentsProcessed").default(0).notNull(),
    documentsNeedingReview: int("documentsNeedingReview").default(0).notNull(),
    errorMessage: text("errorMessage"),
    startedAt: timestamp("startedAt"),
    finishedAt: timestamp("finishedAt"),
    createdAt: timestamp("createdAt").defaultNow().notNull(),
    metadata: json("metadata").$type<Record<string, unknown>>(),
  },
  (table) => [
    index("idx_workflow_status").on(table.status),
    index("idx_workflow_created").on(table.createdAt),
  ]
);

export type WorkflowRun = typeof workflowRuns.$inferSelect;
export type InsertWorkflowRun = typeof workflowRuns.$inferInsert;

export const routingAttempts = mysqlTable(
  "routing_attempts",
  {
    id: int("id").autoincrement().primaryKey(),
    documentId: int("documentId"),
    bookkeepingRecordId: int("bookkeepingRecordId"),
    workflowRunId: int("workflowRunId"),
    target: routingTargetEnum.notNull(),
    status: routeStatusEnum.default("pending").notNull(),
    externalId: varchar("externalId", { length: 255 }),
    message: text("message"),
    attemptedAt: timestamp("attemptedAt").defaultNow().notNull(),
    metadata: json("metadata").$type<Record<string, unknown>>(),
  },
  (table) => [
    index("idx_routing_document").on(table.documentId),
    index("idx_routing_bookkeeping_record").on(table.bookkeepingRecordId),
    index("idx_routing_workflow").on(table.workflowRunId),
    index("idx_routing_target_status").on(table.target, table.status),
  ]
);

export type RoutingAttempt = typeof routingAttempts.$inferSelect;
export type InsertRoutingAttempt = typeof routingAttempts.$inferInsert;

export const reconciliationMatches = mysqlTable(
  "reconciliation_matches",
  {
    id: int("id").autoincrement().primaryKey(),
    documentId: int("documentId"),
    bankTransactionId: varchar("bankTransactionId", { length: 255 }).notNull(),
    status: mysqlEnum("status", ["matched", "unmatched", "partial", "review"]).default("review").notNull(),
    confidenceScore: decimal("confidenceScore", { precision: 5, scale: 4 }),
    amountDifference: decimal("amountDifference", { precision: 12, scale: 2 }),
    matchedAt: timestamp("matchedAt"),
    metadata: json("metadata").$type<Record<string, unknown>>(),
    createdAt: timestamp("createdAt").defaultNow().notNull(),
  },
  (table) => [
    index("idx_reconciliation_document").on(table.documentId),
    index("idx_reconciliation_status").on(table.status),
    index("idx_reconciliation_bank_tx").on(table.bankTransactionId),
  ]
);

export type ReconciliationMatch = typeof reconciliationMatches.$inferSelect;
export type InsertReconciliationMatch = typeof reconciliationMatches.$inferInsert;

export const auditEvents = mysqlTable(
  "audit_events",
  {
    id: int("id").autoincrement().primaryKey(),
    actorUserId: int("actorUserId"),
    action: varchar("action", { length: 120 }).notNull(),
    entityType: varchar("entityType", { length: 80 }).notNull(),
    entityId: varchar("entityId", { length: 120 }),
    details: json("details").$type<Record<string, unknown>>(),
    createdAt: timestamp("createdAt").defaultNow().notNull(),
  },
  (table) => [
    index("idx_audit_actor").on(table.actorUserId),
    index("idx_audit_entity").on(table.entityType, table.entityId),
    index("idx_audit_created").on(table.createdAt),
  ]
);

export type AuditEvent = typeof auditEvents.$inferSelect;
export type InsertAuditEvent = typeof auditEvents.$inferInsert;
