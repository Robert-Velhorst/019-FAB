import express from "express";
import { describe, expect, it } from "vitest";
import { createCookieMutationOriginGuard, createFabSecurityMiddleware } from "./security";

async function startSecurityApp(production: boolean) {
  const app = express();
  app.set("trust proxy", 1);
  app.use(...createFabSecurityMiddleware(production));
  app.use("/api/trpc", createCookieMutationOriginGuard("fab_session"));
  app.get("/", (_req, res) => res.json({ status: "ok" }));
  app.post("/api/trpc/mutation", (_req, res) => res.json({ status: "updated" }));
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

  it("keeps a restrictive development CSP while allowing local Vite HMR", async () => {
    const app = await startSecurityApp(false);
    try {
      const response = await fetch(app.url);
      const policy = response.headers.get("content-security-policy") || "";
      expect(policy).toContain("default-src 'self'");
      expect(policy).toContain("connect-src 'self' ws: wss:");
      expect(policy).toContain("script-src 'self' 'unsafe-eval'");
      expect(policy).toContain("frame-ancestors 'none'");
      expect(policy).not.toContain("upgrade-insecure-requests");
      expect(response.headers.get("strict-transport-security")).toBeNull();
    } finally {
      await app.close();
    }
  });

  it.each([false, true])("preserves key CSP directives with Helmet defaults in %s mode", async production => {
    const app = await startSecurityApp(production);
    try {
      const response = await fetch(app.url);
      const policy = response.headers.get("content-security-policy") || "";
      expect(policy).toContain("font-src 'self' data:");
      expect(policy).toContain("form-action 'self'");
      expect(policy).toContain("base-uri 'self'");
      expect(policy).toContain("script-src-attr 'none'");
      expect(policy).not.toContain("upgrade-insecure-requests");
    } finally {
      await app.close();
    }
  });
});

describe("cookie-backed mutation origin guard", () => {
  it("requires a matching origin and same-origin fetch metadata for session mutations", async () => {
    const app = await startSecurityApp(false);
    const cookie = "fab_session=session-value";
    try {
      const crossOrigin = await fetch(`${app.url}/api/trpc/mutation`, {
        method: "POST",
        headers: { cookie, origin: "https://attacker.example", "sec-fetch-site": "cross-site" },
      });
      const missingOrigin = await fetch(`${app.url}/api/trpc/mutation`, {
        method: "POST",
        headers: { cookie },
      });
      const sameOrigin = await fetch(`${app.url}/api/trpc/mutation`, {
        method: "POST",
        headers: { cookie, origin: app.url, "sec-fetch-site": "same-origin" },
      });

      expect(crossOrigin.status).toBe(403);
      expect(missingOrigin.status).toBe(403);
      expect(sameOrigin.status).toBe(200);
    } finally {
      await app.close();
    }
  });

  it("does not impose browser-origin requirements on requests without the session cookie", async () => {
    const app = await startSecurityApp(false);
    try {
      const response = await fetch(`${app.url}/api/trpc/mutation`, {
        method: "POST",
        headers: { origin: "https://external-client.example" },
      });

      expect(response.status).toBe(200);
    } finally {
      await app.close();
    }
  });
});
