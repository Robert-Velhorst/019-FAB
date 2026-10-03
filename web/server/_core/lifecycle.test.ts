import { EventEmitter, once } from "node:events";
import { createServer, get, type Server } from "node:http";
import { connect, type AddressInfo, type Socket } from "node:net";
import express from "express";
import { afterEach, describe, expect, it } from "vitest";
import { configureFabProxyTrust } from "./deployment";
import { createFabServerLifecycle, listenFabServer } from "./lifecycle";

const servers: Server[] = [];
const sockets: Socket[] = [];
afterEach(async () => {
  for (const socket of sockets.splice(0)) socket.destroy();
  await Promise.all(servers.splice(0).map(server => new Promise<void>(resolve => {
    server.closeAllConnections();
    server.close(() => resolve());
  })));
});

async function listening(server = createServer()) {
  servers.push(server);
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  return { server, port: (server.address() as AddressInfo).port };
}

describe("FAB proxy trust over real HTTP", () => {
  it.each([{ addresses: [] }, { addresses: ["192.0.2.1"] }, { addresses: ["127.0.0.1"] }])("uses only allowlisted peers: $addresses", async ({ addresses }) => {
    const app = express();
    configureFabProxyTrust(app, addresses);
    app.get("/", (req, res) => res.json({ ip: req.ip, secure: req.secure, hostname: req.hostname }));
    const { port } = await listening(createServer(app));
    const response = await fetch(`http://127.0.0.1:${port}`, { headers: {
      "x-forwarded-for": "198.51.100.8", "x-forwarded-proto": "https", "x-forwarded-host": "forwarded.example.test",
    } });
    expect(await response.json()).toEqual(addresses.includes("127.0.0.1")
      ? { ip: "198.51.100.8", secure: true, hostname: "forwarded.example.test" }
      : { ip: "127.0.0.1", secure: false, hostname: "127.0.0.1" });
  });

  it("stops at the first untrusted forwarded hop", async () => {
    const app = express();
    configureFabProxyTrust(app, ["::ffff:127.0.0.1"]);
    app.get("/", (req, res) => res.json({ ip: req.ip }));
    const { port } = await listening(createServer(app));
    const response = await fetch(`http://127.0.0.1:${port}`, { headers: {
      "x-forwarded-for": "127.0.0.1, 198.51.100.9",
    } });
    expect(await response.json()).toEqual({ ip: "198.51.100.9" });
  });
});

describe("FAB exact port binding", () => {
  it("rejects a real occupied port without opening another listener", async () => {
    const { port } = await listening();
    const server = createServer();
    servers.push(server);
    await expect(listenFabServer(server, { host: "127.0.0.1", port, allowPortFallback: false }))
      .rejects.toThrow(/EADDRINUSE.*configured port/);
    expect(server.listening).toBe(false);
    expect(server.listenerCount("error")).toBe(0);
  });

  it("binds the actual server with bounded fallback only when enabled", async () => {
    const { port } = await listening();
    const server = createServer((_req, res) => res.end("ready"));
    servers.push(server);
    const bound = await listenFabServer(server, { host: "127.0.0.1", port, allowPortFallback: true });
    expect(bound).toBeGreaterThan(port);
    expect(bound).toBeLessThanOrEqual(port + 19);
    expect(await (await fetch(`http://127.0.0.1:${bound}`)).text()).toBe("ready");
  });

  it.each([0, -1, 65_536, NaN, 3000.5])("rejects invalid numeric port %s before listening", async port => {
    const server = createServer();
    servers.push(server);
    await expect(listenFabServer(server, { host: "127.0.0.1", port, allowPortFallback: false })).rejects.toThrow("PORT");
    expect(server.listening).toBe(false);
  });
});

describe("FAB bounded shutdown", () => {
  it("drains an active request, rejects new connections, and is idempotent", async () => {
    const server = createServer();
    const lifecycle = createFabServerLifecycle(server, { timeoutMs: 500 });
    const { port } = await listening(server);
    const requestReceived = once(server, "request");
    const response = fetch(`http://127.0.0.1:${port}`);
    const [, outgoing] = await requestReceived;
    const shutdown = lifecycle.shutdown();
    expect(lifecycle.shutdown()).toBe(shutdown);
    expect(server.listening).toBe(false);
    outgoing.end("finished");
    expect(await (await response).text()).toBe("finished");
    expect(await shutdown).toBe(0);
  });

  it("force-closes a hung HTTP request within the deadline", async () => {
    const server = createServer();
    const lifecycle = createFabServerLifecycle(server, { timeoutMs: 50 });
    const { port } = await listening(server);
    const received = once(server, "request");
    const request = get(`http://127.0.0.1:${port}`);
    const failed = once(request, "error");
    await received;
    const start = Date.now();
    expect(await lifecycle.shutdown()).toBe(1);
    expect(Date.now() - start).toBeLessThan(1000);
    await failed;
  });

  it("force-closes upgraded sockets as well as HTTP connections", async () => {
    const server = createServer();
    const lifecycle = createFabServerLifecycle(server, { timeoutMs: 50 });
    const { port } = await listening(server);
    const upgraded = once(server, "upgrade");
    const socket = connect(port, "127.0.0.1");
    sockets.push(socket);
    socket.on("error", () => {});
    await once(socket, "connect");
    socket.write("GET / HTTP/1.1\r\nHost: localhost\r\nConnection: Upgrade\r\nUpgrade: test\r\n\r\n");
    await upgraded;
    const closed = once(socket, "close");
    expect(await lifecycle.shutdown()).toBe(1);
    await closed;
  });

  it("handles repeated signals once and removes listeners after shutdown", async () => {
    const signals = new EventEmitter();
    const exitCodes: number[] = [];
    const server = createServer();
    const lifecycle = createFabServerLifecycle(server);
    await listening(server);
    lifecycle.installSignalHandlers(signals, code => exitCodes.push(code));
    signals.emit("SIGTERM");
    signals.emit("SIGINT");
    await lifecycle.shutdown();
    await new Promise(resolve => setImmediate(resolve));
    expect(exitCodes).toEqual([0]);
    expect(signals.listenerCount("SIGTERM")).toBe(0);
    expect(signals.listenerCount("SIGINT")).toBe(0);
    expect(server.listenerCount("connection")).toBe(1); // Node's HTTP listener remains.
  });
});
