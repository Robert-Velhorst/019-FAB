import { COOKIE_NAME, ONE_YEAR_MS } from "@shared/const";
import type { Express, Request, Response } from "express";
import { createHash, randomBytes, timingSafeEqual } from "node:crypto";
import { parse } from "cookie";
import rateLimit from "express-rate-limit";
import * as db from "../db";
import { isFabManagedAuthEnabled } from "../fabManagedAuth";
import { createLogger } from "../lib/logger";
import { ENV } from "./env";
import { sdk } from "./sdk";

const log = createLogger("OAuth");
const TRANSACTION_TTL_MS = 10 * 60 * 1_000;
const MAX_TRANSACTIONS = 1_000;
const TOKEN_PATTERN = /^[A-Za-z0-9_-]{43}$/;
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

type LoginTransaction = { browserHash: Buffer; redirectUri: string; expiresAt: number };

function oauthConfiguration() {
  const portal = new URL(process.env.VITE_OAUTH_PORTAL_URL || "");
  const redirectUri = process.env.OAUTH_REDIRECT_URI || "";
  const callback = new URL(redirectUri);
  if (!ENV.appId || portal.protocol !== "https:" || portal.username || portal.password
    || portal.search || portal.hash || portal.pathname !== "/"
    || /[\\\s]/.test(redirectUri) || callback.username || callback.password
    || callback.pathname !== "/api/oauth/callback" || callback.search || callback.hash
    || !(callback.protocol === "https:"
      || (callback.protocol === "http:" && LOOPBACK_HOSTS.has(callback.hostname)))) {
    throw new Error("Invalid OAuth configuration");
  }
  return { portal, callback, redirectUri };
}

