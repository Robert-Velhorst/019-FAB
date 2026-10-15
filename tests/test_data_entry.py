import unittest
from unittest.mock import MagicMock, patch
import csv
import json
import os
import shutil
import tempfile
from requests.exceptions import Timeout

from src.data_entry.mijngeldzaken_handler import MijngeldzakenHandler
from src.data_entry.mijngeldzaken_autonomous_operator import MijngeldzakenAutonomousOperator
from src.data_entry.mijngeldzaken_surface import (
    MIJNGELDZAKEN_SURFACE_CATALOG,
    build_mijngeldzaken_import_row,
    list_mijngeldzaken_actions,
    plan_mijngeldzaken_action,
    summarize_mijngeldzaken_parity,
)
from src.data_entry.waveapps_business_handler import WaveappsBusinessHandler
from src.data_entry.waveapps_personal_handler import WaveappsPersonalHandler
from src.utils.rate_limiter import reset_all_limiters
from src.data_entry.waveapps_surface import (
    WAVE_SURFACE_CATALOG,
    build_wave_action_payload,
    build_wave_report_payload,
    classify_wave_destination,
    list_wave_actions,
    list_wave_report_sections,
    list_wave_reports,
    list_wave_surfaces,
    plan_wave_action,
    summarize_wave_parity,
)

