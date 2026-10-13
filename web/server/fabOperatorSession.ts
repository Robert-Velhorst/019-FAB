import { createHmac, randomBytes } from "crypto";
import type { Application, RequestHandler, Response } from "express";
import { ENV } from "./_core/env";
import { getFabBrowserApiBaseUrl } from "./fabLocalGateway";
import { FAB_MANAGED_LOGIN_PATH, getFabManagedSessionBinding, isFabManagedAuthEnabled, isFabManagedRequestSafe } from "./fabManagedAuth";
import {
  resolveFabOperatorAccess,
  type FabOperatorAccessOptions,
} from "./fabOperatorAccess";

export const FAB_OPERATOR_SESSION_AUDIENCE = "fab-local-operator-session";
export const FAB_OPERATOR_SESSION_TTL_SECONDS = 45;
export const MAX_FAB_OPERATOR_TARGET_LENGTH = 2_048;

type FabOperatorSessionOptions = FabOperatorAccessOptions & {
  now?: () => number;
  nonce?: () => string;
  publicBaseUrl?: string;
  token?: string;
};

type FabOperatorSessionPayload = {
  actor: string;
  aud: typeof FAB_OPERATOR_SESSION_AUDIENCE;
  exp: number;
  iat: number;
  nonce: string;
  next: string;
  v: 1 | 2;
  parent?: { id: string; exp: number };
};

function sessionError(res: Response, status: number, error: string) {
  res.setHeader("cache-control", "no-store");
  res.setHeader("pragma", "no-cache");
  res.setHeader("referrer-policy", "no-referrer");
  res.status(status).json({ error });
}

export function normalizeFabOperatorTarget(value: unknown): string | null {
  if (typeof value !== "string" || value.length === 0 || value.length > MAX_FAB_OPERATOR_TARGET_LENGTH) {
    return null;
  }
  if (!value.startsWith("/") || value.startsWith("//") || /[\\\u0000-\u001f\u007f]/.test(value)) {
    return null;
  }

  let decoded = value;
  try {
    for (let index = 0; index < 2; index += 1) {
      const nextDecoded = decodeURIComponent(decoded);
      if (nextDecoded === decoded) break;
      decoded = nextDecoded;
    }
  } catch {
    return null;
  }
  if (!decoded.startsWith("/") || decoded.startsWith("//") || /[\\\u0000-\u001f\u007f]/.test(decoded)) {
    return null;
  }

  try {
    const placeholder = new URL("https://fab.invalid");
    const parsed = new URL(value, placeholder);
    if (parsed.origin !== placeholder.origin || !parsed.pathname.startsWith("/")) {
      return null;
    }
    const decodedPath = new URL(decoded, placeholder).pathname.toLowerCase();
    if (decodedPath === "/operator/session/bootstrap" || decodedPath.startsWith("/operator/session/bootstrap/")) {
      return null;
    }
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return null;
  }
}

export function createFabOperatorSessionTicket(
  payload: FabOperatorSessionPayload,
  token: string,
): string {
  const encodedPayload = Buffer.from(JSON.stringify(payload), "utf8").toString("base64url");
  const signature = createHmac("sha256", token).update(encodedPayload).digest("base64url");
  return `${encodedPayload}.${signature}`;
}

export function registerFabOperatorSessionRoutes(
  app: Application,
  rateLimiter?: RequestHandler,
  options: FabOperatorSessionOptions = {},
) {
  const handlers: RequestHandler[] = [];
  if (rateLimiter) handlers.push(rateLimiter);

  handlers.push(async (req, res) => {
    const access = await resolveFabOperatorAccess(req, options);
    if (!access.allowed || !access.actor) {
      if (isFabManagedAuthEnabled() && isFabManagedRequestSafe(req) && req.headers.accept?.includes("text/html")) {
        res.setHeader("cache-control", "no-store");
        res.setHeader("referrer-policy", "no-referrer");
        res.redirect(303, FAB_MANAGED_LOGIN_PATH);
        return;
      }
      sessionError(res, 403, "FAB operator access is required");
      return;
    }

    const next = normalizeFabOperatorTarget(req.query.next);
    if (!next) {
      sessionError(res, 400, "A safe FAB destination is required");
      return;
    }

    const token = options.token ?? ENV.fabLocalApiToken;
    if (!token) {
      sessionError(res, 503, "The protected FAB ledger is not configured");
      return;
    }

    let publicBaseUrl: URL;
    try {
      publicBaseUrl = getFabBrowserApiBaseUrl(options.publicBaseUrl);
    } catch {
      sessionError(res, 500, "The FAB browser service address is invalid");
      return;
    }

    const issuedAt = Math.floor((options.now?.() ?? Date.now()) / 1_000);
    const managed = isFabManagedAuthEnabled();
    const parent = managed ? await getFabManagedSessionBinding(req) : null;
    if (managed && !parent) { sessionError(res, 403, "FAB operator access is required"); return; }
    const ticket = createFabOperatorSessionTicket({
      actor: access.actor,
      aud: FAB_OPERATOR_SESSION_AUDIENCE,
      exp: Math.min(issuedAt + FAB_OPERATOR_SESSION_TTL_SECONDS, parent?.exp ?? Infinity),
      iat: issuedAt,
      nonce: options.nonce?.() ?? randomBytes(18).toString("base64url"),
      next,
      v: parent ? 2 : 1,
      ...(parent ? { parent } : {}),
    }, token);
    const destination = new URL("/operator/session/bootstrap", publicBaseUrl);
    destination.searchParams.set("ticket", ticket);

    res.setHeader("cache-control", "no-store");
    res.setHeader("pragma", "no-cache");
    res.setHeader("referrer-policy", "no-referrer");
    res.redirect(302, destination.toString());
  });

  app.get("/api/fab/operator-session", ...handlers);
}
