import express from "express";
import { describe, expect, it } from "vitest";
import { createFabSecurityMiddleware } from "./security";

async function startSecurityApp(production: boolean) {
  const app = express();
  app.set("trust proxy", 1);
  app.use(...createFabSecurityMiddleware(production));
  app.get("/", (_req, res) => res.json({ status: "ok" }));
  const server = app.listen(0);
  const address = server.address();
  if (!address || typeof address === "string") {
    server.close();
    throw new Error("Expected TCP test server address");
  }
  return {
    url: `http://127.0.0.1:${address.port}`,
    close: () => new Promise<void>((resolve, reject) => {
      server.close((error) => error ? reject(error) : resolve());
    }),
  };
}

describe("FAB web security headers", () => {
  it("sets a restrictive production CSP without forcing local HTTP to HTTPS", async () => {
    const app = await startSecurityApp(true);
    try {
      const response = await fetch(app.url);
      const policy = response.headers.get("content-security-policy") || "";
      expect(policy).toContain("default-src 'self'");
      expect(policy).toContain("script-src 'self'");
      expect(policy).toContain("script-src-attr 'none'");
      expect(policy).toContain("object-src 'none'");
      expect(policy).toContain("frame-ancestors 'none'");
      expect(policy).not.toContain("upgrade-insecure-requests");
      expect(response.headers.get("content-security-policy-report-only")).toBeNull();
      expect(response.headers.get("strict-transport-security")).toBeNull();
    } finally {
      await app.close();
    }
  });

  it("sets HSTS only when the trusted proxy reports HTTPS", async () => {
    const app = await startSecurityApp(true);
    try {
      const response = await fetch(app.url, {
        headers: { "x-forwarded-proto": "https" },
      });
      expect(response.headers.get("strict-transport-security")).toBe(
        "max-age=31536000; includeSubDomains",
      );
    } finally {
      await app.close();
    }
  });

  it("reports development CSP violations without blocking the Vite app", async () => {
    const app = await startSecurityApp(false);
    try {
      const response = await fetch(app.url);
      const policy = response.headers.get("content-security-policy-report-only") || "";
      expect(policy).toContain("default-src 'self'");
      expect(policy).toContain("script-src 'self'");
      expect(response.headers.get("content-security-policy")).toBeNull();
      expect(response.headers.get("strict-transport-security")).toBeNull();
    } finally {
      await app.close();
    }
  });
});