class TestDataEntry(unittest.TestCase):

    def setUp(self):
        reset_all_limiters()
        self.config = {
            "mijngeldzaken_username": "test_user",
            "mijngeldzaken_password": "test_pass",
            "mijngeldzaken_login_url": "http://mijngeldzaken.test/login",
            "mijngeldzaken_import_url": "http://mijngeldzaken.test/import",
            "mijngeldzaken_csv_template": {
                "columns": ["Date", "Description", "Amount", "Category"],
                "mapping": {
                    "Date": "extracted_data.transaction_date",
                    "Description": "extracted_data.description",
                    "Amount": "extracted_data.total_amount",
                    "Category": "category"
                },
                "delimiter": ";"
            },
            "mijngeldzaken_category_mapping": {"Personal": "Huishouden"},
            "waveapps_business_access_token": "business_token",
            "waveapps_business_id": "business_id",
            "waveapps_business_category_mapping": {"Business": "Office Supplies"},
            "waveapps_business_anchor_account_id": "business-anchor-account",
            "waveapps_business_category_account_ids": {"Office Supplies": "business-office-supplies-account"},
            "waveapps_personal_access_token": "personal_token",
            "waveapps_personal_id": "personal_id",
            "waveapps_personal_category_mapping": {"Handicaps": "Medical Expenses"},
            "waveapps_personal_anchor_account_id": "personal-anchor-account",
            "waveapps_personal_category_account_ids": {"Medical Expenses": "personal-medical-account"},
            "waveapps_handicap_tag": "#handicap"
        }
        self.dummy_doc_id = "doc123"
        self.dummy_processed_data = {
            "document_id": self.dummy_doc_id,
            "ocr_text": "Test receipt for groceries",
            "extracted_data": {
                "vendor_name": "Local Supermarket",
                "transaction_date": "2025-01-15",
                "total_amount": 45.50,
                "currency": "EUR",
                "description": "Weekly groceries"
            },
            "language": "en",
            "category": "Personal",
            "confidence_score": 0.95
        }

    def tearDown(self):
        reset_all_limiters()

    def test_mijngeldzaken_handler_prepares_persistent_supervised_artifact_without_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = dict(self.config)
            config["mijngeldzaken_export_dir"] = temp_dir
            handler = MijngeldzakenHandler(config)

            result = handler.enter_data(self.dummy_processed_data)

            self.assertEqual(result["status"], "supervised_action_required")
            self.assertTrue(result["requires_supervision"])
            self.assertFalse(result["credentials_used"])
            self.assertEqual(result["external_submission"], "not_executed")
            self.assertTrue(os.path.isfile(result["artifact"]["path"]))
            self.assertEqual(len(result["artifact"]["sha256"]), 64)
            with open(result["artifact"]["path"], newline="", encoding="utf-8-sig") as csvfile:
                row = next(csv.DictReader(csvfile, delimiter=";"))
            self.assertEqual(row["Date"], "2025-01-15")
            self.assertEqual(row["Description"], "Weekly groceries")
            self.assertEqual(row["Amount"], "45.5")
            self.assertEqual(row["Category"], "Huishouden")
            rendered = json.dumps(result, sort_keys=True)
            self.assertNotIn("test_user", rendered)
            self.assertNotIn("test_pass", rendered)

            without_credentials = dict(config)
            without_credentials.pop("mijngeldzaken_username", None)
            without_credentials.pop("mijngeldzaken_password", None)
            second = MijngeldzakenHandler(without_credentials).enter_data(self.dummy_processed_data)
            self.assertEqual(second["status"], "supervised_action_required")

    @patch("src.data_entry.waveapps_business_handler.requests.post")
    def test_waveapps_business_handler(self, mock_post):
        # Test successful API call
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "data": {
                "moneyTransactionCreate": {
                    "didSucceed": True,
                    "transaction": {"id": "new_expense_id"}
                }
            }
        }
        mock_post.return_value = mock_response

        handler = WaveappsBusinessHandler(self.config)
        business_data = self.dummy_processed_data.copy()
        business_data["category"] = "Business"
        result = handler.enter_data(business_data)

        self.assertEqual(result["status"], "success")
        self.assertIn("Money transaction created", result["message"])
        self.assertEqual(result["external_id"], "new_expense_id")
        self.assertEqual(result["target_surface"], "transactions")
        self.assertEqual(result["action_plan"]["action_id"], "transaction_add")
        self.assertTrue(result["action_plan"]["can_run_autonomously"])
        request = mock_post.call_args.kwargs["json"]
        self.assertIn("moneyTransactionCreate", request["query"])
        self.assertEqual(request["variables"]["input"]["anchor"]["accountId"], "business-anchor-account")
        self.assertEqual(request["variables"]["input"]["lineItems"][0]["accountId"], "business-office-supplies-account")
        self.assertEqual(request["variables"]["input"]["anchor"]["direction"], "WITHDRAWAL")

        # Test API failure
        mock_response.json.return_value = {
            "data": {
                "moneyTransactionCreate": {
                    "didSucceed": False,
                    "inputErrors": [{"message": "API Error", "code": "123"}]
                }
            }
        }
        result = handler.enter_data(business_data)
        self.assertEqual(result["status"], "failure")
        self.assertNotIn("API Error", result["message"])
        self.assertTrue(result["requires_manual_review"])

        self.config["waveapps_business_category_account_ids"] = {}
        handler = WaveappsBusinessHandler(self.config)
        result = handler.enter_data(business_data)
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("categoryAccountId", result["missing_fields"])

        # Test CSV fallback
        self.config["waveapps_business_access_token"] = None
        handler = WaveappsBusinessHandler(self.config)
        result = handler.enter_data(business_data)
        self.assertEqual(result["status"], "csv_generated")
        self.assertIn("CSV generated", result["message"])
        self.assertTrue(result["requires_manual_review"])
        csv_path = result["message"].split(": ")[1]
        with open(csv_path, newline="") as csvfile:
            row = next(csv.DictReader(csvfile))
        self.assertEqual(row["Wave Surface"], "transactions")
        self.assertEqual(row["Wave Action"], "transaction_add")
        self.assertEqual(row["Vendor"], "Local Supermarket")
        # Clean up generated CSV
        os.remove(csv_path)

    @patch("src.data_entry.waveapps_business_handler.requests.post")
    def test_waveapps_business_timeout_is_unknown_and_does_not_leak_exception(self, mock_post):
        mock_post.side_effect = Timeout("Bearer secret-token receipt=private-receipt")
        handler = WaveappsBusinessHandler(self.config)
        data = {**self.dummy_processed_data, "category": "Business"}

        result = handler.enter_data(data)

        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(result["review_reason"], "wave_api_outcome_unknown")
        self.assertTrue(result["requires_manual_review"])
        self.assertNotIn("secret-token", result["message"])
        self.assertNotIn("private-receipt", result["message"])

    @patch("src.data_entry.waveapps_business_handler.requests.post")
    def test_waveapps_incomplete_graphql_mutation_response_is_unknown(self, mock_post):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "errors": [{"message": "response interrupted after execution"}],
            "data": {"moneyTransactionCreate": None},
        }
        mock_post.return_value = response
        data = {**self.dummy_processed_data, "category": "Business"}

        result = WaveappsBusinessHandler(self.config).enter_data(data)

        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(result["review_reason"], "wave_api_outcome_unknown")
        self.assertTrue(result["requires_manual_review"])
        self.assertNotIn("response interrupted", result["message"])

    @patch("src.data_entry.waveapps_business_handler.requests.post")
    def test_waveapps_graphql_response_without_mutation_result_is_unknown(self, mock_post):
        response = MagicMock()
        response.raise_for_status.return_value = None
        mock_post.return_value = response
        data = {**self.dummy_processed_data, "category": "Business"}

        incomplete_results = [
            {"data": {}},
            {"data": {"moneyTransactionCreate": {"didSucceed": False, "inputErrors": []}}},
            {"data": {"moneyTransactionCreate": {"didSucceed": True, "transaction": None}}},
        ]
        for payload in incomplete_results:
            with self.subTest(payload=payload):
                reset_all_limiters()
                response.json.return_value = payload
                result = WaveappsBusinessHandler(self.config).enter_data(data)

                self.assertEqual(result["status"], "outcome_unknown")
                self.assertEqual(result["review_reason"], "wave_api_outcome_unknown")
                self.assertTrue(result["requires_manual_review"])

    @patch("src.data_entry.waveapps_business_handler.requests.post")
    def test_waveapps_non_object_response_body_is_unknown(self, mock_post):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = None
        mock_post.return_value = response
        data = {**self.dummy_processed_data, "category": "Business"}

        result = WaveappsBusinessHandler(self.config).enter_data(data)

        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(result["review_reason"], "wave_api_outcome_unknown")
        self.assertTrue(result["requires_manual_review"])

    @patch("src.data_entry.waveapps_business_handler.requests.post")
    def test_waveapps_business_credit_note_posts_a_deposit(self, mock_post):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": {
                "moneyTransactionCreate": {
                    "didSucceed": True,
                    "transaction": {"id": "vendor-credit-1"},
                }
            }
        }
        mock_post.return_value = response
        credit_note = {
            **self.dummy_processed_data,
            "document_type": "credit_note",
            "category": "Business",
            "extracted_data": {
                **self.dummy_processed_data["extracted_data"],
                "document_type": "credit_note",
                "total_amount": -45.50,
                "description": "Supplier refund",
            },
        }

        result = WaveappsBusinessHandler(self.config).enter_data(credit_note)

        self.assertEqual(result["status"], "success")
        request_input = mock_post.call_args.kwargs["json"]["variables"]["input"]
        self.assertEqual(request_input["anchor"]["amount"], 45.5)
        self.assertEqual(request_input["anchor"]["direction"], "DEPOSIT")
        self.assertEqual(request_input["lineItems"][0]["balance"], "DECREASE")

    def test_mijngeldzaken_surface_catalog_and_operator(self):
        row = build_mijngeldzaken_import_row(
            self.dummy_processed_data,
            "Huishouden",
            default_account="Betaalrekening",
        )
        planned = plan_mijngeldzaken_action(
            "transactions",
            "transaction_import_prepare",
            {
                "date": "2025-01-15",
                "amount": 45.50,
                "description": "Weekly groceries",
                "category": "Huishouden",
            },
        )
        submit_plan = plan_mijngeldzaken_action(
            "transactions",
            "transaction_import_submit",
            {"importBatchId": "batch-1"},
        )
        operation = MijngeldzakenAutonomousOperator(self.config).prepare_operation(
            "transaction_import_prepare",
            {
                "date": "2025-01-15",
                "amount": 45.50,
                "description": "Weekly groceries",
                "category": "Huishouden",
            },
            surface="transactions",
        )
        parity = summarize_mijngeldzaken_parity()

        self.assertEqual(row["Datum"], "2025-01-15")
        self.assertEqual(row["Categorie"], "Huishouden")
        self.assertEqual(row["Rekening"], "Betaalrekening")
        self.assertEqual(planned["status"], "planned")
        self.assertTrue(planned["can_run_autonomously"])
        self.assertTrue(submit_plan["requires_confirmation"])
        self.assertEqual(operation["operation_id"].split(":", 1)[0], "mijngeldzaken")
        self.assertEqual(operation["safety"], "safe_draft")
        self.assertIn("household_bookkeeping", MIJNGELDZAKEN_SURFACE_CATALOG["feature_inventory"])
        self.assertIn("authenticated_sidebar_navigation", MIJNGELDZAKEN_SURFACE_CATALOG["feature_inventory"])
        self.assertIn("current_month_dashboard", MIJNGELDZAKEN_SURFACE_CATALOG["feature_inventory"])
        self.assertIn("transaction_import_prepare", {action["id"] for action in list_mijngeldzaken_actions("transactions")})
        self.assertIn("current_month_read", {action["id"] for action in list_mijngeldzaken_actions("current_month")})
        self.assertIn("receipt_upload_prepare", {action["id"] for action in list_mijngeldzaken_actions("receipts")})
        self.assertIn("import_mapping_prepare", {action["id"] for action in list_mijngeldzaken_actions("imports")})
        self.assertIn("budget_suggestion_prepare", {action["id"] for action in list_mijngeldzaken_actions("budgets")})
        self.assertIn("scenario_list_read", {action["id"] for action in list_mijngeldzaken_actions("scenarios")})
        self.assertIn("data_connections_read", {action["id"] for action in list_mijngeldzaken_actions("data_connections")})
        control_labels = [
            control["label"]
            for feature in MIJNGELDZAKEN_SURFACE_CATALOG["feature_inventory"].values()
            for control in feature.get("controls", [])
        ]
        self.assertIn("Deze maand", control_labels)
        self.assertIn("Bonnetjes", control_labels)
        self.assertIn("Loonstroken", control_labels)
        self.assertIn("Transaction import wizard", control_labels)
        self.assertIn("Budget suggestion", control_labels)
        self.assertIn("Savings planning", control_labels)
        self.assertIn("Connected accounts", control_labels)
        action_ids = {action["id"] for action in list_mijngeldzaken_actions()}
        missing_control_actions = [
            control["action"]
            for feature in MIJNGELDZAKEN_SURFACE_CATALOG["feature_inventory"].values()
            for control in feature.get("controls", [])
            if control.get("action") and control["action"] not in action_ids
        ]
        self.assertEqual(missing_control_actions, [])
        budget_plan = plan_mijngeldzaken_action(
            "budgets",
            "budget_suggestion_prepare",
            {"category": "Huishouden", "period": "monthly", "suggestedAmount": 500},
        )
        connection_plan = plan_mijngeldzaken_action(
            "data_connections",
            "connected_account_refresh",
            {"connectionId": "mgz-bank-1"},
        )
        self.assertTrue(budget_plan["can_run_autonomously"])
        self.assertFalse(connection_plan["can_run_autonomously"])
        self.assertTrue(connection_plan["requires_confirmation"])
        self.assertGreaterEqual(parity["sync_contracts"], 2)
        self.assertGreaterEqual(parity["feature_pages"], 11)
        self.assertGreaterEqual(parity["observed_controls"], 60)
        self.assertGreaterEqual(parity["actions"], 55)

    @patch("src.data_entry.waveapps_personal_handler.requests.post")
    def test_waveapps_personal_handler(self, mock_post):
        # Test successful API call with handicap tag
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "data": {
                "moneyTransactionCreate": {
                    "didSucceed": True,
                    "transaction": {"id": "new_personal_expense_id"}
                }
            }
        }
        mock_post.return_value = mock_response

        handler = WaveappsPersonalHandler(self.config)
        personal_data = self.dummy_processed_data.copy()
        personal_data["category"] = "Handicaps"
        result = handler.enter_data(personal_data)

        self.assertEqual(result["status"], "success")
        self.assertIn("Money transaction created", result["message"])
        self.assertEqual(result["external_id"], "new_personal_expense_id")
        self.assertEqual(result["target_surface"], "transactions")
        self.assertEqual(result["action_plan"]["action_id"], "transaction_add")
        # Verify handicap tag was added to the GraphQL variables, not interpolated into the query.
        request = mock_post.call_args.kwargs["json"]
        self.assertIn("#handicap", request["variables"]["input"]["description"])
        self.assertEqual(request["variables"]["input"]["anchor"]["accountId"], "personal-anchor-account")
        self.assertEqual(request["variables"]["input"]["lineItems"][0]["accountId"], "personal-medical-account")

        # Test CSV fallback
        self.config["waveapps_personal_access_token"] = None
        handler = WaveappsPersonalHandler(self.config)
        result = handler.enter_data(personal_data)
        self.assertEqual(result["status"], "csv_generated")
        self.assertIn("CSV generated", result["message"])
        self.assertTrue(result["requires_manual_review"])
        csv_path = result["message"].split(": ")[1]
        with open(csv_path, newline="") as csvfile:
            row = next(csv.DictReader(csvfile))
        self.assertEqual(row["Wave Surface"], "transactions")
        self.assertEqual(row["Wave Action"], "transaction_add")
        self.assertIn("#handicap", row["Description"])
        # Clean up generated CSV
        os.remove(csv_path)

    @patch("src.data_entry.waveapps_personal_handler.requests.post")
    def test_waveapps_personal_timeout_is_unknown_and_does_not_leak_exception(self, mock_post):
        mock_post.side_effect = Timeout("Bearer secret-token receipt=private-receipt")
        handler = WaveappsPersonalHandler(self.config)
        data = {**self.dummy_processed_data, "category": "Handicaps"}

        result = handler.enter_data(data)

        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(result["review_reason"], "wave_api_outcome_unknown")
        self.assertTrue(result["requires_manual_review"])
        self.assertNotIn("secret-token", result["message"])
        self.assertNotIn("private-receipt", result["message"])

    def test_waveapps_surface_catalog(self):
        receipt_destination = classify_wave_destination({"document_type": "receipt"})
        vendor_invoice_destination = classify_wave_destination({"document_type": "vendor_invoice"})

        self.assertEqual(receipt_destination["target_surface"], "transactions")
        self.assertEqual(vendor_invoice_destination["target_surface"], "bills")
        self.assertIn("chart_of_accounts", list_wave_surfaces())
        self.assertIn("dashboard", list_wave_surfaces())
        self.assertIn("estimates", list_wave_surfaces())
        self.assertIn("invoices", list_wave_surfaces())
        self.assertIn("recurring_invoices", list_wave_surfaces())
        self.assertIn("estimates_workspace", WAVE_SURFACE_CATALOG["feature_inventory"])
        self.assertIn("invoices_workspace", WAVE_SURFACE_CATALOG["feature_inventory"])
        self.assertIn("recurring_invoices_workspace", WAVE_SURFACE_CATALOG["feature_inventory"])
        self.assertIn("customer_statement_generator", WAVE_SURFACE_CATALOG["feature_inventory"])
        self.assertIn("customers_list_and_form", WAVE_SURFACE_CATALOG["feature_inventory"])
        self.assertIn("customer_form_fields", WAVE_SURFACE_CATALOG["feature_inventory"])
        self.assertIn("customer_csv_import_page", WAVE_SURFACE_CATALOG["feature_inventory"])
        self.assertIn("bills_workspace", WAVE_SURFACE_CATALOG["feature_inventory"])
        self.assertIn("bill_add_form_fields", WAVE_SURFACE_CATALOG["feature_inventory"])
        self.assertIn("vendor_form_fields", WAVE_SURFACE_CATALOG["feature_inventory"])
        self.assertIn("buying_products_services_legacy", WAVE_SURFACE_CATALOG["feature_inventory"])
        self.assertIn("buying_product_service_form_fields", WAVE_SURFACE_CATALOG["feature_inventory"])
        self.assertIn("products_services_legacy", WAVE_SURFACE_CATALOG["feature_inventory"])
        self.assertIn("product_service_form_fields", WAVE_SURFACE_CATALOG["feature_inventory"])
        self.assertIn("transaction_add_form_fields", WAVE_SURFACE_CATALOG["feature_inventory"])
        self.assertIn("transaction_statement_upload_page", WAVE_SURFACE_CATALOG["feature_inventory"])
        self.assertIn("chart_accounts_workspace", WAVE_SURFACE_CATALOG["feature_inventory"])
        self.assertIn("chart_account_editor_controls", WAVE_SURFACE_CATALOG["feature_inventory"])
        self.assertIn("payroll_unsupported_page", WAVE_SURFACE_CATALOG["feature_inventory"])
        self.assertIn("payroll_business_eligibility_settings", WAVE_SURFACE_CATALOG["feature_inventory"])
        report_labels = [control["label"] for control in WAVE_SURFACE_CATALOG["feature_inventory"]["reports_catalog"]["controls"]]
        self.assertIn("Income by Customer", report_labels)
        self.assertIn("Customer Credits", report_labels)
        self.assertIn("Purchases by Vendor", report_labels)
        self.assertIn("Account Balances", report_labels)
        self.assertIn("Account Transactions (General Ledger)", report_labels)
        report_sections = list_wave_report_sections()
        reports = list_wave_reports()
        account_transactions_payload = build_wave_report_payload(
            "account-transactions",
            from_date="2026-06-28",
            to_date="2026-06-28",
            export_format="csv",
        )
        balance_sheet_payload = build_wave_report_payload(
            "balance-sheet",
            as_of_date="2026-06-28",
            export_format="pdf",
        )
        self.assertEqual(len(report_sections), 5)
        self.assertEqual(len(reports), 12)
        self.assertIn("detailed_reporting", {section["id"] for section in report_sections})
        self.assertIn("sales-tax", {report["type"] for report in reports})
        self.assertEqual(account_transactions_payload["reportType"], "account-transactions")
        self.assertEqual(account_transactions_payload["fromDate"], "2026-06-28")
        self.assertEqual(account_transactions_payload["format"], "csv")
        self.assertEqual(balance_sheet_payload["asOfDate"], "2026-06-28")

    def test_wave_payload_uses_reconciled_ocr_line_totals(self):
        payload = build_wave_action_payload(
            {
                "document_type": "receipt",
                "extracted_data": {
                    "vendor_name": "Praxis",
                    "transaction_date": "2026-06-28",
                    "total_amount": 25.10,
                    "line_items": [{"description": "Hardware", "total": 25.10}],
                },
            },
            "Construction Materials & Tools",
            default_account="materials-account",
        )

        self.assertEqual(payload["lineItems"], [{
            "description": "Hardware",
            "amount": 25.10,
            "category": "Construction Materials & Tools",
            "account": "materials-account",
        }])

    def test_wave_payload_never_repeats_document_total_for_uncertain_lines(self):
        payload = build_wave_action_payload(
            {
                "document_type": "receipt",
                "extracted_data": {
                    "vendor_name": "Praxis",
                    "transaction_date": "2026-06-28",
                    "total_amount": 25.10,
                    "line_items": [
                        {"description": "OCR gross column", "total": 28.10},
                        {"description": "Unpriced OCR row"},
                    ],
                },
            },
            "Construction Materials & Tools",
            default_account="materials-account",
        )

        self.assertEqual(len(payload["lineItems"]), 1)
        self.assertEqual(payload["lineItems"][0]["amount"], 25.10)
        self.assertEqual(payload["lineItems"][0]["description"], "Praxis")

    def test_waveapps_action_planner(self):
        transaction_plan = plan_wave_action(
            "transactions",
            "transaction_add",
            {
                "date": "2026-06-28",
                "amount": 12.5,
                "account": "Checking",
                "category": "Office Supplies",
            },
        )
        missing_plan = plan_wave_action("bills", "bill_create", {"vendor": "ACME"})
        confirmation_plan = plan_wave_action(
            "invoices",
            "invoice_send",
            {"invoiceId": "inv-1", "recipientEmail": "customer@example.com"},
        )
        unsupported_plan = plan_wave_action("bills", "invoice_send", {"invoiceId": "inv-1"})
        parity = summarize_wave_parity()

        self.assertEqual(transaction_plan["status"], "planned")
        self.assertTrue(transaction_plan["can_run_autonomously"])
        self.assertEqual(missing_plan["status"], "needs_fields")
        self.assertIn("billDate", missing_plan["missing_fields"])
        self.assertTrue(confirmation_plan["requires_confirmation"])
        self.assertFalse(confirmation_plan["can_run_autonomously"])
        self.assertEqual(unsupported_plan["status"], "unsupported")
        self.assertIn("invoice_send", [action["id"] for action in list_wave_actions("invoices")])
        self.assertIn("estimate_customer_filter", [action["id"] for action in list_wave_actions("estimates")])
        self.assertIn("estimate_status_filter", [action["id"] for action in list_wave_actions("estimates")])
        self.assertIn("estimate_clear_filters", [action["id"] for action in list_wave_actions("estimates")])
        self.assertIn("estimate_tab_view", [action["id"] for action in list_wave_actions("estimates")])
        self.assertIn("estimate_pdf_dialog_close", [action["id"] for action in list_wave_actions("estimates")])
        self.assertIn("invoice_summary_metrics_read", [action["id"] for action in list_wave_actions("invoices")])
        self.assertIn("invoice_customer_filter", [action["id"] for action in list_wave_actions("invoices")])
        self.assertIn("invoice_status_filter", [action["id"] for action in list_wave_actions("invoices")])
        self.assertIn("invoice_clear_filters", [action["id"] for action in list_wave_actions("invoices")])
        self.assertIn("invoice_tab_view", [action["id"] for action in list_wave_actions("invoices")])
        self.assertIn("invoice_view_all", [action["id"] for action in list_wave_actions("invoices")])
        self.assertIn("recurring_invoice_customer_filter", [action["id"] for action in list_wave_actions("recurring_invoices")])
        self.assertIn("recurring_invoice_tab_view", [action["id"] for action in list_wave_actions("recurring_invoices")])
        self.assertIn("recurring_invoice_table_read", [action["id"] for action in list_wave_actions("recurring_invoices")])
        self.assertIn("recurring_invoice_view_drafts", [action["id"] for action in list_wave_actions("recurring_invoices")])
        self.assertIn("customer_statement_help_open", [action["id"] for action in list_wave_actions("customer_statements")])
        self.assertIn("customer_statement_customer_select", [action["id"] for action in list_wave_actions("customer_statements")])
        self.assertIn("customer_statement_type_select", [action["id"] for action in list_wave_actions("customer_statements")])
        self.assertIn("customer_statement_create", [action["id"] for action in list_wave_actions("customer_statements")])
        self.assertIn("customer_list_read", [action["id"] for action in list_wave_actions("customers")])
        self.assertIn("customer_row_actions_open", [action["id"] for action in list_wave_actions("customers")])
        self.assertIn("customer_view", [action["id"] for action in list_wave_actions("customers")])
        self.assertIn("customer_form_fill", [action["id"] for action in list_wave_actions("customers")])
        self.assertIn("customer_add_phone", [action["id"] for action in list_wave_actions("customers")])
        self.assertIn("customer_add_contact", [action["id"] for action in list_wave_actions("customers")])
        self.assertIn("customer_clear_address", [action["id"] for action in list_wave_actions("customers")])
        self.assertIn("customer_cancel_form", [action["id"] for action in list_wave_actions("customers")])
        self.assertIn("customer_create_invoice", [action["id"] for action in list_wave_actions("customers")])
        self.assertIn("customer_delete", [action["id"] for action in list_wave_actions("customers")])
        self.assertIn("customer_import_csv_choose_file", [action["id"] for action in list_wave_actions("customers")])
        self.assertIn("customer_import_csv_preview", [action["id"] for action in list_wave_actions("customers")])
        self.assertIn("customer_import_csv_instructions", [action["id"] for action in list_wave_actions("customers")])
        self.assertIn("customer_import_csv_template_download", [action["id"] for action in list_wave_actions("customers")])
        self.assertIn("product_service_list_read", [action["id"] for action in list_wave_actions("selling_products_services")])
        self.assertIn("product_service_form_read", [action["id"] for action in list_wave_actions("selling_products_services")])
        self.assertIn("product_service_form_fill", [action["id"] for action in list_wave_actions("selling_products_services")])
        self.assertIn("product_service_sell_toggle", [action["id"] for action in list_wave_actions("selling_products_services")])
        self.assertIn("product_service_buy_toggle", [action["id"] for action in list_wave_actions("selling_products_services")])
        self.assertIn("product_service_income_account_select", [action["id"] for action in list_wave_actions("selling_products_services")])
        self.assertIn("product_service_expense_account_select", [action["id"] for action in list_wave_actions("selling_products_services")])
        self.assertIn("product_service_tax_select", [action["id"] for action in list_wave_actions("selling_products_services")])
        self.assertIn("selling_product_service_upsert", [action["id"] for action in list_wave_actions("selling_products_services")])
        self.assertIn("product_service_update", [action["id"] for action in list_wave_actions("selling_products_services")])
        self.assertIn("product_service_delete", [action["id"] for action in list_wave_actions("selling_products_services")])
        self.assertIn("bill_workspace_read", [action["id"] for action in list_wave_actions("bills")])
        self.assertIn("bill_form_read", [action["id"] for action in list_wave_actions("bills")])
        self.assertIn("bill_form_fill", [action["id"] for action in list_wave_actions("bills")])
        self.assertIn("bill_line_item_fill", [action["id"] for action in list_wave_actions("bills")])
        self.assertIn("bill_line_item_delete", [action["id"] for action in list_wave_actions("bills")])
        self.assertIn("bill_cancel_form", [action["id"] for action in list_wave_actions("bills")])
        self.assertIn("statement_upload", [action["id"] for action in list_wave_actions("transactions")])
        self.assertIn("vendor_list_read", [action["id"] for action in list_wave_actions("vendors")])
        self.assertIn("vendor_form_read", [action["id"] for action in list_wave_actions("vendors")])
        self.assertIn("vendor_form_fill", [action["id"] for action in list_wave_actions("vendors")])
        self.assertIn("vendor_import_menu_open", [action["id"] for action in list_wave_actions("vendors")])
        self.assertIn("vendor_create_bill", [action["id"] for action in list_wave_actions("vendors")])
        self.assertIn("vendor_import_csv", [action["id"] for action in list_wave_actions("vendors")])
        self.assertIn("buying_product_service_list_read", [action["id"] for action in list_wave_actions("buying_products_services")])
        self.assertIn("buying_product_service_form_read", [action["id"] for action in list_wave_actions("buying_products_services")])
        self.assertIn("buying_product_service_default_state_read", [action["id"] for action in list_wave_actions("buying_products_services")])
        self.assertIn("buying_product_service_form_fill", [action["id"] for action in list_wave_actions("buying_products_services")])
        self.assertIn("buying_product_service_sell_toggle", [action["id"] for action in list_wave_actions("buying_products_services")])
        self.assertIn("buying_product_service_buy_toggle", [action["id"] for action in list_wave_actions("buying_products_services")])
        self.assertIn("buying_product_service_income_account_select", [action["id"] for action in list_wave_actions("buying_products_services")])
        self.assertIn("buying_product_service_expense_account_select", [action["id"] for action in list_wave_actions("buying_products_services")])
        self.assertIn("buying_product_service_account_create", [action["id"] for action in list_wave_actions("buying_products_services")])
        self.assertIn("buying_product_service_tax_select", [action["id"] for action in list_wave_actions("buying_products_services")])
        self.assertIn("buying_product_service_update", [action["id"] for action in list_wave_actions("buying_products_services")])
        self.assertIn("buying_product_service_delete", [action["id"] for action in list_wave_actions("buying_products_services")])
        self.assertIn("chart_account_archive", [action["id"] for action in list_wave_actions("chart_of_accounts")])
        self.assertIn("invoice_send_reminder", [action["id"] for action in list_wave_actions("invoices")])
        self.assertIn("bill_attach_receipt", [action["id"] for action in list_wave_actions("bills")])
        self.assertIn("transaction_reconcile", [action["id"] for action in list_wave_actions("transactions")])
        self.assertIn("transaction_workspace_read", [action["id"] for action in list_wave_actions("transactions")])
        self.assertIn("transaction_more_menu_open", [action["id"] for action in list_wave_actions("transactions")])
        self.assertIn("transaction_account_filter_select", [action["id"] for action in list_wave_actions("transactions")])
        self.assertIn("transaction_account_upload_statement", [action["id"] for action in list_wave_actions("transactions")])
        self.assertIn("transaction_account_create", [action["id"] for action in list_wave_actions("transactions")])
        self.assertIn("transaction_add_deposit", [action["id"] for action in list_wave_actions("transactions")])
        self.assertIn("transaction_add_withdrawal", [action["id"] for action in list_wave_actions("transactions")])
        self.assertIn("transaction_add_journal_entry", [action["id"] for action in list_wave_actions("transactions")])
        self.assertIn("transaction_form_fill", [action["id"] for action in list_wave_actions("transactions")])
        self.assertIn("transaction_account_select", [action["id"] for action in list_wave_actions("transactions")])
        self.assertIn("transaction_category_select", [action["id"] for action in list_wave_actions("transactions")])
        self.assertIn("transaction_sales_tax_toggle", [action["id"] for action in list_wave_actions("transactions")])
        self.assertIn("transaction_vendor_select", [action["id"] for action in list_wave_actions("transactions")])
        self.assertIn("transaction_row_read", [action["id"] for action in list_wave_actions("transactions")])
        self.assertIn("transaction_sort_newest_to_oldest", [action["id"] for action in list_wave_actions("transactions")])
        self.assertIn("transaction_sort_oldest_to_newest", [action["id"] for action in list_wave_actions("transactions")])
        self.assertIn("transaction_search_submit", [action["id"] for action in list_wave_actions("transactions")])
        self.assertIn("transaction_load_more", [action["id"] for action in list_wave_actions("transactions")])
        self.assertIn("statement_upload_instructions_read", [action["id"] for action in list_wave_actions("transactions")])
        self.assertIn("statement_file_choose", [action["id"] for action in list_wave_actions("transactions")])
        self.assertIn("statement_payment_account_select", [action["id"] for action in list_wave_actions("transactions")])
        self.assertIn("statement_csv_template_download", [action["id"] for action in list_wave_actions("transactions")])
        self.assertIn("chart_account_list_read", [action["id"] for action in list_wave_actions("chart_of_accounts")])
        self.assertIn("chart_account_help_open", [action["id"] for action in list_wave_actions("chart_of_accounts")])
        self.assertIn("chart_account_tab_view", [action["id"] for action in list_wave_actions("chart_of_accounts")])
        self.assertIn("chart_account_section_read", [action["id"] for action in list_wave_actions("chart_of_accounts")])
        self.assertIn("chart_account_activity_read", [action["id"] for action in list_wave_actions("chart_of_accounts")])
        self.assertIn("chart_account_form_read", [action["id"] for action in list_wave_actions("chart_of_accounts")])
        self.assertIn("chart_account_type_picker_open", [action["id"] for action in list_wave_actions("chart_of_accounts")])
        self.assertIn("chart_account_type_search", [action["id"] for action in list_wave_actions("chart_of_accounts")])
        self.assertIn("chart_account_type_select", [action["id"] for action in list_wave_actions("chart_of_accounts")])
        self.assertIn("chart_account_currency_select", [action["id"] for action in list_wave_actions("chart_of_accounts")])
        self.assertIn("chart_account_form_fill", [action["id"] for action in list_wave_actions("chart_of_accounts")])
        self.assertIn("chart_account_cancel_form", [action["id"] for action in list_wave_actions("chart_of_accounts")])
        self.assertIn("connected_account_unavailable_read", [action["id"] for action in list_wave_actions("connected_accounts")])
        self.assertIn("connected_account_manual_entry_plan", [action["id"] for action in list_wave_actions("connected_accounts")])
        self.assertIn("connected_account_statement_help_open", [action["id"] for action in list_wave_actions("connected_accounts")])
        self.assertIn("connected_account_statement_format_read", [action["id"] for action in list_wave_actions("connected_accounts")])
        self.assertIn("connected_account_statement_upload_steps_read", [action["id"] for action in list_wave_actions("connected_accounts")])
        self.assertIn("connected_account_statement_mapping_confirm", [action["id"] for action in list_wave_actions("connected_accounts")])
        self.assertIn("connected_account_statement_upload_complete", [action["id"] for action in list_wave_actions("connected_accounts")])
        self.assertIn("connected_account_csv_help_open", [action["id"] for action in list_wave_actions("connected_accounts")])
        self.assertIn("connected_account_payment_account_help_open", [action["id"] for action in list_wave_actions("connected_accounts")])
        self.assertIn("connected_account_wave_connect_help_open", [action["id"] for action in list_wave_actions("connected_accounts")])
        self.assertIn("connected_account_auto_updates_help_open", [action["id"] for action in list_wave_actions("connected_accounts")])
        self.assertIn("connected_account_support_open", [action["id"] for action in list_wave_actions("connected_accounts")])
        self.assertIn("connected_account_help_feedback", [action["id"] for action in list_wave_actions("connected_accounts")])
        self.assertIn("connected_account_help_share", [action["id"] for action in list_wave_actions("connected_accounts")])
        self.assertIn("business_checking_offer_read", [action["id"] for action in list_wave_actions("business_checking")])
        self.assertIn("business_checking_feature_read", [action["id"] for action in list_wave_actions("business_checking")])
        self.assertIn("business_checking_claim_steps_read", [action["id"] for action in list_wave_actions("business_checking")])
        self.assertIn("business_checking_promo_terms_read", [action["id"] for action in list_wave_actions("business_checking")])
        self.assertIn("report_catalog_section_read", [action["id"] for action in list_wave_actions("reports")])
        self.assertIn("report_date_range_set", [action["id"] for action in list_wave_actions("reports")])
        self.assertIn("report_as_of_date_set", [action["id"] for action in list_wave_actions("reports")])
        self.assertIn("report_basis_select", [action["id"] for action in list_wave_actions("reports")])
        self.assertIn("report_account_filter_select", [action["id"] for action in list_wave_actions("reports")])
        self.assertIn("report_contact_filter_select", [action["id"] for action in list_wave_actions("reports")])
        self.assertIn("report_update", [action["id"] for action in list_wave_actions("reports")])
        self.assertIn("report_table_read", [action["id"] for action in list_wave_actions("reports")])
        self.assertIn("report_empty_state_read", [action["id"] for action in list_wave_actions("reports")])
        self.assertIn("wave_payments_get_started", [action["id"] for action in list_wave_actions("wave_payments")])
        self.assertIn("sales_tax_create", [action["id"] for action in list_wave_actions("sales_tax_settings")])
        self.assertIn("financial_settings_update", [action["id"] for action in list_wave_actions("financial_settings")])
        self.assertIn("zoho_offer_open", [action["id"] for action in list_wave_actions("zoho_migration_offer")])
        self.assertIn("payroll_availability_read", [action["id"] for action in list_wave_actions("payroll")])
        self.assertIn("payroll_eligibility_rules_read", [action["id"] for action in list_wave_actions("payroll")])
        self.assertIn("payroll_business_settings_open", [action["id"] for action in list_wave_actions("business_settings")])
        self.assertIn("payroll_currency_read", [action["id"] for action in list_wave_actions("business_settings")])
        self.assertIn("business_profile_update", [action["id"] for action in list_wave_actions("business_settings")])
        action_ids = {action["id"] for action in list_wave_actions()}
        missing_control_actions = [
            control["action"]
            for feature in WAVE_SURFACE_CATALOG["feature_inventory"].values()
            for control in feature.get("controls", [])
            if control.get("action") and control["action"] not in action_ids
        ]
        self.assertEqual(missing_control_actions, [])
        self.assertGreaterEqual(parity["menu_groups"], 6)
        self.assertGreaterEqual(parity["menu_items"], 20)
        self.assertGreaterEqual(parity["sync_contracts"], 5)
        self.assertGreaterEqual(parity["feature_pages"], 48)
        self.assertGreaterEqual(parity["observed_controls"], 525)
        self.assertGreaterEqual(parity["actions"], 265)
        self.assertEqual(parity["report_sections"], 5)
        self.assertEqual(parity["reports"], 12)
        self.assertGreater(parity["pages_by_automation_mode"]["safe_draft"], 0)

if __name__ == "__main__":
    unittest.main()


