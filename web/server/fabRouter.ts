import { z } from "zod";
import { TRPCError } from "@trpc/server";
import { COOKIE_NAME } from "@shared/const";
import { getSessionCookieOptions } from "./_core/cookies";
import { fabOperatorProcedure, publicProcedure, router } from "./_core/trpc";
import { isFabManagedAuthEnabled, logoutFabManagedOperator } from "./fabManagedAuth";
import {
  FAB_OPERATOR_COMMAND_IDS,
  createFabBackup,
  createFabSupportBundle,
  getFabControlCenter,
  getFabReviewPage,
  importFabBankStatement,
  refreshFabControlCenter,
  resolveFabReviewItem,
  runFabOperatorCommand,
  saveFabWaveSetup,
  startFabGmailAuthorization,
  startFabGoogleDriveAuthorization,
  uploadFabGmailCredentials,
  uploadFabGoogleDriveCredentials,
  uploadFabIntakeFile,
  validateFabWaveSetup,
} from "./fabLocalGateway";

function actor(ctx: { fabOperatorActor?: string | null; user?: { id?: number | string } | null }): string {
  if (isFabManagedAuthEnabled() && ctx.fabOperatorActor) return ctx.fabOperatorActor;
  return ctx.user?.id
    ? `fab_dashboard:${ctx.user.id}`
    : "fab_dashboard:local_operator";
}

