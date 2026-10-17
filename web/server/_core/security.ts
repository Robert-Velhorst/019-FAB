import type { RequestHandler } from "express";
import helmet from "helmet";

const ONE_YEAR_SECONDS = 31_536_000;

const CONTENT_SECURITY_POLICY_DIRECTIVES = {
  defaultSrc: ["'self'"],
  baseUri: ["'self'"],
  connectSrc: ["'self'"],
  fontSrc: ["'self'", "data:"],
  formAction: ["'self'"],
  frameAncestors: ["'none'"],
  frameSrc: ["'self'"],
  imgSrc: ["'self'", "data:", "blob:", "https:"],
  objectSrc: ["'none'"],
  scriptSrc: ["'self'"],
  scriptSrcAttr: ["'none'"],
  styleSrc: ["'self'", "'unsafe-inline'"],
};

export function createFabSecurityMiddleware(production: boolean): RequestHandler[] {
  const headers = helmet({
    contentSecurityPolicy: {
      useDefaults: false,
      directives: CONTENT_SECURITY_POLICY_DIRECTIVES,
      reportOnly: !production,
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
