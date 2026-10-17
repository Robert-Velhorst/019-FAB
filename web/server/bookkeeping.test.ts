import { describe, expect, it, vi, beforeEach } from "vitest";
import { appRouter } from "./routers";
import type { TrpcContext } from "./_core/context";

vi.mock("./db", () => ({
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
  getBookkeepingOverview: vi.fn().mockResolvedValue({
    documents: 3,
    needsReview: 1,
    routed: 1,
    failed: 0,
    pendingReviews: 1,
    activeWorkflowRuns: 1,
  }),
  createBookkeepingDocument: vi.fn().mockResolvedValue({ id: 12 }),
  createBookkeepingDocumentWithAudit: vi.fn().mockResolvedValue({ id: 12 }),
  addReviewItem: vi.fn().mockResolvedValue({ id: 23 }),
  addReviewItemWithAudit: vi.fn().mockResolvedValue({ id: 23 }),
  createWorkflowRun: vi.fn().mockResolvedValue({ id: 34 }),
  createWorkflowRunWithAudit: vi.fn().mockResolvedValue({ id: 34 }),
  updateWorkflowRun: vi.fn().mockResolvedValue(true),
  updateWorkflowRunWithAudit: vi.fn().mockResolvedValue(true),
  getWorkflowRunById: vi.fn().mockResolvedValue(null),
  createRoutingAttempt: vi.fn().mockResolvedValue({ id: 45 }),
  createRoutingAttemptWithAudit: vi.fn().mockResolvedValue({ id: 45 }),
  recordAuditEvent: vi.fn().mockResolvedValue(undefined),
  getReviewQueue: vi.fn().mockResolvedValue([]),
  getRecentWorkflowRuns: vi.fn().mockResolvedValue([]),
  getRecentReconciliationMatches: vi.fn().mockResolvedValue([
    {
      id: 56,
      documentId: 12,
      bankTransactionId: "bank-transaction-1",
      status: "matched",
      confidenceScore: "0.9400",
      amountDifference: "0.00",
      matchedAt: new Date("2026-06-04T10:00:00Z"),
      metadata: {},
      createdAt: new Date("2026-06-04T10:00:00Z"),
      document: { id: 12, originalFilename: "receipt.pdf" },
    },
  ]),
  getRecentAuditEvents: vi.fn().mockResolvedValue([
    {
      id: 77,
      actorUserId: null,
      action: "workflow.document.skipped",
      entityType: "bookkeeping_document",
      entityId: "12",
      details: { reason: "duplicate_document" },
      createdAt: new Date("2026-06-04T11:00:00Z"),
    },
  ]),
  updateReviewItemStatus: vi.fn().mockResolvedValue(true),
}));

vi.mock("./_core/notification", () => ({
  notifyOwner: vi.fn().mockResolvedValue(true),
}));

vi.mock("./stripe/stripe", () => ({
  getStripe: vi.fn().mockReturnValue({}),
  getOrCreateStripeCustomer: vi.fn().mockResolvedValue("cus_test"),
  createCheckoutSession: vi.fn().mockResolvedValue("https://checkout.stripe.com/test"),
  retrieveCheckoutSession: vi.fn(),
  getCustomerSubscriptions: vi.fn().mockResolvedValue({ data: [] }),
  listCustomerInvoices: vi.fn().mockResolvedValue({ data: [] }),
}));

import {
  addReviewItem,
  addReviewItemWithAudit,
  createBookkeepingDocument,
  createBookkeepingDocumentWithAudit,
  createRoutingAttempt,
  createRoutingAttemptWithAudit,
  createWorkflowRun,
  createWorkflowRunWithAudit,
  getBookkeepingOverview,
  getRecentAuditEvents,
  getRecentReconciliationMatches,
  getRecentWorkflowRuns,
  getWorkflowRunById,
  recordAuditEvent,
  updateReviewItemStatus,
  updateWorkflowRun,
  updateWorkflowRunWithAudit,
} from "./db";

function createAdminContext(): TrpcContext {
  return {
    user: {
      id: 1,
      openId: "admin-openid",
      email: "admin@example.com",
      name: "Admin User",
      loginMethod: "manus",
      role: "admin",
      createdAt: new Date(),
      updatedAt: new Date(),
      lastSignedIn: new Date(),
      stripeCustomerId: null,
    },
    req: { protocol: "https", headers: {} } as TrpcContext["req"],
    res: { clearCookie: vi.fn() } as unknown as TrpcContext["res"],
  };
}

function createPublicContext(): TrpcContext {
  return {
    user: null,
    req: { protocol: "https", headers: {} } as TrpcContext["req"],
    res: { clearCookie: vi.fn() } as unknown as TrpcContext["res"],
  };
}

