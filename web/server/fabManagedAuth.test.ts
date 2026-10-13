import { createHash, createHmac, randomBytes } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import express from "express";
import { createExpressMiddleware } from "@trpc/server/adapters/express";
import { decodeJwt, SignJWT } from "jose";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ENV } from "./_core/env";
import { createFabContext } from "./fabContext";
import { fabStandaloneRouter } from "./fabRouter";
import { resolveFabOperatorAccess } from "./fabOperatorAccess";
import { registerFabOperatorSessionRoutes } from "./fabOperatorSession";
import { registerFabSourcePreviewRoutes } from "./fabSourcePreview";

const origin = "https://fab.example.test";
const accessToken = randomBytes(32).toString("base64url");
const jwtSecret = randomBytes(32).toString("base64url");
const cookieName = "__Host-fab_operator";
const savedEnv = { ...ENV };
const cleanup: Array<() => void | Promise<void>> = [];

async function listen(app: ReturnType<typeof express>) {
  const server = app.listen(0, "127.0.0.1");
  await new Promise<void>(resolve => server.once("listening", resolve));
  cleanup.push(() => new Promise<void>((resolve, reject) => {
    server.close(error => error ? reject(error) : resolve());
    server.closeAllConnections();
  }));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Expected TCP address");
  return `http://127.0.0.1:${address.port}`;
}

async function fixture() {
  const upstreamCalls: Array<{ path: string; authorization: string | undefined; body: unknown }> = [];
  const ledger = express();
  ledger.use(express.json());
  ledger.use((req, res) => {
    upstreamCalls.push({ path: req.path, authorization: req.headers.authorization, body: req.body });
    if (req.path.endsWith("/source")) {
      res.set({ "content-type": "text/plain", "x-fab-source-integrity": "verified",
        "x-fab-source-sha256": createHash("sha256").update("test evidence").digest("hex") });
      res.send("test evidence");
    } else {
      res.json({ success: true, status: "created", bundleFilename: "test.zip" });
    }
  });
  ENV.fabLocalApiUrl = await listen(ledger);
  ENV.fabLocalApiToken = "test-ledger-token";
  ENV.fabLocalApiPublicUrl = "https://ledger.example.test";
  const auth = await import("./fabManagedAuth");
  const app = express();
  app.set("trust proxy", ["127.0.0.1", "::ffff:127.0.0.1"]);
  auth.registerFabManagedAuthRoutes(app);
  registerFabSourcePreviewRoutes(app);
  registerFabOperatorSessionRoutes(app);
  app.use(express.json());
  app.use("/api/trpc", createExpressMiddleware({ router: fabStandaloneRouter, createContext: createFabContext }));
  app.get("/admin/operations", (_req, res) => res.send("dashboard"));
  const baseUrl = await listen(app);
  const request = (path: string, init: RequestInit = {}) => fetch(`${baseUrl}${path}`, {
    ...init, redirect: "manual", headers: {
      "x-forwarded-host": "fab.example.test", "x-forwarded-proto": "https", ...init.headers,
    },
  });
  const login = (token = accessToken, headers: Record<string, string> = {}) => request("/operator/login", {
    method: "POST", headers: { origin, "content-type": "application/x-www-form-urlencoded", ...headers },
    body: new URLSearchParams({ accessToken: token }),
  });
  return { auth, request, login, upstreamCalls };
}

function sessionCookie(response: Response) {
  const cookie = response.headers.get("set-cookie");
  expect(cookie).toContain(`${cookieName}=`);
  return cookie!.split(";")[0];
}

beforeEach(() => {
  vi.stubEnv("FAB_DEPLOYMENT_PROFILE", "vm");
  vi.stubEnv("FAB_OPERATOR_ACCESS_TOKEN", accessToken);
  vi.stubEnv("FAB_OPERATOR_ACCESS_TOKEN_FILE", "");
  vi.stubEnv("JWT_SECRET", jwtSecret);
  vi.stubEnv("JWT_SECRET_FILE", "");
  vi.stubEnv("FAB_OPERATOR_PUBLIC_ORIGIN", origin);
  ENV.cookieSecret = jwtSecret;
  ENV.fabOperatorLocalMode = true; // VM must ignore even a misconfigured local mode.
  ENV.fabOperatorTrustedProxyAddresses = ["127.0.0.1", "::ffff:127.0.0.1"];
});

