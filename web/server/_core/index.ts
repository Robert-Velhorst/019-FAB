import "dotenv/config";
import express from "express";
import compression from "compression";
import { createServer } from "http";
import { createExpressMiddleware } from "@trpc/server/adapters/express";
import { registerOAuthRoutes } from "./oauth";
import { appRouter } from "../routers";
import { createContext } from "./context";
import { serveStatic } from "./static";
import { webhookLimiter, relaxedLimiter } from "../lib/rateLimiter";
import { createLogger } from "../lib/logger";
import { registerFabOperationsRoutes } from "../fabOperations";
import { registerFabRuntimeRoute } from "../fabRuntime";
import { registerFabManagedAuthRoutes } from "../fabManagedAuth";
import { registerFabSourcePreviewRoutes } from "../fabSourcePreview";
import { registerFabOperatorSessionRoutes } from "../fabOperatorSession";
import { ENV } from "./env";
import { createFabSecurityMiddleware } from "./security";
import { configureFabProxyTrust } from "./deployment";
import { createFabServerLifecycle, listenFabServer } from "./lifecycle";

const log = createLogger("Server");

async function startServer() {
  const app = express();
  const server = createServer(app);
  const lifecycle = createFabServerLifecycle(server);

  configureFabProxyTrust(app, ENV.fabOperatorTrustedProxyAddresses);

  app.use(...createFabSecurityMiddleware(ENV.isProduction));

  // Compress JSON and static responses for remote/ngrok clients. Small
  // responses stay uncompressed to avoid spending CPU for negligible savings.
  app.use(compression({ threshold: 1_024 }));
  registerFabManagedAuthRoutes(app);

  // ── Stripe webhook — BEFORE express.json() for raw body ───────
  app.post(
    "/api/stripe/webhook",
    webhookLimiter,
    express.raw({ type: "application/json" }),
    async (req, res) => {
      try {
        const { handleStripeWebhook } = await import("../stripe/webhook");
        await handleStripeWebhook(req, res);
      } catch (err) {
        log.error("Webhook handler failed", {}, err instanceof Error ? err : new Error(String(err)));
        res.status(500).json({ error: "Webhook handler failed" });
      }
    }
  );

  // ── Body parsers ──────────────────────────────────────────────
  // 10mb limit is generous for form data; file uploads go to S3
  app.use(express.json({ limit: "10mb" }));
  app.use(express.urlencoded({ limit: "10mb", extended: true }));

  registerFabRuntimeRoute(app);

  // ── OAuth callback ────────────────────────────────────────────
  registerOAuthRoutes(app);
  registerFabOperationsRoutes(app, relaxedLimiter);
  registerFabSourcePreviewRoutes(app);
  registerFabOperatorSessionRoutes(app, relaxedLimiter);

  // ── tRPC API with relaxed rate limiting ───────────────────────
  app.use(
    "/api/trpc",
    relaxedLimiter,
    createExpressMiddleware({
      router: appRouter,
      createContext,
    })
  );

  // ── Static / Vite ─────────────────────────────────────────────
  if (process.env.NODE_ENV === "development" && ENV.fabDeploymentProfile === "local") {
    const developmentServer = "./vite";
    const { setupVite } = await import(developmentServer);
    await setupVite(app, server);
  } else {
    serveStatic(app);
  }

  // ── Unhandled rejection / exception safety net ────────────────
  process.on("unhandledRejection", (reason) => {
    log.error("Unhandled promise rejection", {
      reason: reason instanceof Error ? reason.message : String(reason),
    });
  });

  process.on("uncaughtException", (err) => {
    log.error("Uncaught exception — shutting down", {}, err);
    process.exit(1);
  });

  // ── Start listening ───────────────────────────────────────────
  const preferredPort = ENV.fabWebPort;
  const port = await listenFabServer(server, {
    host: ENV.fabWebHost,
    port: preferredPort,
    allowPortFallback: ENV.fabAllowPortFallback,
  });
  lifecycle.installSignalHandlers();

  if (port !== preferredPort) {
    log.info(`Port ${preferredPort} is busy, using port ${port} instead`);
  }

  const displayHost = ENV.fabWebHost === "0.0.0.0" ? "localhost" : ENV.fabWebHost;
  log.info(`Server running on http://${displayHost}:${port}/`);
}

startServer().catch((err) => {
  const log = createLogger("Server");
  log.error("Failed to start server", {}, err);
  process.exit(1);
});
