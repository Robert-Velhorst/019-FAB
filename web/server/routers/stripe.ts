/**
 * Stripe tRPC Router — Production-hardened
 * Handles checkout sessions, portal sessions, and subscription queries.
 * Uses TRPCError for proper error propagation, structured logging,
 * and shared Zod schemas for validation.
 */
import { TRPCError } from "@trpc/server";
import { publicProcedure, protectedProcedure, router } from "../_core/trpc";
import {
  stripeCheckoutSchema,
  stripePortalSchema,
  stripeVerifySessionSchema,
} from "@shared/validation";
import {
  getStripe,
  getOrCreateStripeCustomer,
  createCheckoutSession,
  retrieveCheckoutSession,
  getCustomerSubscriptions,
  getCustomerPaymentMethods,
  listCustomerInvoices,
} from "../stripe/stripe";
import { getDefaultProduct, FAB_PRODUCTS } from "../stripe/products";
import { getDb } from "../db";
import { users } from "../../drizzle/schema";
import { eq } from "drizzle-orm";
import { createLogger } from "../lib/logger";
import { ENV } from "../_core/env";

const log = createLogger("Stripe");

function assertBillingEnabled() {
  if (!ENV.fabBillingEnabled) {
    throw new TRPCError({
      code: "PRECONDITION_FAILED",
      message: "FAB commercial billing is not enabled for this deployment.",
    });
  }
}

