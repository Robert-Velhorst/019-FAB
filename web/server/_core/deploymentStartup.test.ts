import { spawn, type ChildProcess } from "node:child_process";
import { once } from "node:events";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import path from "node:path";
import { build } from "esbuild";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const root = path.resolve(import.meta.dirname, "../..");
let directory: string;
const children = new Set<ChildProcess>();

beforeAll(async () => {
  // Keep generated bundles under node_modules so external dependencies resolve.
  directory = mkdtempSync(path.join(root, "node_modules", "fab-startup-test-"));
  mkdirSync(path.join(directory, "public"));
  writeFileSync(path.join(directory, "public", "index.html"), "<!doctype html><title>Startup fixture</title>");
  await build({
    absWorkingDir: root,
    entryPoints: { main: "server/_core/index.ts", standalone: "server/fabStandalone.ts" },
    outdir: directory,
    outExtension: { ".js": ".mjs" },
    bundle: true,
    platform: "node",
    format: "esm",
    packages: "external",
    banner: { js: "import { createRequire } from 'node:module'; const require = createRequire(import.meta.url);" },
  });
}, 30_000);

afterAll(async () => {
  for (const child of Array.from(children)) {
    const stopped = once(child, "close");
    child.kill();
    await stopped;
  }
  if (directory) rmSync(directory, { recursive: true, force: true });
});

async function run(entry: string, overrides: NodeJS.ProcessEnv) {
  const env = { ...process.env };
  for (const key of Object.keys(env)) {
    if (key.startsWith("FAB_") || ["JWT_SECRET", "JWT_SECRET_FILE", "DATABASE_URL", "OAUTH_SERVER_URL"].includes(key)) delete env[key];
  }
  const child = spawn(process.execPath, [path.join(directory, `${entry}.mjs`)], {
    cwd: directory,
    windowsHide: true,
    env: {
      ...env,
      NODE_ENV: "production",
      DOTENV_CONFIG_PATH: path.join(directory, "absent.env"),
      FAB_WEB_HOST: "127.0.0.1",
      FAB_DEPLOYMENT_PROFILE: "local",
      ...overrides,
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  children.add(child);
  let output = "";
  child.stdout!.on("data", chunk => { output += chunk; });
  child.stderr!.on("data", chunk => { output += chunk; });
  const timer = setTimeout(() => child.kill(), 30_000);
  try {
    const [code, signal] = await once(child, "close");
    return { code, signal, output };
  } finally {
    clearTimeout(timer);
    children.delete(child);
  }
}

describe.each(["main", "standalone"])("%s production startup", entry => {
  it("serves its runtime identity and denies anonymous operations in production", async () => {
    const reservation = createServer();
    reservation.listen(0, "127.0.0.1");
    await once(reservation, "listening");
    const port = (reservation.address() as AddressInfo).port;
    await new Promise<void>(resolve => reservation.close(() => resolve()));
    const env = { ...process.env };
    for (const key of Object.keys(env)) {
      if (key.startsWith("FAB_") || ["JWT_SECRET", "JWT_SECRET_FILE", "DATABASE_URL", "OAUTH_SERVER_URL"].includes(key)) delete env[key];
    }
    const child = spawn(process.execPath, [path.join(directory, `${entry}.mjs`)], {
      cwd: directory, windowsHide: true, stdio: ["ignore", "pipe", "pipe"],
      env: { ...env, NODE_ENV: "production", DOTENV_CONFIG_PATH: path.join(directory, "absent.env"),
        FAB_WEB_HOST: "127.0.0.1", FAB_DEPLOYMENT_PROFILE: "local", PORT: String(port),
        FAB_INSTANCE_ROOT: directory, FAB_LOCAL_API_URL: "http://127.0.0.1:9" },
    });
    children.add(child);
    const closed = once(child, "close");
    let output = "";
    child.stdout!.on("data", chunk => { output += chunk; });
    child.stderr!.on("data", chunk => { output += chunk; });
    try {
      const base = `http://127.0.0.1:${port}`;
      const deadline = Date.now() + 20_000;
      let identity: Response | undefined;
      while (Date.now() < deadline && child.exitCode === null && child.signalCode === null) {
        try {
          identity = await fetch(`${base}/api/fab/runtime`, { signal: AbortSignal.timeout(1_000) });
          break;
        } catch {
          await new Promise(resolve => setTimeout(resolve, 50));
        }
      }
      expect(identity?.status, output).toBe(200);
      expect(await identity!.json()).toMatchObject({ service: "fab-operator-dashboard", apiVersion: "1",
        localApiEndpoint: "http://127.0.0.1:9" });
      const denied = await fetch(`${base}/api/trpc/fab.access`, { signal: AbortSignal.timeout(1_000) });
      expect(denied.status).toBe(403);
      const page = await fetch(`${base}/admin/operations`, { signal: AbortSignal.timeout(1_000) });
      expect(page.status).toBe(200);
      expect(await page.text()).toContain("Startup fixture");
    } finally {
      if (child.exitCode === null && child.signalCode === null) child.kill();
      await closed;
      children.delete(child);
    }
  }, 30_000);

  it("exits nonzero on a real port conflict instead of selecting another port", async () => {
    const occupied = createServer();
    occupied.listen(0, "127.0.0.1");
    await once(occupied, "listening");
    try {
      const result = await run(entry, { PORT: String((occupied.address() as AddressInfo).port) });
      expect(result.code, result.output).toBe(1);
      expect(result.signal).toBeNull();
      expect(result.output).toContain("EADDRINUSE");
      expect(result.output).toContain("configured port");
    } finally {
      await new Promise<void>(resolve => occupied.close(() => resolve()));
    }
  }, 35_000);

  it("rejects an invalid port before listening", async () => {
    const result = await run(entry, { PORT: "3000invalid" });
    expect(result.code).toBe(1);
    expect(result.output).toContain("PORT must be an integer");
  }, 35_000);

  it("fails closed on conflicting secret sources without logging values or paths", async () => {
    const result = await run(entry, {
      JWT_SECRET: "do-not-log-this-inline-secret",
      JWT_SECRET_FILE: path.join(directory, "do-not-log-this-secret-path"),
    });
    expect(result.code).toBe(1);
    expect(result.output).toContain("JWT_SECRET and JWT_SECRET_FILE");
    expect(result.output).not.toContain("do-not-log-this");
  }, 35_000);
});
