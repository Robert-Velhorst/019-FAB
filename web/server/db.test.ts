import { afterEach, describe, expect, it, vi } from "vitest";
import { MySqlDialect } from "drizzle-orm/mysql-core";

const { drizzleMock } = vi.hoisted(() => ({ drizzleMock: vi.fn() }));

vi.mock("drizzle-orm/mysql2", () => ({ drizzle: drizzleMock }));

describe("createBookkeepingDocument", () => {
  const originalDatabaseUrl = process.env.DATABASE_URL;

  afterEach(() => {
    if (originalDatabaseUrl === undefined) delete process.env.DATABASE_URL;
    else process.env.DATABASE_URL = originalDatabaseUrl;
    drizzleMock.mockReset();
  });

  it("does not overwrite reviewed data when a source document is retried", async () => {
    vi.resetModules();
    const { createBookkeepingDocument } = await import("./db");
    process.env.DATABASE_URL = "mysql://fab-test.invalid/fab";
    const onDuplicateKeyUpdate = vi.fn().mockResolvedValue(undefined);
    const duplicateSelect = vi.fn().mockResolvedValue([{ id: 17 }]);
    const database = {
      insert: vi.fn().mockReturnValue({
        values: vi.fn().mockReturnValue({ onDuplicateKeyUpdate }),
      }),
      select: vi.fn().mockReturnValue({
        from: vi.fn().mockReturnValue({
          where: vi.fn().mockReturnValue({ limit: duplicateSelect }),
        }),
      }),
    };
    drizzleMock.mockReturnValue(database);

    const result = await createBookkeepingDocument({
      source: "gmail",
      sourceDocumentId: "gmail-message-17",
      originalFilename: "receipt.pdf",
      documentType: "unknown",
      processingStatus: "imported",
    });

    expect(result).toEqual({ id: 17 });
    expect(onDuplicateKeyUpdate).toHaveBeenCalledWith({
      set: { sourceDocumentId: "gmail-message-17" },
    });
    expect(duplicateSelect).toHaveBeenCalledOnce();
  });

  it("commits a source-document registration and its audit event in one transaction", async () => {
    vi.resetModules();
    const { createBookkeepingDocumentWithAudit } = await import("./db");
    process.env.DATABASE_URL = "mysql://fab-test.invalid/fab";
    const onDuplicateKeyUpdate = vi.fn().mockResolvedValue(undefined);
    const documentValues = vi.fn().mockReturnValue({ onDuplicateKeyUpdate });
    const auditValues = vi.fn().mockResolvedValue(undefined);
    const selectLimit = vi.fn().mockResolvedValue([{ id: 17 }]);
    const transaction = vi.fn(async (operation: (tx: any) => Promise<unknown>) => operation({
      insert: vi.fn()
        .mockReturnValueOnce({ values: documentValues })
        .mockReturnValueOnce({ values: auditValues }),
      select: vi.fn().mockReturnValue({
        from: vi.fn().mockReturnValue({
          where: vi.fn().mockReturnValue({ limit: selectLimit }),
        }),
      }),
    }));
    drizzleMock.mockReturnValue({ transaction });

    const result = await createBookkeepingDocumentWithAudit({
      source: "gmail",
      sourceDocumentId: "gmail-message-17",
      originalFilename: "receipt.pdf",
      documentType: "unknown",
      processingStatus: "imported",
    }, {
      actorUserId: null,
      action: "service.document.register",
      entityType: "bookkeeping_document",
      details: { source: "gmail" },
    });

    expect(result).toEqual({ id: 17 });
    expect(transaction).toHaveBeenCalledOnce();
    expect(onDuplicateKeyUpdate).toHaveBeenCalledWith({ set: { sourceDocumentId: "gmail-message-17" } });
    expect(auditValues).toHaveBeenCalledWith({
      actorUserId: null,
      action: "service.document.register",
      entityType: "bookkeeping_document",
      entityId: "17",
      details: { source: "gmail" },
    });
  });
});

