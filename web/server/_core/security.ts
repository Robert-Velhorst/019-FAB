import type { RequestHandler } from "express";
import helmet from "helmet";

const ONE_YEAR_SECONDS = 31_536_000;
const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export function createCookieMutationOriginGuard(cookieName: string): RequestHandler {
  return (req, res, next) => {
    const hasSessionCookie = String(req.headers.cookie || "")
      .split(";")
      .some(cookie => cookie.trim().startsWith(`${cookieName}=`));
    if (!MUTATING_METHODS.has(req.method.toUpperCase()) || !hasSessionCookie) {
      next();
      return;
    }

    const host = req.get("host");
    const origin = req.get("origin");
    const fetchSite = req.get("sec-fetch-site");
    let expectedOrigin: string;
    try {
      if (!host || /[\\/?#@\s]/.test(host)) throw new Error("invalid host");
      expectedOrigin = new URL(`${req.protocol}://${host}`).origin;
    } catch {
      res.status(403).json({ error: "A same-origin request is required" });
      return;
    }

    if (origin !== expectedOrigin || (fetchSite && fetchSite !== "same-origin" && fetchSite !== "none")) {
      res.status(403).json({ error: "A same-origin request is required" });
      return;
    }
    next();
  };
}

export function createFabSecurityMiddleware(production: boolean): RequestHandler[] {
  const headers = helmet({
    contentSecurityPolicy: {
      directives: {
        defaultSrc: ["'self'"],
        baseUri: ["'self'"],
        connectSrc: production ? ["'self'"] : ["'self'", "ws:", "wss:"],
        fontSrc: ["'self'", "data:"],
        formAction: ["'self'"],
        frameAncestors: ["'none'"],
        frameSrc: ["'self'"],
        imgSrc: ["'self'", "data:", "blob:", "https:"],
        objectSrc: ["'none'"],
        scriptSrc: production ? ["'self'"] : ["'self'", "'unsafe-eval'"],
        scriptSrcAttr: ["'none'"],
        styleSrc: ["'self'", "'unsafe-inline'"],
        upgradeInsecureRequests: null,
      },
    },
    crossOriginEmbedderPolicy: false,
    strictTransportSecurity: false,
  });
  const strictTransport = helmet.strictTransportSecurity({
    maxAge: ONE_YEAR_SECONDS,
    includeSubDomains: true,
  });
  const conditionalStrictTransport: RequestHandler = (req, res, next) => {
    if (!production || !req.secure) {
      next();
      return;
    }
    strictTransport(req, res, next);
  };
  return [headers, conditionalStrictTransport];
}
