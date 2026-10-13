import type { EventEmitter } from "node:events";
import type { IncomingMessage, Server, ServerResponse } from "node:http";
import type { Socket } from "node:net";
import { parseFabPort } from "./deployment";

type BindOptions = { host: string; port: number; allowPortFallback: boolean };

function listen(server: Server, host: string, port: number): Promise<void> {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      server.off("error", failed);
      server.off("listening", ready);
    };
    const failed = (error: Error) => { cleanup(); reject(error); };
    const ready = () => { cleanup(); resolve(); };
    server.once("error", failed);
    server.once("listening", ready);
    try {
      server.listen(port, host);
    } catch (error) {
      cleanup();
      reject(error);
    }
  });
}

export async function listenFabServer(server: Server, options: BindOptions): Promise<number> {
  const first = parseFabPort(String(options.port));
  const last = options.allowPortFallback ? Math.min(first + 19, 65_535) : first;
  for (let port = first; port <= last; port += 1) {
    try {
      await listen(server, options.host, port);
      return port;
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code === "EADDRINUSE" && port < last) continue;
      if (code === "EADDRINUSE") {
        throw new Error(`FAB web (pid ${process.pid}): EADDRINUSE on configured port ${first}; inspect the existing listener and stop it only if owned, or explicitly configure a different PORT`);
      }
      throw new Error("FAB web could not bind; verify FAB_WEB_HOST, PORT, and listening permissions");
    }
  }
  throw new Error("FAB web could not bind the configured port");
}

export function createFabServerLifecycle(server: Server, options: { timeoutMs?: number } = {}) {
  const timeoutMs = options.timeoutMs ?? 10_000;
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 60_000) {
    throw new Error("FAB shutdown timeout must be between 1 and 60000 milliseconds");
  }
  const sockets = new Set<Socket>();
  const responses = new Set<ServerResponse>();
  let shutdownPromise: Promise<number> | undefined;
  let serverClosed = false;
  let complete: ((code: number) => void) | undefined;
  let disposeSignals = () => {};

  const drained = () => {
    if (serverClosed && sockets.size === 0) complete?.(0);
  };
  const connection = (socket: Socket) => {
    sockets.add(socket);
    socket.once("close", () => { sockets.delete(socket); drained(); });
  };
  server.on("connection", connection);
  const request = (_request: IncomingMessage, response: ServerResponse) => {
    responses.add(response);
    response.once("close", () => responses.delete(response));
    response.once("finish", () => {
      responses.delete(response);
      if (shutdownPromise) server.closeIdleConnections();
    });
  };
  server.prependListener("request", request);

  function shutdown(): Promise<number> {
    if (shutdownPromise) return shutdownPromise;
    shutdownPromise = new Promise<number>(resolve => {
      let settled = false;
      const finish = (code: number) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        disposeSignals();
        server.off("connection", connection);
        server.off("request", request);
        resolve(code);
      };
      const timer = setTimeout(() => {
        // closeAllConnections excludes upgraded sockets; track all TCP peers too.
        server.closeAllConnections();
        for (const socket of Array.from(sockets)) socket.destroy();
        finish(1);
      }, timeoutMs);
      complete = finish;
      for (const response of Array.from(responses)) {
        response.shouldKeepAlive = false;
        if (!response.headersSent) response.setHeader("connection", "close");
      }
      server.close(error => {
        serverClosed = true;
        if (error && (error as NodeJS.ErrnoException).code !== "ERR_SERVER_NOT_RUNNING") {
          for (const socket of Array.from(sockets)) socket.destroy();
          finish(1);
          return;
        }
        drained();
      });
      server.closeIdleConnections();
    });
    return shutdownPromise;
  }

  function installSignalHandlers(
    signals: Pick<EventEmitter, "on" | "off"> = process,
    exit: (code: number) => void = code => process.exit(code),
  ): () => void {
    disposeSignals();
    let requested = false;
    const onSignal = () => {
      if (requested) return;
      requested = true;
      void shutdown().then(exit);
    };
    signals.on("SIGTERM", onSignal);
    signals.on("SIGINT", onSignal);
    disposeSignals = () => {
      signals.off("SIGTERM", onSignal);
      signals.off("SIGINT", onSignal);
    };
    return disposeSignals;
  }

  return { shutdown, installSignalHandlers };
}
