import { beforeEach, describe, expect, it, vi } from "vitest";

const { stripeClient, stripeConstructor } = vi.hoisted(() => {
  const stripeClient = {
    customers: {
      retrieve: vi.fn(),
      list: vi.fn(),
      create: vi.fn(),
    },
  };
  return {
    stripeClient,
    stripeConstructor: vi.fn(function StripeMock() { return stripeClient; }),
  };
});

vi.mock("stripe", () => ({ default: stripeConstructor }));

import { findOwnedStripeCustomer, getOrCreateStripeCustomer } from "./stripe/stripe";

beforeEach(() => {
  vi.stubEnv("STRIPE_SECRET_KEY", "sk_test_fab_customer_lookup");
  stripeClient.customers.retrieve.mockReset();
  stripeClient.customers.list.mockReset();
  stripeClient.customers.create.mockReset();
});

function paginatedCustomerList(customers: Array<{ id: string; metadata?: Record<string, string> }>) {
  return {
    data: customers.slice(0, 100),
    autoPagingEach: async (handler: (customer: (typeof customers)[number]) => boolean | void | Promise<boolean | void>) => {
      for (const customer of customers) {
        if (await handler(customer) === false) break;
      }
    },
  };
}

describe("findOwnedStripeCustomer", () => {
  it("reuses only an active customer bound to the same FAB user", () => {
    const customers = [
      { id: "cus_other", metadata: { user_id: "2" } },
      { id: "cus_legacy", metadata: {} },
      { id: "cus_deleted", deleted: true, metadata: { user_id: "1" } },
      { id: "cus_owned", metadata: { user_id: "1" } },
    ];

    expect(findOwnedStripeCustomer(customers, 1)?.id).toBe("cus_owned");
  });

  it("does not adopt an unowned or differently owned email match", () => {
    const customers = [
      { id: "cus_other", metadata: { user_id: "2" } },
      { id: "cus_legacy", metadata: {} },
    ];

    expect(findOwnedStripeCustomer(customers, 1)).toBeUndefined();
  });

  it("finds the user-bound customer beyond the first email-result page", async () => {
    const customers = Array.from({ length: 100 }, (_, index) => ({
      id: `cus_other_${index}`,
      metadata: { user_id: "99" },
    }));
    customers.push({ id: "cus_owned_after_page", metadata: { user_id: "7" } });
    stripeClient.customers.list.mockReturnValueOnce(paginatedCustomerList(customers));
    stripeClient.customers.create.mockResolvedValueOnce({ id: "cus_duplicate" });

    await expect(getOrCreateStripeCustomer(7, "owner@example.test"))
      .resolves.toBe("cus_owned_after_page");
    expect(stripeClient.customers.create).not.toHaveBeenCalled();
  });

  it("does not create a customer when email lookup fails transiently", async () => {
    stripeClient.customers.list.mockReturnValueOnce({
      autoPagingEach: async () => { throw new Error("Stripe unavailable"); },
    });
    stripeClient.customers.create.mockResolvedValueOnce({ id: "cus_duplicate" });

    await expect(getOrCreateStripeCustomer(7, "owner@example.test"))
      .rejects.toThrow("Stripe unavailable");
    expect(stripeClient.customers.create).not.toHaveBeenCalled();
  });

  it("does not replace a customer when retrieving its saved ID fails transiently", async () => {
    stripeClient.customers.retrieve.mockRejectedValueOnce(new Error("Stripe unavailable"));
    stripeClient.customers.create.mockResolvedValueOnce({ id: "cus_duplicate" });

    await expect(getOrCreateStripeCustomer(7, "owner@example.test", null, "cus_saved"))
      .rejects.toThrow("Stripe unavailable");
    expect(stripeClient.customers.list).not.toHaveBeenCalled();
    expect(stripeClient.customers.create).not.toHaveBeenCalled();
  });

  it("creates a replacement only after Stripe confirms the saved customer is missing", async () => {
    stripeClient.customers.retrieve.mockRejectedValueOnce({
      statusCode: 404,
      code: "resource_missing",
    });
    stripeClient.customers.list.mockReturnValueOnce(paginatedCustomerList([]));
    stripeClient.customers.create.mockResolvedValueOnce({ id: "cus_replacement" });

    await expect(getOrCreateStripeCustomer(7, "owner@example.test", null, "cus_deleted"))
      .resolves.toBe("cus_replacement");
    expect(stripeClient.customers.create).toHaveBeenCalledOnce();
  });

  it("uses one Stripe idempotency key for concurrent customer creation by the same user", async () => {
    stripeClient.customers.list.mockReturnValue(paginatedCustomerList([]));
    const customersByKey = new Map<string, { id: string }>();
    stripeClient.customers.create.mockImplementation(async (_params, options) => {
      const key = options?.idempotencyKey;
      if (!key) throw new Error("Missing Stripe idempotency key");
      const customer = customersByKey.get(key) ?? { id: "cus_concurrent" };
      customersByKey.set(key, customer);
      return customer;
    });

    const customerIds = await Promise.all([
      getOrCreateStripeCustomer(17, "owner@example.test", "Owner"),
      getOrCreateStripeCustomer(17, "owner@example.test", "Owner"),
    ]);

    expect(customerIds).toEqual(["cus_concurrent", "cus_concurrent"]);
    expect(stripeClient.customers.create).toHaveBeenCalledTimes(2);
    expect(stripeClient.customers.create.mock.calls.map((call) => call[1]?.idempotencyKey))
      .toEqual(["fab-customer-17", "fab-customer-17"]);
  });
});
