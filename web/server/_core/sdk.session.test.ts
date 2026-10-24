import { createHash } from "node:crypto";
import { SignJWT, decodeJwt } from "jose";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const database = vi.hoisted(() => ({
  createOAuthSession: vi.fn(),
  isOAuthSessionActive: vi.fn(),
  revokeOAuthSession: vi.fn(),
}));

vi.mock("../db", () => database);

import { sdk } from "./sdk";
import { ENV } from "./env";
import { COOKIE_NAME } from "@shared/const";

const secret = "oauth-session-test-secret-with-at-least-32-characters";
let previousSecret: string;
let previousAppId: string;

beforeEach(() => {
  previousSecret = ENV.cookieSecret;
  previousAppId = ENV.appId;
  ENV.cookieSecret = secret;
  ENV.appId = "fab-session-test";
  database.createOAuthSession.mockReset().mockResolvedValue(undefined);
  database.isOAuthSessionActive.mockReset().mockResolvedValue(true);
  database.revokeOAuthSession.mockReset().mockResolvedValue(undefined);
});

afterEach(() => {
  ENV.cookieSecret = previousSecret;
  ENV.appId = previousAppId;
});

describe("OAuth session revocation", () => {
  it("persists only a hash of a random session id before returning the cookie token", async () => {
    const token = await sdk.createSessionToken("user-1", {
      name: "Sample User",
      expiresInMs: 60_000,
    });
    const { jti, exp } = decodeJwt(token);
    const expectedHash = createHash("sha256").update(String(jti)).digest("hex");

    expect(database.createOAuthSession).toHaveBeenCalledWith({
      tokenHash: expectedHash,
      openId: "user-1",
      expiresAt: new Date(Number(exp) * 1_000),
    });
    expect(expectedHash).not.toBe(jti);
    await expect(sdk.verifySession(token)).resolves.toMatchObject({
      openId: "user-1",
      appId: "fab-session-test",
      name: "Sample User",
    });
    expect(database.isOAuthSessionActive).toHaveBeenCalledWith(expectedHash, "user-1");
  });

  it("rejects a copied token after the corresponding session is revoked", async () => {
    const token = await sdk.createSessionToken("user-2", { expiresInMs: 60_000 });
    const jti = String(decodeJwt(token).jti);
    const tokenHash = createHash("sha256").update(jti).digest("hex");
    const req = { headers: { cookie: `${COOKIE_NAME}=${token}` } } as import("express").Request;

    await sdk.revokeSessionFromRequest(req);
    expect(database.revokeOAuthSession).toHaveBeenCalledWith(tokenHash);
    database.isOAuthSessionActive.mockResolvedValue(false);
    await expect(sdk.verifySession(token)).resolves.toBeNull();
  });

  it("rejects pre-revocation JWTs without a server-issued session id", async () => {
    const legacyToken = await new SignJWT({
      openId: "user-3",
      appId: "fab-session-test",
      name: "Legacy User",
    })
      .setProtectedHeader({ alg: "HS256", typ: "JWT" })
      .setExpirationTime("1h")
      .sign(new TextEncoder().encode(secret));

    await expect(sdk.verifySession(legacyToken)).resolves.toBeNull();
    expect(database.isOAuthSessionActive).not.toHaveBeenCalled();
  });

  it("does not issue a session token if durable registration fails", async () => {
    database.createOAuthSession.mockRejectedValue(new Error("database unavailable"));

    await expect(sdk.createSessionToken("user-4")).rejects.toThrow("database unavailable");
  });
});
