import { createHash } from "crypto";
import express from "express";
import { get } from "node:http";
import { once } from "node:events";
import { describe, expect, it, vi } from "vitest";
import { registerFabSourcePreviewRoutes } from "./fabSourcePreview";

async function startTestServer(
  options: Parameters<typeof registerFabSourcePreviewRoutes>[1],
  configure?: (app: express.Application) => void,
): Promise<{ baseUrl: string; close: () => Promise<void> }> {
  const app = express();
  configure?.(app);
  registerFabSourcePreviewRoutes(app, options);
  const server = app.listen(0);
  const address = server.address();
  if (!address || typeof address === "string") {
    throw new Error("Expected TCP test server address");
  }
  return {
    baseUrl: `http://127.0.0.1:${address.port}`,
    close: () => new Promise<void>((resolve, reject) => {
      server.close((error) => error ? reject(error) : resolve());
    }),
  };
}

describe("FAB source preview proxy", () => {
  it("keeps a slot occupied until a deferred source response finishes", async () => {
    const bytes = new TextEncoder().encode("verified receipt");
    const digest = createHash("sha256").update(bytes).digest("hex");
    let upstreamCalls = 0;
    let release!: () => void;
    const server = await startTestServer({
      localOperatorMode: true, maxConcurrent: 1,
      fetchImpl: async () => {
        upstreamCalls += 1;
        return new Response(bytes, { headers: {
          "content-type": "image/png", "x-fab-source-integrity": "verified",
          "x-fab-source-sha256": digest,
        } });
      },
    }, app => {
      app.use((req, res, next) => {
        if (req.path.endsWith("/42")) {
          const end = res.end;
          res.end = ((...args: unknown[]) => {
            release = () => { release = () => {}; Reflect.apply(end, res, args); };
            res.flushHeaders();
            return res;
          }) as typeof res.end;
        }
        next();
      });
    });
    const request = get(`${server.baseUrl}/api/fab/source/42`);
    request.on("error", () => {});
    try {
      const [response] = await once(request, "response");
      response.pause();
      expect(response.statusCode).toBe(200);
      const excess = await fetch(`${server.baseUrl}/api/fab/source/43`);
      expect(excess.status).toBe(429);
      expect(upstreamCalls).toBe(1);
      const finished = once(response, "end");
      response.resume();
      release();
      await finished;
      expect((await fetch(`${server.baseUrl}/api/fab/source/44`)).status).toBe(200);
    } finally {
      release?.();
      request.destroy();
      await server.close();
    }
  });

  it.each([
    { maxBytes: 0 }, { maxBytes: 25 * 1024 * 1024 + 1 },
    { maxConcurrent: 0 }, { maxConcurrent: 5 }, { maxConcurrent: 1.5 },
    { timeoutMs: 0 }, { timeoutMs: NaN }, { timeoutMs: 120_001 },
  ])("rejects invalid resource configuration %j before serving requests", options => {
    expect(() => registerFabSourcePreviewRoutes(express(), options)).toThrow("resource limits");
  });

  it("releases its download slot after a timeout and does not blame an unrelated abort on timeouts", async () => {
    let mode = "timeout";
    const bytes = new TextEncoder().encode("receipt");
    const server = await startTestServer({
      localOperatorMode: true, timeoutMs: 30, maxConcurrent: 1,
      fetchImpl: async (_input, init) => {
        if (mode === "abort") throw new DOMException("Other abort", "AbortError");
        if (mode === "timeout") return new Response(new ReadableStream({ start(controller) {
          init!.signal!.addEventListener("abort", () => controller.error(new DOMException("Abort", "AbortError")), { once: true });
        } }), { headers: { "content-type": "text/plain" } });
        return new Response(bytes, { headers: {
          "content-type": "text/plain", "x-fab-source-integrity": "verified",
          "x-fab-source-sha256": createHash("sha256").update(bytes).digest("hex"),
        } });
      },
    });
    try {
      expect((await fetch(`${server.baseUrl}/api/fab/source/42`)).status).toBe(504);
      mode = "abort";
      expect((await fetch(`${server.baseUrl}/api/fab/source/43`)).status).toBe(502);
      mode = "success";
      expect((await fetch(`${server.baseUrl}/api/fab/source/44`)).status).toBe(200);
    } finally {
      await server.close();
    }
  });

  it("cancels an upstream download when the browser disconnects", async () => {
    let began!: () => void;
    let aborted!: () => void;
    const started = new Promise<void>(resolve => { began = resolve; });
    const cancelled = new Promise<void>(resolve => { aborted = resolve; });
    const server = await startTestServer({
      localOperatorMode: true, timeoutMs: 1_000,
      fetchImpl: async (_input, init) => {
        const body = new ReadableStream<Uint8Array>({ start(controller) {
          init!.signal!.addEventListener("abort", () => {
            aborted();
            controller.error(new DOMException("Aborted", "AbortError"));
          }, { once: true });
        } });
        began();
        return new Response(body, { headers: { "content-type": "text/plain" } });
      },
    });
    const request = get(`${server.baseUrl}/api/fab/source/42`);
    request.on("error", () => {});
    try {
      await started;
      request.destroy();
      const promptlyCancelled = await Promise.race([
        cancelled.then(() => true),
        new Promise<boolean>(resolve => setTimeout(() => resolve(false), 150)),
      ]);
      expect(promptlyCancelled).toBe(true);
    } finally {
      request.destroy();
      await cancelled;
      await server.close();
    }
  });

  it("rejects excess simultaneous previews without another upstream read", async () => {
    const bytes = new TextEncoder().encode("verified receipt");
    const response = () => new Response(bytes, { headers: {
      "content-type": "text/plain", "x-fab-source-integrity": "verified",
      "x-fab-source-sha256": createHash("sha256").update(bytes).digest("hex"),
    } });
    let began!: () => void;
    let release!: () => void;
    const started = new Promise<void>(resolve => { began = resolve; });
    const held = new Promise<void>(resolve => { release = resolve; });
    let calls = 0;
    const server = await startTestServer({
      localOperatorMode: true, maxConcurrent: 1,
      fetchImpl: async () => {
        calls += 1;
        if (calls === 1) { began(); await held; }
        return response();
      },
    });
    const first = fetch(`${server.baseUrl}/api/fab/source/42`);
    try {
      await started;
      const rejected = await fetch(`${server.baseUrl}/api/fab/source/43`);
      expect(rejected.status).toBe(429);
      expect(rejected.headers.get("retry-after")).toBe("1");
      expect(calls).toBe(1);
      release();
      expect((await first).status).toBe(200);
      expect((await fetch(`${server.baseUrl}/api/fab/source/44`)).status).toBe(200);
    } finally {
      release();
      await first;
      await server.close();
    }
  });

  it("refuses redirects and oversized chunked source responses", async () => {
    let redirect = true;
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.redirect).toBe("manual");
      if (redirect) return new Response(null, { status: 307, headers: { location: "https://example.test/private" } });
      return new Response(new ReadableStream({ start(controller) {
        controller.enqueue(new TextEncoder().encode("1234"));
        controller.close();
      } }), { headers: { "content-type": "text/plain" } });
    });
    const server = await startTestServer({
      baseUrl: "http://127.0.0.1:5001", fetchImpl, localOperatorMode: true, maxBytes: 3,
    });
    try {
      expect((await fetch(`${server.baseUrl}/api/fab/source/42`)).status).toBe(502);
      redirect = false;
      expect((await fetch(`${server.baseUrl}/api/fab/source/42`)).status).toBe(413);
      expect(fetchImpl).toHaveBeenCalledTimes(2);
    } finally {
      await server.close();
    }
  });

  it("keeps the API token server-side and re-verifies source bytes", async () => {
    const source = new TextEncoder().encode("verified receipt");
    const sha256 = createHash("sha256").update(source).digest("hex");
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(new Headers(init?.headers).get("authorization")).toBe("Bearer private-token");
      expect(init?.redirect).toBe("manual");
      return new Response(source, {
        headers: {
          "content-length": String(source.byteLength),
          "content-type": "text/plain",
          "x-fab-source-integrity": "verified",
          "x-fab-source-sha256": sha256,
          "x-frame-options": "DENY",
        },
      });
    });
    const server = await startTestServer({
      baseUrl: "http://127.0.0.1:5001",
      fetchImpl,
      localOperatorMode: true,
      token: "private-token",
    });
    try {
      const response = await fetch(`${server.baseUrl}/api/fab/source/42`);

      expect(response.status).toBe(200);
      await expect(response.text()).resolves.toBe("verified receipt");
      expect(response.headers.get("cache-control")).toBe("no-store");
      expect(response.headers.get("x-fab-source-integrity")).toBe("verified");
      expect(response.headers.get("x-fab-source-sha256")).toBe(sha256);
      expect(response.headers.get("x-frame-options")).toBe("SAMEORIGIN");
      expect(response.headers.get("content-security-policy")).toContain("sandbox");
    } finally {
      await server.close();
    }
  });

  it("allows the browser PDF viewer while retaining same-origin framing", async () => {
    const source = new TextEncoder().encode("%PDF verified receipt");
    const sha256 = createHash("sha256").update(source).digest("hex");
    const server = await startTestServer({
      baseUrl: "http://127.0.0.1:5001",
      fetchImpl: async () => new Response(source, {
        headers: {
          "content-type": "application/pdf",
          "x-fab-source-integrity": "verified",
          "x-fab-source-sha256": sha256,
        },
      }),
      localOperatorMode: true,
    });
    try {
      const response = await fetch(`${server.baseUrl}/api/fab/source/42`);

      expect(response.status).toBe(200);
      expect(response.headers.get("content-security-policy")).toBe("frame-ancestors 'self'");
      expect(response.headers.get("x-frame-options")).toBe("SAMEORIGIN");
      expect(response.headers.get("x-content-type-options")).toBe("nosniff");
    } finally {
      await server.close();
    }
  });

  it("rejects requests without an admin or enabled loopback operator", async () => {
    const fetchImpl = vi.fn();
    const server = await startTestServer({
      authenticateRequest: async () => {
        throw new Error("No session");
      },
      fetchImpl,
      localOperatorMode: false,
    });
    try {
      const response = await fetch(`${server.baseUrl}/api/fab/source/42`);

      expect(response.status).toBe(403);
      expect(fetchImpl).not.toHaveBeenCalled();
    } finally {
      await server.close();
    }
  });

  it("rejects a mismatched upstream integrity proof", async () => {
    const source = new TextEncoder().encode("changed receipt");
    const fetchImpl = vi.fn(async () => new Response(source, {
      headers: {
        "content-type": "text/plain",
        "x-fab-source-integrity": "verified",
        "x-fab-source-sha256": "0".repeat(64),
      },
    }));
    const server = await startTestServer({
      baseUrl: "http://127.0.0.1:5001",
      fetchImpl,
      localOperatorMode: true,
    });
    try {
      const response = await fetch(`${server.baseUrl}/api/fab/source/42`);

      expect(response.status).toBe(502);
      await expect(response.json()).resolves.toEqual({
        error: "The FAB source preview failed its gateway integrity check",
      });
    } finally {
      await server.close();
    }
  });

  it("rejects unsafe MIME types and bounded-length violations", async () => {
    const source = new TextEncoder().encode("1234");
    const sha256 = createHash("sha256").update(source).digest("hex");
    const unsafeServer = await startTestServer({
      baseUrl: "http://127.0.0.1:5001",
      fetchImpl: async () => new Response(source, {
        headers: {
          "content-type": "text/html",
          "x-fab-source-integrity": "verified",
          "x-fab-source-sha256": sha256,
        },
      }),
      localOperatorMode: true,
    });
    const oversizedServer = await startTestServer({
      baseUrl: "http://127.0.0.1:5001",
      fetchImpl: async () => new Response(source, {
        headers: {
          "content-length": String(source.byteLength),
          "content-type": "text/plain",
          "x-fab-source-integrity": "verified",
          "x-fab-source-sha256": sha256,
        },
      }),
      localOperatorMode: true,
      maxBytes: 3,
    });
    try {
      expect((await fetch(`${unsafeServer.baseUrl}/api/fab/source/42`)).status).toBe(415);
      expect((await fetch(`${oversizedServer.baseUrl}/api/fab/source/42`)).status).toBe(413);
    } finally {
      await unsafeServer.close();
      await oversizedServer.close();
    }
  });
});