describe("bookkeeping operations router", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("returns operations overview for admins", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.overview();

    expect(result.pendingReviews).toBe(1);
    expect(getBookkeepingOverview).toHaveBeenCalled();
  });

  it("returns the Wave-informed bookkeeping surface model for admins", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.waveSurface();
    const purchases = result.modules.find((module) => module.id === "purchases");
    const receiptRule = result.documentRoutingRules.find((rule) => rule.documentTypes.includes("receipt"));
    const billRule = result.documentRoutingRules.find((rule) => rule.documentTypes.includes("vendor_invoice"));

    expect(purchases?.surfaces.map((surface) => surface.id)).toEqual([
      "bills",
      "vendors",
      "buying_products_services",
    ]);
    expect(receiptRule?.targetSurfaceId).toBe("transactions");
    expect(billRule?.targetSurfaceId).toBe("bills");
    expect(result.integrationChannels).toContain("Google Sheets");
    expect(result.integrationChannels).toContain("Make");
    expect(result.menuInventory.find((group) => group.id === "quick_create")?.items.map((item) => item.label)).toContain(
      "Recurring Invoice"
    );
    expect(result.syncContracts.map((contract) => contract.id)).toContain("documents_to_wave_ledger");
    expect(
      result.syncContracts.find((contract) => contract.id === "banking_reconciliation")?.confirmationRequiredFor
    ).toContain("connect account");
    expect(result.featureInventory.map((page) => page.id)).toContain("transactions_workspace");
    expect(result.featureInventory.map((page) => page.id)).toContain("create_new_menu");
    expect(result.featureInventory.map((page) => page.id)).toContain("estimates_workspace");
    expect(result.featureInventory.map((page) => page.id)).toContain("invoices_workspace");
    expect(result.featureInventory.map((page) => page.id)).toContain("invoice_editor_lifecycle");
    expect(result.featureInventory.map((page) => page.id)).toContain("recurring_invoices_workspace");
    expect(result.featureInventory.map((page) => page.id)).toContain("customer_statement_generator");
    expect(result.featureInventory.map((page) => page.id)).toContain("customers_list_and_form");
    expect(result.featureInventory.map((page) => page.id)).toContain("customer_form_fields");
    expect(result.featureInventory.map((page) => page.id)).toContain("customer_csv_import_page");
    expect(result.featureInventory.map((page) => page.id)).toContain("bills_workspace");
    expect(result.featureInventory.map((page) => page.id)).toContain("bill_add_form_fields");
    expect(result.featureInventory.map((page) => page.id)).toContain("vendor_form_fields");
    expect(result.featureInventory.map((page) => page.id)).toContain("buying_products_services_legacy");
    expect(result.featureInventory.map((page) => page.id)).toContain("buying_product_service_form_fields");
    expect(result.featureInventory.map((page) => page.id)).toContain("products_services_legacy");
    expect(result.featureInventory.map((page) => page.id)).toContain("product_service_form_fields");
    expect(result.featureInventory.map((page) => page.id)).toContain("transaction_row_controls");
    expect(result.featureInventory.map((page) => page.id)).toContain("transaction_add_form_fields");
    expect(result.featureInventory.map((page) => page.id)).toContain("transaction_statement_upload_page");
    expect(result.featureInventory.map((page) => page.id)).toContain("chart_accounts_workspace");
    expect(result.featureInventory.map((page) => page.id)).toContain("chart_account_editor_controls");
    expect(result.featureInventory.map((page) => page.id)).toContain("dashboard_widgets_and_offers");
    expect(result.featureInventory.map((page) => page.id)).toContain("wave_payments_page");
    expect(result.featureInventory.map((page) => page.id)).toContain("sales_tax_settings_page");
    expect(result.featureInventory.map((page) => page.id)).toContain("payroll_unsupported_page");
    expect(result.featureInventory.map((page) => page.id)).toContain("payroll_business_eligibility_settings");
    expect(
      result.featureInventory
        .find((page) => page.id === "reports_catalog")
        ?.controls.map((control) => control.label)
    ).toContain("Sales Tax Report");
    expect(
      result.featureInventory
        .find((page) => page.id === "reports_catalog")
        ?.controls.map((control) => control.label)
    ).toEqual(expect.arrayContaining([
      "Income by Customer",
      "Customer Credits",
      "Purchases by Vendor",
      "Account Balances",
      "Account Transactions (General Ledger)",
    ]));
    expect(result.actions.map((action) => action.id)).toContain("invoice_send");
    expect(result.actions.map((action) => action.id)).toContain("estimate_customer_filter");
    expect(result.actions.map((action) => action.id)).toContain("estimate_status_filter");
    expect(result.actions.map((action) => action.id)).toContain("estimate_clear_filters");
    expect(result.actions.map((action) => action.id)).toContain("estimate_tab_view");
    expect(result.actions.map((action) => action.id)).toContain("estimate_pdf_dialog_close");
    expect(result.actions.map((action) => action.id)).toContain("invoice_summary_metrics_read");
    expect(result.actions.map((action) => action.id)).toContain("invoice_customer_filter");
    expect(result.actions.map((action) => action.id)).toContain("invoice_status_filter");
    expect(result.actions.map((action) => action.id)).toContain("invoice_clear_filters");
    expect(result.actions.map((action) => action.id)).toContain("invoice_tab_view");
    expect(result.actions.map((action) => action.id)).toContain("invoice_view_all");
    expect(result.actions.map((action) => action.id)).toContain("invoice_send_reminder");
    expect(result.actions.map((action) => action.id)).toContain("recurring_invoice_customer_filter");
    expect(result.actions.map((action) => action.id)).toContain("recurring_invoice_tab_view");
    expect(result.actions.map((action) => action.id)).toContain("recurring_invoice_table_read");
    expect(result.actions.map((action) => action.id)).toContain("recurring_invoice_view_drafts");
    expect(result.actions.map((action) => action.id)).toContain("customer_statement_help_open");
    expect(result.actions.map((action) => action.id)).toContain("customer_statement_customer_select");
    expect(result.actions.map((action) => action.id)).toContain("customer_statement_type_select");
    expect(result.actions.map((action) => action.id)).toContain("customer_statement_create");
    expect(result.actions.map((action) => action.id)).toContain("customer_list_read");
    expect(result.actions.map((action) => action.id)).toContain("customer_row_actions_open");
    expect(result.actions.map((action) => action.id)).toContain("customer_view");
    expect(result.actions.map((action) => action.id)).toContain("customer_form_fill");
    expect(result.actions.map((action) => action.id)).toContain("customer_add_phone");
    expect(result.actions.map((action) => action.id)).toContain("customer_add_contact");
    expect(result.actions.map((action) => action.id)).toContain("customer_clear_address");
    expect(result.actions.map((action) => action.id)).toContain("customer_cancel_form");
    expect(result.actions.map((action) => action.id)).toContain("customer_create_invoice");
    expect(result.actions.map((action) => action.id)).toContain("customer_delete");
    expect(result.actions.map((action) => action.id)).toContain("customer_import_csv_choose_file");
    expect(result.actions.map((action) => action.id)).toContain("customer_import_csv_preview");
    expect(result.actions.map((action) => action.id)).toContain("customer_import_csv_instructions");
    expect(result.actions.map((action) => action.id)).toContain("customer_import_csv_template_download");
    expect(result.actions.map((action) => action.id)).toContain("product_service_list_read");
    expect(result.actions.map((action) => action.id)).toContain("product_service_form_read");
    expect(result.actions.map((action) => action.id)).toContain("product_service_form_fill");
    expect(result.actions.map((action) => action.id)).toContain("product_service_sell_toggle");
    expect(result.actions.map((action) => action.id)).toContain("product_service_buy_toggle");
    expect(result.actions.map((action) => action.id)).toContain("product_service_income_account_select");
    expect(result.actions.map((action) => action.id)).toContain("product_service_expense_account_select");
    expect(result.actions.map((action) => action.id)).toContain("product_service_tax_select");
    expect(result.actions.map((action) => action.id)).toContain("selling_product_service_upsert");
    expect(result.actions.map((action) => action.id)).toContain("product_service_update");
    expect(result.actions.map((action) => action.id)).toContain("product_service_delete");
    expect(result.actions.map((action) => action.id)).toContain("bill_workspace_read");
    expect(result.actions.map((action) => action.id)).toContain("bill_form_read");
    expect(result.actions.map((action) => action.id)).toContain("bill_form_fill");
    expect(result.actions.map((action) => action.id)).toContain("bill_line_item_fill");
    expect(result.actions.map((action) => action.id)).toContain("bill_line_item_delete");
    expect(result.actions.map((action) => action.id)).toContain("bill_cancel_form");
    expect(result.actions.map((action) => action.id)).toContain("bill_attach_receipt");
    expect(result.actions.map((action) => action.id)).toContain("vendor_list_read");
    expect(result.actions.map((action) => action.id)).toContain("vendor_form_read");
    expect(result.actions.map((action) => action.id)).toContain("vendor_form_fill");
    expect(result.actions.map((action) => action.id)).toContain("vendor_import_menu_open");
    expect(result.actions.map((action) => action.id)).toContain("vendor_create_bill");
    expect(result.actions.map((action) => action.id)).toContain("buying_product_service_list_read");
    expect(result.actions.map((action) => action.id)).toContain("buying_product_service_form_read");
    expect(result.actions.map((action) => action.id)).toContain("buying_product_service_default_state_read");
    expect(result.actions.map((action) => action.id)).toContain("buying_product_service_form_fill");
    expect(result.actions.map((action) => action.id)).toContain("buying_product_service_sell_toggle");
    expect(result.actions.map((action) => action.id)).toContain("buying_product_service_buy_toggle");
    expect(result.actions.map((action) => action.id)).toContain("buying_product_service_income_account_select");
    expect(result.actions.map((action) => action.id)).toContain("buying_product_service_expense_account_select");
    expect(result.actions.map((action) => action.id)).toContain("buying_product_service_account_create");
    expect(result.actions.map((action) => action.id)).toContain("buying_product_service_tax_select");
    expect(result.actions.map((action) => action.id)).toContain("buying_product_service_update");
    expect(result.actions.map((action) => action.id)).toContain("buying_product_service_delete");
    expect(result.actions.map((action) => action.id)).toContain("transaction_attach_receipt");
    expect(result.actions.map((action) => action.id)).toContain("transaction_workspace_read");
    expect(result.actions.map((action) => action.id)).toContain("transaction_more_menu_open");
    expect(result.actions.map((action) => action.id)).toContain("transaction_account_filter_select");
    expect(result.actions.map((action) => action.id)).toContain("transaction_account_upload_statement");
    expect(result.actions.map((action) => action.id)).toContain("transaction_account_create");
    expect(result.actions.map((action) => action.id)).toContain("transaction_add_deposit");
    expect(result.actions.map((action) => action.id)).toContain("transaction_add_withdrawal");
    expect(result.actions.map((action) => action.id)).toContain("transaction_add_journal_entry");
    expect(result.actions.map((action) => action.id)).toContain("transaction_form_fill");
    expect(result.actions.map((action) => action.id)).toContain("transaction_account_select");
    expect(result.actions.map((action) => action.id)).toContain("transaction_category_select");
    expect(result.actions.map((action) => action.id)).toContain("transaction_sales_tax_toggle");
    expect(result.actions.map((action) => action.id)).toContain("transaction_vendor_select");
    expect(result.actions.map((action) => action.id)).toContain("transaction_row_read");
    expect(result.actions.map((action) => action.id)).toContain("transaction_sort_newest_to_oldest");
    expect(result.actions.map((action) => action.id)).toContain("transaction_sort_oldest_to_newest");
    expect(result.actions.map((action) => action.id)).toContain("transaction_search_submit");
    expect(result.actions.map((action) => action.id)).toContain("transaction_load_more");
    expect(result.actions.map((action) => action.id)).toContain("statement_upload_instructions_read");
    expect(result.actions.map((action) => action.id)).toContain("statement_file_choose");
    expect(result.actions.map((action) => action.id)).toContain("statement_payment_account_select");
    expect(result.actions.map((action) => action.id)).toContain("statement_csv_template_download");
    expect(result.actions.map((action) => action.id)).toContain("chart_account_list_read");
    expect(result.actions.map((action) => action.id)).toContain("chart_account_help_open");
    expect(result.actions.map((action) => action.id)).toContain("chart_account_tab_view");
    expect(result.actions.map((action) => action.id)).toContain("chart_account_section_read");
    expect(result.actions.map((action) => action.id)).toContain("chart_account_activity_read");
    expect(result.actions.map((action) => action.id)).toContain("chart_account_form_read");
    expect(result.actions.map((action) => action.id)).toContain("chart_account_type_picker_open");
    expect(result.actions.map((action) => action.id)).toContain("chart_account_type_search");
    expect(result.actions.map((action) => action.id)).toContain("chart_account_type_select");
    expect(result.actions.map((action) => action.id)).toContain("chart_account_currency_select");
    expect(result.actions.map((action) => action.id)).toContain("chart_account_form_fill");
    expect(result.actions.map((action) => action.id)).toContain("chart_account_cancel_form");
    expect(result.actions.map((action) => action.id)).toContain("connected_account_unavailable_read");
    expect(result.actions.map((action) => action.id)).toContain("connected_account_manual_entry_plan");
    expect(result.actions.map((action) => action.id)).toContain("connected_account_statement_help_open");
    expect(result.actions.map((action) => action.id)).toContain("connected_account_statement_format_read");
    expect(result.actions.map((action) => action.id)).toContain("connected_account_statement_upload_steps_read");
    expect(result.actions.map((action) => action.id)).toContain("connected_account_statement_mapping_confirm");
    expect(result.actions.map((action) => action.id)).toContain("connected_account_statement_upload_complete");
    expect(result.actions.map((action) => action.id)).toContain("connected_account_csv_help_open");
    expect(result.actions.map((action) => action.id)).toContain("connected_account_payment_account_help_open");
    expect(result.actions.map((action) => action.id)).toContain("connected_account_wave_connect_help_open");
    expect(result.actions.map((action) => action.id)).toContain("connected_account_auto_updates_help_open");
    expect(result.actions.map((action) => action.id)).toContain("connected_account_support_open");
    expect(result.actions.map((action) => action.id)).toContain("connected_account_help_feedback");
    expect(result.actions.map((action) => action.id)).toContain("connected_account_help_share");
    expect(result.actions.map((action) => action.id)).toContain("business_checking_offer_read");
    expect(result.actions.map((action) => action.id)).toContain("business_checking_feature_read");
    expect(result.actions.map((action) => action.id)).toContain("business_checking_claim_steps_read");
    expect(result.actions.map((action) => action.id)).toContain("business_checking_promo_terms_read");
    expect(result.actions.map((action) => action.id)).toContain("transaction_reconcile");
    expect(result.actions.map((action) => action.id)).toContain("statement_upload");
    expect(result.actions.map((action) => action.id)).toContain("report_catalog_section_read");
    expect(result.actions.map((action) => action.id)).toContain("report_date_range_set");
    expect(result.actions.map((action) => action.id)).toContain("report_as_of_date_set");
    expect(result.actions.map((action) => action.id)).toContain("report_basis_select");
    expect(result.actions.map((action) => action.id)).toContain("report_account_filter_select");
    expect(result.actions.map((action) => action.id)).toContain("report_contact_filter_select");
    expect(result.actions.map((action) => action.id)).toContain("report_update");
    expect(result.actions.map((action) => action.id)).toContain("report_table_read");
    expect(result.actions.map((action) => action.id)).toContain("report_empty_state_read");
    expect(result.actions.map((action) => action.id)).toContain("wave_payments_get_started");
    expect(result.actions.map((action) => action.id)).toContain("sales_tax_create");
    expect(result.actions.map((action) => action.id)).toContain("financial_settings_update");
    expect(result.actions.map((action) => action.id)).toContain("zoho_offer_open");
    expect(result.actions.map((action) => action.id)).toContain("payroll_availability_read");
    expect(result.actions.map((action) => action.id)).toContain("payroll_eligibility_rules_read");
    expect(result.actions.map((action) => action.id)).toContain("payroll_business_settings_open");
    expect(result.actions.map((action) => action.id)).toContain("payroll_currency_read");
    expect(result.actions.map((action) => action.id)).toContain("business_profile_update");

    const actionIds = new Set(result.actions.map((action) => action.id));
    const missingControlActions = result.featureInventory
      .flatMap((page) => page.controls)
      .filter((control) => control.fabActionId && !actionIds.has(control.fabActionId))
      .map((control) => control.fabActionId);
    expect(missingControlActions).toEqual([]);
  });

  it("returns Wave parity coverage counts", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.waveParity();

    expect(result.modules).toBeGreaterThanOrEqual(8);
    expect(result.surfaces).toBeGreaterThanOrEqual(24);
    expect(result.menuGroups).toBeGreaterThanOrEqual(6);
    expect(result.menuItems).toBeGreaterThanOrEqual(20);
    expect(result.syncContracts).toBeGreaterThanOrEqual(5);
    expect(result.featurePages).toBeGreaterThanOrEqual(48);
    expect(result.observedControls).toBeGreaterThanOrEqual(525);
    expect(result.actions).toBeGreaterThanOrEqual(265);
    expect(result.actionsBySafety.requires_confirmation).toBeGreaterThan(0);
    expect(result.pagesByAutomationMode.safe_draft).toBeGreaterThan(0);
  });

  it("returns the MijnGeldzaken master-ledger surface model for admins", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.mijngeldzakenSurface();
    const masterLedger = result.modules.find((module) => module.id === "master_ledger");
    const contract = result.syncContracts.find((item) => item.id === "fab_master_ledger_to_mijngeldzaken");
    const pageIds = result.featureInventory.map((page) => page.id);
    const actionIds = new Set(result.actions.map((action) => action.id));
    const missingControlActions = result.featureInventory
      .flatMap((page) => page.controls)
      .filter((control) => !actionIds.has(control.actionId))
      .map((control) => control.actionId);

    expect(masterLedger?.surfaces).toEqual(expect.arrayContaining(["transactions", "categories", "budgets"]));
    expect(contract?.fabOwns).toContain("canonical transaction identity");
    expect(contract?.mijngeldzakenOwns).toContain("budget views");
    expect(pageIds).toContain("authenticated_sidebar_navigation");
    expect(pageIds).toContain("household_bookkeeping");
    expect(pageIds).toContain("document_vault");
    expect(actionIds).toContain("transaction_import_prepare");
    expect(actionIds).toContain("receipt_upload_prepare");
    expect(actionIds).toContain("category_mapping_prepare");
    expect(missingControlActions).toEqual([]);
  });

  it("returns MijnGeldzaken parity and action planning controls", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const parity = await caller.bookkeeping.mijngeldzakenParity();
    const ready = await caller.bookkeeping.planMijngeldzakenAction({
      surfaceId: "transactions",
      actionId: "transaction_import_prepare",
      payload: {
        date: "2026-06-28",
        amount: 42.5,
        description: "Weekly groceries",
        category: "Huishouden",
      },
    });
    const missing = await caller.bookkeeping.planMijngeldzakenAction({
      surfaceId: "transactions",
      actionId: "transaction_import_prepare",
      payload: {
        amount: 42.5,
      },
    });
    const confirmation = await caller.bookkeeping.planMijngeldzakenAction({
      surfaceId: "transactions",
      actionId: "transaction_import_submit",
      payload: {
        importBatchId: "batch-1",
      },
    });

    expect(parity.modules).toBeGreaterThanOrEqual(7);
    expect(parity.surfaces).toBeGreaterThanOrEqual(20);
    expect(parity.actionsBySafety.safe_draft).toBeGreaterThan(0);
    expect(parity.actionsBySafety.requires_confirmation).toBeGreaterThan(0);
    expect(parity.pagesByAutomationMode.requires_user_auth).toBeGreaterThan(0);
    expect(ready.status).toBe("planned");
    expect(ready.canRunAutonomously).toBe(true);
    expect(missing.status).toBe("needs_fields");
    expect(missing.missingFields).toEqual(expect.arrayContaining(["date", "description", "category"]));
    expect(confirmation.status).toBe("planned");
    expect(confirmation.requiresConfirmation).toBe(true);
    expect(confirmation.canRunAutonomously).toBe(false);
  });

  it("dry-runs MijnGeldzaken execution and records an audit event", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.executeMijngeldzakenAction({
      surfaceId: "transactions",
      actionId: "transaction_import_prepare",
      mode: "dry_run",
      payload: {
        date: "2026-06-28",
        amount: 42.5,
        description: "Weekly groceries",
        category: "Huishouden",
      },
    });

    expect(result.status).toBe("planned");
    expect(result.operation?.operationId).toMatch(/^mijngeldzaken:/);
    expect(result.operation?.targetSystem).toBe("mijngeldzaken");
    expect(recordAuditEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        action: "mijngeldzaken_action.dry_run",
        entityType: "mijngeldzaken_action",
      })
    );
  });

  it("blocks and queues MijnGeldzaken confirmed actions by policy", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const missing = await caller.bookkeeping.executeMijngeldzakenAction({
      surfaceId: "transactions",
      actionId: "transaction_import_prepare",
      mode: "queue",
      payload: {
        amount: 42.5,
      },
    });
    const credentials = await caller.bookkeeping.executeMijngeldzakenAction({
      surfaceId: "login",
      actionId: "login_submit",
      mode: "queue",
      payload: {},
    });
    const blocked = await caller.bookkeeping.executeMijngeldzakenAction({
      surfaceId: "transactions",
      actionId: "transaction_import_submit",
      mode: "queue",
      payload: {
        importBatchId: "batch-1",
      },
    });
    const queued = await caller.bookkeeping.executeMijngeldzakenAction({
      surfaceId: "transactions",
      actionId: "transaction_import_submit",
      mode: "queue",
      confirmed: true,
      payload: {
        importBatchId: "batch-1",
      },
    });

    expect(missing.status).toBe("needs_review");
    expect(missing.missingFields).toEqual(expect.arrayContaining(["date", "description", "category"]));
    expect(credentials.status).toBe("blocked_requires_credentials");
    expect(blocked.status).toBe("blocked_requires_confirmation");
    expect(queued.status).toBe("queued");
    expect(queued.operation?.safety).toBe("requires_confirmation");
    expect(recordAuditEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        action: "mijngeldzaken_action.queue",
        entityType: "mijngeldzaken_action",
      })
    );
  });

  it("returns the competitor-informed autonomous bookkeeping playbook", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.automationPlaybook();

    expect(result.sources.map((source) => source.id)).toEqual([
      "booke_ai",
      "outmin",
      "bookeeping_ai",
      "layernext",
    ]);
    expect(result.stages.map((stage) => stage.id)).toContain("match_reconcile");
    expect(result.capabilities.map((capability) => capability.id)).toContain("month_end_close_pack");
    expect(result.capabilities.map((capability) => capability.id)).toContain("ledger_report_reconciliation");
    expect(
      result.capabilities.find((capability) => capability.id === "ledger_report_reconciliation")?.waveActions
    ).toContain("report_table_read");
    expect(result.capabilities.find((capability) => capability.id === "app_layer_executor")?.waveActions).toContain(
      "transaction_add"
    );
    expect(result.serviceOfferings.map((service) => service.id)).toContain("layernext_custom_erp_desktop");
    expect(
      result.serviceOfferings.find((service) => service.id === "bookeeping_vertical_templates")?.netherlandsAdaptation
    ).toContain("ZZP");
    expect(result.benchmarkAreas.map((area) => area.id)).toContain("continuous_reconciliation");
    expect(result.benchmarkAreas.find((area) => area.id === "inside_accounting_platform_execution")?.riskControl).toContain(
      "confirmed execution"
    );
  });

  it("summarizes automation playbook coverage", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.automationParity();

    expect(result.sources).toBe(4);
    expect(result.stages).toBeGreaterThanOrEqual(8);
    expect(result.capabilities).toBeGreaterThanOrEqual(10);
    expect(result.serviceOfferings).toBeGreaterThanOrEqual(20);
    expect(result.servicesBySource.booke_ai).toBeGreaterThan(0);
    expect(result.servicesBySource.outmin).toBeGreaterThan(0);
    expect(result.servicesBySource.bookeeping_ai).toBeGreaterThan(0);
    expect(result.servicesBySource.layernext).toBeGreaterThan(0);
    expect(result.servicesByStatus.planned).toBeGreaterThan(0);
    expect(result.servicesByCategory.platform).toBeGreaterThan(0);
    expect(result.benchmarkAreas).toBeGreaterThanOrEqual(10);
    expect(result.benchmarkByStatus.partial).toBeGreaterThan(0);
    expect(result.highPriorityBenchmarkGaps).toBeGreaterThan(0);
    expect(result.waveLinkedCapabilities).toBeGreaterThan(0);
    expect(result.capabilitiesByAutonomy.safe_draft).toBeGreaterThan(0);
  });

  it("plans autonomous capabilities with signal and review gates", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const ready = await caller.bookkeeping.planAutomationCapability({
      capabilityId: "receipt_to_bank_match",
      availableSignals: ["source_document", "bank_transaction", "duplicate_fingerprint"],
      confidence: 0.94,
    });
    const missing = await caller.bookkeeping.planAutomationCapability({
      capabilityId: "ap_invoice_workflow",
      availableSignals: ["vendor_invoice"],
      confidence: 0.98,
    });
    const lowConfidence = await caller.bookkeeping.planAutomationCapability({
      capabilityId: "vendor_category_learning",
      availableSignals: ["vendor_identity", "category_candidates"],
      confidence: 0.7,
    });
    const ledgerReport = await caller.bookkeeping.planAutomationCapability({
      capabilityId: "ledger_report_reconciliation",
      availableSignals: ["ledger_period", "account_scope", "reconciliation_status"],
      confidence: 0.95,
    });

    expect(ready.status).toBe("ready");
    expect(ready.canRunAutonomously).toBe(true);
    expect(ledgerReport.status).toBe("ready");
    expect(ledgerReport.canRunAutonomously).toBe(true);
    expect(missing.status).toBe("needs_signals");
    expect(missing.missingSignals).toContain("vendor_identity");
    expect(lowConfidence.status).toBe("blocked_by_review");
    expect(lowConfidence.reviewGates).toContain("confidence below 85%");
  });

  it("plans autonomous reconciliation workflows with Wave ledger steps", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.planAutomationWorkflow({
      workflowId: "daily_reconciliation_run",
      fromDate: "2026-06-28",
      toDate: "2026-06-28",
      accountOption: "-1",
      contactOption: "0",
      availableSignals: [
        "ledger_period",
        "account_scope",
        "reconciliation_status",
        "source_document",
        "bank_transaction",
        "duplicate_fingerprint",
      ],
      confidence: 0.96,
    });

    expect(result.status).toBe("ready");
    expect(result.canRunAutonomously).toBe(true);
    expect(result.steps.map((step) => step.actionId)).toContain("report_table_read");
    expect(result.steps.map((step) => step.actionId)).toContain("report_empty_state_read");
  });

  it("plans MijnGeldzaken master-ledger sync workflows with downstream steps", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.planAutomationWorkflow({
      workflowId: "mijngeldzaken_master_ledger_sync",
      fromDate: "2026-06-01",
      toDate: "2026-06-30",
      availableSignals: [
        "ledger_period",
        "account_scope",
        "reconciliation_status",
        "vendor_identity",
        "category_candidates",
        "source_document",
        "ocr_text",
        "approved_operation",
        "idempotency_key",
        "target_surface",
      ],
      confidence: 0.96,
    });

    expect(result.status).toBe("ready");
    expect(result.canRunAutonomously).toBe(true);
    expect(new Set(result.steps.map((step) => step.targetSystem))).toEqual(new Set(["mijngeldzaken"]));
    expect(result.steps.map((step) => step.actionId)).toEqual(
      expect.arrayContaining([
        "current_month_read",
        "category_list_read",
        "category_mapping_prepare",
        "transaction_import_prepare",
        "receipt_upload_prepare",
      ])
    );
  });

  it("dry-runs queued autonomous workflows as Wave operation batches", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.queueAutomationWorkflow({
      workflowId: "daily_reconciliation_run",
      mode: "dry_run",
      fromDate: "2026-06-28",
      toDate: "2026-06-28",
      accountOption: "-1",
      contactOption: "0",
      availableSignals: [
        "ledger_period",
        "account_scope",
        "reconciliation_status",
        "source_document",
        "bank_transaction",
        "duplicate_fingerprint",
      ],
      confidence: 0.96,
    });

    expect(result.status).toBe("planned");
    expect(result.workflowRunId).toBe(34);
    expect(result.plan.status).toBe("ready");
    expect(result.operations.length).toBe(result.plan.steps.length);
    expect(result.operations.map((operation) => operation.actionId)).toContain("report_table_read");
    expect(createWorkflowRunWithAudit).toHaveBeenCalledWith(
      expect.objectContaining({
        status: "completed",
        triggerSource: "automation:daily_reconciliation_run",
        documentsProcessed: result.operations.length,
        documentsNeedingReview: 0,
      }),
      expect.objectContaining({
        action: "automation_workflow.dry_run",
        entityType: "automation_workflow",
      }),
    );
    expect(recordAuditEvent).not.toHaveBeenCalledWith(expect.objectContaining({ action: "automation_workflow.dry_run" }));
  });

  it("dry-runs queued MijnGeldzaken master-ledger workflows as downstream operation batches", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.queueAutomationWorkflow({
      workflowId: "mijngeldzaken_master_ledger_sync",
      mode: "dry_run",
      fromDate: "2026-06-01",
      toDate: "2026-06-30",
      availableSignals: [
        "ledger_period",
        "account_scope",
        "reconciliation_status",
        "vendor_identity",
        "category_candidates",
        "source_document",
        "ocr_text",
        "approved_operation",
        "idempotency_key",
        "target_surface",
      ],
      confidence: 0.96,
    });

    expect(result.status).toBe("planned");
    expect(result.workflowRunId).toBe(34);
    expect(result.plan.status).toBe("ready");
    expect(result.operations.length).toBe(result.plan.steps.length);
    expect(new Set(result.operations.map((operation) => operation.targetSystem))).toEqual(new Set(["mijngeldzaken"]));
    expect(result.operations.every((operation) => operation.operationId.startsWith("mijngeldzaken:"))).toBe(true);
    expect(result.operations.map((operation) => operation.actionId)).toContain("transaction_import_prepare");
    expect(createWorkflowRunWithAudit).toHaveBeenCalledWith(
      expect.objectContaining({
        status: "completed",
        triggerSource: "automation:mijngeldzaken_master_ledger_sync",
        documentsProcessed: result.operations.length,
        documentsNeedingReview: 0,
      }),
      expect.objectContaining({
        action: "automation_workflow.dry_run",
        entityType: "automation_workflow",
      }),
    );
    expect(recordAuditEvent).not.toHaveBeenCalledWith(expect.objectContaining({ action: "automation_workflow.dry_run" }));
  });

  it("blocks autonomous workflow queueing when required signals are missing", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.queueAutomationWorkflow({
      workflowId: "daily_reconciliation_run",
      mode: "queue",
      fromDate: "2026-06-28",
      toDate: "2026-06-28",
      accountOption: "-1",
      contactOption: "0",
      availableSignals: ["ledger_period"],
      confidence: 0.96,
    });

    expect(result.status).toBe("needs_signals");
    expect(result.workflowRunId).toBe(34);
    expect(result.plan.missingSignals).toContain("account_scope");
    expect(result.operations.length).toBe(result.plan.steps.length);
    expect(createWorkflowRunWithAudit).toHaveBeenCalledWith(
      expect.objectContaining({
        status: "completed_with_review",
        triggerSource: "automation:daily_reconciliation_run",
        documentsProcessed: result.operations.length,
      }),
      expect.objectContaining({
        action: "automation_workflow.blocked",
        entityType: "automation_workflow",
      }),
    );
    expect(recordAuditEvent).not.toHaveBeenCalledWith(expect.objectContaining({ action: "automation_workflow.blocked" }));
    expect(addReviewItemWithAudit).toHaveBeenCalledWith(
      expect.objectContaining({
        documentId: null,
        reason: "autonomous_wave_workflow_blocked",
        status: "pending",
        correctedData: expect.objectContaining({
          workflowRunId: 34,
          workflowId: "daily_reconciliation_run",
          source: "automation_workflow.blocked",
          missingSignals: expect.arrayContaining(["account_scope"]),
        }),
      }),
      expect.objectContaining({ action: "review_item.create_autonomous_exception" }),
    );
    expect(recordAuditEvent).not.toHaveBeenCalledWith(
      expect.objectContaining({
        action: "automation_workflow.queue",
      })
    );
  });

  it("dry-runs Wave actions with safety and required field checks", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const ready = await caller.bookkeeping.planWaveAction({
      surfaceId: "transactions",
      actionId: "transaction_add",
      payload: {
        date: "2026-06-28",
        amount: 12.5,
        account: "Checking",
        category: "Office Supplies",
      },
    });
    const missing = await caller.bookkeeping.planWaveAction({
      surfaceId: "bills",
      actionId: "bill_create",
      payload: { vendor: "ACME" },
    });
    const confirmation = await caller.bookkeeping.planWaveAction({
      surfaceId: "invoices",
      actionId: "invoice_send",
      payload: { invoiceId: "inv-1", recipientEmail: "customer@example.com" },
    });

    expect(ready.status).toBe("planned");
    expect(ready.canRunAutonomously).toBe(true);
    expect(missing.status).toBe("needs_fields");
    expect(missing.missingFields).toContain("billDate");
    expect(confirmation.requiresConfirmation).toBe(true);
    expect(confirmation.canRunAutonomously).toBe(false);
  });

  it("rejects action plans for mismatched Wave surfaces", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.planWaveAction({
      surfaceId: "bills",
      actionId: "invoice_send",
      payload: { invoiceId: "inv-1", recipientEmail: "customer@example.com" },
    });

    expect(result.status).toBe("unsupported");
    expect(result.canRunAutonomously).toBe(false);
  });

  it("dry-runs Wave execution and records an audit event", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.executeWaveAction({
      surfaceId: "transactions",
      actionId: "transaction_add",
      mode: "dry_run",
      payload: {
        date: "2026-06-28",
        amount: 12.5,
        account: "Checking",
        category: "Office Supplies",
      },
    });

    expect(result.status).toBe("planned");
    expect(result.operation?.operationId).toMatch(/^wave:/);
    expect(recordAuditEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        action: "wave_action.dry_run",
        entityType: "wave_action",
      })
    );
  });

  it("blocks high-impact Wave execution without confirmation", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.executeWaveAction({
      surfaceId: "invoices",
      actionId: "invoice_send",
      mode: "queue",
      payload: { invoiceId: "inv-1", recipientEmail: "customer@example.com" },
    });

    expect(result.status).toBe("blocked_requires_confirmation");
    expect(recordAuditEvent).not.toHaveBeenCalledWith(
      expect.objectContaining({
        action: "wave_action.queue",
      })
    );
  });

  it("queues confirmed Wave execution requests", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.executeWaveAction({
      surfaceId: "invoices",
      actionId: "invoice_send",
      mode: "queue",
      confirmed: true,
      payload: { invoiceId: "inv-1", recipientEmail: "customer@example.com" },
    });

    expect(result.status).toBe("queued");
    expect(result.operation?.safety).toBe("requires_confirmation");
    expect(recordAuditEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        action: "wave_action.queue",
      })
    );
  });

  it("rejects unauthenticated operations access", async () => {
    const caller = appRouter.createCaller(createPublicContext());

    await expect(caller.bookkeeping.overview()).rejects.toThrow();
  });

  it("returns recent reconciliation matches for admins", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.reconciliationMatches();

    expect(result[0].bankTransactionId).toBe("bank-transaction-1");
    expect(result[0].document?.originalFilename).toBe("receipt.pdf");
    expect(getRecentReconciliationMatches).toHaveBeenCalledWith(10);
  });

  it("returns recent audit events for admins", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.auditEvents({ limit: 12 });

    expect(result[0].action).toBe("workflow.document.skipped");
    expect(getRecentAuditEvents).toHaveBeenCalledWith(12);
  });

  it("summarizes autonomous workflow runs from persisted metadata", async () => {
    vi.mocked(getRecentWorkflowRuns).mockResolvedValueOnce([
      {
        id: 101,
        status: "queued",
        triggerSource: "automation:daily_reconciliation_run",
        documentsImported: 0,
        documentsProcessed: 3,
        documentsNeedingReview: 1,
        errorMessage: null,
        startedAt: new Date("2026-06-28T10:00:00Z"),
        finishedAt: null,
        createdAt: new Date("2026-06-28T10:00:00Z"),
        metadata: {
          workflowId: "daily_reconciliation_run",
          mode: "queue",
          planStatus: "ready",
          canRunAutonomously: true,
          missingSignals: [],
          reviewGates: ["empty ledger scope"],
          operations: [
            {
              operationId: "wave:open",
              targetSystem: "waveapps",
              stepId: "open_account_transactions_report",
              surfaceId: "reports",
              actionId: "report_open",
              mode: "read",
              safety: "read_only",
            },
            {
              operationId: "mijngeldzaken:read",
              targetSystem: "mijngeldzaken",
              stepId: "read_mijngeldzaken_current_month",
              surfaceId: "current_month",
              actionId: "current_month_read",
              mode: "read",
              safety: "read_only",
            },
            {
              operationId: "mijngeldzaken:transaction-import",
              workflowId: "daily_reconciliation_run",
              targetSystem: "mijngeldzaken",
              stepId: "prepare_mijngeldzaken_transaction_import",
              surfaceId: "transactions",
              actionId: "transaction_import_prepare",
              mode: "import",
              safety: "safe_draft",
              payload: {
                date: "2026-06-28",
                amount: 42.5,
                description: "Weekly groceries",
                counterparty: "Local Supermarket",
                category: "Huishouden",
                account: "Huishouden",
                currency: "EUR",
              },
            },
          ],
          blockingActions: [],
        },
      },
      {
        id: 102,
        status: "completed",
        triggerSource: "google_drive",
        documentsImported: 1,
        documentsProcessed: 1,
        documentsNeedingReview: 0,
        errorMessage: null,
        startedAt: new Date("2026-06-28T11:00:00Z"),
        finishedAt: new Date("2026-06-28T11:01:00Z"),
        createdAt: new Date("2026-06-28T11:00:00Z"),
        metadata: { folder: "sort out" },
      },
    ]);
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.autonomousWorkflowRuns();

    expect(result).toHaveLength(1);
    expect(result[0]).toEqual(
      expect.objectContaining({
        id: 101,
        workflowId: "daily_reconciliation_run",
        mode: "queue",
        operationCount: 3,
        targetSystems: ["mijngeldzaken", "waveapps"],
        targetBreakdown: {
          mijngeldzaken: 2,
          waveapps: 1,
        },
        waveOperationCount: 1,
        mijngeldzakenOperationCount: 2,
        documentsNeedingReview: 1,
        masterLedger: expect.objectContaining({
          totalRows: 3,
          blockedRows: 0,
          readyForDraft: 1,
          readyForExternalExecution: 1,
          ledgerChecksum: expect.any(String),
        }),
      })
    );
    expect(result[0].masterLedger.ledgerChecksum).toHaveLength(64);
    expect(result[0].operations.map((operation) => operation.actionId)).toEqual([
      "report_open",
      "current_month_read",
      "transaction_import_prepare",
    ]);
    expect(result[0].operations.map((operation) => operation.targetSystem)).toEqual([
      "waveapps",
      "mijngeldzaken",
      "mijngeldzaken",
    ]);
    expect(result[0].operations[2]).toEqual(expect.objectContaining({
      masterLedgerDraftType: "transaction_import",
      masterLedgerChecksum: expect.any(String),
    }));
    expect(result[0].operations[2].masterLedgerChecksum).toHaveLength(64);
    expect(getRecentWorkflowRuns).toHaveBeenCalledWith(25);
  });

  it("returns a checksum-bound master-ledger projection for autonomous workflow operations", async () => {
    vi.mocked(getWorkflowRunById).mockResolvedValueOnce({
      id: 101,
      status: "queued",
      triggerSource: "automation:mijngeldzaken_master_ledger_sync",
      documentsImported: 0,
      documentsProcessed: 2,
      documentsNeedingReview: 0,
      errorMessage: null,
      startedAt: new Date("2026-06-28T10:00:00Z"),
      finishedAt: null,
      createdAt: new Date("2026-06-28T10:00:00Z"),
      metadata: {
        workflowId: "mijngeldzaken_master_ledger_sync",
        operations: [
          {
            operationId: "mijngeldzaken:read",
            targetSystem: "mijngeldzaken",
            stepId: "read_current_month",
            surfaceId: "current_month",
            actionId: "current_month_read",
            mode: "read",
            safety: "read_only",
            status: "succeeded",
          },
          {
            operationId: "mijngeldzaken:transaction-import",
            workflowId: "mijngeldzaken_master_ledger_sync",
            targetSystem: "mijngeldzaken",
            stepId: "prepare_mijngeldzaken_transaction_import",
            surfaceId: "transactions",
            actionId: "transaction_import_prepare",
            mode: "import",
            safety: "safe_draft",
            status: "pending",
            payload: {
              date: "2026-06-28",
              amount: 42.5,
              description: "Weekly groceries",
              counterparty: "Local Supermarket",
              category: "Huishouden",
              account: "Huishouden",
              currency: "EUR",
              sourceDocumentId: 12,
            },
          },
        ],
      },
    });
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.automationWorkflowMasterLedger({
      workflowRunId: 101,
      targetSystem: "mijngeldzaken",
      audit: true,
    });

    expect(result.success).toBe(true);
    expect(result.workflowRunId).toBe(101);
    expect(result.workflowId).toBe("mijngeldzaken_master_ledger_sync");
    expect(result.summary.totalRows).toBe(2);
    expect(result.summary.byTargetSystem.mijngeldzaken.rows).toBe(2);
    expect(result.summary.downstreamStatuses.ready_for_draft).toBe(1);
    expect(result.summary.downstreamStatuses.read_completed).toBe(1);
    const importRow = result.rows.find((row) => row.operationId === "mijngeldzaken:transaction-import");
    expect(importRow).toEqual(expect.objectContaining({
      operationId: "mijngeldzaken:transaction-import",
      masterLedgerDraftType: "transaction_import",
      masterLedgerChecksum: expect.any(String),
      readyForDraft: true,
    }));
    expect(importRow?.masterLedgerChecksum).toHaveLength(64);
    expect(result.ledgerChecksum).toHaveLength(64);
    expect(result.csvArtifact.content).toContain("workflowRunId,workflowId,operationId");
    expect(result.csvArtifact.content).toContain("mijngeldzaken:transaction-import");
    expect(recordAuditEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        actorUserId: 1,
        action: "automation_workflow.master_ledger_projection_prepared",
        entityType: "automation_workflow",
        entityId: "101",
        details: expect.objectContaining({
          workflowRunId: 101,
          targetSystem: "mijngeldzaken",
          ledgerChecksum: result.ledgerChecksum,
          totalRows: 2,
          externalSubmission: "not_executed",
        }),
      })
    );
  });

  it("returns a CSV artifact for queued MijnGeldzaken transaction-import workflow drafts", async () => {
    vi.mocked(getWorkflowRunById).mockResolvedValueOnce({
      id: 101,
      status: "queued",
      triggerSource: "automation:mijngeldzaken_master_ledger_sync",
      documentsImported: 0,
      documentsProcessed: 1,
      documentsNeedingReview: 0,
      errorMessage: null,
      startedAt: new Date("2026-06-28T10:00:00Z"),
      finishedAt: null,
      createdAt: new Date("2026-06-28T10:00:00Z"),
      metadata: {
        workflowId: "mijngeldzaken_master_ledger_sync",
        operations: [
          {
            operationId: "mijngeldzaken:transaction-import",
            workflowId: "mijngeldzaken_master_ledger_sync",
            targetSystem: "mijngeldzaken",
            stepId: "prepare_mijngeldzaken_transaction_import",
            surfaceId: "transactions",
            actionId: "transaction_import_prepare",
            mode: "import",
            safety: "safe_draft",
            status: "pending",
            payload: {
              date: "2026-06-28",
              amount: 42.5,
              description: "Weekly groceries",
              counterparty: "Local Supermarket",
              category: "Huishouden",
              account: "Huishouden",
              currency: "EUR",
              sourceDocumentId: 12,
            },
          },
        ],
      },
    });
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.automationWorkflowDraftArtifact({
      workflowRunId: 101,
      operationId: "mijngeldzaken:transaction-import",
      format: "csv",
    });

    expect(result.status).toBe("prepared");
    expect(result.artifact).toEqual(expect.objectContaining({
      format: "csv",
      contentType: "text/csv",
      draftType: "transaction_import",
      externalSubmission: "not_executed",
      checksum: expect.any(String),
    }));
    expect(result.artifact?.checksum).toHaveLength(64);
    expect(result.artifact?.content).toContain("Datum,Omschrijving,Tegenpartij,Bedrag,Categorie,Rekening,Valuta,FAB Document ID");
    expect(result.artifact?.content).toContain("2026-06-28,Weekly groceries,Local Supermarket,42.5,Huishouden,Huishouden,EUR,12");
    expect(recordAuditEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        actorUserId: 1,
        action: "automation_workflow.draft_artifact_prepared",
        entityType: "automation_workflow_operation",
        entityId: "mijngeldzaken:transaction-import",
        details: expect.objectContaining({
          workflowRunId: 101,
          targetSystem: "mijngeldzaken",
          actionId: "transaction_import_prepare",
          format: "csv",
          checksum: result.artifact?.checksum,
          draftType: "transaction_import",
          externalSubmission: "not_executed",
        }),
      })
    );
  });

  it("blocks CSV artifact requests for non-transaction MijnGeldzaken drafts", async () => {
    vi.mocked(getWorkflowRunById).mockResolvedValueOnce({
      id: 101,
      status: "queued",
      triggerSource: "automation:mijngeldzaken_master_ledger_sync",
      documentsImported: 0,
      documentsProcessed: 1,
      documentsNeedingReview: 0,
      errorMessage: null,
      startedAt: new Date("2026-06-28T10:00:00Z"),
      finishedAt: null,
      createdAt: new Date("2026-06-28T10:00:00Z"),
      metadata: {
        workflowId: "mijngeldzaken_master_ledger_sync",
        operations: [
          {
            operationId: "mijngeldzaken:receipt-upload",
            workflowId: "mijngeldzaken_master_ledger_sync",
            targetSystem: "mijngeldzaken",
            stepId: "prepare_mijngeldzaken_receipt_upload",
            surfaceId: "receipts",
            actionId: "receipt_upload_prepare",
            mode: "upload",
            safety: "safe_draft",
            status: "pending",
            payload: {
              documentId: "fab-master-ledger-batch",
              filename: "fab-master-ledger-evidence.zip",
            },
          },
        ],
      },
    });
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.automationWorkflowDraftArtifact({
      workflowRunId: 101,
      operationId: "mijngeldzaken:receipt-upload",
      format: "csv",
    });

    expect(result).toEqual(expect.objectContaining({
      workflowRunId: 101,
      operationId: "mijngeldzaken:receipt-upload",
      status: "unsupported_format",
      supportedFormats: ["json"],
    }));
  });

  it("updates autonomous workflow operation status and recomputes run state", async () => {
    vi.mocked(getWorkflowRunById).mockResolvedValueOnce({
      id: 101,
      status: "queued",
      triggerSource: "automation:daily_reconciliation_run",
      documentsImported: 0,
      documentsProcessed: 2,
      documentsNeedingReview: 0,
      errorMessage: null,
      startedAt: new Date("2026-06-28T10:00:00Z"),
      finishedAt: null,
      createdAt: new Date("2026-06-28T10:00:00Z"),
      metadata: {
        workflowId: "daily_reconciliation_run",
        mode: "queue",
        operations: [
          {
            operationId: "wave:open",
            stepId: "open_account_transactions_report",
            surfaceId: "reports",
            actionId: "report_open",
            mode: "read",
            safety: "read_only",
            status: "succeeded",
          },
          {
            operationId: "wave:read",
            stepId: "read_ledger_rows",
            surfaceId: "reports",
            actionId: "report_table_read",
            mode: "read",
            safety: "read_only",
            status: "pending",
          },
        ],
      },
    });
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.updateAutomationWorkflowOperation({
      workflowRunId: 101,
      operationId: "wave:read",
      status: "succeeded",
      actor: "browser_executor",
      externalId: "wave-report-1",
      evidence: { rowsRead: 12 },
    });

    expect(result.status).toBe("updated");
    expect(result.workflowRun?.status).toBe("completed");
    expect(updateWorkflowRunWithAudit).toHaveBeenCalledWith(
      101,
      expect.objectContaining({
        status: "completed",
        documentsNeedingReview: 0,
        errorMessage: null,
        metadata: expect.objectContaining({
          lastOperationUpdate: expect.objectContaining({
            operationId: "wave:read",
            status: "succeeded",
          }),
        }),
      }),
      expect.objectContaining({ action: "automation_workflow.operation_update" }),
      expect.objectContaining({ expectedMetadata: expect.any(Object) }),
    );
    expect(updateWorkflowRunWithAudit).toHaveBeenCalledWith(
      101,
      expect.any(Object),
      expect.objectContaining({
        action: "automation_workflow.operation_update",
        entityType: "automation_workflow_operation",
        entityId: "wave:read",
      }),
      expect.objectContaining({ expectedMetadata: expect.any(Object) }),
    );
  });

  it("claims the next pending autonomous workflow operation for execution", async () => {
    vi.mocked(getWorkflowRunById).mockResolvedValueOnce({
      id: 101,
      status: "queued",
      triggerSource: "automation:daily_reconciliation_run",
      documentsImported: 0,
      documentsProcessed: 2,
      documentsNeedingReview: 0,
      errorMessage: null,
      startedAt: new Date("2026-06-28T10:00:00Z"),
      finishedAt: null,
      createdAt: new Date("2026-06-28T10:00:00Z"),
      metadata: {
        workflowId: "daily_reconciliation_run",
        mode: "queue",
        operations: [
          {
            operationId: "wave:open",
            stepId: "open_account_transactions_report",
            surfaceId: "reports",
            actionId: "report_open",
            mode: "read",
            safety: "read_only",
            status: "succeeded",
          },
          {
            operationId: "wave:read",
            stepId: "read_ledger_rows",
            surfaceId: "reports",
            actionId: "report_table_read",
            mode: "read",
            safety: "read_only",
            status: "pending",
          },
        ],
      },
    });
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.claimAutomationWorkflowOperation({
      workflowRunId: 101,
      actor: "browser_executor",
      leaseSeconds: 120,
    });

    expect(result.status).toBe("claimed");
    expect(result.operation?.operationId).toBe("wave:read");
    expect(result.operation?.status).toBe("running");
    expect(updateWorkflowRunWithAudit).toHaveBeenCalledWith(
      101,
      expect.objectContaining({
        status: "running",
        finishedAt: null,
        errorMessage: null,
        metadata: expect.objectContaining({
          lastOperationClaim: expect.objectContaining({
            operationId: "wave:read",
            actor: "browser_executor",
          }),
        }),
      }),
      expect.objectContaining({ action: "automation_workflow.operation_claim" }),
      expect.objectContaining({ expectedMetadata: expect.any(Object) }),
    );
    expect(updateWorkflowRunWithAudit).toHaveBeenCalledWith(
      101,
      expect.any(Object),
      expect.objectContaining({
        action: "automation_workflow.operation_claim",
        entityType: "automation_workflow_operation",
        entityId: "wave:read",
      }),
      expect.objectContaining({ expectedMetadata: expect.any(Object) }),
    );
  });

  it("returns a conflict instead of claiming a workflow changed by another executor", async () => {
    const metadata = {
      workflowId: "daily_reconciliation_run",
      operations: [{
        operationId: "wave:read",
        stepId: "read_ledger_rows",
        surfaceId: "reports",
        actionId: "report_table_read",
        mode: "read",
        safety: "read_only",
        status: "pending",
      }],
    };
    vi.mocked(getWorkflowRunById).mockResolvedValueOnce({
      id: 101,
      status: "queued",
      triggerSource: "automation:daily_reconciliation_run",
      documentsImported: 0,
      documentsProcessed: 1,
      documentsNeedingReview: 0,
      errorMessage: null,
      startedAt: new Date("2026-06-28T10:00:00Z"),
      finishedAt: null,
      createdAt: new Date("2026-06-28T10:00:00Z"),
      metadata,
    });
    vi.mocked(updateWorkflowRunWithAudit).mockResolvedValueOnce(false);
    const caller = appRouter.createCaller(createAdminContext());

    await expect(caller.bookkeeping.claimAutomationWorkflowOperation({
      workflowRunId: 101,
      actor: "browser_executor",
      leaseSeconds: 120,
    })).rejects.toMatchObject({ code: "CONFLICT" });
    expect(updateWorkflowRunWithAudit).toHaveBeenCalledWith(
      101,
      expect.any(Object),
      expect.any(Object),
      { expectedMetadata: metadata },
    );
  });

  it("runs one executor cycle and completes read-only Wave operations", async () => {
    vi.mocked(getWorkflowRunById).mockResolvedValueOnce({
      id: 101,
      status: "queued",
      triggerSource: "automation:daily_reconciliation_run",
      documentsImported: 0,
      documentsProcessed: 1,
      documentsNeedingReview: 0,
      errorMessage: null,
      startedAt: new Date("2026-06-28T10:00:00Z"),
      finishedAt: null,
      createdAt: new Date("2026-06-28T10:00:00Z"),
      metadata: {
        workflowId: "daily_reconciliation_run",
        mode: "queue",
        operations: [
          {
            operationId: "wave:read",
            stepId: "read_ledger_rows",
            surfaceId: "reports",
            actionId: "report_table_read",
            mode: "read",
            safety: "read_only",
            status: "pending",
          },
        ],
      },
    });
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.runAutomationWorkflowExecutorCycle({
      workflowRunId: 101,
      actor: "browser_executor",
    });

    expect(result.status).toBe("executed");
    expect(result.operation?.status).toBe("succeeded");
    expect(result.workflowRun?.status).toBe("completed");
    expect(updateWorkflowRunWithAudit).toHaveBeenCalledWith(
      101,
      expect.objectContaining({
        status: "completed",
        documentsNeedingReview: 0,
        errorMessage: null,
        metadata: expect.objectContaining({
          lastOperationUpdate: expect.objectContaining({
            operationId: "wave:read",
            status: "succeeded",
          }),
        }),
      }),
      expect.objectContaining({ action: "automation_workflow.executor_cycle" }),
      expect.objectContaining({ expectedMetadata: expect.any(Object) }),
    );
    expect(updateWorkflowRunWithAudit).toHaveBeenCalledWith(
      101,
      expect.any(Object),
      expect.objectContaining({
        action: "automation_workflow.executor_cycle",
        entityType: "automation_workflow_operation",
        entityId: "wave:read",
      }),
      expect.objectContaining({ expectedMetadata: expect.any(Object) }),
    );
  });

  it("runs one executor cycle and completes read-only MijnGeldzaken operations", async () => {
    vi.mocked(getWorkflowRunById).mockResolvedValueOnce({
      id: 101,
      status: "queued",
      triggerSource: "automation:mijngeldzaken_master_ledger_sync",
      documentsImported: 0,
      documentsProcessed: 1,
      documentsNeedingReview: 0,
      errorMessage: null,
      startedAt: new Date("2026-06-28T10:00:00Z"),
      finishedAt: null,
      createdAt: new Date("2026-06-28T10:00:00Z"),
      metadata: {
        workflowId: "mijngeldzaken_master_ledger_sync",
        mode: "queue",
        operations: [
          {
            operationId: "mijngeldzaken:read",
            targetSystem: "mijngeldzaken",
            stepId: "read_mijngeldzaken_current_month",
            surfaceId: "current_month",
            actionId: "current_month_read",
            mode: "read",
            safety: "read_only",
            status: "pending",
          },
        ],
      },
    });
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.runAutomationWorkflowExecutorCycle({
      workflowRunId: 101,
      actor: "browser_executor",
    });

    expect(result.status).toBe("executed");
    expect(result.operation?.status).toBe("succeeded");
    expect(result.operation?.evidence).toEqual(expect.objectContaining({ targetSystem: "mijngeldzaken" }));
    expect(result.workflowRun?.status).toBe("completed");
    expect(updateWorkflowRunWithAudit).toHaveBeenCalledWith(
      101,
      expect.objectContaining({
        status: "completed",
        documentsNeedingReview: 0,
        errorMessage: null,
        metadata: expect.objectContaining({
          lastOperationUpdate: expect.objectContaining({
            operationId: "mijngeldzaken:read",
            status: "succeeded",
          }),
        }),
      }),
      expect.objectContaining({ action: "automation_workflow.executor_cycle" }),
      expect.objectContaining({ expectedMetadata: expect.any(Object) }),
    );
    expect(updateWorkflowRunWithAudit).toHaveBeenCalledWith(
      101,
      expect.any(Object),
      expect.objectContaining({
        action: "automation_workflow.executor_cycle",
        entityType: "automation_workflow_operation",
        entityId: "mijngeldzaken:read",
      }),
      expect.objectContaining({ expectedMetadata: expect.any(Object) }),
    );
  });

  it("runs one executor cycle and prepares safe-draft Wave operations locally", async () => {
    vi.mocked(getWorkflowRunById).mockResolvedValueOnce({
      id: 101,
      status: "queued",
      triggerSource: "automation:daily_reconciliation_run",
      documentsImported: 0,
      documentsProcessed: 1,
      documentsNeedingReview: 0,
      errorMessage: null,
      startedAt: new Date("2026-06-28T10:00:00Z"),
      finishedAt: null,
      createdAt: new Date("2026-06-28T10:00:00Z"),
      metadata: {
        workflowId: "daily_reconciliation_run",
        mode: "queue",
        operations: [
          {
            operationId: "wave:draft",
            targetSystem: "waveapps",
            stepId: "prepare_wave_transaction",
            surfaceId: "transactions",
            actionId: "transaction_add",
            mode: "write",
            safety: "safe_draft",
            status: "pending",
          },
        ],
      },
    });
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.runAutomationWorkflowExecutorCycle({
      workflowRunId: 101,
      actor: "browser_executor",
    });

    expect(result.status).toBe("executed");
    expect(result.operation?.status).toBe("succeeded");
    expect(result.operation?.externalId).toBe("fab-draft:wave:draft");
    expect(result.operation?.evidence).toEqual(expect.objectContaining({
      targetSystem: "waveapps",
      externalSubmission: "not_executed",
      draftPrepared: true,
    }));
    expect(result.workflowRun?.status).toBe("completed");
    expect(updateWorkflowRunWithAudit).toHaveBeenCalledWith(
      101,
      expect.objectContaining({
        status: "completed",
        documentsNeedingReview: 0,
        errorMessage: null,
      }),
      expect.objectContaining({ action: "automation_workflow.executor_cycle" }),
      expect.objectContaining({ expectedMetadata: expect.any(Object) }),
    );
  });

  it("runs one executor cycle and prepares safe-draft MijnGeldzaken operations locally", async () => {
    vi.mocked(getWorkflowRunById).mockResolvedValueOnce({
      id: 101,
      status: "queued",
      triggerSource: "automation:mijngeldzaken_master_ledger_sync",
      documentsImported: 0,
      documentsProcessed: 1,
      documentsNeedingReview: 0,
      errorMessage: null,
      startedAt: new Date("2026-06-28T10:00:00Z"),
      finishedAt: null,
      createdAt: new Date("2026-06-28T10:00:00Z"),
      metadata: {
        workflowId: "mijngeldzaken_master_ledger_sync",
        mode: "queue",
        operations: [
          {
            operationId: "mijngeldzaken:draft",
            targetSystem: "mijngeldzaken",
            stepId: "prepare_mijngeldzaken_transaction_import",
            surfaceId: "transactions",
            actionId: "transaction_import_prepare",
            mode: "import",
            safety: "safe_draft",
            status: "pending",
            payload: {
              date: "2026-06-28",
              amount: 0,
              description: "FAB approved master-ledger import batch",
              category: "Huishouden",
              account: "Huishouden",
            },
          },
        ],
      },
    });
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.runAutomationWorkflowExecutorCycle({
      workflowRunId: 101,
      actor: "browser_executor",
    });

    expect(result.status).toBe("executed");
    expect(result.operation?.status).toBe("succeeded");
    expect(result.operation?.externalId).toBe("fab-draft:mijngeldzaken:draft");
    expect(result.operation?.evidence).toEqual(expect.objectContaining({
      targetSystem: "mijngeldzaken",
      externalSubmission: "not_executed",
      draftPrepared: true,
      masterLedgerChecksum: expect.any(String),
      masterLedgerDraft: expect.objectContaining({
        draftType: "transaction_import",
        exportFormat: "csv",
        importRow: expect.objectContaining({
          Categorie: "Huishouden",
          Omschrijving: "FAB approved master-ledger import batch",
        }),
        externalSubmission: "not_executed",
      }),
    }));
    expect(result.operation?.masterLedgerChecksum).toHaveLength(64);
    expect(result.operation?.masterLedgerDraft).toEqual(expect.objectContaining({
      draftType: "transaction_import",
      targetSystem: "mijngeldzaken",
      checksum: result.operation?.masterLedgerChecksum,
    }));
    expect(result.workflowRun?.status).toBe("completed");
    expect(updateWorkflowRunWithAudit).toHaveBeenCalledWith(
      101,
      expect.objectContaining({
        status: "completed",
        documentsNeedingReview: 0,
        errorMessage: null,
      }),
      expect.objectContaining({ action: "automation_workflow.executor_cycle" }),
      expect.objectContaining({ expectedMetadata: expect.any(Object) }),
    );
  });

  it("blocks executor cycle for Wave actions that need confirmation", async () => {
    vi.mocked(getWorkflowRunById).mockResolvedValueOnce({
      id: 101,
      status: "queued",
      triggerSource: "automation:daily_reconciliation_run",
      documentsImported: 0,
      documentsProcessed: 1,
      documentsNeedingReview: 0,
      errorMessage: null,
      startedAt: new Date("2026-06-28T10:00:00Z"),
      finishedAt: null,
      createdAt: new Date("2026-06-28T10:00:00Z"),
      metadata: {
        workflowId: "daily_reconciliation_run",
        mode: "queue",
        operations: [
          {
            operationId: "wave:send",
            stepId: "send_invoice",
            surfaceId: "invoices",
            actionId: "invoice_send",
            mode: "write",
            safety: "requires_confirmation",
            status: "pending",
          },
        ],
      },
    });
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.runAutomationWorkflowExecutorCycle({
      workflowRunId: 101,
      actor: "browser_executor",
    });

    expect(result.status).toBe("blocked");
    expect(result.operation?.status).toBe("blocked");
    expect(result.workflowRun?.status).toBe("completed_with_review");
    expect(updateWorkflowRunWithAudit).toHaveBeenCalledWith(
      101,
      expect.objectContaining({
        status: "completed_with_review",
        documentsNeedingReview: 1,
        errorMessage: expect.stringContaining("requires a dedicated"),
      }),
      expect.objectContaining({ action: "automation_workflow.executor_cycle" }),
      expect.objectContaining({ expectedMetadata: expect.any(Object) }),
    );
    expect(updateWorkflowRunWithAudit).toHaveBeenCalledWith(
      101,
      expect.any(Object),
      expect.objectContaining({
        action: "automation_workflow.executor_cycle",
        entityType: "automation_workflow_operation",
        entityId: "wave:send",
      }),
      expect.objectContaining({ expectedMetadata: expect.any(Object) }),
    );
    expect(addReviewItemWithAudit).toHaveBeenCalledWith(
      expect.objectContaining({
        documentId: null,
        reason: "autonomous_wave_workflow_blocked",
        status: "pending",
        correctedData: expect.objectContaining({
          workflowRunId: 101,
          workflowId: "daily_reconciliation_run",
          source: "automation_workflow.executor_cycle",
          operationId: "wave:send",
          actionId: "invoice_send",
        }),
      }),
      expect.objectContaining({ action: "review_item.create_autonomous_exception" }),
    );
  });

  it("runs an executor loop across multiple read-only Wave operations", async () => {
    vi.mocked(getWorkflowRunById).mockResolvedValueOnce({
      id: 101,
      status: "queued",
      triggerSource: "automation:daily_reconciliation_run",
      documentsImported: 0,
      documentsProcessed: 2,
      documentsNeedingReview: 0,
      errorMessage: null,
      startedAt: new Date("2026-06-28T10:00:00Z"),
      finishedAt: null,
      createdAt: new Date("2026-06-28T10:00:00Z"),
      metadata: {
        workflowId: "daily_reconciliation_run",
        mode: "queue",
        operations: [
          {
            operationId: "wave:open",
            stepId: "open_account_transactions_report",
            surfaceId: "reports",
            actionId: "report_open",
            mode: "read",
            safety: "read_only",
            status: "pending",
          },
          {
            operationId: "wave:read",
            stepId: "read_ledger_rows",
            surfaceId: "reports",
            actionId: "report_table_read",
            mode: "read",
            safety: "read_only",
            status: "pending",
          },
        ],
      },
    });
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.runAutomationWorkflowExecutorLoop({
      workflowRunId: 101,
      actor: "browser_executor",
      maxSteps: 10,
    });

    expect(result.status).toBe("executed");
    expect(result.operations).toHaveLength(2);
    expect(result.operations.map((operation) => operation.status)).toEqual(["succeeded", "succeeded"]);
    expect(result.workflowRun?.status).toBe("completed");
    expect(updateWorkflowRunWithAudit).toHaveBeenCalledWith(
      101,
      expect.objectContaining({
        status: "completed",
        documentsNeedingReview: 0,
        errorMessage: null,
        metadata: expect.objectContaining({
          lastExecutorLoop: expect.objectContaining({
            status: "executed",
            operationCount: 2,
          }),
        }),
      }),
      expect.objectContaining({ action: "automation_workflow.executor_loop" }),
      expect.objectContaining({ expectedMetadata: expect.any(Object) }),
    );
    expect(updateWorkflowRunWithAudit).toHaveBeenCalledWith(
      101,
      expect.any(Object),
      expect.objectContaining({
        action: "automation_workflow.executor_loop",
        entityType: "automation_workflow",
      }),
      expect.objectContaining({ expectedMetadata: expect.any(Object) }),
    );
  });

  it("stops the executor loop when a gated Wave operation is reached", async () => {
    vi.mocked(getWorkflowRunById).mockResolvedValueOnce({
      id: 101,
      status: "queued",
      triggerSource: "automation:daily_reconciliation_run",
      documentsImported: 0,
      documentsProcessed: 2,
      documentsNeedingReview: 0,
      errorMessage: null,
      startedAt: new Date("2026-06-28T10:00:00Z"),
      finishedAt: null,
      createdAt: new Date("2026-06-28T10:00:00Z"),
      metadata: {
        workflowId: "daily_reconciliation_run",
        mode: "queue",
        operations: [
          {
            operationId: "wave:read",
            stepId: "read_ledger_rows",
            surfaceId: "reports",
            actionId: "report_table_read",
            mode: "read",
            safety: "read_only",
            status: "pending",
          },
          {
            operationId: "wave:send",
            stepId: "send_invoice",
            surfaceId: "invoices",
            actionId: "invoice_send",
            mode: "write",
            safety: "requires_confirmation",
            status: "pending",
          },
        ],
      },
    });
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.runAutomationWorkflowExecutorLoop({
      workflowRunId: 101,
      actor: "browser_executor",
      maxSteps: 10,
    });

    expect(result.status).toBe("blocked");
    expect(result.operations).toHaveLength(2);
    expect(result.operations.map((operation) => operation.status)).toEqual(["succeeded", "blocked"]);
    expect(result.workflowRun?.status).toBe("completed_with_review");
    expect(updateWorkflowRunWithAudit).toHaveBeenCalledWith(
      101,
      expect.objectContaining({
        status: "completed_with_review",
        documentsNeedingReview: 1,
        errorMessage: expect.stringContaining("requires a dedicated"),
      }),
      expect.objectContaining({ action: "automation_workflow.executor_loop" }),
      expect.objectContaining({ expectedMetadata: expect.any(Object) }),
    );
    expect(addReviewItemWithAudit).toHaveBeenCalledWith(
      expect.objectContaining({
        documentId: null,
        reason: "autonomous_wave_workflow_blocked",
        status: "pending",
        correctedData: expect.objectContaining({
          workflowRunId: 101,
          workflowId: "daily_reconciliation_run",
          source: "automation_workflow.executor_loop",
          operationId: "wave:send",
          actionId: "invoice_send",
        }),
      }),
      expect.objectContaining({ action: "review_item.create_autonomous_exception" }),
    );
  });

  it("creates a workflow run and records an audit event", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.createWorkflowRun({
      status: "running",
      triggerSource: "google_drive",
      metadata: { folder: "sort out" },
    });

    expect(result).toEqual({ id: 34 });
    expect(createWorkflowRunWithAudit).toHaveBeenCalledWith({
      status: "running",
      triggerSource: "google_drive",
      metadata: { folder: "sort out" },
    }, expect.objectContaining({
      actorUserId: 1,
      action: "workflow_run.create",
      entityType: "workflow_run",
      details: { status: "running", triggerSource: "google_drive" },
    }));
    expect(recordAuditEvent).not.toHaveBeenCalled();
    expect(vi.mocked(createWorkflowRunWithAudit).mock.calls[0]?.[1]).toEqual(
      expect.objectContaining({
        details: { status: "running", triggerSource: "google_drive" },
      })
    );
    expect(JSON.stringify(vi.mocked(createWorkflowRunWithAudit).mock.calls[0]?.[1])).not.toContain("sort out");
  });

  it("updates workflow run counters", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    await caller.bookkeeping.updateWorkflowRun({
      id: 34,
      status: "completed_with_review",
      documentsImported: 4,
      documentsProcessed: 3,
      documentsNeedingReview: 1,
      errorMessage: "DATABASE_URL=private-db-secret",
    });

    expect(updateWorkflowRunWithAudit).toHaveBeenCalledWith(34, {
      status: "completed_with_review",
      documentsImported: 4,
      documentsProcessed: 3,
      documentsNeedingReview: 1,
      errorMessage: "DATABASE_URL=private-db-secret",
    }, expect.objectContaining({
      actorUserId: 1,
      action: "workflow_run.update",
      entityType: "workflow_run",
      details: {
        changedFields: ["documentsImported", "documentsNeedingReview", "documentsProcessed", "errorMessage", "status"],
      },
    }));
    expect(recordAuditEvent).not.toHaveBeenCalled();
    expect(JSON.stringify(vi.mocked(updateWorkflowRunWithAudit).mock.calls[0]?.[2])).not.toContain("private-db-secret");
  });

  it("returns not found for a workflow run that does not exist", async () => {
    vi.mocked(updateWorkflowRunWithAudit).mockResolvedValueOnce(false);
    const caller = appRouter.createCaller(createAdminContext());

    await expect(caller.bookkeeping.updateWorkflowRun({ id: 404, status: "failed" }))
      .rejects.toMatchObject({ code: "NOT_FOUND" });
    expect(recordAuditEvent).not.toHaveBeenCalled();
  });

  it("rejects an empty workflow update", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    await expect(caller.bookkeeping.updateWorkflowRun({ id: 34 }))
      .rejects.toMatchObject({ code: "BAD_REQUEST" });
    expect(updateWorkflowRunWithAudit).not.toHaveBeenCalled();
    expect(recordAuditEvent).not.toHaveBeenCalled();
  });

  it("registers a document with sanitized text fields", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.registerDocument({
      source: "google_drive",
      sourceDocumentId: "file-123",
      originalFilename: "<b>receipt.pdf</b>",
      mimeType: "application/pdf",
      documentType: "receipt",
      processingStatus: "needs_review",
      vendorName: "<script>alert(1)</script>Vendor",
      totalAmount: 42.5,
      confidenceScore: 0.87,
      extractedData: { total_amount: 42.5 },
    });

    expect(result).toEqual({ id: 12 });
    expect(createBookkeepingDocumentWithAudit).toHaveBeenCalledWith(
      expect.objectContaining({
        originalFilename: "receipt.pdf",
        vendorName: "Vendor",
        totalAmount: "42.50",
        confidenceScore: "0.8700",
      }),
      expect.objectContaining({
        action: "document.register",
        entityType: "bookkeeping_document",
        details: { source: "google_drive" },
      })
    );
    expect(recordAuditEvent).not.toHaveBeenCalled();
    expect(JSON.stringify(vi.mocked(createBookkeepingDocumentWithAudit).mock.calls[0]?.[1])).not.toContain("file-123");
  });

  it("creates and updates review items", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    await caller.bookkeeping.createReviewItem({
      documentId: 12,
      reason: "validation_failed",
      details: "Missing VAT number",
    });
    await caller.bookkeeping.updateReviewStatus({
      id: 23,
      status: "approved",
      resolution: "Corrected fields",
    });

    expect(addReviewItemWithAudit).toHaveBeenCalledWith(
      expect.objectContaining({
        documentId: 12,
        reason: "validation_failed",
        status: "pending",
      }),
      expect.objectContaining({
        action: "review_item.create",
        details: { documentId: 12, status: "pending" },
      }),
    );
    expect(updateReviewItemStatus).toHaveBeenCalledWith(23, "approved", "Corrected fields", 1);
    expect(recordAuditEvent).not.toHaveBeenCalled();
    expect(JSON.stringify(vi.mocked(addReviewItemWithAudit).mock.calls[0]?.[1])).not.toContain("Missing VAT number");
  });

  it("returns not found when the review item was deleted before the update", async () => {
    vi.mocked(updateReviewItemStatus).mockResolvedValueOnce(false);
    const caller = appRouter.createCaller(createAdminContext());

    await expect(caller.bookkeeping.updateReviewStatus({
      id: 404,
      status: "approved",
    })).rejects.toMatchObject({ code: "NOT_FOUND" });
  });

  it("records routing attempts", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.createRoutingAttempt({
      documentId: 12,
      workflowRunId: 34,
      target: "waveapps_business",
      status: "submitted",
      externalId: "wave-expense-1",
    });

    expect(result).toEqual({ id: 45 });
    expect(createRoutingAttemptWithAudit).toHaveBeenCalledWith(
      expect.objectContaining({
        documentId: 12,
        workflowRunId: 34,
        target: "waveapps_business",
        status: "submitted",
        externalId: "wave-expense-1",
      }),
      expect.objectContaining({ action: "routing_attempt.create" }),
    );
    expect(recordAuditEvent).not.toHaveBeenCalled();
  });

  it("records bookkeeping-record routing attempts without a document", async () => {
    const caller = appRouter.createCaller(createAdminContext());

    const result = await caller.bookkeeping.createRoutingAttempt({
      bookkeepingRecordId: 98,
      workflowRunId: 34,
      target: "waveapps_business",
      status: "submitted",
      externalId: "wave-bank-expense-1",
    });

    expect(result).toEqual({ id: 45 });
    expect(createRoutingAttemptWithAudit).toHaveBeenCalledWith(
      expect.objectContaining({
        documentId: null,
        bookkeepingRecordId: 98,
        workflowRunId: 34,
        target: "waveapps_business",
        status: "submitted",
        externalId: "wave-bank-expense-1",
      }),
      expect.objectContaining({
        action: "routing_attempt.create",
        details: expect.objectContaining({
          documentId: null,
          bookkeepingRecordId: 98,
        }),
      }),
    );
    expect(recordAuditEvent).not.toHaveBeenCalled();
  });
});
