import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const secretNames = ["FAB_LOCAL_API_TOKEN", "FAB_OPERATIONS_SERVICE_TOKEN", "JWT_SECRET"];
const strongSecret = "ebd65f4b2fc31407a0d25babc55d416a8ac690d2c654e31a";
let directory: string;

beforeEach(() => {
  directory = mkdtempSync(join(tmpdir(), "fab-web-deployment-"));
  for (const name of [
    ...secretNames, ...secretNames.map(name => `${name}_FILE`),
    "FAB_DEPLOYMENT_PROFILE", "FAB_WEB_HOST", "FAB_OPERATOR_PUBLIC_ORIGIN",
    "FAB_LOCAL_API_URL", "FAB_LOCAL_API_PUBLIC_URL", "PORT",
    "FAB_OPERATOR_TRUSTED_PROXY_ADDRESSES",
  ]) vi.stubEnv(name, undefined);
  vi.stubEnv("NODE_ENV", "test");
  vi.resetModules();
});

afterEach(() => {
  vi.unstubAllEnvs();
  rmSync(directory, { recursive: true, force: true });
});

async function environment() {
  return (await import("./env")).ENV;
}

function production(profile: "windows" | "vm") {
  vi.stubEnv("FAB_DEPLOYMENT_PROFILE", profile);
  for (const name of secretNames) vi.stubEnv(name, strongSecret);
  vi.stubEnv("FAB_LOCAL_API_PUBLIC_URL", "https://ledger.example.test");
  vi.stubEnv("FAB_OPERATOR_PUBLIC_ORIGIN", "https://fab.example.test");
}

