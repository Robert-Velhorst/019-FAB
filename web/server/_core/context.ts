import type { CreateExpressContextOptions } from "@trpc/server/adapters/express";
import type { User } from "../../drizzle/schema";
import { isLoopbackRequest } from "../lib/loopback";
import { ENV } from "./env";
import { sdk } from "./sdk";
import { authenticateFabManagedRequest, isFabManagedAuthEnabled } from "../fabManagedAuth";

export type TrpcContext = {
  req: CreateExpressContextOptions["req"];
  res: CreateExpressContextOptions["res"];
  user: User | null;
};

export async function createContext(
  opts: CreateExpressContextOptions
): Promise<TrpcContext> {
  let user: User | null = null;

  const localOperatorRequest = ENV.fabOperatorLocalMode && isLoopbackRequest(
    opts.req,
    ENV.fabOperatorTrustedProxyAddresses,
    ENV.fabOperatorTrustDockerGateway,
  );
  if (!localOperatorRequest) {
    try {
      user = isFabManagedAuthEnabled()
        ? await authenticateFabManagedRequest(opts.req)
        : await sdk.authenticateRequest(opts.req);
    } catch (error) {
      // Authentication is optional for public procedures.
      user = null;
    }
  }

  return {
    req: opts.req,
    res: opts.res,
    user,
  };
}
