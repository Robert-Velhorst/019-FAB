import { createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import express, { type Application, type Request, type Response } from "express";
import { parse } from "cookie";
import { jwtVerify, SignJWT } from "jose";
import rateLimit from "express-rate-limit";
import type { User } from "../drizzle/schema";
import { ENV } from "./_core/env";
import { resolveSecret } from "./_core/deployment";

export const FAB_MANAGED_COOKIE_NAME = "__Host-fab_operator";
export const FAB_MANAGED_SESSION_TTL_SECONDS = 15 * 60;
export const FAB_MANAGED_LOGIN_PATH = "/operator/login";
const SUBJECT = "fab-single-business-administrator";
const AUDIENCE = "fab-managed-operator";
const ISSUER = "fab-standalone";
const MAX_SECRET_BYTES = 4_096;
const MAX_SESSIONS = 1_000;
const cookieOptions = { httpOnly: true, secure: true, sameSite: "strict" as const, path: "/" };

// One process, one business. Bounded issuance records make logout/restart revoke
// cookies, without introducing a user database or retaining credentials in sessions.
const sessions = new Map<string, number>();
const consumedHandoffs = new Map<string, number>();
let sessionKey: Buffer | undefined;

export function isFabManagedAuthEnabled(): boolean {
  return process.env.FAB_DEPLOYMENT_PROFILE?.trim().toLowerCase() === "vm"
    || Boolean(process.env.FAB_OPERATOR_ACCESS_TOKEN || process.env.FAB_OPERATOR_ACCESS_TOKEN_FILE);
}

function equalSecret(left: string, right: string): boolean {
  return timingSafeEqual(createHash("sha256").update(left).digest(), createHash("sha256").update(right).digest());
}

function strongSecret(value: string): boolean {
  return value.length >= 32 && Buffer.byteLength(value) <= MAX_SECRET_BYTES
    && !/\s/.test(value) && new Set(value).size >= 12
    && !/change.?me|replace.?me|placeholder|example|your[-_ ]?(secret|token)/i.test(value);
}

function operatorSecret(): string {
  // Preserve empty optional environment values and reread for rotation/revocation.
  return resolveSecret({
    FAB_OPERATOR_ACCESS_TOKEN: process.env.FAB_OPERATOR_ACCESS_TOKEN || undefined,
    FAB_OPERATOR_ACCESS_TOKEN_FILE: process.env.FAB_OPERATOR_ACCESS_TOKEN_FILE || undefined,
  }, "FAB_OPERATOR_ACCESS_TOKEN", MAX_SECRET_BYTES);
}

function configuration() {
  const accessToken = operatorSecret();
  // Startup owns JWT_SECRET/_FILE resolution. Do not reread the raw environment
  // here: doing so would bypass its mounted-secret validation and precedence.
  const signingSecret = ENV.cookieSecret;
  if (!strongSecret(accessToken) || !strongSecret(signingSecret)
    || [signingSecret, ENV.fabLocalApiToken, ENV.fabOperationsServiceToken]
      .some(secret => Boolean(secret) && equalSecret(accessToken, secret))) {
    throw new Error("FAB operator access and JWT signing secrets must be strong and distinct");
  }
  let publicOrigin: URL;
  try {
    publicOrigin = new URL(process.env.FAB_OPERATOR_PUBLIC_ORIGIN || "");
    if (publicOrigin.protocol !== "https:" || publicOrigin.username || publicOrigin.password
      || publicOrigin.pathname !== "/" || publicOrigin.search || publicOrigin.hash) throw new Error();
  } catch {
    throw new Error("FAB_OPERATOR_PUBLIC_ORIGIN must be a clean HTTPS origin");
  }
  const key = createHmac("sha256", signingSecret)
    .update("fab-managed-session:v1\0").update(accessToken).digest();
  if (sessionKey && !timingSafeEqual(sessionKey, key)) { sessions.clear(); consumedHandoffs.clear(); }
  sessionKey = key;
  return { accessToken, key, publicOrigin };
}

export function validateFabManagedAuthConfiguration(): void {
  if (isFabManagedAuthEnabled()) configuration();
}

function safeRequest(req: Request, publicOrigin: URL, mutation = !["GET", "HEAD", "OPTIONS"].includes(req.method)): boolean {
  const peer = String(req.socket?.remoteAddress || "").toLowerCase().replace(/^::ffff:/, "");
  const trustedProxy = ENV.fabOperatorTrustedProxyAddresses.some(address =>
    address.toLowerCase().replace(/^::ffff:/, "") === peer);
  const directTls = (req.socket as typeof req.socket & { encrypted?: boolean })?.encrypted === true;
  // Check the actual peer, not a hop count or a client-supplied forwarded chain.
  if (!directTls && !(trustedProxy && req.headers?.["x-forwarded-proto"] === "https")) return false;
  const forwardedHost = trustedProxy ? req.headers?.["x-forwarded-host"] : undefined;
  const host = forwardedHost ?? req.headers?.host;
  if (typeof host !== "string" || host.toLowerCase() !== publicOrigin.host) return false;
  const sourceOrigin = req.headers?.origin;
  if ((mutation || sourceOrigin !== undefined) && sourceOrigin !== publicOrigin.origin) return false;
  const fetchSite = req.headers?.["sec-fetch-site"];
  if (fetchSite !== undefined && fetchSite !== "same-origin" && fetchSite !== "none") return false;
  return true;
}

export function isFabManagedRequestSafe(req: Request, mutation?: boolean): boolean {
  try { return safeRequest(req, configuration().publicOrigin, mutation); } catch { return false; }
}

function pruneSessions() {
  const now = Math.floor(Date.now() / 1_000);
  sessions.forEach((expires, id) => { if (expires <= now) sessions.delete(id); });
}

async function verifySession(req: Request) {
  try {
    const config = configuration();
    if (!safeRequest(req, config.publicOrigin)) return null;
    const raw = req.headers.cookie || "";
    if (raw.length > 16_384) return null;
    const token = parse(raw)[FAB_MANAGED_COOKIE_NAME];
    if (!token || token.length > 4_096) return null;
    const now = Math.floor(Date.now() / 1_000);
    const { payload } = await jwtVerify(token, config.key, {
      algorithms: ["HS256"], typ: "JWT", issuer: ISSUER, audience: AUDIENCE, subject: SUBJECT,
      requiredClaims: ["iat", "exp", "jti", "sub", "aud", "iss"],
      currentDate: new Date(now * 1_000), maxTokenAge: FAB_MANAGED_SESSION_TTL_SECONDS,
    });
    if (payload.aud !== AUDIENCE || payload.role !== "admin" || payload.v !== 1
      || !Number.isSafeInteger(payload.iat) || !Number.isSafeInteger(payload.exp)
      || payload.iat! > now || payload.exp! <= now || payload.exp! <= payload.iat!
      || payload.exp! - payload.iat! > FAB_MANAGED_SESSION_TTL_SECONDS
      || typeof payload.jti !== "string" || sessions.get(payload.jti) !== payload.exp) return null;
    return payload;
  } catch {
    return null;
  }
}

export async function authenticateFabManagedRequest(req: Request): Promise<User | null> {
  const session = await verifySession(req);
  if (!session) return null;
  const issuedAt = new Date(session.iat! * 1_000);
  return {
    id: -1, openId: SUBJECT, role: "admin", name: "FAB administrator", email: null,
    loginMethod: "fab-operator-secret", createdAt: issuedAt, updatedAt: issuedAt,
    lastSignedIn: issuedAt, stripeCustomerId: null,
  };
}

export async function getFabManagedSessionBinding(req: Request): Promise<{ id: string; exp: number } | null> {
  const payload = await verifySession(req);
  return payload ? { id: payload.jti!, exp: payload.exp! } : null;
}

export function fabParentStatusCredential(token: string): string {
  return createHmac("sha256", token).update("fab-managed-parent-status:v1").digest("base64url");
}

function securityHeaders(res: Response) {
  res.set({
    // Same-origin form POSTs need their Origin; cross-origin referrers stay private.
    "cache-control": "no-store", pragma: "no-cache", "referrer-policy": "same-origin",
    "x-content-type-options": "nosniff", "x-frame-options": "DENY",
    "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
  });
}

function formPage(res: Response, logout: boolean, status = 200) {
  securityHeaders(res);
  const title = logout ? "Sign out" : "Sign in";
  const error = status === 401 ? '<p role="alert">Access denied. Check the operator access secret and try again.</p>' : "";
  res.status(status).type("html").send(`<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>${title} | FAB</title>
<style>*{box-sizing:border-box}body{margin:0;background:#f4f6f7;color:#18252c;font:16px system-ui,sans-serif;letter-spacing:0}main{width:min(100%,440px);margin:12vh auto;padding:24px}header{display:flex;align-items:center;gap:12px;border-bottom:2px solid #14765e;padding-bottom:20px}img{width:40px;height:40px}h1{font-size:24px;margin:0}h2{font-size:20px;margin-top:28px}label{display:block;margin:24px 0 8px}input,button{font:inherit;min-height:44px;width:100%;border-radius:4px}input{border:1px solid #64777f;padding:10px}button{margin-top:20px;background:#146e58;color:white;border:0;padding:10px;cursor:pointer}a{display:inline-block;margin-top:20px;color:#155f8a}input:focus-visible,button:focus-visible,a:focus-visible{outline:3px solid #296ed4;outline-offset:3px}[role=alert]{color:#a01826;overflow-wrap:anywhere}</style></head>
<body><main><header><img src="/fab-mark.svg" alt=""><h1>FAB</h1></header><h2>${title}</h2>${error}
<form method="post" action="/operator/${logout ? "logout" : "login"}">${logout ? "" : '<label for="accessToken">Operator access secret</label><input id="accessToken" name="accessToken" type="password" autocomplete="current-password" required maxlength="4096" autofocus>'}
<button type="submit">${title}</button></form>${logout ? '<a href="/admin/operations">Back to dashboard</a>' : ""}</main></body></html>`);
}

export async function logoutFabManagedOperator(req: Request, res: Response): Promise<boolean> {
  if (!isFabManagedRequestSafe(req, true)) return false;
  const session = await verifySession(req);
  if (session?.jti) sessions.delete(session.jti);
  res.setHeader("cache-control", "no-store");
  res.clearCookie(FAB_MANAGED_COOKIE_NAME, cookieOptions);
  return true;
}

/** Register before general body parsers, gateway routes, tRPC, and SPA/static serving. */
export function registerFabManagedAuthRoutes(app: Application): void {
  if (!isFabManagedAuthEnabled()) return;
  validateFabManagedAuthConfiguration();
  // Backend-only authority check, independent of browser origin/cookie authentication.
  app.post("/api/fab/operator-session/status", (req, res, next) => {
    res.set({ "cache-control": "no-store", "referrer-policy": "no-referrer" });
    if (!ENV.fabLocalApiToken || !equalSecret(req.headers.authorization || "", `Bearer ${fabParentStatusCredential(ENV.fabLocalApiToken)}`)) {
      res.sendStatus(403); return;
    }
    next();
  }, express.json({ limit: "2kb" }), (req, res) => {
    try { configuration(); } catch { res.json({ active: false }); return; }
    const id = req.body?.id;
    const now = Math.floor(Date.now() / 1_000);
    const exp = typeof id === "string" && /^[A-Za-z0-9_-]{32}$/.test(id) ? sessions.get(id) : undefined;
    if (!exp || exp <= now) { res.json({ active: false }); return; }
    if (req.body.nonce !== undefined) {
      const nonce = req.body.nonce;
      const ticketExpiry = req.body.exp;
      if (typeof nonce !== "string" || !/^[A-Za-z0-9_-]{16,96}$/.test(nonce)
        || !Number.isSafeInteger(ticketExpiry) || ticketExpiry <= now
        || ticketExpiry > now + 45 || ticketExpiry > exp) { res.json({ active: false }); return; }
      consumedHandoffs.forEach((expiry, key) => { if (expiry <= now) consumedHandoffs.delete(key); });
      const key = `${id}:${nonce}`;
      if (consumedHandoffs.has(key) || consumedHandoffs.size >= 10_000) { res.json({ active: false }); return; }
      consumedHandoffs.set(key, ticketExpiry);
    }
    res.json({ active: true, exp });
  });
  // Deny before the SPA redirect/fallback, including in the standalone server
  // where the legacy OAuth router is not registered at all.
  app.all("/api/oauth/callback", (_req, res) => {
    res.setHeader("cache-control", "no-store");
    res.sendStatus(404);
  });
  const loginLimiter = rateLimit({
    windowMs: 15 * 60 * 1_000, limit: 10, standardHeaders: "draft-7", legacyHeaders: false,
    // The actual connection peer is stable and cannot be rotated by spoofing XFF.
    // A single-business reverse proxy intentionally shares this bounded limit.
    keyGenerator: req => req.socket.remoteAddress || "unknown", validate: false,
    message: { error: "Too many sign-in attempts. Try again later." },
  });
  app.use(["/operator/login", "/operator/logout"], (req, res, next) => {
    securityHeaders(res);
    try { configuration(); } catch {
      res.status(503).send("FAB operator authentication is not configured");
      return;
    }
    if (!isFabManagedRequestSafe(req)) {
      res.status(403).send("A same-origin HTTPS request is required");
      return;
    }
    next();
  });
  app.get(FAB_MANAGED_LOGIN_PATH, async (req, res) => {
    if (await authenticateFabManagedRequest(req)) { res.redirect(303, "/admin/operations"); return; }
    formPage(res, false);
  });
  app.post(FAB_MANAGED_LOGIN_PATH, loginLimiter, express.urlencoded({ extended: false, limit: "8kb", parameterLimit: 2 }), async (req, res) => {
    const value = req.body?.accessToken;
    const config = configuration();
    if (typeof value !== "string" || Buffer.byteLength(value) > MAX_SECRET_BYTES || !equalSecret(value, config.accessToken)) {
      formPage(res, false, 401);
      return;
    }
    const previous = await verifySession(req);
    if (previous?.jti) sessions.delete(previous.jti);
    pruneSessions();
    while (sessions.size >= MAX_SESSIONS) sessions.delete(sessions.keys().next().value!);
    const now = Math.floor(Date.now() / 1_000);
    const expires = now + FAB_MANAGED_SESSION_TTL_SECONDS;
    const id = randomBytes(24).toString("base64url");
    const token = await new SignJWT({ role: "admin", v: 1 })
      .setProtectedHeader({ alg: "HS256", typ: "JWT" }).setIssuer(ISSUER).setAudience(AUDIENCE)
      .setSubject(SUBJECT).setIssuedAt(now).setExpirationTime(expires).setJti(id).sign(config.key);
    sessions.set(id, expires);
    res.cookie(FAB_MANAGED_COOKIE_NAME, token, { ...cookieOptions, maxAge: FAB_MANAGED_SESSION_TTL_SECONDS * 1_000 });
    res.redirect(303, "/admin/operations");
  });
  app.get("/operator/logout", (_req, res) => formPage(res, true));
  app.post("/operator/logout", async (req, res) => {
    if (!await logoutFabManagedOperator(req, res)) { res.status(403).send("A same-origin HTTPS request is required"); return; }
    res.redirect(303, FAB_MANAGED_LOGIN_PATH);
  });
  // Only known API routes bypass the SPA guard; their own authorization checks
  // run next. Unknown API/asset paths must not reach the static HTML fallback.
  app.use(async (req, res, next) => {
    if (/^\/api\/trpc(?:\/|$)/.test(req.path)
      || /^\/api\/fab\/source\/[^/]+\/?$/.test(req.path)
      || ["/api/fab/runtime", "/api/fab/operator-session"].includes(req.path)
      || ["/fab-mark.svg", "/favicon.ico", "/robots.txt"].includes(req.path)) { next(); return; }
    res.setHeader("cache-control", "no-store");
    res.setHeader("referrer-policy", "no-referrer");
    if (!isFabManagedRequestSafe(req)) { res.status(403).send("A same-origin HTTPS request is required"); return; }
    if (!await authenticateFabManagedRequest(req)) { res.redirect(303, FAB_MANAGED_LOGIN_PATH); return; }
    if (req.path === "/") { res.redirect(303, "/admin/operations"); return; }
    next();
  });
}