describe("FAB deployment preflight", () => {
  it.each(["FAB_LOCAL_API_TOKEN", "FAB_OPERATIONS_SERVICE_TOKEN"])(
    "accepts the 8192-byte shared API boundary for inline and file %s", async name => {
      const value = strongSecret + "Z".repeat(8192 - strongSecret.length);
      const property = name === "FAB_LOCAL_API_TOKEN" ? "fabLocalApiToken" : "fabOperationsServiceToken";
      vi.stubEnv(name, value);
      expect((await environment())[property]).toBe(value);
      vi.stubEnv(name, undefined);
      const file = join(directory, "secret");
      writeFileSync(file, value);
      vi.stubEnv(`${name}_FILE`, file);
      vi.resetModules();
      expect((await environment())[property]).toBe(value);
    },
  );
  it.each(["FAB_LOCAL_API_TOKEN", "FAB_OPERATIONS_SERVICE_TOKEN"])(
    "rejects a readable but relative shared API secret path for %s", async name => {
      const file = join(directory, "private-secret-path");
      writeFileSync(file, strongSecret);
      vi.stubEnv(`${name}_FILE`, relative(process.cwd(), file));
      await expect(environment()).rejects.toThrow("absolute secret-file path");
      await expect(environment()).rejects.not.toThrow("private-secret-path");
    },
  );
  it.each(["FAB_LOCAL_API_TOKEN", "FAB_OPERATIONS_SERVICE_TOKEN"])(
    "rejects backend-incompatible inline and file credentials for %s", async name => {
      for (const value of [strongSecret + "\u00e9", strongSecret + " ", "x".repeat(8193)]) {
        vi.stubEnv(name, value);
        vi.resetModules();
        await expect(environment()).rejects.toThrow(name);
        vi.stubEnv(name, undefined);
        const file = join(directory, "private-secret-path");
        writeFileSync(file, value);
        vi.stubEnv(`${name}_FILE`, file);
        vi.resetModules();
        await expect(environment()).rejects.toThrow(name);
        vi.stubEnv(`${name}_FILE`, undefined);
      }
    },
  );

  it("preserves secret-free local operation and defaults to loopback", async () => {
    const env = await environment();
    expect(env.fabDeploymentProfile).toBe("local");
    expect(env.fabWebHost).toBe("127.0.0.1");
    expect(env.fabLocalApiToken).toBe("");
    expect(env.fabWebPort).toBe(3000);
  });

  it.each(secretNames)("resolves %s from a file without exposing it in process.env", async name => {
    const path = join(directory, "secret");
    writeFileSync(path, `${strongSecret}\r\n`);
    vi.stubEnv(`${name}_FILE`, path);
    const env = await environment();
    const property = { FAB_LOCAL_API_TOKEN: "fabLocalApiToken", FAB_OPERATIONS_SERVICE_TOKEN: "fabOperationsServiceToken", JWT_SECRET: "cookieSecret" }[name]!;
    expect(env[property as keyof typeof env]).toBe(strongSecret);
    expect(process.env[name]).toBeUndefined();
  });

  it.each(secretNames)("rejects conflicting %s sources without disclosing either value", async name => {
    const path = join(directory, "sensitive-path");
    writeFileSync(path, strongSecret);
    vi.stubEnv(name, "inline-sensitive-value");
    vi.stubEnv(`${name}_FILE`, path);
    await expect(environment()).rejects.toThrow(`${name} and ${name}_FILE`);
    await expect(environment()).rejects.not.toThrow(/inline-sensitive-value|sensitive-path/);
  });

  it.each(["missing", "empty", "directory", "oversized", "multiline"])("rejects %s secret files with redacted errors", async kind => {
    const path = kind === "directory" ? directory : join(directory, "sensitive-path");
    if (kind === "empty") writeFileSync(path, "\r\n");
    if (kind === "oversized") writeFileSync(path, "a".repeat(65_537));
    if (kind === "multiline") writeFileSync(path, "first\nsecond");
    vi.stubEnv("JWT_SECRET_FILE", path);
    await expect(environment()).rejects.toThrow("JWT_SECRET_FILE");
    await expect(environment()).rejects.not.toThrow(/sensitive-path|first|second/);
  });

  it.each(["windows", "vm"] as const)("accepts configured %s profile", async profile => {
    production(profile);
    const env = await environment();
    expect(env.fabDeploymentProfile).toBe(profile);
    expect(env.isProduction).toBe(true);
    expect(env.fabAllowPortFallback).toBe(false);
  });

  it.each(["production", "test", "development", undefined])("allows port fallback only in local development (%s)", async mode => {
    vi.stubEnv("NODE_ENV", mode);
    const env = await environment();
    expect(env.fabAllowPortFallback).toBe(mode === "development");
  });

  it("allows legacy local production with inline secrets", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("FAB_LOCAL_API_TOKEN", "legacy-local-token");
    expect((await environment()).fabLocalApiToken).toBe("legacy-local-token");
  });

  it.each(["", " ", "http://127.0.0.1", "127.0.0.1\n"])("rejects invalid bind hosts %j", async host => {
    vi.stubEnv("FAB_WEB_HOST", host);
    await expect(environment()).rejects.toThrow("FAB_WEB_HOST");
  });

  it.each(["0.0.0.0", "::", "192.168.1.10", "203.0.113.10", "fab.example.test", "localhost.example.test"])("rejects non-loopback Windows bind host %j", async host => {
    production("windows");
    vi.stubEnv("FAB_WEB_HOST", host);
    await expect(environment()).rejects.toThrow("FAB_WEB_HOST must be loopback in windows profile");
  });

  it.each(["127.0.0.1", "::1", "localhost"])("accepts Windows loopback bind host %j", async host => {
    production("windows");
    vi.stubEnv("FAB_WEB_HOST", host);
    expect((await environment()).fabWebHost).toBe(host);
  });

  it.each(["local", "vm"] as const)("preserves explicit wildcard binding for %s", async profile => {
    if (profile === "vm") production(profile);
    vi.stubEnv("FAB_WEB_HOST", "0.0.0.0");
    expect((await environment()).fabWebHost).toBe("0.0.0.0");
  });

  it("rejects even an empty inline variable when a file is configured", async () => {
    vi.stubEnv("JWT_SECRET", "");
    vi.stubEnv("JWT_SECRET_FILE", join(directory, "secret"));
    await expect(environment()).rejects.toThrow("JWT_SECRET and JWT_SECRET_FILE");
  });

  it.each(secretNames)("requires %s in explicit production profiles", async name => {
    production("windows");
    vi.stubEnv(name, undefined);
    await expect(environment()).rejects.toThrow(name);
  });

  it.each(["", "short", "x".repeat(64), "change-me-to-a-long-random-secret-value", "0123456789abcdef".repeat(4)])("rejects weak/default secrets (%#)", async secret => {
    production("vm");
    vi.stubEnv("JWT_SECRET", secret);
    await expect(environment()).rejects.toThrow("JWT_SECRET");
  });

  it("rejects unknown profiles", async () => {
    vi.stubEnv("FAB_DEPLOYMENT_PROFILE", "staging");
    await expect(environment()).rejects.toThrow("FAB_DEPLOYMENT_PROFILE");
  });

  it.each(["", "0", "65536", "-1", "3000abc", "3.5", "1e3", " 3000 "])("rejects invalid port %j", async port => {
    vi.stubEnv("PORT", port);
    await expect(environment()).rejects.toThrow("PORT");
  });

  it.each(["FAB_OPERATOR_PUBLIC_ORIGIN", "FAB_LOCAL_API_PUBLIC_URL"])("requires a valid HTTPS %s in vm", async name => {
    for (const value of [undefined, "http://127.0.0.1:5001", "https://user:secret@example.test", "https://example.test/path", "https://example.test?secret=x", "https://example.test#fragment", "invalid"]) {
      production("vm");
      vi.stubEnv(name, value);
      vi.resetModules();
      await expect(environment()).rejects.toThrow(name);
    }
  });

  it.each(["loopback", "10.0.0.0/8", "proxy.example.test", "*", "127.0.0.1,,::1"])("rejects non-address proxy allowlists %j", async value => {
    vi.stubEnv("FAB_OPERATOR_TRUSTED_PROXY_ADDRESSES", value);
    await expect(environment()).rejects.toThrow("FAB_OPERATOR_TRUSTED_PROXY_ADDRESSES");
  });
});
