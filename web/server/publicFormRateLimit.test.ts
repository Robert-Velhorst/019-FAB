import express from "express";
import { createServer } from "http";
import type { RequestHandler } from "express";
import { afterEach, describe, expect, it } from "vitest";
import { createPublicFormRateLimitMiddleware } from "./lib/publicFormRateLimit";

const servers: ReturnType<typeof createServer>[] = [];

async function startApp() {
  const app = express();
  const limitedPaths: string[] = [];
  const limiter: RequestHandler = (req, _res, next) => {
    limitedPaths.push(req.originalUrl);
    next();
  };
  const parsePublicFormBody: RequestHandler = (req, res, next) => {
    express.json({ limit: "64kb" })(req, res, next);
  };
  app.use("/api/trpc", createPublicFormRateLimitMiddleware(limiter, parsePublicFormBody));
  app.use(express.json({ limit: "10mb" }));
  app.use("/api/trpc", (req, res) => res.status(200).send(req.originalUrl));
  const server = createServer(app);
  servers.push(server);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Expected TCP address");
  return { baseUrl: `http://127.0.0.1:${address.port}`, limitedPaths };
}

afterEach(async () => {
  await Promise.all(servers.splice(0).map((server) => new Promise<void>((resolve, reject) => {
    server.close((error) => error ? reject(error) : resolve());
  })));
});

describe("public form tRPC rate limiting", () => {
  it("limits direct and percent-encoded public procedures", async () => {
    const { baseUrl, limitedPaths } = await startApp();
    expect((await fetch(`${baseUrl}/api/trpc/waitlist.join`)).status).toBe(200);
    expect((await fetch(`${baseUrl}/api/trpc/contact%2Esubmit`)).status).toBe(200);
    expect(limitedPaths).toHaveLength(2);
  });

  it("rejects double-encoded public procedure paths before routing", async () => {
    const { baseUrl, limitedPaths } = await startApp();
    for (const path of [
      "/api/trpc/contact%252Esubmit",
      "/api/trpc/contact%25252Esubmit",
    ]) {
      const response = await fetch(`${baseUrl}${path}`);
      expect(response.status).toBe(400);
    }

    expect(limitedPaths).toHaveLength(0);
  });

  it("limits request bodies before parsing and bounds public-form JSON size", async () => {
    const { baseUrl, limitedPaths } = await startApp();
    const response = await fetch(`${baseUrl}/api/trpc/contact.submit`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ json: { message: "x".repeat(70_000) } }),
    });

    expect(response.status).toBe(413);
    expect(limitedPaths).toHaveLength(1);
  });

  it("applies the same limit to case-variant tRPC mount paths", async () => {
    const { baseUrl, limitedPaths } = await startApp();
    const response = await fetch(`${baseUrl}/API/TRPC/contact.submit`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ json: { message: "x".repeat(70_000) } }),
    });

    expect(response.status).toBe(413);
    expect(limitedPaths).toHaveLength(1);
  });

  it("rejects batches containing public mutations before they reach tRPC", async () => {
    const { baseUrl, limitedPaths } = await startApp();
    for (const path of [
      "/api/trpc/waitlist.join,contact.submit?batch=1",
      "/api/trpc/blog.list%2Ccontact.submit?batch=1",
      "/api/trpc/waitlist.join%2Cblog.list?batch=1",
    ]) {
      expect((await fetch(`${baseUrl}${path}`)).status).toBe(400);
    }
    expect(limitedPaths).toHaveLength(0);
  });

  it("preserves batching for unrelated procedures", async () => {
    const { baseUrl, limitedPaths } = await startApp();
    expect((await fetch(`${baseUrl}/api/trpc/blog.list,blog.detail?batch=1`)).status).toBe(200);
    expect(limitedPaths).toHaveLength(0);
  });

  it("rejects malformed path escaping", async () => {
    const { baseUrl } = await startApp();
    expect((await fetch(`${baseUrl}/api/trpc/contact%2`)).status).toBe(400);
  });
});
