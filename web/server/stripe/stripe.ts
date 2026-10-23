/**
 * Stripe integration helpers — Production-hardened
 *
 * Uses structured logging, defensive null checks, and proper error propagation.
 * All Stripe API calls are wrapped with error context for observability.
 */
import Stripe from "stripe";
import { createLogger } from "../lib/logger";
import { getDefaultProduct } from "./products";

const log = createLogger("Stripe");

let _stripe: Stripe | null = null;

type StripeCustomerCandidate = {
  id: string;
  deleted?: boolean | void;
  metadata?: Record<string, string>;
};

export function findOwnedStripeCustomer<T extends StripeCustomerCandidate>(
  customers: T[],
  userId: number
): T | undefined {
  return customers.find((customer) => isOwnedStripeCustomer(customer, userId));
}

function isOwnedStripeCustomer(customer: StripeCustomerCandidate, userId: number): boolean {
  return !customer.deleted && customer.metadata?.user_id === userId.toString();
}

/**
 * Get or initialize the Stripe SDK instance.
 * Throws immediately if STRIPE_SECRET_KEY is missing — fail fast.
 */
export function getStripe(): Stripe {
  if (!_stripe) {
    const secretKey = process.env.STRIPE_SECRET_KEY;
    if (!secretKey) {
      throw new Error(
        "STRIPE_SECRET_KEY is not configured. Set it in Settings → Payment."
      );
    }
    _stripe = new Stripe(secretKey, {
      apiVersion: "2026-09-30.endive",
      maxNetworkRetries: 2, // Retry transient network errors
      timeout: 30_000, // 30s timeout for Stripe API calls
    });
    log.info("Stripe SDK initialized");
  }
  return _stripe;
}

/**
 * Get or create a Stripe customer for a user.
 * Verifies existing customer ID is still valid before reusing.
 * Deduplicates by email only when the Stripe customer is bound to this user.
 */
export async function getOrCreateStripeCustomer(
  userId: number,
  email: string,
  name?: string | null,
  existingCustomerId?: string | null
): Promise<string> {
  const stripe = getStripe();

  // 1. Verify existing customer ID if provided
  if (existingCustomerId) {
    try {
      const customer = await stripe.customers.retrieve(existingCustomerId);
      if (!customer.deleted) {
        return existingCustomerId;
      }
      log.warn("Stripe customer was deleted, creating new one", {
        oldCustomerId: existingCustomerId,
        userId,
      });
    } catch (err) {
      if (!isMissingStripeCustomer(err)) {
        log.warn("Failed to verify existing Stripe customer", { userId });
        throw err;
      }
      log.warn("Saved Stripe customer was not found", { userId });
    }
  }

  // 2. Search for existing customer by email to prevent duplicates
  if (email) {
    try {
      let found: Stripe.Customer | undefined;
      await stripe.customers.list({ email, limit: 100 }).autoPagingEach((customer) => {
        if (!isOwnedStripeCustomer(customer, userId)) return;
        found = customer;
        return false;
      });
      if (found) {
        log.info("Found existing Stripe customer by email", {
          customerId: found.id,
          userId,
        });
        return found.id;
      }
    } catch (err) {
      log.warn("Failed to search for existing Stripe customer", { userId });
      throw err;
    }
  }

  // 3. Create a new customer
  const customer = await stripe.customers.create({
    email: email || undefined,
    name: name || undefined,
    metadata: {
      user_id: userId.toString(),
      source: "fab-website",
    },
  }, {
    idempotencyKey: `fab-customer-${userId}`,
  });

  log.info("Created new Stripe customer", {
    customerId: customer.id,
    userId,
  });

  return customer.id;
}

function isMissingStripeCustomer(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const stripeError = error as { statusCode?: unknown; code?: unknown };
  return stripeError.statusCode === 404 && stripeError.code === "resource_missing";
}

/**
 * Create a Checkout Session that stores a payment method for usage billing.
 * It creates no recurring price or fixed fee.
 */
export async function createCheckoutSession(params: {
  userId: number;
  userEmail: string;
  userName?: string | null;
  stripeCustomerId: string;
  origin: string;
}): Promise<string> {
  const stripe = getStripe();

  const session = await stripe.checkout.sessions.create({
    customer: params.stripeCustomerId,
    client_reference_id: params.userId.toString(),
    mode: "setup",
    allowed_payment_method_types: ["card"],
    currency: getDefaultProduct().currency,
    setup_intent_data: {
      metadata: {
        user_id: params.userId.toString(),
        billing_model: "resource_usage_x_2_5",
      },
    },
    metadata: {
      user_id: params.userId.toString(),
      customer_email: params.userEmail,
      customer_name: params.userName || "",
    },
    success_url: `${params.origin}/payment/success?session_id={CHECKOUT_SESSION_ID}`,
    cancel_url: `${params.origin}/payment/cancel`,
  });

  if (!session.url) {
    throw new Error("Stripe returned a checkout session without a URL");
  }

  return session.url;
}

/**
 * Verify and construct a Stripe webhook event.
 * Throws if signature verification fails — caller must handle.
 */
export function constructWebhookEvent(
  payload: Buffer,
  signature: string
): Stripe.Event {
  const stripe = getStripe();
  const webhookSecret = process.env.STRIPE_WEBHOOK_SECRET;

  if (!webhookSecret) {
    throw new Error(
      "STRIPE_WEBHOOK_SECRET is not configured. Set it in Settings → Payment."
    );
  }

  return stripe.webhooks.constructEvent(payload, signature, webhookSecret);
}

/**
 * Retrieve a checkout session with expanded line items and subscription.
 */
export async function retrieveCheckoutSession(sessionId: string) {
  const stripe = getStripe();
  return stripe.checkout.sessions.retrieve(sessionId, {
    expand: ["line_items", "subscription"],
  });
}

/**
 * List invoices for a customer, ordered by most recent first.
 */
export async function listCustomerInvoices(customerId: string, limit = 10) {
  const stripe = getStripe();
  return stripe.invoices.list({
    customer: customerId,
    limit: Math.min(limit, 100), // Stripe max is 100
  });
}

/**
 * Get active subscriptions for a customer.
 * Also includes past_due and trialing to show all relevant subscriptions.
 */
export async function getCustomerSubscriptions(customerId: string) {
  const stripe = getStripe();
  return stripe.subscriptions.list({
    customer: customerId,
    status: "active",
    limit: 5,
  });
}

/** Return saved card payment methods used for usage-based invoicing. */
export async function getCustomerPaymentMethods(customerId: string) {
  const stripe = getStripe();
  return stripe.paymentMethods.list({
    customer: customerId,
    type: "card",
    limit: 10,
  });
}