export const stripeRouter = router({
  /**
   * Create a Stripe Checkout Session for subscription.
   * Requires authentication. Validates product key exists.
   */
  createCheckout: protectedProcedure
    .input(stripeCheckoutSchema)
    .mutation(async ({ ctx, input }) => {
      assertBillingEnabled();
      const user = ctx.user;
      const product = FAB_PRODUCTS[input.productKey];

      if (!product) {
        throw new TRPCError({
          code: "BAD_REQUEST",
          message: `Unknown product: ${input.productKey}`,
        });
      }

      try {
        // Get or create Stripe customer
        const customerId = await getOrCreateStripeCustomer(
          user.id,
          user.email || "",
          user.name,
          user.stripeCustomerId
        );

        // Persist Stripe customer ID if it's new or changed
        if (customerId !== user.stripeCustomerId) {
          const db = await getDb();
          if (db) {
            await db
              .update(users)
              .set({ stripeCustomerId: customerId })
              .where(eq(users.id, user.id));
          }
        }

        const url = await createCheckoutSession({
          userId: user.id,
          userEmail: user.email || "",
          userName: user.name,
          stripeCustomerId: customerId,
          origin: input.origin,
        });

        log.info("Checkout session created", {
          userId: user.id,
          product: input.productKey,
        });

        return { url };
      } catch (err) {
        log.error(
          "Failed to create checkout session",
          { userId: user.id, product: input.productKey },
          err instanceof Error ? err : new Error(String(err))
        );
        throw new TRPCError({
          code: "INTERNAL_SERVER_ERROR",
          message: "Failed to create checkout session. Please try again.",
        });
      }
    }),

  /**
   * Create a Stripe Customer Portal session for managing billing.
   * Requires authentication and an existing Stripe customer.
   */
  createPortalSession: protectedProcedure
    .input(stripePortalSchema)
    .mutation(async ({ ctx, input }) => {
      assertBillingEnabled();
      const user = ctx.user;

      if (!user.stripeCustomerId) {
        throw new TRPCError({
          code: "PRECONDITION_FAILED",
          message: "No billing account found. Please subscribe first.",
        });
      }

      try {
        const stripe = getStripe();
        const session = await stripe.billingPortal.sessions.create({
          customer: user.stripeCustomerId,
          return_url: `${input.origin}/account`,
        });

        log.info("Portal session created", { userId: user.id });
        return { url: session.url };
      } catch (err) {
        log.error(
          "Failed to create portal session",
          { userId: user.id },
          err instanceof Error ? err : new Error(String(err))
        );
        throw new TRPCError({
          code: "INTERNAL_SERVER_ERROR",
          message: "Failed to open billing portal. Please try again.",
        });
      }
    }),

  /**
   * Get usage-billing readiness. Legacy subscription fields remain for compatibility.
   */
  subscriptionStatus: protectedProcedure.query(async ({ ctx }) => {
    const user = ctx.user;
    const noSub = {
      billingAvailable: ENV.fabBillingEnabled,
      hasSubscription: false,
      billingReady: false,
      status: "none" as const,
      subscription: null,
    };

    if (!ENV.fabBillingEnabled) {
      return noSub;
    }

    if (!user.stripeCustomerId) {
      return noSub;
    }

    try {
      const [paymentMethods, subscriptions] = await Promise.all([
        getCustomerPaymentMethods(user.stripeCustomerId),
        getCustomerSubscriptions(user.stripeCustomerId),
      ]);

      if (paymentMethods.data.length > 0) {
        return {
          billingAvailable: true,
          hasSubscription: false,
          billingReady: true,
          status: "ready" as const,
          subscription: null,
        };
      }

      if (subscriptions.data.length === 0) {
        return noSub;
      }

      const sub = subscriptions.data[0];
      const subAny = sub as any;

      return {
        billingAvailable: true,
        hasSubscription: true,
        billingReady: true,
        status: sub.status as string,
        subscription: {
          id: sub.id,
          status: sub.status,
          currentPeriodEnd: subAny.current_period_end
            ? new Date(subAny.current_period_end * 1000)
            : null,
          cancelAtPeriodEnd: sub.cancel_at_period_end,
        },
      };
    } catch (err) {
      log.error(
        "Error fetching subscription",
        { userId: user.id },
        err instanceof Error ? err : new Error(String(err))
      );
      return { ...noSub, status: "error" as const };
    }
  }),

  /**
   * Get the current user's recent invoices.
   * Returns empty array on error (graceful degradation).
   */
  invoices: protectedProcedure.query(async ({ ctx }) => {
    const user = ctx.user;

    if (!ENV.fabBillingEnabled || !user.stripeCustomerId) {
      return { invoices: [] };
    }

    try {
      const result = await listCustomerInvoices(user.stripeCustomerId, 10);
      return {
        invoices: result.data.map((inv) => ({
          id: inv.id,
          amountDue: inv.amount_due,
          amountPaid: inv.amount_paid,
          currency: inv.currency,
          status: inv.status,
          created: new Date(inv.created * 1000),
          hostedInvoiceUrl: inv.hosted_invoice_url,
          invoicePdf: inv.invoice_pdf,
        })),
      };
    } catch (err) {
      log.error(
        "Error fetching invoices",
        { userId: user.id },
        err instanceof Error ? err : new Error(String(err))
      );
      return { invoices: [] };
    }
  }),

  /**
   * Verify a checkout session after redirect from Stripe.
   * Validates session ID format before querying Stripe API.
   */
  verifySession: publicProcedure
    .input(stripeVerifySessionSchema)
    .query(async ({ input }) => {
      if (!ENV.fabBillingEnabled) {
        return {
          success: false,
          status: "disabled",
        };
      }
      try {
        const session = await retrieveCheckoutSession(input.sessionId);
        return {
          success: (session.mode === "setup" && session.status === "complete") || session.payment_status === "paid",
          status: session.mode === "setup" ? session.status : session.payment_status,
        };
      } catch (err) {
        log.error(
          "Error verifying session",
          { sessionId: input.sessionId },
          err instanceof Error ? err : new Error(String(err))
        );
        return {
          success: false,
          status: "error",
        };
      }
    }),

  /**
   * Get product/pricing info (public — no auth required).
   * Returns static data from the products definition.
   */
  products: publicProcedure.query(() => {
    return Object.entries(FAB_PRODUCTS).map(([key, product]) => ({
      key,
      id: product.id,
      name: product.name,
      nameNl: product.nameNl,
      description: product.description,
      descriptionNl: product.descriptionNl,
      priceAmountCents: product.priceAmountCents,
      usageMultiplier: product.usageMultiplier,
      currency: product.currency,
      interval: product.interval,
      features: product.features,
      featuresNl: product.featuresNl,
      popular: product.popular,
      billingAvailable: ENV.fabBillingEnabled,
    }));
  }),
});
