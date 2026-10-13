import "dotenv/config";
import compression from "compression";
import express from "express";
import { createServer } from "http";
import { createExpressMiddleware } from "@trpc/server/adapters/express";
import { createFabContext } from "./fabContext";
import { registerFabOperatorSessionRoutes } from "./fabOperatorSession";
import { fabStandaloneRouter } from "./fabRouter";
import { registerFabRuntimeRoute } from "./fabRuntime";
import { registerFabManagedAuthRoutes } from "./fabManagedAuth";
import { registerFabSourcePreviewRoutes } from "./fabSourcePreview";
import { ENV } from "./_core/env";
import { createFabSecurityMiddleware } from "./_core/security";
import { serveStatic } from "./_core/static";
import { sanitizeExternalMessage } from "./lib/errorSanitizer";
import { relaxedLimiter } from "./lib/rateLimiter";
import { configureFabProxyTrust } from "./_core/deployment";
import { createFabServerLifecycle, listenFabServer } from "./_core/lifecycle";

export async function startFabStandaloneServer() {
  const app = express();
  const server = createServer(app);
  const lifecycle = createFabServerLifecycle(server);
  configureFabProxyTrust(app, ENV.fabOperatorTrustedProxyAddresses);
  app.use(...createFabSecurityMiddleware(ENV.isProduction));
  app.use(compression({ threshold: 1_024 }));
  registerFabManagedAuthRoutes(app);
  app.use(express.json({ limit: "10mb" }));
  app.use(express.urlencoded({ limit: "10mb", extended: true }));

  registerFabRuntimeRoute(app);
  registerFabSourcePreviewRoutes(app);
  registerFabOperatorSessionRoutes(app, relaxedLimiter);
  app.use(
    "/api/trpc",
    relaxedLimiter,
    createExpressMiddleware({
      router: fabStandaloneRouter,
      createContext: createFabContext,
    }),
  );
  serveStatic(app);

  const port = await listenFabServer(server, {
    host: ENV.fabWebHost,
    port: ENV.fabWebPort,
    allowPortFallback: ENV.fabAllowPortFallback,
  });
  lifecycle.installSignalHandlers();
  return { app, port, server, shutdown: lifecycle.shutdown };
}

if (process.env.NODE_ENV !== "test") {
  startFabStandaloneServer().catch((error) => {
    console.error(sanitizeExternalMessage(error, 500, "FAB dashboard startup failed"));
    process.exit(1);
  });
}
