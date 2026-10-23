import { afterEach, expect, it, vi } from "vitest";

const fixture = vi.hoisted(() => ({
  constructor: vi.fn(),
  checkout: vi
    .fn()
    .mockResolvedValue({ url: "https://checkout.stripe.test/synthetic" }),
}));

vi.mock("stripe", () => ({
  default: class {
    checkout = { sessions: { create: fixture.checkout } };
    constructor(...args: unknown[]) {
      fixture.constructor(...args);
    }
  },
}));

afterEach(() => vi.unstubAllEnvs());

it("uses the SDK's typed API version and preserves card-only setup with the existing currency", async () => {
  vi.stubEnv("STRIPE_SECRET_KEY", "sk_test_synthetic_fixture_only");
  const { createCheckoutSession } = await import("./stripe");
  const url = await createCheckoutSession({
    userId: 7,
    userEmail: "synthetic@example.test",
    stripeCustomerId: "cus_synthetic",
    origin: "https://fab.example.test",
  });
  expect(url).toBe("https://checkout.stripe.test/synthetic");
  expect(fixture.constructor).toHaveBeenCalledWith(
    "sk_test_synthetic_fixture_only",
    expect.objectContaining({ apiVersion: "2026-09-30.endive" })
  );
  const request = fixture.checkout.mock.calls[0][0];
  expect(request).toMatchObject({
    mode: "setup",
    allowed_payment_method_types: ["card"],
    currency: "eur",
    setup_intent_data: { metadata: { billing_model: "resource_usage_x_2_5" } },
  });
  expect(request).not.toHaveProperty("payment_method_types");
  expect(request).not.toHaveProperty("line_items");
});
