import { describe, expect, it, vi, beforeEach } from "vitest";
import { appRouter } from "./routers";
import type { TrpcContext } from "./_core/context";
import { ENV } from "./_core/env";

// Mock the Stripe module
vi.mock("./stripe/stripe", () => ({
  getStripe: vi.fn().mockReturnValue({
    billingPortal: {
      sessions: {
        create: vi.fn().mockResolvedValue({ url: "https://billing.stripe.com/session/test" }),
      },
    },
    checkout: {
      sessions: {
        retrieve: vi.fn().mockResolvedValue({
          payment_status: "paid",
          customer_details: { email: "test@example.com" },
        }),
      },
    },
    subscriptions: {
      list: vi.fn().mockResolvedValue({ data: [] }),
    },
    invoices: {
      list: vi.fn().mockResolvedValue({ data: [] }),
    },
  }),
  getOrCreateStripeCustomer: vi.fn().mockResolvedValue("cus_test123"),
  createCheckoutSession: vi.fn().mockResolvedValue("https://checkout.stripe.com/test"),
  retrieveCheckoutSession: vi.fn().mockResolvedValue({
    payment_status: "paid",
    customer_details: { email: "test@example.com" },
  }),
  getCustomerSubscriptions: vi.fn().mockResolvedValue({ data: [] }),
  getCustomerPaymentMethods: vi.fn().mockResolvedValue({ data: [] }),
  listCustomerInvoices: vi.fn().mockResolvedValue({ data: [] }),
}));

// Mock the db module
vi.mock("./db", () => ({
  getDb: vi.fn().mockResolvedValue({
    update: vi.fn().mockReturnValue({
      set: vi.fn().mockReturnValue({
        where: vi.fn().mockResolvedValue(undefined),
      }),
    }),
  }),
  addToWaitlist: vi.fn(),
  getWaitlistCount: vi.fn(),
  getWaitlistEntries: vi.fn(),
  getWaitlistStats: vi.fn(),
  addContactMessage: vi.fn(),
  getContactMessages: vi.fn(),
  getContactMessageCount: vi.fn(),
  updateContactMessageStatus: vi.fn(),
  createBlogPost: vi.fn(),
  updateBlogPost: vi.fn(),
  deleteBlogPost: vi.fn(),
  getBlogPostBySlug: vi.fn(),
  getBlogPostById: vi.fn(),
  getPublishedBlogPosts: vi.fn(),
  getAllBlogPosts: vi.fn(),
  getBlogPostCount: vi.fn(),
  getBookkeepingOverview: vi.fn().mockResolvedValue({}),
  createBookkeepingDocument: vi.fn(),
  addReviewItem: vi.fn(),
  createWorkflowRun: vi.fn(),
  updateWorkflowRun: vi.fn(),
  createRoutingAttempt: vi.fn(),
  recordAuditEvent: vi.fn(),
  getReviewQueue: vi.fn().mockResolvedValue([]),
  getRecentWorkflowRuns: vi.fn().mockResolvedValue([]),
  getRecentAuditEvents: vi.fn().mockResolvedValue([]),
  updateReviewItemStatus: vi.fn(),
}));

// Mock the notification module
vi.mock("./_core/notification", () => ({
  notifyOwner: vi.fn().mockResolvedValue(true),
}));

import {
  getOrCreateStripeCustomer,
  createCheckoutSession,
  retrieveCheckoutSession,
  getCustomerSubscriptions,
  getCustomerPaymentMethods,
  listCustomerInvoices,
} from "./stripe/stripe";

type AuthenticatedUser = NonNullable<TrpcContext["user"]>;

function createPublicContext(): TrpcContext {
  return {
    user: null,
    req: {
      protocol: "https",
      headers: {},
    } as TrpcContext["req"],
    res: {
      clearCookie: vi.fn(),
    } as unknown as TrpcContext["res"],
  };
}

function createAuthContext(overrides?: Partial<AuthenticatedUser>): TrpcContext {
  const user: AuthenticatedUser = {
    id: 1,
    openId: "test-user-openid",
    email: "user@example.com",
    name: "Test User",
    loginMethod: "manus",
    role: "user",
    createdAt: new Date(),
    updatedAt: new Date(),
    lastSignedIn: new Date(),
    stripeCustomerId: null,
    ...overrides,
  };

  return {
    user,
    req: {
      protocol: "https",
      headers: {},
    } as TrpcContext["req"],
    res: {
      clearCookie: vi.fn(),
    } as unknown as TrpcContext["res"],
  };
}