describe("database pool lifecycle", () => {
  const originalDatabaseUrl = process.env.DATABASE_URL;

  afterEach(() => {
    if (originalDatabaseUrl === undefined) delete process.env.DATABASE_URL;
    else process.env.DATABASE_URL = originalDatabaseUrl;
    drizzleMock.mockReset();
  });

  it("creates a bounded connection pool only when the database is configured", async () => {
    vi.resetModules();
    const { getDb } = await import("./db");
    delete process.env.DATABASE_URL;
    await expect(getDb()).resolves.toBeNull();

    process.env.DATABASE_URL = "mysql://fab-test.invalid/fab";
    const database = { $client: { end: vi.fn() } };
    drizzleMock.mockReturnValue(database);

    await expect(getDb()).resolves.toBe(database);
    expect(drizzleMock).toHaveBeenCalledWith({
      connection: {
        uri: "mysql://fab-test.invalid/fab",
        waitForConnections: true,
        connectionLimit: 10,
        maxIdle: 5,
        idleTimeout: 60_000,
        queueLimit: 50,
        connectTimeout: 10_000,
      },
    });
  });

  it("closes and clears the pool so the next access creates a fresh one", async () => {
    vi.resetModules();
    const { closeDb, getDb } = await import("./db");
    process.env.DATABASE_URL = "mysql://fab-test.invalid/fab";
    const end = vi.fn((callback: (error?: Error | null) => void) => callback(null));
    drizzleMock.mockReturnValue({ $client: { end } });

    await getDb();
    await closeDb();
    expect(end).toHaveBeenCalledOnce();
    await getDb();
    expect(drizzleMock).toHaveBeenCalledTimes(2);
  });
});

