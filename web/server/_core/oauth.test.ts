import { randomBytes } from "node:crypto";
import express from "express";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  exchangeCodeForToken: vi.fn(), getUserInfo: vi.fn(), createSessionToken: vi.fn(), upsertUser: vi.fn(),
}));
vi.mock("./sdk", () => ({ sdk: mocks }));
vi.mock("../db", () => ({ upsertUser: mocks.upsertUser }));

import { ENV } from "./env";
import { registerOAuthRoutes } from "./oauth";
import { registerFabManagedAuthRoutes } from "../fabManagedAuth";
import { configureFabProxyTrust } from "./deployment";

const originalEnv = { ...ENV };
const origin = "https://fab.example.test";
const accessToken = randomBytes(32).toString("base64url");
const cleanups: Array<() => Promise<void>> = [];

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubEnv("FAB_DEPLOYMENT_PROFILE", "vm");
  vi.stubEnv("FAB_OPERATOR_ACCESS_TOKEN", accessToken);
  vi.stubEnv("FAB_OPERATOR_ACCESS_TOKEN_FILE", "");
  vi.stubEnv("FAB_OPERATOR_PUBLIC_ORIGIN", origin);
  ENV.cookieSecret = randomBytes(32).toString("base64url");
  ENV.fabOperatorTrustedProxyAddresses = ["127.0.0.1"];
  ENV.appId = "test-app";
  vi.stubEnv("VITE_OAUTH_PORTAL_URL", "https://identity.example.test");
  vi.stubEnv("OAUTH_REDIRECT_URI", `${origin}/api/oauth/callback`);
  mocks.exchangeCodeForToken.mockResolvedValue({ accessToken: "test-oauth-token" });
  mocks.getUserInfo.mockResolvedValue({ openId: "legacy-user", name: "Legacy user" });
  mocks.createSessionToken.mockResolvedValue("test-legacy-session");
});