describe("stripe.products", () => {
  it("returns the product catalog (public)", async () => {
    const ctx = createPublicContext();
    const caller = appRouter.createCaller(ctx);

    const products = await caller.stripe.products();

    expect(products).toBeInstanceOf(Array);
    expect(products.length).toBeGreaterThan(0);

    const payg = products.find((p) => p.key === "payAsYouGo");
    expect(payg).toBeDefined();
    expect(payg!.name).toBe("FAB Pay-As-You-Go");
    expect(payg!.nameNl).toBe("FAB Betaal per Gebruik");
    expect(payg!.currency).toBe("eur");
    expect(payg!.priceAmountCents).toBe(0);
    expect(payg!.usageMultiplier).toBe(2.5);
    expect(payg!.interval).toBe("month");
    expect(payg!.features).toBeInstanceOf(Array);
    expect(payg!.featuresNl).toBeInstanceOf(Array);
  });
});

describe("stripe.createCheckout", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("creates a checkout session for authenticated user", async () => {
    const mockCreateCheckout = createCheckoutSession as ReturnType<typeof vi.fn>;
    mockCreateCheckout.mockResolvedValue("https://checkout.stripe.com/test-session");

    const ctx = createAuthContext();
    const caller = appRouter.createCaller(ctx);

    const result = await caller.stripe.createCheckout({
      origin: "https://example.com",
      productKey: "payAsYouGo",
    });

    expect(result.url).toBe("https://checkout.stripe.com/test-session");
    expect(getOrCreateStripeCustomer).toHaveBeenCalledWith(
      1,
      "user@example.com",
      "Test User",
      null
    );
    expect(mockCreateCheckout).toHaveBeenCalledWith(
      expect.objectContaining({
        userId: 1,
        userEmail: "user@example.com",
        userName: "Test User",
        stripeCustomerId: "cus_test123",
        origin: "https://example.com",
      })
    );
  });

  it("rejects unauthenticated users", async () => {
    const ctx = createPublicContext();
    const caller = appRouter.createCaller(ctx);

    await expect(
      caller.stripe.createCheckout({
        origin: "https://example.com",
        productKey: "payAsYouGo",
      })
    ).rejects.toThrow();
  });
});

describe("stripe.subscriptionStatus", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("returns no subscription for user without Stripe customer", async () => {
    const ctx = createAuthContext({ stripeCustomerId: null });
    const caller = appRouter.createCaller(ctx);

    const result = await caller.stripe.subscriptionStatus();

    expect(result.hasSubscription).toBe(false);
    expect(result.billingReady).toBe(false);
    expect(result.status).toBe("none");
    expect(result.subscription).toBeNull();
  });

  it("returns no subscription when customer has none", async () => {
    const mockSubs = getCustomerSubscriptions as ReturnType<typeof vi.fn>;
    mockSubs.mockResolvedValue({ data: [] });

    const ctx = createAuthContext({ stripeCustomerId: "cus_existing" });
    const caller = appRouter.createCaller(ctx);

    const result = await caller.stripe.subscriptionStatus();

    expect(result.hasSubscription).toBe(false);
    expect(result.status).toBe("none");
    expect(mockSubs).toHaveBeenCalledWith("cus_existing");
  });

  it("returns active subscription details", async () => {
    const mockSubs = getCustomerSubscriptions as ReturnType<typeof vi.fn>;
    mockSubs.mockResolvedValue({
      data: [
        {
          id: "sub_test123",
          status: "active",
          current_period_end: Math.floor(Date.now() / 1000) + 86400 * 30,
          cancel_at_period_end: false,
        },
      ],
    });

    const ctx = createAuthContext({ stripeCustomerId: "cus_existing" });
    const caller = appRouter.createCaller(ctx);

    const result = await caller.stripe.subscriptionStatus();

    expect(result.hasSubscription).toBe(true);
    expect(result.status).toBe("active");
    expect(result.subscription).toBeDefined();
    expect(result.subscription!.id).toBe("sub_test123");
    expect(result.subscription!.cancelAtPeriodEnd).toBe(false);
  });

  it("returns ready when a usage-billing payment method is configured", async () => {
    const mockMethods = getCustomerPaymentMethods as ReturnType<typeof vi.fn>;
    mockMethods.mockResolvedValue({ data: [{ id: "pm_test123", type: "card" }] });

    const ctx = createAuthContext({ stripeCustomerId: "cus_existing" });
    const caller = appRouter.createCaller(ctx);
    const result = await caller.stripe.subscriptionStatus();

    expect(result.billingReady).toBe(true);
    expect(result.hasSubscription).toBe(false);
    expect(result.status).toBe("ready");
    expect(result.subscription).toBeNull();
  });
});