it("binds ledger handoffs to the managed session and revokes status on logout", async () => {
  const { request, login } = await fixture();
  const cookie = sessionCookie(await login());
  const handoff = await request("/api/fab/operator-session?next=%2F", { headers: { cookie } });
  const ticket = new URL(handoff.headers.get("location")!).searchParams.get("ticket")!;
  const payload = JSON.parse(Buffer.from(ticket.split(".")[0], "base64url").toString("utf8"));
  expect(payload.v).toBe(2);
  const jwt = decodeJwt(cookie.split("=")[1]);
  expect(payload.parent).toEqual({ id: jwt.jti, exp: jwt.exp });
  const status = (authorization: string) => request("/api/fab/operator-session/status", {
    method: "POST", headers: { authorization, "content-type": "application/json" },
    body: JSON.stringify({ id: payload.parent.id }),
  });
  expect((await status("Bearer invalid")).status).toBe(403);
  expect((await status(`Bearer ${ENV.fabLocalApiToken}`)).status).toBe(403);
  const credential = createHmac("sha256", ENV.fabLocalApiToken).update("fab-managed-parent-status:v1").digest("base64url");
  const active = await status(`Bearer ${credential}`);
  expect(active.status).toBe(200);
  expect(await active.json()).toEqual({ active: true, exp: jwt.exp });
  expect(active.headers.get("cache-control")).toBe("no-store");
  await request("/operator/logout", { method: "POST", headers: { cookie, origin } });
  expect(await (await status(`Bearer ${credential}`)).json()).toEqual({ active: false });
});

it.each(["/operator/logout", "/api/trpc/auth.logout"])("invalidates ledger authority through %s", async logoutPath => {
  const { request, login } = await fixture();
  const cookie = sessionCookie(await login());
  const jwt = decodeJwt(cookie.split("=")[1]);
  const authorization = `Bearer ${createHmac("sha256", ENV.fabLocalApiToken).update("fab-managed-parent-status:v1").digest("base64url")}`;
  const status = (extra: Record<string, unknown> = {}) => request("/api/fab/operator-session/status", {
    method: "POST", headers: { authorization, "content-type": "application/json" },
    body: JSON.stringify({ id: jwt.jti, ...extra }),
  });
  const handoff = { nonce: randomBytes(18).toString("base64url"), exp: Math.floor(Date.now() / 1_000) + 45 };
  expect(await (await status(handoff)).json()).toEqual({ active: true, exp: jwt.exp });
  expect(await (await status(handoff)).json()).toEqual({ active: false });
  expect(await (await status()).json()).toEqual({ active: true, exp: jwt.exp });
  const logout = await request(logoutPath, {
    method: "POST", headers: { cookie, origin, "content-type": "application/json" }, body: '{"json":null}',
  });
  expect(logout.status).toBe(logoutPath.startsWith("/operator") ? 303 : 200);
  expect(await (await status()).json()).toEqual({ active: false });
});

it.each(["expiry", "operator rotation", "signing rotation"])("denies parent session status after %s", async change => {
  const { request, login } = await fixture();
  const cookie = sessionCookie(await login());
  const jwt = decodeJwt(cookie.split("=")[1]);
  if (change === "expiry") vi.spyOn(Date, "now").mockReturnValue((jwt.exp! + 1) * 1_000);
  if (change === "operator rotation") vi.stubEnv("FAB_OPERATOR_ACCESS_TOKEN", randomBytes(32).toString("base64url"));
  if (change === "signing rotation") ENV.cookieSecret = randomBytes(32).toString("base64url");
  const authorization = `Bearer ${createHmac("sha256", ENV.fabLocalApiToken).update("fab-managed-parent-status:v1").digest("base64url")}`;
  const response = await request("/api/fab/operator-session/status", {
    method: "POST", headers: { authorization, "content-type": "application/json" }, body: JSON.stringify({ id: jwt.jti }),
  });
  expect(await response.json()).toEqual({ active: false });
});

