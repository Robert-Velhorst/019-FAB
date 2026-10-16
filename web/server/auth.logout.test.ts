import { describe, expect, it, vi } from "vitest";
import { appRouter } from "./routers";
import { COOKIE_NAME } from "../shared/const";
import type { TrpcContext } from "./_core/context";
import { ENV } from "./_core/env";
import { FAB_MANAGED_COOKIE_NAME } from "./fabManagedAuth";

type CookieCall = {
  name: string;
  options: Record<string, unknown>;
};

type AuthenticatedUser = NonNullable<TrpcContext["user"]>;

function createAuthContext(): { ctx: TrpcContext; clearedCookies: CookieCall[] } {
  const clearedCookies: CookieCall[] = [];

  const user: AuthenticatedUser = {
    id: 1,
    openId: "sample-user",
    email: "sample@example.com",
    name: "Sample User",
    loginMethod: "manus",
    role: "user",
    createdAt: new Date(),
    updatedAt: new Date(),
    lastSignedIn: new Date(),
  };

  const ctx: TrpcContext = {
    user,
    req: {
      protocol: "https",
      headers: {},
    } as TrpcContext["req"],
    res: {
      setHeader: () => undefined,
      clearCookie: (name: string, options: Record<string, unknown>) => {
        clearedCookies.push({ name, options });
      },
    } as TrpcContext["res"],
  };

  return { ctx, clearedCookies };
}

describe("auth.logout", () => {
  it("clears the session cookie and reports success", async () => {
    const { ctx, clearedCookies } = createAuthContext();
    const caller = appRouter.createCaller(ctx);

    const result = await caller.auth.logout();

    expect(result).toEqual({ success: true });
    expect(clearedCookies).toHaveLength(1);
    expect(clearedCookies[0]?.name).toBe(COOKIE_NAME);
    expect(clearedCookies[0]?.options).toMatchObject({
      maxAge: -1,
      secure: true,
      sameSite: "none",
      httpOnly: true,
      path: "/",
    });
  });

  it("revokes the managed operator cookie instead of clearing only the app cookie", async () => {
    const { ctx, clearedCookies } = createAuthContext();
    const previousCookieSecret = ENV.cookieSecret;
    vi.stubEnv("FAB_DEPLOYMENT_PROFILE", "vm");
    vi.stubEnv("FAB_OPERATOR_ACCESS_TOKEN", "operator-secret-for-test-0123456789abcdef");
    vi.stubEnv("FAB_OPERATOR_PUBLIC_ORIGIN", "https://fab.example.test");
    ENV.cookieSecret = "jwt-secret-for-test-0123456789abcdef-0123456789";
    ctx.req = {
      protocol: "https",
      hostname: "fab.example.test",
      headers: { host: "fab.example.test", origin: "https://fab.example.test" },
      socket: { encrypted: true, remoteAddress: "127.0.0.1" },
    } as TrpcContext["req"];

    try {
      const result = await appRouter.createCaller(ctx).auth.logout();
      expect(result).toEqual({ success: true });
      expect(clearedCookies).toHaveLength(1);
      expect(clearedCookies[0]?.name).toBe(FAB_MANAGED_COOKIE_NAME);
    } finally {
      ENV.cookieSecret = previousCookieSecret;
      vi.unstubAllEnvs();
    }
  });

  it("does not treat the managed FAB operator as a general application administrator", async () => {
    const { ctx } = createAuthContext();
    ctx.user = { ...ctx.user!, role: "admin", loginMethod: "fab-operator-secret" };

    await expect(appRouter.createCaller(ctx).waitlist.list()).rejects.toMatchObject({
      code: "FORBIDDEN",
    });
  });
});
