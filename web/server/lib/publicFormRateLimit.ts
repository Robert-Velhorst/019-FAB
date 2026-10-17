import type { RequestHandler } from "express";

const PUBLIC_FORM_PROCEDURES = new Set(["waitlist.join", "contact.submit"]);
const TRPC_PREFIX = "/api/trpc/";

export function createPublicFormRateLimitMiddleware(
  standardLimiter: RequestHandler,
  parseBody?: RequestHandler,
): RequestHandler {
  return (req, res, next) => {
    let procedurePath: string;
    try {
      procedurePath = decodeURIComponent(
        new URL(req.originalUrl, "http://fab.invalid").pathname,
      );
    } catch {
      res.status(400).json({ error: "Malformed tRPC procedure path." });
      return;
    }

    if (!procedurePath.toLowerCase().startsWith(TRPC_PREFIX)) {
      next();
      return;
    }
    if (/%[0-9a-f]{2}/i.test(procedurePath)) {
      res.status(400).json({ error: "Nested encoding in tRPC procedure paths is not allowed." });
      return;
    }

    const procedures = procedurePath
      .slice(TRPC_PREFIX.length)
      .replace(/\/$/, "")
      .split(",");
    const containsPublicForm = procedures.some((procedure) =>
      PUBLIC_FORM_PROCEDURES.has(procedure),
    );

    if (!containsPublicForm) {
      next();
      return;
    }
    if (procedures.length !== 1) {
      res.status(400).json({
        error: "Public form submissions cannot be batched.",
      });
      return;
    }

    standardLimiter(req, res, (error?: unknown) => {
      if (error) {
        next(error);
        return;
      }
      if (req.method === "POST" && parseBody) {
        parseBody(req, res, next);
        return;
      }
      next();
    });
  };
}