afterEach(async () => {
  for (const close of cleanups.splice(0)) await close();
  Object.assign(ENV, originalEnv);
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

async function server(withManagedRoutes = true) {
  const app = express();
  configureFabProxyTrust(app, ENV.fabOperatorTrustedProxyAddresses);
  if (withManagedRoutes) registerFabManagedAuthRoutes(app);
  registerOAuthRoutes(app);
  app.get("/admin/operations", (_req, res) => res.send("authenticated dashboard"));
  const listener = app.listen(0, "127.0.0.1");
  await new Promise<void>(resolve => listener.once("listening", resolve));
  cleanups.push(() => new Promise<void>((resolve, reject) => {
    listener.close(error => error ? reject(error) : resolve());
    listener.closeAllConnections();
  }));
  const address = listener.address();
  if (!address || typeof address === "string") throw new Error("Expected TCP address");
  const baseUrl = `http://127.0.0.1:${address.port}`;
  return Object.assign((path: string, init: RequestInit = {}) => fetch(`${baseUrl}${path}`, {
    ...init, redirect: "manual", headers: {
      "x-forwarded-host": "fab.example.test", "x-forwarded-proto": "https", ...init.headers,
    },
  }), { baseUrl });
}

function expectNoOAuthSideEffects() {
  expect(mocks.exchangeCodeForToken).not.toHaveBeenCalled();
  expect(mocks.getUserInfo).not.toHaveBeenCalled();
  expect(mocks.createSessionToken).not.toHaveBeenCalled();
  expect(mocks.upsertUser).not.toHaveBeenCalled();
}

describe("legacy OAuth callback deployment boundary", () => {
  it("returns 404 before SDK exchange in managed mode even without the SPA guard", async () => {
    const request = await server(false);
    for (const path of ["/api/oauth/callback", "/api/oauth/callback?code=test-code&state=unbound-state"]) {
      const response = await request(path);
      expect(response.status).toBe(404);
      expect(response.headers.get("set-cookie")).toBeNull();
    }
    expectNoOAuthSideEffects();
  });

  it.each([false, true])("returns 404 with actual managed registration, authenticated=%s", async authenticated => {
    const request = await server();
    let cookie = "";
    if (authenticated) {
      const login = await request("/operator/login", { method: "POST", headers: {
        origin, "content-type": "application/x-www-form-urlencoded",
      }, body: new URLSearchParams({ accessToken }) });
      expect(login.status).toBe(303);
      cookie = login.headers.get("set-cookie")!.split(";")[0];
      expect((await request("/admin/operations", { headers: { cookie } })).status).toBe(200);
    }
    const response = await request("/api/oauth/callback?code=test-code&state=unbound-state", { headers: { cookie } });
    expect(response.status).toBe(404);
    expect(response.headers.get("set-cookie")).toBeNull();
    expectNoOAuthSideEffects();
  });

  it("rejects browser-unbound legacy state before any account/session side effects", async () => {
    vi.stubEnv("FAB_DEPLOYMENT_PROFILE", "local");
    vi.stubEnv("FAB_OPERATOR_ACCESS_TOKEN", "");
    const request = await server();
    expect((await request("/api/oauth/callback")).status).toBe(400);
    expectNoOAuthSideEffects();
    const response = await request("/api/oauth/callback?code=test-code&state=existing-state");
    expect(response.status).toBe(400);
    expectNoOAuthSideEffects();
  });
});

describe("browser-bound OAuth transactions", () => {
  beforeEach(() => {
    vi.stubEnv("FAB_DEPLOYMENT_PROFILE", "local");
    vi.stubEnv("FAB_OPERATOR_ACCESS_TOKEN", "");
  });

  async function start(request: Awaited<ReturnType<typeof server>>) {
    const response = await request("/api/oauth/start");
    expect(response.status).toBe(302);
    const location = new URL(response.headers.get("location")!);
    const cookie = response.headers.get("set-cookie")!;
    expect(location.origin).toBe("https://identity.example.test");
    expect(location.pathname).toBe("/app-auth");
    expect(location.searchParams.get("redirectUri")).toBe(`${origin}/api/oauth/callback`);
    expect(location.searchParams.get("appId")).toBe("test-app");
    expect(cookie).toContain("HttpOnly");
    expect(cookie).toContain("Secure");
    expect(cookie).toContain("SameSite=Lax");
    expect(cookie).toContain("Max-Age=600");
    expect(cookie).not.toContain("Domain=");
    expect(response.headers.get("cache-control")).toBe("no-store");
    return { cookie: cookie.split(";")[0], state: location.searchParams.get("state")! };
  }

  it("uses unpredictable state and consumes a matching browser challenge once", async () => {
    const request = await server();
    const first = await start(request);
    const second = await start(request);
    expect(first.state).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(second.state).not.toBe(first.state);
    expect(first.cookie).not.toContain(first.state);
    const callback = `/api/oauth/callback?code=test-code&state=${first.state}`;
    const response = await request(callback, { headers: { cookie: first.cookie } });
    expect(response.status).toBe(302);
    expect(response.headers.get("set-cookie")).toContain("test-legacy-session");
    expect(mocks.exchangeCodeForToken).toHaveBeenCalledWith("test-code", btoa(`${origin}/api/oauth/callback`));
    expect(mocks.upsertUser).toHaveBeenCalledOnce();
    expect((await request(callback, { headers: { cookie: first.cookie } })).status).toBe(400);
    expect(mocks.exchangeCodeForToken).toHaveBeenCalledOnce();
  });

  it("keeps two sign-in tabs working in the same browser cookie jar", async () => {
    const request = await server();
    const first = await start(request);
    const secondStart = await request("/api/oauth/start", { headers: { cookie: first.cookie } });
    const cookie = secondStart.headers.get("set-cookie")!.split(";")[0];
    expect(cookie).toBe(first.cookie);
    const secondState = new URL(secondStart.headers.get("location")!).searchParams.get("state");
    for (const state of [first.state, secondState]) {
      const response = await request(`/api/oauth/callback?code=test-code&state=${state}`, { headers: { cookie } });
      expect(response.status).toBe(302);
      expect(response.headers.get("set-cookie")).not.toContain("__Host-fab_oauth=;");
    }
    expect(mocks.exchangeCodeForToken).toHaveBeenCalledTimes(2);
  });

  it("normalizes explicit default HTTPS ports on initiation and callback", async () => {
    const request = await server();
    const response = await request("/api/oauth/start", { headers: { "x-forwarded-host": "fab.example.test:443" } });
    expect(response.status).toBe(302);
    const state = new URL(response.headers.get("location")!).searchParams.get("state");
    const cookie = response.headers.get("set-cookie")!.split(";")[0];
    expect((await request(`/api/oauth/callback?code=test-code&state=${state}`, {
      headers: { cookie, "x-forwarded-host": "fab.example.test:443" },
    })).status).toBe(302);
  });

  it("limits repeated starts from one client without exhausting another client's login", async () => {
    const request = await server();
    for (let count = 0; count < 20; count++) {
      expect((await request("/api/oauth/start")).status).toBe(302);
    }
    expect((await request("/api/oauth/start")).status).toBe(429);
    expect((await request("/api/oauth/start", { headers: { "x-forwarded-for": "192.0.2.4" } })).status).toBe(302);
  });

  it("rejects a different browser without consuming the legitimate challenge", async () => {
    const request = await server();
    const alice = await start(request);
    const bob = await start(request);
    const callback = `/api/oauth/callback?code=test-code&state=${alice.state}`;
    for (const cookie of ["", bob.cookie]) {
      expect((await request(callback, { headers: { cookie } })).status).toBe(400);
    }
    expectNoOAuthSideEffects();
    expect((await request(callback, { headers: { cookie: alice.cookie } })).status).toBe(302);
  });

  it("rejects expired challenges without exchanging the code", async () => {
    const request = await server();
    const transaction = await start(request);
    const now = Date.now();
    vi.spyOn(Date, "now").mockReturnValue(now + 601_000);
    const response = await request(`/api/oauth/callback?code=test-code&state=${transaction.state}`, {
      headers: { cookie: transaction.cookie },
    });
    expect(response.status).toBe(400);
    expectNoOAuthSideEffects();
  });

  it("consumes a valid challenge before a failing token exchange", async () => {
    const request = await server();
    const transaction = await start(request);
    mocks.exchangeCodeForToken.mockRejectedValueOnce(new Error("Provider unavailable"));
    const callback = `/api/oauth/callback?code=test-code&state=${transaction.state}`;
    expect((await request(callback, { headers: { cookie: transaction.cookie } })).status).toBe(500);
    expect((await request(callback, { headers: { cookie: transaction.cookie } })).status).toBe(400);
    expect(mocks.exchangeCodeForToken).toHaveBeenCalledOnce();
    expect(mocks.upsertUser).not.toHaveBeenCalled();
  });

  it("rejects wrong hosts and untrusted forwarded transport", async () => {
    const request = await server();
    expect((await request("/api/oauth/start", { headers: { "x-forwarded-host": "attacker.example" } })).status).toBe(403);
    expectNoOAuthSideEffects();
    ENV.fabOperatorTrustedProxyAddresses = [];
    const untrusted = await server();
    expect((await untrusted("/api/oauth/start")).status).toBe(403);
  });

  it("rejects duplicate query state and cookie values", async () => {
    const request = await server();
    const transaction = await start(request);
    const callback = `/api/oauth/callback?code=test-code&state=${transaction.state}`;
    expect((await request(`${callback}&state=${transaction.state}`, {
      headers: { cookie: transaction.cookie },
    })).status).toBe(400);
    expect((await request(callback, {
      headers: { cookie: `${transaction.cookie}; ${transaction.cookie}` },
    })).status).toBe(400);
    expectNoOAuthSideEffects();
  });

  it("allows only one of two concurrent matching callbacks to exchange", async () => {
    const request = await server();
    const transaction = await start(request);
    const callback = `/api/oauth/callback?code=test-code&state=${transaction.state}`;
    const responses = await Promise.all([
      request(callback, { headers: { cookie: transaction.cookie } }),
      request(callback, { headers: { cookie: transaction.cookie } }),
    ]);
    expect(responses.map(response => response.status).sort()).toEqual([302, 400]);
    expect(mocks.exchangeCodeForToken).toHaveBeenCalledOnce();
  });

  it("supports a configured loopback HTTP callback with browser-usable cookies", async () => {
    ENV.fabOperatorTrustedProxyAddresses = [];
    const request = await server();
    vi.stubEnv("OAUTH_REDIRECT_URI", `${request.baseUrl}/api/oauth/callback`);
    const response = await request("/api/oauth/start");
    expect(response.status).toBe(302);
    const cookie = response.headers.get("set-cookie")!;
    expect(cookie).toContain("fab_oauth_local=");
    expect(cookie).toContain("SameSite=Lax");
    expect(cookie).not.toContain("Secure");
    const location = new URL(response.headers.get("location")!);
    const callback = await request(`/api/oauth/callback?code=test-code&state=${location.searchParams.get("state")}`, {
      headers: { cookie: cookie.split(";")[0] },
    });
    expect(callback.status).toBe(302);
    expect(callback.headers.get("set-cookie")).toContain("test-legacy-session");
    expect(callback.headers.get("set-cookie")).toContain("SameSite=Lax");
    expect(callback.headers.get("set-cookie")).not.toContain("Secure");
  });

  it.each(["", "http://fab.example.test/api/oauth/callback", "https://fab.example.test/other", `${origin}/api/oauth/callback?next=evil`])(
    "fails closed for invalid callback configuration %s", async redirectUri => {
      vi.stubEnv("OAUTH_REDIRECT_URI", redirectUri);
      const request = await server();
      const response = await request("/api/oauth/start");
      expect(response.status).toBe(503);
      expect(response.headers.get("set-cookie")).toBeNull();
      expectNoOAuthSideEffects();
    },
  );

  it("bounds pending transactions without evicting legitimate challenges", async () => {
    const request = await server();
    const transaction = await start(request);
    for (let index = 1; index < 1_000; index++) {
      const response = await request("/api/oauth/start", {
        headers: { "x-forwarded-for": `192.0.2.${index % 250 + 1}` },
      });
      expect(response.status).toBe(302);
      await response.text();
    }
    expect((await request("/api/oauth/start")).status).toBe(429);
    const callback = await request(`/api/oauth/callback?code=test-code&state=${transaction.state}`, {
      headers: { cookie: transaction.cookie },
    });
    expect(callback.status).toBe(302);
    expect((await request("/api/oauth/start")).status).toBe(302);
  }, 30_000);
});