export const fabRouter = router({
  access: fabOperatorProcedure.query(({ ctx }) => ({
    allowed: true,
    mode: ctx.fabOperatorMode,
    operatorLabel: ctx.user?.name || ctx.user?.email || "Local operator",
  })),
  controlCenter: fabOperatorProcedure.query(async () => getFabControlCenter()),
  refreshControlCenter: fabOperatorProcedure.mutation(async () => refreshFabControlCenter()),
  reviewPage: fabOperatorProcedure
    .input(z.object({
      offset: z.number().int().min(0).max(10_000_000),
      limit: z.number().int().min(1).max(100).optional(),
    }).strict())
    .query(async ({ input }) => getFabReviewPage(input)),
  createBackup: fabOperatorProcedure
    .mutation(async ({ ctx }) => createFabBackup(actor(ctx))),
  createSupportBundle: fabOperatorProcedure
    .mutation(async ({ ctx }) => createFabSupportBundle(actor(ctx))),
  uploadIntake: fabOperatorProcedure
    .input(z.object({
      filename: z.string().trim().min(1).max(255),
      mimeType: z.string().trim().max(150).optional(),
      contentBase64: z.string().min(4).max(8_500_000),
    }).strict())
    .mutation(async ({ input }) => uploadFabIntakeFile(input)),
  importBankStatement: fabOperatorProcedure
    .input(z.object({
      filename: z.string().trim().min(1).max(255).regex(
        /\.(csv|json|xml|camt|sta|mt940)$/i,
        "Bank statement must be CSV, JSON, CAMT/XML, or MT940",
      ),
      format: z.enum(["csv", "json", "camt", "mt940"]),
      accountIdentifier: z.string().trim().min(1).max(200),
      contentBase64: z.string().min(4).max(5_600_000),
    }).strict().superRefine((input, context) => {
      const extensions: Record<typeof input.format, string[]> = {
        csv: [".csv"],
        json: [".json"],
        camt: [".camt", ".xml"],
        mt940: [".mt940", ".sta"],
      };
      const filename = input.filename.toLowerCase();
      if (!extensions[input.format].some((extension) => filename.endsWith(extension))) {
        context.addIssue({
          code: "custom",
          path: ["filename"],
          message: `File extension does not match ${input.format} format`,
        });
      }
    }))
    .mutation(async ({ input, ctx }) => importFabBankStatement({
      ...input,
      actor: actor(ctx),
    })),
  installGmailCredentials: fabOperatorProcedure
    .input(z.object({
      filename: z.string().trim().min(1).max(255).regex(/\.json$/i, "Desktop OAuth credentials must be a JSON file"),
      contentBase64: z.string().min(4).max(90_000),
      replace: z.boolean().optional(),
    }).strict())
    .mutation(async ({ input, ctx }) => uploadFabGmailCredentials({
      ...input,
      actor: actor(ctx),
    })),
  startGmailAuthorization: fabOperatorProcedure
    .mutation(async ({ ctx }) => startFabGmailAuthorization(actor(ctx))),
  installGoogleDriveCredentials: fabOperatorProcedure
    .input(z.object({
      filename: z.string().trim().min(1).max(255).regex(/\.json$/i, "Desktop OAuth credentials must be a JSON file"),
      contentBase64: z.string().min(4).max(90_000),
      replace: z.boolean().optional(),
    }).strict())
    .mutation(async ({ input, ctx }) => uploadFabGoogleDriveCredentials({
      ...input,
      actor: actor(ctx),
    })),
  startGoogleDriveAuthorization: fabOperatorProcedure
    .mutation(async ({ ctx }) => startFabGoogleDriveAuthorization(actor(ctx))),
  saveWaveSetup: fabOperatorProcedure
    .input(z.object({
      targetSystem: z.enum(["waveapps_business", "waveapps_personal"]).optional(),
      accessToken: z.string().trim().min(10).max(16_384).optional(),
      businessId: z.string().trim().min(1).max(255).optional(),
      anchorAccountId: z.string().trim().min(1).max(255).optional(),
      defaultCategoryAccountId: z.string().trim().max(255).optional(),
      categoryAccountIds: z.record(
        z.string().trim().min(1).max(255),
        z.string().trim().min(1).max(255),
      ).optional(),
      clearAccessToken: z.boolean().optional(),
    }).strict())
    .mutation(async ({ input, ctx }) => saveFabWaveSetup({
      ...input,
      actor: actor(ctx),
    })),
  validateWaveSetup: fabOperatorProcedure
    .input(z.object({
      targetSystem: z.enum(["waveapps_business", "waveapps_personal"]).optional(),
    }).strict())
    .mutation(async ({ input }) => validateFabWaveSetup(input.targetSystem)),
  resolveReview: fabOperatorProcedure
    .input(z.object({
      reviewItemId: z.number().int().positive(),
      status: z.enum(["approved", "rejected", "resolved", "ignored"]),
      resolution: z.string().trim().min(3).max(1000),
      corrections: z.object({
        vendorName: z.string().trim().min(1).max(255).optional(),
        category: z.string().trim().min(1).max(255).optional(),
        transactionDate: z.iso.date().optional(),
        totalAmount: z.number().finite().positive().optional(),
        vatAmount: z.number().finite().nonnegative().optional(),
        targetSystem: z.enum(["waveapps_business", "waveapps_personal", "mijngeldzaken"]).optional(),
        duplicateOfDocumentId: z.number().int().positive().optional(),
        duplicateCandidateId: z.number().int().positive().optional(),
        documentType: z.enum([
          "receipt",
          "vendor_invoice",
          "credit_note",
          "order_confirmation",
          "estimate",
          "bank_statement",
          "insurance_policy",
          "government_correspondence",
        ]).optional(),
      }).strict().optional(),
      learnRule: z.boolean().optional(),
      applyToMatchingVendor: z.boolean().optional(),
    }).strict())
    .mutation(async ({ input }) => resolveFabReviewItem(input)),
  runCommand: fabOperatorProcedure
    .input(z.object({
      commandId: z.enum(FAB_OPERATOR_COMMAND_IDS),
      payload: z.object({
        limit: z.number().int().min(1).max(500).optional(),
        sources: z.array(z.enum(["gmail", "google_drive", "freshdesk", "google_photos"])).max(4).optional(),
        dryRun: z.boolean().optional(),
        fromDate: z.iso.date().optional(),
        toDate: z.iso.date().optional(),
        targetSystem: z.string().trim().max(100).optional(),
        reason: z.string().trim().min(1).max(500).optional(),
        confirmation: z.string().trim().max(100).optional(),
      }).strict().optional(),
    }))
    .mutation(async ({ input, ctx }) => runFabOperatorCommand(
      input.commandId,
      actor(ctx),
      input.payload || {},
    )),
});

const fabStandaloneAuthRouter = router({
  me: publicProcedure.query(({ ctx }) => ctx.user),
  logout: publicProcedure.mutation(async ({ ctx }) => {
    if (isFabManagedAuthEnabled()) {
      if (!await logoutFabManagedOperator(ctx.req, ctx.res)) {
        throw new TRPCError({ code: "FORBIDDEN", message: "A same-origin HTTPS request is required" });
      }
      return { success: true } as const;
    }
    const cookieOptions = getSessionCookieOptions(ctx.req);
    ctx.res.clearCookie(COOKIE_NAME, { ...cookieOptions, maxAge: -1 });
    return { success: true } as const;
  }),
});

export const fabStandaloneRouter = router({
  auth: fabStandaloneAuthRouter,
  fab: fabRouter,
});
export type FabStandaloneRouter = typeof fabStandaloneRouter;