describe("audited bookkeeping mutations", () => {
  const originalDatabaseUrl = process.env.DATABASE_URL;

  afterEach(() => {
    if (originalDatabaseUrl === undefined) delete process.env.DATABASE_URL;
    else process.env.DATABASE_URL = originalDatabaseUrl;
    drizzleMock.mockReset();
  });

  it("writes the workflow update and audit event inside one transaction", async () => {
    vi.resetModules();
    const { updateWorkflowRunWithAudit } = await import("./db");
    process.env.DATABASE_URL = "mysql://fab-test.invalid/fab";
    const updateWhere = vi.fn().mockResolvedValue([{ affectedRows: 1 }]);
    const updateSet = vi.fn().mockReturnValue({ where: updateWhere });
    const auditValues = vi.fn().mockResolvedValue(undefined);
    const transaction = vi.fn(async (operation: (tx: any) => Promise<unknown>) => operation({
      select: vi.fn().mockReturnValue({
        from: vi.fn().mockReturnValue({
          where: vi.fn().mockReturnValue({
            limit: vi.fn().mockReturnValue({ for: vi.fn().mockResolvedValue([{ id: 42 }]) }),
          }),
        }),
      }),
      update: vi.fn().mockReturnValue({ set: updateSet }),
      insert: vi.fn().mockReturnValue({ values: auditValues }),
    }));
    drizzleMock.mockReturnValue({ transaction });

    await expect(updateWorkflowRunWithAudit(42, { status: "failed" }, {
      actorUserId: 7,
      action: "workflow_run.update",
      entityType: "workflow_run",
      details: { changedFields: ["status"] },
    })).resolves.toBe(true);

    expect(transaction).toHaveBeenCalledOnce();
    expect(updateSet).toHaveBeenCalledWith({ status: "failed" });
    expect(auditValues).toHaveBeenCalledWith({
      actorUserId: 7,
      action: "workflow_run.update",
      entityType: "workflow_run",
      entityId: "42",
      details: { changedFields: ["status"] },
    });
  });

  it("rolls back an audited mutation when the audit insert fails", async () => {
    vi.resetModules();
    const { updateWorkflowRunWithAudit } = await import("./db");
    process.env.DATABASE_URL = "mysql://fab-test.invalid/fab";
    const updateWhere = vi.fn().mockResolvedValue([{ affectedRows: 1 }]);
    const auditValues = vi.fn().mockRejectedValue(new Error("audit storage unavailable"));
    const transaction = vi.fn(async (operation: (tx: any) => Promise<unknown>) => operation({
      select: vi.fn().mockReturnValue({
        from: vi.fn().mockReturnValue({
          where: vi.fn().mockReturnValue({
            limit: vi.fn().mockReturnValue({ for: vi.fn().mockResolvedValue([{ id: 42 }]) }),
          }),
        }),
      }),
      update: vi.fn().mockReturnValue({ set: vi.fn().mockReturnValue({ where: updateWhere }) }),
      insert: vi.fn().mockReturnValue({ values: auditValues }),
    }));
    drizzleMock.mockReturnValue({ transaction });

    await expect(updateWorkflowRunWithAudit(42, { status: "failed" }, {
      actorUserId: 7,
      action: "workflow_run.update",
      entityType: "workflow_run",
    })).rejects.toThrow("audit storage unavailable");
    expect(transaction).toHaveBeenCalledOnce();
    expect(updateWhere).toHaveBeenCalledOnce();
  });

  it("does not create an audit event when the updated workflow run is missing", async () => {
    vi.resetModules();
    const { updateWorkflowRunWithAudit } = await import("./db");
    process.env.DATABASE_URL = "mysql://fab-test.invalid/fab";
    const auditValues = vi.fn();
    const updateWhere = vi.fn().mockResolvedValue([{ affectedRows: 0 }]);
    const transaction = vi.fn(async (operation: (tx: any) => Promise<unknown>) => operation({
      select: vi.fn().mockReturnValue({
        from: vi.fn().mockReturnValue({
          where: vi.fn().mockReturnValue({
            limit: vi.fn().mockReturnValue({ for: vi.fn().mockResolvedValue([]) }),
          }),
        }),
      }),
      update: vi.fn().mockReturnValue({
        set: vi.fn().mockReturnValue({ where: updateWhere }),
      }),
      insert: vi.fn().mockReturnValue({ values: auditValues }),
    }));
    drizzleMock.mockReturnValue({ transaction });

    await expect(updateWorkflowRunWithAudit(404, { status: "failed" }, {
      actorUserId: 7,
      action: "workflow_run.update",
      entityType: "workflow_run",
    })).resolves.toBe(false);

    expect(updateWhere).toHaveBeenCalledOnce();
    expect(auditValues).not.toHaveBeenCalled();
  });

  it("treats an unchanged existing workflow run as a successful no-op", async () => {
    vi.resetModules();
    const { updateWorkflowRunWithAudit } = await import("./db");
    process.env.DATABASE_URL = "mysql://fab-test.invalid/fab";
    const updateWhere = vi.fn().mockResolvedValue([{ affectedRows: 0 }]);
    const auditValues = vi.fn();
    const transaction = vi.fn(async (operation: (tx: any) => Promise<unknown>) => operation({
      select: vi.fn().mockReturnValue({
        from: vi.fn().mockReturnValue({
          where: vi.fn().mockReturnValue({
            limit: vi.fn().mockReturnValue({ for: vi.fn().mockResolvedValue([{ id: 42 }]) }),
          }),
        }),
      }),
      update: vi.fn().mockReturnValue({
        set: vi.fn().mockReturnValue({ where: updateWhere }),
      }),
      insert: vi.fn().mockReturnValue({ values: auditValues }),
    }));
    drizzleMock.mockReturnValue({ transaction });

    await expect(updateWorkflowRunWithAudit(42, { status: "running" }, {
      actorUserId: 7,
      action: "workflow_run.update",
      entityType: "workflow_run",
    })).resolves.toBe(true);

    expect(updateWhere).toHaveBeenCalledOnce();
    expect(auditValues).not.toHaveBeenCalled();
  });

  it("treats an unchanged existing bookkeeping document as a successful no-op", async () => {
    vi.resetModules();
    const { updateBookkeepingDocumentWithAudit } = await import("./db");
    process.env.DATABASE_URL = "mysql://fab-test.invalid/fab";
    const updateWhere = vi.fn().mockResolvedValue([{ affectedRows: 0 }]);
    const auditValues = vi.fn();
    const transaction = vi.fn(async (operation: (tx: any) => Promise<unknown>) => operation({
      select: vi.fn().mockReturnValue({
        from: vi.fn().mockReturnValue({
          where: vi.fn().mockReturnValue({
            limit: vi.fn().mockReturnValue({ for: vi.fn().mockResolvedValue([{ id: 17 }]) }),
          }),
        }),
      }),
      update: vi.fn().mockReturnValue({
        set: vi.fn().mockReturnValue({ where: updateWhere }),
      }),
      insert: vi.fn().mockReturnValue({ values: auditValues }),
    }));
    drizzleMock.mockReturnValue({ transaction });

    await expect(updateBookkeepingDocumentWithAudit(17, { processingStatus: "imported" }, {
      actorUserId: null,
      action: "service.document.update",
      entityType: "bookkeeping_document",
    })).resolves.toBe(true);

    expect(updateWhere).toHaveBeenCalledOnce();
    expect(auditValues).not.toHaveBeenCalled();
  });

  it("compares the prior workflow metadata before applying a read-modify-write update", async () => {
    vi.resetModules();
    const { updateWorkflowRunWithAudit } = await import("./db");
    process.env.DATABASE_URL = "mysql://fab-test.invalid/fab";
    const priorMetadata = { workflowId: "wave-import", operations: [{ operationId: "op-1", status: "pending" }] };
    let condition: unknown;
    const updateWhere = vi.fn().mockResolvedValue([{ affectedRows: 0 }]);
    const selectWhere = vi.fn().mockImplementation((where: unknown) => {
      condition = where;
      return { limit: vi.fn().mockReturnValue({ for: vi.fn().mockResolvedValue([]) }) };
    });
    const auditValues = vi.fn();
    const transaction = vi.fn(async (operation: (tx: any) => Promise<unknown>) => operation({
      select: vi.fn().mockReturnValue({ from: vi.fn().mockReturnValue({ where: selectWhere }) }),
      update: vi.fn().mockReturnValue({ set: vi.fn().mockReturnValue({ where: updateWhere }) }),
      insert: vi.fn().mockReturnValue({ values: auditValues }),
    }));
    drizzleMock.mockReturnValue({ transaction });

    await expect(updateWorkflowRunWithAudit(42, { status: "running" }, {
      actorUserId: 7,
      action: "automation_workflow.operation_claim",
      entityType: "automation_workflow_operation",
    }, { expectedMetadata: priorMetadata })).resolves.toBe(false);

    const query = new MySqlDialect().sqlToQuery(condition as Parameters<MySqlDialect["sqlToQuery"]>[0]);
    expect(query.sql).toContain("`workflow_runs`.`id` = ?");
    expect(query.sql).toContain("JSON_CONTAINS(`workflow_runs`.`metadata`, CAST(? AS JSON))");
    expect(query.sql).toContain("JSON_CONTAINS(CAST(? AS JSON), `workflow_runs`.`metadata`)");
    expect(query.params).toEqual([
      42,
      JSON.stringify(priorMetadata),
      JSON.stringify(priorMetadata),
    ]);
    expect(updateWhere).toHaveBeenCalledOnce();
    expect(auditValues).not.toHaveBeenCalled();
  });
});

