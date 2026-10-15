import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./sdk", () => ({ sdk: { authenticateRequest: vi.fn() } }));

const managedAuth = vi.hoisted(() => ({ enabled: false, authenticate: vi.fn() }));

vi.mock("../fabManagedAuth", () => ({
  isFabManagedAuthEnabled: () => managedAuth.enabled,
  authenticateFabManagedRequest: managedAuth.authenticate,
}));

import { ENV } from "./env";
import { createContext } from "./context";
import { sdk } from "./sdk";

const authenticateRequest = vi.mocked(sdk.authenticateRequest);
const originalLocalMode = ENV.fabOperatorLocalMode;

function options(remoteAddress: string, hostname: string) {
  return {
    req: { socket: { remoteAddress }, hostname },
    res: {},
    info: {} as never,
  };
}

describe("createContext authentication", () => {
  beforeEach(() => {
    managedAuth.enabled = false;
    managedAuth.authenticate.mockReset();
    authenticateRequest.mockReset();
    ENV.fabOperatorLocalMode = true;
  });

  afterEach(() => {
    ENV.fabOperatorLocalMode = originalLocalMode;
  });

  it("does not verify irrelevant session cookies for a loopback operator", async () => {
    const context = await createContext(options("127.0.0.1", "127.0.0.1"));

    expect(authenticateRequest).not.toHaveBeenCalled();
    expect(context.user).toBeNull();
  });

  it("still authenticates a non-loopback request outside managed mode", async () => {
    authenticateRequest.mockResolvedValue({ id: 7, role: "admin" } as never);

    const context = await createContext(options("203.0.113.10", "example.test"));

    expect(authenticateRequest).toHaveBeenCalledOnce();
    expect(context.user).toMatchObject({ id: 7, role: "admin" });
  });

  it("uses managed sessions instead of legacy OAuth cookies in managed deployments", async () => {
    const legacyUser = { id: 1, role: "admin" } as never;
    managedAuth.enabled = true;
    managedAuth.authenticate.mockResolvedValue(null);
    authenticateRequest.mockResolvedValue(legacyUser);

    const context = await createContext(options("203.0.113.8", "fab.example.test"));

    expect(context.user).toBeNull();
    expect(managedAuth.authenticate).toHaveBeenCalledOnce();
    expect(authenticateRequest).not.toHaveBeenCalled();
  });

  it("preserves the configured OAuth authentication flow outside managed deployments", async () => {
    const oauthUser = { id: 2, role: "admin" } as never;
    authenticateRequest.mockResolvedValue(oauthUser);

    const context = await createContext(options("203.0.113.8", "fab.example.test"));

    expect(context.user).toBe(oauthUser);
    expect(managedAuth.authenticate).not.toHaveBeenCalled();
    expect(authenticateRequest).toHaveBeenCalledOnce();
  });
});
