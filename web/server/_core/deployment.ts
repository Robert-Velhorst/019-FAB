import { closeSync, constants, fstatSync, openSync, readSync } from "node:fs";
import { isIP } from "node:net";
import { isAbsolute } from "node:path";
import type { Express } from "express";

export type FabDeploymentProfile = "local" | "windows" | "vm";
type Environment = Record<string, string | undefined>;
const MAX_SECRET_BYTES = 65_536;
const MAX_API_TOKEN_BYTES = 8_192;

export function resolveSecret(source: Environment, name: string, maxBytes = MAX_SECRET_BYTES): string {
  const fileName = `${name}_FILE`;
  const file = source[fileName];
  if (file === undefined) return source[name] ?? "";
  if (source[name] !== undefined) {
    throw new Error(`${name} and ${fileName} must not both be configured`);
  }

  let descriptor: number | undefined;
  try {
    // Bounded descriptor reads avoid special files and unbounded allocations.
    descriptor = openSync(file, constants.O_RDONLY | constants.O_NONBLOCK);
    const stat = fstatSync(descriptor);
    if (!stat.isFile() || stat.size > maxBytes) throw new Error();
    const buffer = Buffer.alloc(maxBytes + 1);
    let size = 0;
    while (size < buffer.length) {
      const count = readSync(descriptor, buffer, size, buffer.length - size, null);
      if (count === 0) break;
      size += count;
    }
    const value = new TextDecoder("utf-8", { fatal: true })
      .decode(buffer.subarray(0, size)).replace(/\r?\n$/, "");
    if (size > maxBytes || !value.trim() || /[\x00-\x1f\x7f]/.test(value)) {
      throw new Error();
    }
    return value;
  } catch {
    // Do not attach the filesystem error: it can contain a sensitive path.
    throw new Error(`${fileName} must reference a readable, nonempty UTF-8 secret file (maximum ${maxBytes} bytes, one line)`);
  } finally {
    if (descriptor !== undefined) closeSync(descriptor);
  }
}

function requireStrongSecret(name: string, value: string, minDistinct = 10): void {
  const normalized = value.toLowerCase().replace(/[^a-z0-9]/g, "");
  if (
    value.length < 32 || value.length > MAX_SECRET_BYTES || /\s|[\x00-\x1f\x7f]/.test(value)
    || new Set(value).size < minDistinct || /^(.{1,32})\1+$/.test(value)
    || /changeme|replaceme|replacewith|yoursecret|yourtoken|placeholder|development|defaultsecret|defaulttoken|example|password/.test(normalized)
  ) {
    throw new Error(`${name} requires a non-placeholder, high-entropy secret of at least 32 characters in windows/vm profiles`);
  }
}

function resolveApiToken(source: Environment, name: string): string {
  const file = source[`${name}_FILE`];
  if (file !== undefined && !isAbsolute(file)) {
    throw new Error(`${name}_FILE must reference an absolute secret-file path`);
  }
  const value = resolveSecret(source, name, MAX_API_TOKEN_BYTES);
  if (value && (value.length > MAX_API_TOKEN_BYTES || /[^\x21-\x7e]/.test(value))) {
    throw new Error(`${name} requires printable ASCII without whitespace, at most 8192 characters`);
  }
  return value;
}

function requireHttpsOrigin(name: string, value: string | undefined): string {
  try {
    const url = new URL(value ?? "");
    if (url.protocol !== "https:" || url.username || url.password
      || url.pathname !== "/" || url.search || url.hash
      || !value || value !== value.trim() || /[\\\s]/.test(value)) throw new Error();
    return url.origin;
  } catch {
    throw new Error(`${name} must be an explicit HTTPS origin without credentials, path, query, or fragment in vm profile`);
  }
}

export function parseFabPort(value: string = "3000"): number {
  if (!/^[0-9]+$/.test(value) || Number(value) < 1 || Number(value) > 65_535) {
    throw new Error("PORT must be an integer from 1 to 65535");
  }
  return Number(value);
}

export function parseFabTrustedProxyAddresses(value: string = ""): string[] {
  if (!value.trim()) return [];
  const addresses = value.split(",").map(address => address.trim().toLowerCase());
  if (addresses.some(address => !isIP(address) || address.includes("%"))) {
    throw new Error("FAB_OPERATOR_TRUSTED_PROXY_ADDRESSES must contain only comma-separated IP addresses (no ranges, hostnames, or aliases)");
  }
  return Array.from(new Set(addresses));
}

export function configureFabProxyTrust(app: Express, addresses: readonly string[]): void {
  const validated = parseFabTrustedProxyAddresses(addresses.join(","));
  // Express compiles individual IPs, including equivalent IPv4-mapped IPv6 forms.
  app.set("trust proxy", validated.length ? validated : false);
}

export function readFabDeployment(source: Environment) {
  const profile = source.FAB_DEPLOYMENT_PROFILE ?? "local";
  if (profile !== "local" && profile !== "windows" && profile !== "vm") {
    throw new Error("FAB_DEPLOYMENT_PROFILE must be local, windows, or vm");
  }
  const host = source.FAB_WEB_HOST ?? "127.0.0.1";
  if (!host || /[\s\\/:]/.test(host) && !isIP(host)
    || (!isIP(host) && !/^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/i.test(host))) {
    throw new Error("FAB_WEB_HOST must be a nonempty IP address or hostname without a scheme or port");
  }
  if (profile === "windows" && !["127.0.0.1", "localhost", "::1"].includes(host.toLowerCase())) {
    throw new Error("FAB_WEB_HOST must be loopback in windows profile (127.0.0.1, localhost, or ::1)");
  }
  const fabLocalApiToken = resolveApiToken(source, "FAB_LOCAL_API_TOKEN");
  const fabOperationsServiceToken = resolveApiToken(source, "FAB_OPERATIONS_SERVICE_TOKEN");
  const cookieSecret = resolveSecret(source, "JWT_SECRET");
  if (profile !== "local") {
    requireStrongSecret("FAB_LOCAL_API_TOKEN", fabLocalApiToken, 12);
    requireStrongSecret("FAB_OPERATIONS_SERVICE_TOKEN", fabOperationsServiceToken, 12);
    requireStrongSecret("JWT_SECRET", cookieSecret);
  }

  return {
    fabDeploymentProfile: profile as FabDeploymentProfile,
    isProduction: source.NODE_ENV === "production" || profile !== "local",
    fabWebPort: parseFabPort(source.PORT),
    fabWebHost: host,
    fabOperatorPublicOrigin: profile === "vm"
      ? requireHttpsOrigin("FAB_OPERATOR_PUBLIC_ORIGIN", source.FAB_OPERATOR_PUBLIC_ORIGIN)
      : source.FAB_OPERATOR_PUBLIC_ORIGIN ?? "",
    fabLocalApiPublicUrl: profile === "vm"
      ? requireHttpsOrigin("FAB_LOCAL_API_PUBLIC_URL", source.FAB_LOCAL_API_PUBLIC_URL)
      : source.FAB_LOCAL_API_PUBLIC_URL ?? source.FAB_LOCAL_API_URL ?? "http://127.0.0.1:5001",
    fabOperatorTrustedProxyAddresses: parseFabTrustedProxyAddresses(source.FAB_OPERATOR_TRUSTED_PROXY_ADDRESSES),
    fabAllowPortFallback: profile === "local" && source.NODE_ENV === "development",
    fabLocalApiToken,
    fabOperationsServiceToken,
    cookieSecret,
  };
}
