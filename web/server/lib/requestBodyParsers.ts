import express, { type Express } from "express";
import type { RequestHandler } from "express";
import { createPublicFormRateLimitMiddleware } from "./publicFormRateLimit";

export function registerFabRequestBodyParsers(
  app: Express,
  standardLimiter: RequestHandler,
): void {
  const parsePublicFormBody: RequestHandler = (req, res, next) => {
    express.json({ limit: "64kb" })(req, res, (jsonError?: unknown) => {
      if (jsonError) {
        next(jsonError);
        return;
      }
      express.urlencoded({ limit: "64kb", extended: true })(req, res, next);
    });
  };

  app.use(
    "/api/trpc",
    createPublicFormRateLimitMiddleware(standardLimiter, parsePublicFormBody),
  );
  app.use(express.json({ limit: "10mb" }));
  app.use(express.urlencoded({ limit: "10mb", extended: true }));
}