afterEach(async () => {
  for (const close of cleanup.splice(0).reverse()) await close();
  Object.assign(ENV, savedEnv);
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe("FAB managed operator authentication", () => {
  it("preserves the browser Origin on login, login-error, and logout form submissions", async () => {
    const { request, login } = await fixture();
    for (const path of ["/operator/login", "/operator/logout"]) {
      expect((await request(path)).headers.get("referrer-policy")).toBe("same-origin");
    }
    const rejected = await login("incorrect-synthetic-password");
    expect(rejected.status).toBe(401);
    expect(rejected.headers.get("referrer-policy")).toBe("same-origin");
    expect((await login(accessToken, { origin: "null" })).status).toBe(403);
  });

  it("never accepts loopback or the external SDK identity in the VM profile", async () => {
    const req = { hostname: "localhost", socket: { remoteAddress: "127.0.0.1" } } as never;
    const authenticateRequest = vi.fn(async () => ({ role: "admin" }));
    expect(await resolveFabOperatorAccess(req, { localOperatorMode: true, authenticateRequest }))
      .toEqual({ actor: null, allowed: false, mode: null });
    expect(authenticateRequest).not.toHaveBeenCalled();
  });

  it("serves a real login form and redirects unauthenticated dashboard navigation", async () => {
    const { request } = await fixture();
    for (const path of ["/", "/admin/operations", "/index.html", "/any-spa-route", "/assets/missing.html", "/api/missing"]) {
      const dashboard = await request(path);
      expect(dashboard.status).toBe(303);
      expect(dashboard.headers.get("location")).toBe("/operator/login");
    }
    const page = await request("/operator/login");
    expect(page.status).toBe(200);
    expect(page.headers.get("cache-control")).toBe("no-store");
    expect(page.headers.get("referrer-policy")).toBe("same-origin");
    const html = await page.text();
    expect(html).toContain('action="/operator/login"');
    expect(html).toContain('method="post"');
    expect(html).toContain('type="password"');
    expect(html).toContain('name="accessToken"');
    expect(html).not.toContain(accessToken);
  });

  it("logs in and authorizes real gateway reads, mutation, and handoff without OAuth or a user DB", async () => {
    const { request, login, upstreamCalls } = await fixture();
    const response = await login();
    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toBe("/admin/operations");
    const cookie = sessionCookie(response);
    expect(response.headers.get("set-cookie")).toMatch(/HttpOnly/);
    expect(response.headers.get("set-cookie")).toMatch(/Secure/);
    expect(response.headers.get("set-cookie")).toMatch(/SameSite=Strict/);
    expect(response.headers.get("set-cookie")).toMatch(/Max-Age=900/);
    expect(response.headers.get("set-cookie")).not.toContain("Domain=");
    const claims = decodeJwt(cookie.slice(cookie.indexOf("=") + 1));
    expect(claims).toMatchObject({ sub: "fab-single-business-administrator", aud: "fab-managed-operator", iss: "fab-standalone", role: "admin" });
    expect(JSON.stringify(claims)).not.toContain(accessToken);
    expect(cookie).not.toContain(accessToken);
    expect((await request("/admin/operations", { headers: { cookie } })).status).toBe(200);
    const me = await request("/api/trpc/auth.me", { headers: { cookie } });
    expect(await me.text()).toContain("FAB administrator");
    const access = await request("/api/trpc/fab.access", { headers: { cookie } });
    expect(access.status).toBe(200);
    expect(await access.text()).toContain('"mode":"admin"');
    const operation = await request("/api/trpc/fab.createSupportBundle", {
      method: "POST", headers: { cookie, origin, "content-type": "application/json" }, body: '{"json":null}',
    });
    expect(operation.status).toBe(200);
    expect(upstreamCalls[0]).toMatchObject({ path: "/api/support-bundles", authorization: "Bearer test-ledger-token" });
    expect(upstreamCalls[0].body).toMatchObject({ actor: "fab_dashboard:admin:fab-single-business-administrator" });
    const preview = await request("/api/fab/source/42", { headers: { cookie } });
    expect(preview.status).toBe(200);
    expect(await preview.text()).toBe("test evidence");
    const handoff = await request("/api/fab/operator-session?next=%2F", { headers: { cookie } });
    expect(handoff.status).toBe(302);
    expect(handoff.headers.get("location")).toMatch(/^https:\/\/ledger.example.test\/operator\/session\/bootstrap/);
    expect(handoff.headers.get("location")).not.toContain(accessToken);
  });

  it("rejects invalid credentials without echoing them or issuing a cookie", async () => {
    const { login, request } = await fixture();
    const response = await login("incorrect-private-value");
    expect(response.status).toBe(401);
    expect(response.headers.get("set-cookie")).toBeNull();
    expect(await response.text()).not.toContain("incorrect-private-value");
    expect((await request(`/operator/login?accessToken=${accessToken}`)).headers.get("set-cookie")).toBeNull();
  });

  it.each(["invalid-utf8", "extra-newline", "trailing-tab", "oversized"])(
    "rejects %s operator secret files without exposing paths or contents", async kind => {
      const { auth, login } = await fixture();
      const directory = mkdtempSync(join(tmpdir(), "fab-auth-invalid-"));
      cleanup.push(() => rmSync(directory, { recursive: true, force: true }));
      const path = join(directory, "private-secret-path");
      const bytes = kind === "invalid-utf8" ? Buffer.concat([Buffer.from(accessToken), Buffer.from([0xff])])
        : Buffer.from(kind === "extra-newline" ? `${accessToken}\n\n`
          : kind === "trailing-tab" ? `${accessToken}\t` : accessToken.repeat(100));
      writeFileSync(path, bytes);
      vi.stubEnv("FAB_OPERATOR_ACCESS_TOKEN", "");
      vi.stubEnv("FAB_OPERATOR_ACCESS_TOKEN_FILE", path);
      expect(() => auth.validateFabManagedAuthConfiguration()).toThrow();
      const response = await login();
      expect(response.status).toBe(503);
      const body = await response.text();
      expect(body).not.toContain(path);
      expect(body).not.toContain(accessToken);
    },
  );

  it.each([
    ["missing origin", { origin: "" }],
    ["foreign origin", { origin: "https://evil.example" }],
    ["null origin", { origin: "null" }],
    ["insecure transport", { "x-forwarded-proto": "http" }],
    ["ambiguous transport", { "x-forwarded-proto": "https,http" }],
    ["wrong host", { "x-forwarded-host": "evil.example" }],
    ["cross-site fetch", { "sec-fetch-site": "cross-site" }],
    ["same-site sibling", { "sec-fetch-site": "same-site" }],
  ])("rejects login with %s", async (_name, headers) => {
    const { login } = await fixture();
    const response = await login(accessToken, headers);
    expect(response.status).toBe(403);
    expect(response.headers.get("set-cookie")).toBeNull();
  });

  it("does not trust forwarded HTTPS from a non-allowlisted peer", async () => {
    const { login } = await fixture();
    ENV.fabOperatorTrustedProxyAddresses = [];
    expect((await login()).status).toBe(403);
  });

  it("rejects unsafe requests at every operator gateway even with a valid session", async () => {
    const { login, request, upstreamCalls } = await fixture();
    const cookie = sessionCookie(await login());
    for (const path of ["/api/trpc/fab.access", "/api/fab/source/42", "/api/fab/operator-session?next=%2F"]) {
      for (const headers of [{ origin: "https://evil.example" }, { "x-forwarded-proto": "http" }]) {
        expect((await request(path, { headers: { cookie, ...headers } })).status).toBe(403);
      }
      expect((await request(path)).status).toBe(403);
    }
    const operation = await request("/api/trpc/fab.createSupportBundle", {
      method: "POST", headers: { cookie, "content-type": "application/json" }, body: '{"json":null}',
    });
    expect(operation.status).toBe(403);
    expect(upstreamCalls).toEqual([]);
  });

  it("rejects tampered, expired and wrong-purpose JWTs on all gateways", async () => {
    const { login, request, upstreamCalls } = await fixture();
    const cookie = sessionCookie(await login());
    const token = cookie.slice(cookie.indexOf("=") + 1);
    const claims = decodeJwt(token);
    const key = createHmac("sha256", jwtSecret).update("fab-managed-session:v1\0").update(accessToken).digest();
    const invalid = [token.slice(0, -8) + "tampered"];
    for (const overrides of [
      { aud: "fab-local-operator-session" }, { aud: ["fab-managed-operator", "other"] },
      { sub: "another-admin" }, { iss: "other" }, { role: "user" },
      { exp: Math.floor(Date.now() / 1000) - 1 }, { exp: claims.iat! + 901 },
      { iat: Math.floor(Date.now() / 1000) + 10 }, { v: 2 }, { jti: "not-issued" },
    ]) {
      invalid.push(await new SignJWT({ ...claims, ...overrides }).setProtectedHeader({ alg: "HS256", typ: "JWT" }).sign(key));
    }
    invalid.push(await new SignJWT(claims).setProtectedHeader({ alg: "HS512", typ: "JWT" }).sign(key));
    for (const forged of invalid) {
      for (const path of ["/api/trpc/fab.access", "/api/fab/source/42", "/api/fab/operator-session?next=%2F"]) {
        expect((await request(path, { headers: { cookie: `${cookieName}=${forged}` } })).status).toBe(403);
      }
    }
    expect(upstreamCalls).toEqual([]);
  });

  it("expires a genuine issued session after fifteen minutes", async () => {
    const { login, request } = await fixture();
    const cookie = sessionCookie(await login());
    vi.spyOn(Date, "now").mockReturnValue(Date.now() + 901_000);
    expect((await request("/api/trpc/fab.access", { headers: { cookie } })).status).toBe(403);
  });

  it.each(["/operator/logout", "/api/trpc/auth.logout"])("revokes the session through %s and prevents replay", async path => {
    const { login, request } = await fixture();
    const cookie = sessionCookie(await login());
    const response = await request(path, { method: "POST", headers: {
      cookie, origin, "content-type": "application/json",
    }, body: '{"json":null}' });
    expect(response.status).toBe(path.startsWith("/operator") ? 303 : 200);
    expect(response.headers.get("set-cookie")).toContain(`${cookieName}=;`);
    expect(response.headers.get("set-cookie")).toContain("HttpOnly");
    expect(response.headers.get("set-cookie")).toContain("Secure");
    expect((await request("/api/trpc/fab.access", { headers: { cookie } })).status).toBe(403);
  });

  it("does not log out on GET or cross-origin POST", async () => {
    const { login, request } = await fixture();
    const cookie = sessionCookie(await login());
    const page = await request("/operator/logout", { headers: { cookie } });
    expect(page.status).toBe(200);
    expect(await page.text()).toContain('action="/operator/logout"');
    for (const path of ["/operator/logout", "/api/trpc/auth.logout"]) {
      expect((await request(path, { method: "POST", headers: {
        cookie, origin: "https://evil.example", "content-type": "application/json",
      }, body: '{"json":null}' })).status).toBe(403);
    }
    expect((await request("/api/trpc/fab.access", { headers: { cookie } })).status).toBe(200);
  });

  it("invalidates sessions when the environment access secret rotates", async () => {
    const { login, request } = await fixture();
    const cookie = sessionCookie(await login());
    const replacement = randomBytes(32).toString("base64url");
    vi.stubEnv("FAB_OPERATOR_ACCESS_TOKEN", replacement);
    expect((await request("/api/trpc/fab.access", { headers: { cookie } })).status).toBe(403);
    expect((await login()).status).toBe(401);
    expect((await login(replacement)).status).toBe(303);
    vi.stubEnv("FAB_OPERATOR_ACCESS_TOKEN", accessToken);
    expect((await request("/api/trpc/fab.access", { headers: { cookie } })).status).toBe(403);
  });

  it("reads mounted secrets and detects file rotation without restart", async () => {
    const directory = mkdtempSync(join(tmpdir(), "fab-auth-test-"));
    cleanup.push(() => rmSync(directory, { recursive: true, force: true }));
    const path = join(directory, "operator-secret");
    writeFileSync(path, `${accessToken}\n`);
    vi.stubEnv("FAB_OPERATOR_ACCESS_TOKEN", "");
    vi.stubEnv("FAB_OPERATOR_ACCESS_TOKEN_FILE", path);
    const { login, request } = await fixture();
    const cookie = sessionCookie(await login());
    const replacement = randomBytes(32).toString("base64url");
    writeFileSync(path, replacement);
    expect((await request("/api/trpc/fab.access", { headers: { cookie } })).status).toBe(403);
    expect((await login(replacement)).status).toBe(303);
  });

  it.each([
    ["FAB_OPERATOR_ACCESS_TOKEN", ""], ["FAB_OPERATOR_ACCESS_TOKEN", "a".repeat(64)],
    ["JWT_SECRET", ""], ["JWT_SECRET", accessToken],
    ["FAB_OPERATOR_PUBLIC_ORIGIN", "http://fab.example.test"], ["FAB_OPERATOR_PUBLIC_ORIGIN", `${origin}/path`],
    ["FAB_OPERATOR_ACCESS_TOKEN_FILE", "missing-secret-file"],
  ])("fails closed for invalid configuration %s", async (key, value) => {
    const { login, auth } = await fixture();
    vi.stubEnv(key, value);
    if (key === "JWT_SECRET") ENV.cookieSecret = value;
    expect(() => auth.validateFabManagedAuthConfiguration()).toThrow();
    expect((await login()).status).toBe(503);
  });

  it("limits repeated attempts, including valid credentials after the limit", async () => {
    const { login } = await fixture();
    for (let i = 0; i < 10; i++) expect((await login("wrong")).status).toBe(401);
    const response = await login();
    expect(response.status).toBe(429);
    expect(response.headers.get("retry-after")).not.toBeNull();
    expect(response.headers.get("set-cookie")).toBeNull();
  });

  it("rejects an access secret shared with either backend API authority", async () => {
    const { auth, login } = await fixture();
    for (const property of ["fabLocalApiToken", "fabOperationsServiceToken"] as const) {
      const saved = ENV[property];
      ENV[property] = accessToken;
      expect(() => auth.validateFabManagedAuthConfiguration()).toThrow();
      expect((await login()).status).toBe(503);
      ENV[property] = saved;
    }
  });
});