describe("stripe.verifySession", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("verifies a successful checkout session", async () => {
    const mockRetrieve = retrieveCheckoutSession as ReturnType<typeof vi.fn>;
    mockRetrieve.mockResolvedValue({
      payment_status: "paid",
      customer_details: { email: "paid@example.com" },
    });

    const ctx = createPublicContext();
    const caller = appRouter.createCaller(ctx);

    const result = await caller.stripe.verifySession({
      sessionId: "cs_test_123",
    });

    expect(result.success).toBe(true);
    expect(result.status).toBe("paid");
    expect(result).not.toHaveProperty("customerEmail");
  });

  it("verifies a completed usage-billing setup session", async () => {
    const mockRetrieve = retrieveCheckoutSession as ReturnType<typeof vi.fn>;
    mockRetrieve.mockResolvedValue({
      mode: "setup",
      status: "complete",
      payment_status: "no_payment_required",
      customer_details: { email: "usage@example.com" },
    });

    const caller = appRouter.createCaller(createPublicContext());
    const result = await caller.stripe.verifySession({ sessionId: "cs_test_usage" });

    expect(result.success).toBe(true);
    expect(result.status).toBe("complete");
    expect(result).not.toHaveProperty("customerEmail");
  });

  it("returns failure for unpaid session", async () => {
    const mockRetrieve = retrieveCheckoutSession as ReturnType<typeof vi.fn>;
    mockRetrieve.mockResolvedValue({
      payment_status: "unpaid",
      customer_details: { email: "user@example.com" },
    });

    const ctx = createPublicContext();
    const caller = appRouter.createCaller(ctx);

    const result = await caller.stripe.verifySession({
      sessionId: "cs_test_456",
    });

    expect(result.success).toBe(false);
    expect(result.status).toBe("unpaid");
  });

  it("handles retrieval errors gracefully", async () => {
    const mockRetrieve = retrieveCheckoutSession as ReturnType<typeof vi.fn>;
    mockRetrieve.mockRejectedValue(new Error("Session not found"));

    const ctx = createPublicContext();
    const caller = appRouter.createCaller(ctx);

    const result = await caller.stripe.verifySession({
      sessionId: "cs_invalid",
    });

    expect(result.success).toBe(false);
    expect(result.status).toBe("error");
    expect(result).not.toHaveProperty("customerEmail");
  });
});

describe("stripe.invoices", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("returns empty invoices for user without Stripe customer", async () => {
    const ctx = createAuthContext({ stripeCustomerId: null });
    const caller = appRouter.createCaller(ctx);

    const result = await caller.stripe.invoices();

    expect(result.invoices).toEqual([]);
  });

  it("returns formatted invoices for customer", async () => {
    const mockInvoices = listCustomerInvoices as ReturnType<typeof vi.fn>;
    mockInvoices.mockResolvedValue({
      data: [
        {
          id: "in_test_123",
          amount_due: 499,
          amount_paid: 499,
          currency: "eur",
          status: "paid",
          created: Math.floor(Date.now() / 1000),
          hosted_invoice_url: "https://invoice.stripe.com/test",
          invoice_pdf: "https://invoice.stripe.com/test.pdf",
        },
      ],
    });

    const ctx = createAuthContext({ stripeCustomerId: "cus_existing" });
    const caller = appRouter.createCaller(ctx);

    const result = await caller.stripe.invoices();

    expect(result.invoices).toHaveLength(1);
    expect(result.invoices[0].id).toBe("in_test_123");
    expect(result.invoices[0].amountPaid).toBe(499);
    expect(result.invoices[0].currency).toBe("eur");
  });
});

describe("stripe deployment gate", () => {
  it("does not contact Stripe when commercial billing is disabled", async () => {
    const previous = ENV.fabBillingEnabled;
    ENV.fabBillingEnabled = false;
    vi.clearAllMocks();
    try {
      const caller = appRouter.createCaller(createAuthContext());
      const products = await caller.stripe.products();
      const status = await caller.stripe.subscriptionStatus();
      const session = await appRouter
        .createCaller(createPublicContext())
        .stripe.verifySession({ sessionId: "cs_test_disabled" });

      expect(products.every((product) => product.billingAvailable === false)).toBe(true);
      expect(status.billingAvailable).toBe(false);
      expect(session).toEqual({ success: false, status: "disabled" });
      expect(retrieveCheckoutSession).not.toHaveBeenCalled();
      await expect(caller.stripe.createCheckout({
        origin: "https://example.com",
        productKey: "payAsYouGo",
      })).rejects.toMatchObject({ code: "PRECONDITION_FAILED" });
      expect(getOrCreateStripeCustomer).not.toHaveBeenCalled();
      expect(createCheckoutSession).not.toHaveBeenCalled();
    } finally {
      ENV.fabBillingEnabled = previous;
    }
  });
});
