import type { Request } from "express";
import { ENV } from "./_core/env";
import { isLoopbackRequest } from "./lib/loopback";
import { authenticateFabManagedRequest, isFabManagedAuthEnabled } from "./fabManagedAuth";

type AuthenticatedOperator = {
  id?: number | string;
  openId?: string;
  role?: string;
};
type AuthenticateRequest = (request: Request) => Promise<AuthenticatedOperator | null>;

export type FabOperatorAccess = {
  actor: string | null;
  allowed: boolean;
  mode: "admin" | "local" | null;
};

export type FabOperatorAccessOptions = {
  authenticateRequest?: AuthenticateRequest;
  localOperatorMode?: boolean;
};

function operatorActor(value: unknown): string {
  const bounded = String(value || "operator")
    .replace(/[^A-Za-z0-9._:-]/g, "_")
    .slice(0, 96);
  return bounded || "operator";
}

export async function resolveFabOperatorAccess(
  req: Request,
  options: FabOperatorAccessOptions = {},
): Promise<FabOperatorAccess> {
  if (isFabManagedAuthEnabled()) {
    const user = await authenticateFabManagedRequest(req);
    return user
      ? { actor: `fab_dashboard:admin:${operatorActor(user.openId)}`, allowed: true, mode: "admin" }
      : { actor: null, allowed: false, mode: null };
  }
  const localOperatorMode = options.localOperatorMode ?? ENV.fabOperatorLocalMode;
  const localOperator = localOperatorMode && isLoopbackRequest(
    req,
    ENV.fabOperatorTrustedProxyAddresses,
    ENV.fabOperatorTrustDockerGateway,
  );
  if (localOperator) {
    return {
      actor: "fab_dashboard:local_operator",
      allowed: true,
      mode: "local",
    };
  }

  try {
    const authenticateRequest = options.authenticateRequest ?? (async (request: Request) => {
      const { sdk } = await import("./_core/sdk");
      return sdk.authenticateRequest(request);
    });
    const user = await authenticateRequest(req);
    if (user?.role === "admin") {
      return {
        actor: `fab_dashboard:admin:${operatorActor(user.openId || user.id)}`,
        allowed: true,
        mode: "admin",
      };
    }
  } catch {
    // The caller returns the same bounded denial for every auth failure.
  }

  return { actor: null, allowed: false, mode: null };
}
