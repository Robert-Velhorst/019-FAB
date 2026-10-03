import { NOT_ADMIN_ERR_MSG, UNAUTHED_ERR_MSG } from '@shared/const';
import { initTRPC, TRPCError } from "@trpc/server";
import superjson from "superjson";
import type { TrpcContext } from "./context";
import { ENV } from "./env";
import { isLoopbackRequest } from "../lib/loopback";
import { resolveFabOperatorAccess } from "../fabOperatorAccess";

const t = initTRPC.context<TrpcContext>().create({
  transformer: superjson,
});

export const router = t.router;
export const publicProcedure = t.procedure;

const requireUser = t.middleware(async opts => {
  const { ctx, next } = opts;

  if (!ctx.user) {
    throw new TRPCError({ code: "UNAUTHORIZED", message: UNAUTHED_ERR_MSG });
  }

  return next({
    ctx: {
      ...ctx,
      user: ctx.user,
    },
  });
});

export const protectedProcedure = t.procedure.use(requireUser);

export function isLoopbackFabOperatorRequest(ctx: TrpcContext): boolean {
  return isLoopbackRequest(
    ctx.req,
    ENV.fabOperatorTrustedProxyAddresses,
    ENV.fabOperatorTrustDockerGateway,
  );
}

export const fabOperatorProcedure = t.procedure.use(
  t.middleware(async opts => {
    const { ctx, next } = opts;
    const access = await resolveFabOperatorAccess(ctx.req, { authenticateRequest: async () => ctx.user });
    if (!access.allowed || !access.mode) {
      throw new TRPCError({ code: "FORBIDDEN", message: NOT_ADMIN_ERR_MSG });
    }
    return next({ ctx: { ...ctx, fabOperatorMode: access.mode, fabOperatorActor: access.actor } });
  }),
);

export const adminProcedure = t.procedure.use(
  t.middleware(async opts => {
    const { ctx, next } = opts;

    if (!ctx.user || ctx.user.role !== 'admin') {
      throw new TRPCError({ code: "FORBIDDEN", message: NOT_ADMIN_ERR_MSG });
    }

    return next({
      ctx: {
        ...ctx,
        user: ctx.user,
      },
    });
  }),
);