describe("updateReviewItemStatus", () => {
  it("commits the status change and audit event in one transaction", async () => {
    vi.resetModules();
    const { updateReviewItemStatus } = await import("./db");
    process.env.DATABASE_URL = "mysql://fab-test.invalid/fab";
    const updateWhere = vi.fn().mockResolvedValue([{ affectedRows: 1 }]);
    const updateSet = vi.fn().mockReturnValue({ where: updateWhere });
    const auditValues = vi.fn().mockResolvedValue(undefined);
    const transaction = vi.fn(async (operation: (tx: any) => Promise<unknown>) => operation({
      select: vi.fn().mockReturnValue({
        from: vi.fn().mockReturnValue({
          where: vi.fn().mockReturnValue({
            limit: vi.fn().mockReturnValue({ for: vi.fn().mockResolvedValue([{ id: 9 }]) }),
          }),
        }),
      }),
      update: vi.fn().mockReturnValue({ set: updateSet }),
      insert: vi.fn().mockReturnValue({ values: auditValues }),
    }));
    drizzleMock.mockReturnValue({ transaction });

    await expect(updateReviewItemStatus(9, "approved", "Matched receipt", 4)).resolves.toBe(true);

    expect(transaction).toHaveBeenCalledOnce();
    expect(updateWhere).toHaveBeenCalledOnce();
    expect(updateSet).toHaveBeenCalledWith({
      status: "approved",
      resolution: "Matched receipt",
      resolvedAt: expect.any(Date),
    });
    expect(auditValues).toHaveBeenCalledWith({
      actorUserId: 4,
      action: "review_item.update_status",
      entityType: "review_item",
      entityId: "9",
      details: { status: "approved", resolution: "Matched receipt" },
    });
  });

  it("does not write an audit event when the review item does not exist", async () => {
    vi.resetModules();
    const { updateReviewItemStatus } = await import("./db");
    process.env.DATABASE_URL = "mysql://fab-test.invalid/fab";
    const auditValues = vi.fn();
    const transaction = vi.fn(async (operation: (tx: any) => Promise<unknown>) => operation({
      select: vi.fn().mockReturnValue({
        from: vi.fn().mockReturnValue({
          where: vi.fn().mockReturnValue({
            limit: vi.fn().mockReturnValue({ for: vi.fn().mockResolvedValue([]) }),
          }),
        }),
      }),
      update: vi.fn().mockReturnValue({
        set: vi.fn().mockReturnValue({
          where: vi.fn().mockResolvedValue([{ affectedRows: 0 }]),
        }),
      }),
      insert: vi.fn().mockReturnValue({ values: auditValues }),
    }));
    drizzleMock.mockReturnValue({ transaction });

    await expect(updateReviewItemStatus(404, "resolved")).resolves.toBe(false);
    expect(auditValues).not.toHaveBeenCalled();
  });

  it("propagates audit failure so the database transaction can roll back", async () => {
    vi.resetModules();
    const { updateReviewItemStatus } = await import("./db");
    process.env.DATABASE_URL = "mysql://fab-test.invalid/fab";
    const transaction = vi.fn(async (operation: (tx: any) => Promise<unknown>) => operation({
      select: vi.fn().mockReturnValue({
        from: vi.fn().mockReturnValue({
          where: vi.fn().mockReturnValue({
            limit: vi.fn().mockReturnValue({ for: vi.fn().mockResolvedValue([{ id: 9 }]) }),
          }),
        }),
      }),
      update: vi.fn().mockReturnValue({
        set: vi.fn().mockReturnValue({
          where: vi.fn().mockResolvedValue([{ affectedRows: 1 }]),
        }),
      }),
      insert: vi.fn().mockReturnValue({
        values: vi.fn().mockRejectedValue(new Error("audit storage unavailable")),
      }),
    }));
    drizzleMock.mockReturnValue({ transaction });

    await expect(updateReviewItemStatus(9, "rejected", "Duplicate")).rejects.toThrow("audit storage unavailable");
    expect(transaction).toHaveBeenCalledOnce();
  });
});