function matchesCallbackOrigin(req: Request, callback: URL) {
  // Express uses forwarded values only through the startup's explicit proxy trust.
  const host = req.host;
  if (typeof host !== "string" || /[\\/?#@\s]/.test(host)) return false;
  try { return new URL(`${req.protocol}://${host}`).origin === callback.origin; } catch { return false; }
}

function browserCookie(req: Request, name: string) {
  const raw = req.headers.cookie || "";
  if (raw.length > 16_384) return undefined;
  const copies = raw.split(";").filter(value => value.trim().startsWith(`${name}=`));
  const value = parse(raw)[name];
  return copies.length === 1 && value && TOKEN_PATTERN.test(value) ? value : undefined;
}

function transactionCookie(callback: URL) {
  const secure = callback.protocol === "https:";
  return {
    name: secure ? "__Host-fab_oauth" : "fab_oauth_local",
    options: { httpOnly: true, secure, sameSite: "lax" as const, path: "/" },
  };
}

function getQueryParam(req: Request, key: string): string | undefined {
  const value = req.query[key];
  return typeof value === "string" ? value : undefined;
}

export function registerOAuthRoutes(app: Express) {
  // Process-local, bounded transactions: restart invalidates outstanding logins.
  const transactions = new Map<string, LoginTransaction>();
  const prune = () => {
    const now = Date.now();
    transactions.forEach((transaction, state) => {
      if (transaction.expiresAt <= now) transactions.delete(state);
    });
  };
  const startLimiter = rateLimit({
    windowMs: TRANSACTION_TTL_MS, limit: 20,
    standardHeaders: "draft-7", legacyHeaders: false,
    message: { error: "Too many sign-in attempts. Try again later." },
  });
  app.get("/api/oauth/start", startLimiter, (req, res) => {
    res.set({ "cache-control": "no-store", "referrer-policy": "no-referrer" });
    if (isFabManagedAuthEnabled()) { res.redirect(303, "/operator/login"); return; }
    let config: ReturnType<typeof oauthConfiguration>;
    try { config = oauthConfiguration(); } catch {
      res.status(503).json({ error: "OAuth login is not configured" }); return;
    }
    if (!matchesCallbackOrigin(req, config.callback)) { res.sendStatus(403); return; }
    prune();
    if (transactions.size >= MAX_TRANSACTIONS) {
      res.status(429).json({ error: "Too many pending logins. Try again later." }); return;
    }
    const state = randomBytes(32).toString("base64url");
    const cookie = transactionCookie(config.callback);
    const previousBrowser = browserCookie(req, cookie.name);
    const previousHash = previousBrowser ? createHash("sha256").update(previousBrowser).digest() : undefined;
    let knownBrowser = false;
    transactions.forEach(transaction => {
      if (previousHash && timingSafeEqual(transaction.browserHash, previousHash)) knownBrowser = true;
    });
    // Reuse only a server-issued, still-pending browser binding across login tabs.
    const browser = knownBrowser ? previousBrowser! : randomBytes(32).toString("base64url");
    transactions.set(state, {
      browserHash: createHash("sha256").update(browser).digest(),
      redirectUri: config.redirectUri, expiresAt: Date.now() + TRANSACTION_TTL_MS,
    });
    res.cookie(cookie.name, browser, { ...cookie.options, maxAge: TRANSACTION_TTL_MS });
    const url = new URL("/app-auth", config.portal);
    url.searchParams.set("appId", ENV.appId);
    url.searchParams.set("redirectUri", config.redirectUri);
    url.searchParams.set("state", state);
    url.searchParams.set("type", "signIn");
    res.redirect(302, url.href);
  });

  app.get("/api/oauth/callback", async (req: Request, res: Response) => {
    res.set({ "cache-control": "no-store", "referrer-policy": "no-referrer" });
    if (isFabManagedAuthEnabled()) {
      res.setHeader("cache-control", "no-store");
      res.sendStatus(404);
      return;
    }
    const code = getQueryParam(req, "code");
    const state = getQueryParam(req, "state");

    if (!code || code.length > 4_096 || !state || !TOKEN_PATTERN.test(state)) {
      res.status(400).json({ error: "Invalid or expired login. Start sign-in again." });
      return;
    }

    let config: ReturnType<typeof oauthConfiguration>;
    try { config = oauthConfiguration(); } catch {
      res.status(503).json({ error: "OAuth login is not configured" }); return;
    }
    if (!matchesCallbackOrigin(req, config.callback)) { res.sendStatus(403); return; }
    prune();
    const transaction = transactions.get(state);
    const cookie = transactionCookie(config.callback);
    const browser = browserCookie(req, cookie.name);
    if (!transaction || transaction.redirectUri !== config.redirectUri || !browser
      || !timingSafeEqual(transaction.browserHash, createHash("sha256").update(browser).digest())) {
      res.status(400).json({ error: "Invalid or expired login. Start sign-in again." }); return;
    }
    // Consume before the first await, so concurrent callbacks cannot exchange twice.
    transactions.delete(state);
    // Let the short-lived binding cookie expire; another login tab can still need it.

    try {
      // The existing SDK expects base64(callback URI), not the public state nonce.
      const tokenResponse = await sdk.exchangeCodeForToken(code, Buffer.from(transaction.redirectUri).toString("base64"));
      const userInfo = await sdk.getUserInfo(tokenResponse.accessToken);

      if (!userInfo.openId) {
        res.status(400).json({ error: "openId missing from user info" });
        return;
      }

      await db.upsertUser({
        openId: userInfo.openId,
        name: userInfo.name || null,
        email: userInfo.email ?? null,
        loginMethod: userInfo.loginMethod ?? userInfo.platform ?? null,
        lastSignedIn: new Date(),
      });

      const sessionToken = await sdk.createSessionToken(userInfo.openId, {
        name: userInfo.name || "",
        expiresInMs: ONE_YEAR_MS,
      });

      res.cookie(COOKIE_NAME, sessionToken, { ...cookie.options, maxAge: ONE_YEAR_MS });

      res.redirect(302, "/");
    } catch (error) {
      log.error(
        "OAuth callback failed",
        {},
        error instanceof Error ? error : new Error(String(error)),
      );
      res.status(500).json({ error: "OAuth callback failed" });
    }
  });
}
