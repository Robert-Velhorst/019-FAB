import { beforeEach, describe, expect, it, vi } from "vitest";
import viteConfig from "../../vite.config";

const { createServerMock, middlewares } = vi.hoisted(() => ({
  createServerMock: vi.fn(),
  middlewares: vi.fn(),
}));

vi.mock("vite", async (importOriginal) => ({
  ...(await importOriginal<typeof import("vite")>()),
  createServer: createServerMock,
}));

import { setupVite } from "./vite";

describe("setupVite", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    createServerMock.mockResolvedValue({ middlewares });
  });

  it("preserves the configured host allowlist in middleware mode", async () => {
    const app = { use: vi.fn() };
    const server = {};

    await setupVite(app as never, server as never);

    expect(createServerMock).toHaveBeenCalledWith(
      expect.objectContaining({
        server: expect.objectContaining({
          middlewareMode: true,
          allowedHosts: viteConfig.server?.allowedHosts,
        }),
      }),
    );
  });
});
