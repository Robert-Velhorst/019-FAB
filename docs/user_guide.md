# User Guide for Automated Bookkeeping Solution

## 1. Introduction

Welcome to the Automated Bookkeeping Solution! This guide will walk you through how to set up, configure, and use the system to automate your financial document processing and data entry.

## 2. System Overview

The Automated Bookkeeping Solution is designed to streamline your bookkeeping process by:

*   **Fetching Documents**: Automatically retrieves financial documents (receipts, invoices, statements) from various sources like Gmail, Google Drive, Google Photos, and Freshdesk.
*   **Processing Documents**: Extracts key information from these documents using advanced OCR (Optical Character Recognition) and data extraction techniques.
*   **Categorizing Transactions**: Categorizes transactions into predefined accounts (e.g., Personal, Business, Handicap-related) using a combination of rule-based and machine learning models.
*   **Automated Data Entry**: Enters the processed and categorized data into your chosen bookkeeping platforms, currently supporting mijngeldzaken.nl and Waveapps.
*   **Learning and Improvement**: Continuously learns from your data and feedback to improve categorization accuracy over time.

## 3. Getting Started

Before you can use the system, you need to complete the initial setup steps.

### 3.1. Prerequisites

Ensure you have the following installed on your system:

*   **Python 3.15.1**: `Start-FAB.cmd` provisions the project-local runtime automatically on Windows through the Windows Python launcher, `uv`, or a matching system `python` installation.
*   **`pip`**: Python's package installer, usually comes with Python.
*   **`git`** (optional, if cloning from a repository): Download from [git-scm.com](https://git-scm.com/downloads).
*   **Docker** (optional, for containerized deployment): Download from [docker.com](https://www.docker.com/products/docker-desktop).

### 3.2. Installation

1.  **Download the Project:**

    If you received a zip archive, extract its contents to your desired directory. For example, `C:\bookkeeping_app` on Windows or `/home/user/bookkeeping_app` on Linux/macOS.

    If you are cloning from a Git repository:
    ```bash
        git clone https://github.com/Robert-Velhorst/019-FAB.git
        cd 019-FAB
    ```

2.  **Create a Python Virtual Environment (Recommended):**

    A virtual environment isolates your project's dependencies from other Python projects.
    ```bash
    python3.13 -m venv .venv
    source .venv/bin/activate  # On Windows, use `.venv\Scripts\activate`
    ```

3.  **Install Dependencies:**

    Navigate to the project's root directory (where `requirements.txt` is located) and install the necessary Python packages:
    ```bash
    pip install -r requirements.txt
    ```

## 4. Configuration

The system's behavior is controlled by the `config/config.ini` file. A template `config_template.ini` is provided. You **must** copy this template and rename it to `config.ini`.

```bash
cp config/config_template.ini config/config.ini
```

Now, open `config/config.ini` in a text editor and fill in the required details for each section. Below is a breakdown of the key sections and parameters.

**Security Note**: For sensitive credentials (like API keys, passwords), it is highly recommended to use **environment variables** instead of directly writing them into `config.ini`. The system's `ConfigLoader` is designed to automatically override `config.ini` values with environment variables that are prefixed with `APP_` (e.g., `APP_GMAIL_CLIENT_ID` will override `gmail.client_id` in `config.ini`). Local operations settings also accept direct `FAB_LOCAL_*` flat variables such as `FAB_LOCAL_LEDGER_PATH` and `FAB_LOCAL_API_PORT`.

### 4.1. `[app]` Section

General application settings.

*   `log_file`: Path to the application log file. (e.g., `logs/app.log`)

### 4.1.1. `[operations]` Section

Local operating ledger and optional web operations API settings.

*   `local_ledger_enabled`: Set to `true` to persist workflow runs and ordered step evidence, document status, normalized bookkeeping records and line items, review items, routing attempts, export attempts, bank statement imports, bank transactions, reconciliation matches, and audit events to local SQLite.
*   `ledger_path`: Path to the SQLite ledger file. On Windows, prefer a private folder such as `C:\Users\<you>\AppData\Local\FAB\fab_operations.sqlite3`.
*   `api_host`: Host for the optional local operations API. Keep FAB on loopback for local use; production hosting is targeted to Hetzner.
*   `api_port`: Port for the optional local operations API. Default: `5001`.
*   `api_base_url`: Trusted public origin for an authenticated reverse proxy. Leave blank for local use; FAB does not trust forwarded host headers implicitly.
*   `api_token`: Bearer token required by the optional local operations API and optional web operations API. Configure this before any non-loopback exposure.
*   `local_intake_paths`: Comma- or semicolon-separated local folders to scan from the dashboard, such as a Google Drive-synced `sort out` folder.
*   `local_intake_extensions`: File extensions accepted by local intake. Default document types include PDF, image, text, and CSV files.
*   `backup_dir`: Folder where the local dashboard/API stores recovery packages. Keep this outside Git-tracked folders for real financial metadata.
*   `backup_restore_source_root`: Immutable local root used for checksum-verified source evidence recovered from a version 2 package. Keep it on durable storage and outside `backup_dir`.
*   `categorization_review_confidence_threshold`: Minimum category confidence before FAB can keep a processed document out of manual review.
*   `waveapps_default_account`: Default Wave account name used when preparing local transaction draft payloads.
*   `review_stale_hours`: Age at which open review items are flagged in Operations Health. Default: `48`.
*   `document_stale_hours`: Age at which imported, processing, or review documents are flagged as stuck. Default: `24`.
*   `routing_stale_hours`: Age at which prepared routing drafts are flagged as waiting too long for approval/export. Default: `24`.
*   `workflow_stale_hours`: Age at which a running workflow is treated as stale. Default: `6`.
*   `source_stale_hours`: Age at which a previously ready connector is considered stale. Default: `24`.
*   `health_api_issue_limit`: Maximum prioritized issue details serialized by `/api/health`; exact aggregate counts and safety status still cover every issue. Default: `50`, allowed range: `1-500`.
*   `support_health_issue_limit`: Maximum sanitized issue details included in doctor/support output; type and severity totals remain complete. Default: `100`, allowed range: `1-500`.
*   `health_cache_ttl_seconds`: Server-side single-flight window for identical `/api/health` projections. Default: `2`, allowed range: `0-30`; `0` disables it. This never caches internal autonomy, notification, close, exception, or external-execution safety decisions, and HTTP responses remain `no-store`.
*   `worker_sync_source_connectors`: Run durable enabled-source intake before the autonomous cycle. Default: `true`.
*   `worker_source_connectors`: Optional comma-separated allowlist of connector names; blank means all currently syncable unattended connectors.
*   `connector_intake_lease_seconds`: Prevent overlapping API, worker, and recovery connector syncs. Default: `21600`.
*   `worker_recover_workflows`: Run bounded governed recovery before normal connector intake. Default: `true`.
*   `worker_recovery_batch_limit`: Maximum due recoveries attempted per worker cycle. Default: `5`.
*   `workflow_recovery_max_retries`: Maximum linked automatic retry depth. Default: `3`.
*   `workflow_recovery_base_delay_seconds` / `workflow_recovery_max_delay_seconds`: Exponential retry backoff bounds. Defaults: `300` / `3600`.
*   `workflow_recovery_stale_seconds`: Grace period before a running connector/autonomy workflow with no active lease is finalized as interrupted. Default: `900` (15 minutes). An active runtime lease always prevents finalization.
*   Local reconciliation uses the `[reconciliation]` matching thresholds and stores imported bank transactions, candidate matches, missing-receipt alerts, unmatched documents, and approval decisions in the local ledger.
*   `enabled`: Set to `true` only when the local web operations API is running and protected by a token.
*   `api_url`: Base URL for the optional web operations API, such as `http://127.0.0.1:3000`.
*   `timeout_seconds`: Request timeout for the optional web operations API.

The local ledger does not store API tokens or passwords. It can store financial document metadata and OCR text, so keep the file out of Git and protect it like bookkeeping records.

The Workflow Runs panel shows recent autonomous and connector-intake runs with ordered step status, attempt, start time, duration, error, and aborted downstream work. `GET /api/workflows` filters runs by `status` or `triggerSource`; `GET /api/workflows/{id}` returns the exact run and its redacted step evidence. A failed autonomous action stops dependent downstream work and marks it `not_run`. For a recoverable run, FAB displays the exact safe path and enables **Resume safe workflow**. `GET /api/workflows/{id}/recovery-plan` returns the current gate; authenticated `POST /api/workflows/{id}/retry` creates a new linked run rather than rewriting prior evidence. The Recovery policy summary and `GET /api/workflows/recovery` show due, deferred, blocked, and exhausted work; **Run due safe recovery** or authenticated `POST /api/workflows/recovery/run-due` executes the same bounded policy used by the worker and HAI connector. Connector recovery retries only failed read-only sources. Autonomous recovery retries the first actual failed low-risk step, then selects only source-proven `not_run` descendants from the explicit local dependency graph. A continuation runs only when its recovered predecessor produced relevant output; unrelated queued work, approved export execution, and other external actions are excluded. The worker applies exponential backoff, prevents the normal connector stage from bypassing that backoff, and finalizes abandoned runs only when the workflow is stale and its runtime lease is inactive.
One-click and HAI safe-cycle requests collect every currently syncable read-only connector before local folder intake and document processing. The background worker already has a dedicated connector stage, so it disables this nested collection step in its immediately following autonomous cycle to avoid duplicate provider reads.

Run the local API with:

```bash
python -m src.operations.local_api
```

Open `http://127.0.0.1:5001/` for the local dashboard. Use the Operations Health panel to see stale review items, stuck documents, failed records, routing blocks, prepared drafts waiting for approval, and stale workflow runs. Use the Autonomous Cycle panel to plan or run the safe local loop: folder intake, imported-document processing, Wave draft preparation, reconciliation candidate creation from persisted or supplied bank transactions, and read-only Wave reconciliation planning. It never submits data to Wave, resolves reviews, changes credentials, restores backups, deletes files, or sends external messages. Use the Folder Intake panel to rescan configured folders into the ledger, then process imported documents through OCR/text extraction, categorization, validation, and review gates. The Bookkeeping Records panel shows FAB's normalized financial records across document and bank sources, including review-required state, export readiness, reconciliation status, target system, vendor, date, category, amount, line-item count, account mapping, and tax mapping. The Financial Reports panel shows provisional accrual-basis P&L, VAT position, bank cash movement, and expense breakdowns for the current year. `/api/reports` can select accrual or cash basis, date range, target system, JSON or CSV; totals remain separated by currency and completeness gates expose undated, unreconciled, review-required, unmapped, or truncated inputs. The worker also claims deterministic daily, weekly, or monthly report slots and writes checksum-bound JSON/CSV artifacts locally. The dashboard shows slot, period, retry state, completeness gates, and verified artifact links; `/api/report-runs` exposes the same evidence. These reports are not statutory filings and are never emailed or submitted externally. The Extracted Fields panel shows field-level vendor/date/amount/VAT/category evidence, confidence, and provenance for processed documents. Manual Review cards can approve, reject, resolve, and correct vendor/category/date/amount fields; approved corrections update the normalized bookkeeping record and create suggested vendor/category rules for future learning. Reconciliation-related review cards also show linked bank transaction evidence: approving a candidate marks the document, normalized record, and bank transaction reconciled, while resolving or choosing "No receipt needed" closes missing-receipt exceptions without posting to Wave. The Routing & Export Drafts panel prepares Wave draft operation plans for reviewed documents and records them as routing attempts without submitting anything to Wave. The Export Attempts panel turns those routing drafts into approval records, requires `APPROVE FAB EXPORT DRAFT` before local approval, and requires `RECORD FAB EXPORT RESULT` before recording a separate executor result; approval does not submit to Wave by itself. The Wave Control Center models Wave menus, reports, report packs, and operation safety gates so FAB can plan account-transactions, trial balance, sales tax, customer/vendor, and close-pack workflows before a separate approved executor touches Wave. Planned report work is persisted as Wave report snapshots with report type, period/as-of date, cash/accrual basis, account/contact scope, export format, operation id, and workflow provenance so FAB can audit what evidence should be read or exported later. The Bank Transactions panel persists Wave account-transactions exports and bank statements from JSON, CSV, CAMT XML, or MT940-style text, normalizes localized amounts/dates, detects duplicate transaction identities, and exposes unreconciled rows to the autonomous loop. The Reconciliation panel can use imported bank transactions automatically or accept a temporary JSON override batch; it matches them to processed documents, records candidate matches, opens missing-receipt or unmatched-document review items, and requires an audited decision before a document and linked bank transaction are marked reconciled. The Backups panel creates manifest-based SQLite ledger backups, lists backup checksums, and only restores when the exact phrase `RESTORE FAB LOCAL LEDGER` is supplied. The Settings panel and `/api/settings` show source readiness, dependency status, storage paths, credential presence, and remote exposure safety without returning API tokens, passwords, or other secret values. The API also exposes `/api/health`, `/api/settings`, `/api/autonomy/plan`, `/api/autonomy/run`, `/api/dashboard`, `/api/workflows`, `/api/workflows/{id}`, `/api/workflows/{id}/recovery-plan`, `/api/workflows/{id}/retry`, `/api/sources`, `/api/sources/readiness`, `/api/sources/sync`, `/api/sources/google-photos/sessions`, `/api/sources/google-photos/sessions/{id}`, `/api/sources/google-photos/sessions/{id}/collect`, `/api/sources/google-photos/sessions/{id}/cancel`, `/api/extracted-fields`, `/api/bookkeeping-records`, `/api/bookkeeping-records/{id}`, `/api/bookkeeping-records/{id}/line-items`, `/api/bookkeeping-records/refresh`, `/api/reports`, `/api/report-runs`, `/api/report-runs/run-due`, `/api/report-runs/{id}`, `/api/report-runs/{id}/artifact`, `/api/wave`, `/api/wave/actions`, `/api/wave/reports`, `/api/wave/reports/plan`, `/api/wave/report-snapshots`, `/api/wave/plan`, `/api/wave/workflows/plan`, `/api/bank-transactions`, `/api/bank-transactions/import`, `/api/intake/rescan`, `/api/documents`, `/api/documents/{id}/process`, `/api/documents/process-imported`, `/api/documents/{id}/route`, `/api/routing`, `/api/routing/prepare-ready`, `/api/routing/{id}/export-attempt`, `/api/export-attempts`, `/api/export-attempts/prepare-ready`, `/api/export-attempts/{id}/approve`, `/api/export-attempts/{id}/result`, `/api/reconciliation`, `/api/reconciliation/run`, `/api/reconciliation/{id}/resolve`, `/api/backups`, `/api/backups/inspect`, `/api/backups/restore`, `/api/review`, `/api/rules`, `/api/corrections`, and `/api/audit`. `/api/health` returns the same operational health summary used by the dashboard plus a compact readiness summary.

The operator dashboard initially loads 50 complete document reviews. Use **Load next** to retrieve the next bounded page; the status shows loaded versus authoritative total. FAB keeps every open decision for one document together, and the activation flow always uses queue-wide posting blockers even when only the first page is visible.

The activation flow is ordered as Google Drive, Gmail scanner, Wave, Wave receipt session, and document decisions. Its progress and current action come from live readiness; completing a setup drawer does not advance the flow until FAB reads the new state. Activation never submits a transaction or archives a source file.

Edits in a source-backed document review are retained in the current browser tab. Reopening the review or reloading the dashboard restores the draft only while the same review and source evidence are still current. FAB discards stale or malformed drafts automatically, clears a draft after successful approval, and provides a trash icon for explicit discard. A saved draft is not a bookkeeping decision and never changes the ledger or a provider account.

Use the Notification Center to refresh current operating alerts, open their evidence section, and mark them read, acknowledged, or resolved. FAB deduplicates repeated worker observations and automatically resolves an alert after the underlying health issue clears. The inbox includes overdue and upcoming unsettled Wave invoices when the read-only mirror has due-date evidence. Notification preferences can set a wildcard or event-specific minimum severity and disable the local inbox for an event. `/api/notifications`, `/api/notifications/refresh`, `/api/notifications/{id}/status`, and `/api/notification-preferences` expose the same controls. This notification layer is local only: email, Slack, invoice reminders, and other external delivery remain disabled and require a future separately approved delivery workflow.

Use VAT & Compliance to assess the current quarter or call `POST /api/compliance/assessments` with an explicit `fromDate` and `toDate`. FAB stores a source-checksummed provisional assessment, VAT totals by currency, record-linked findings, source-file presence, and seven-year retain-until dates. Correct the underlying record and assess again, or acknowledge a finding; resolving or accepting an exception requires a written reason. Restored source files change the checksum and replace the old finding snapshot. `/api/compliance/assessments`, `/api/compliance/assessments/{id}`, `/api/compliance/findings`, `/api/compliance/findings/{id}/status`, and `/api/compliance/retention` expose the evidence. This is a review aid, not a Dutch tax return: FAB does not map every Belastingdienst return box, file a return, make a legal conclusion, or authorize deletion of source documents.

The Sources panel and `/api/sources` show observed folder or connector sources, their latest status, last scan time, seen/imported/duplicate counters, and source identifiers. `GET /api/sources/readiness` shows whether each connector is configured, enabled, syncable, or requires supervision. `POST /api/sources/sync` synchronizes selected enabled Gmail, Google Drive, and Freshdesk sources into the durable document ledger without running OCR or posting downstream. Google Photos selections are separate and user-owned: start a session, open its Google `pickerUri`, select receipt images, then use **Check & import**. Exact-content duplicates are idempotent; a changed provider item creates a new revision and review item instead of overwriting prior evidence. Connector metadata and errors that look like tokens, passwords, secrets, credentials, authorization headers, or API keys are redacted before they are stored.

### 4.2. `[gmail]` Section

Configuration for fetching documents from Gmail.

*   `enabled`: Opt in to connector intake. Defaults to `false`.
*   `credentials_file`: Path to your Google API credentials JSON file (downloaded from Google Cloud Console). (e.g., `credentials/gmail_credentials.json`)
*   `token_file`: Path to a token created during supervised OAuth setup. Workers do not open an OAuth browser by default.
*   `attachment_download_dir`: Directory to save downloaded Gmail attachments. (e.g., `downloads/gmail`)
*   `query`: Gmail search query to filter emails. Examples:
    *   `has:attachment from:"example@vendor.com" subject:"Invoice"`
    *   `label:receipts after:2025/01/01`
*   `scanner_mode`: Enables strict scanner-mailbox intake. In this mode FAB accepts only PDF filenames with an allowed PDF MIME type and a verified `%PDF-` file signature.
*   `trusted_senders`: Comma-separated exact sender addresses accepted in scanner mode. The Noodzakelijk Online HP ePrint profile uses `eprintcenter@hp8.us`.
*   `max_attachment_bytes`: Rejects oversized scanner attachments before writing them to the intake cache.
*   `max_run_bytes`: Caps combined attachment bytes in one scan; reaching it marks the intake partial and prevents checkpoint advancement. Defaults to 250 MiB.
*   `max_run_attachments`: Caps accepted attachments per scan; reaching it marks the intake partial and prevents checkpoint advancement. Defaults to 5,000.
*   `incremental_overlap_seconds`: Rechecks this many seconds before the durable last-successful checkpoint. The overlap prevents boundary loss while stable provider IDs keep repeated results idempotent.

Use the **Gmail scanner** activation step in the operator dashboard to install a desktop OAuth client and complete user-owned, read-only Gmail consent. FAB reads matching attachments directly into its durable ledger; it does not stage them through Drive, mark messages read, add labels, or delete source email. The worker then runs OCR, field extraction, validation, duplicate detection, learned vendor categorization, routing, and the existing Wave approval/readback gates. Disable the older Apps Script trigger after activation so it cannot create a second, racing Drive copy.

When Google reports that a Gmail or Drive refresh token was revoked or expired, FAB changes that source to **Reauthorization required**, creates a non-secret local marker, and pauses repeated unattended attempts. Open the matching activation card, complete consent, then refresh readiness. Local rescans, projections, and draft preparation can continue during this repair; approved external execution stays blocked while operations health is unresolved.

Normalized bookkeeping records apply fail-closed financial consistency controls. Dutch day-first dates are canonicalized to ISO dates; impossible or ambiguous dates are retained only as evidence and block export. VAT and line-item tax amounts that lack a non-zero total, conflict in sign, or exceed the configured `vat_max_total_ratio` are retained as evidence but removed from normalized posting totals until an operator corrects the source-backed review item.

The operator activation checklist distinguishes posting-blocking decisions from evidence-only reviews. Bank statements, sensitive government documents, and other confidently non-posting material remain visible and retained for review, but they no longer inflate the number of documents blocking Wave delivery. The complete review workspace still shows both groups; no review item is silently resolved.

Use **Bank statement** in the operator dashboard Overview, or the setup button on the **Banking API** connection row, to import an account export directly into FAB's local ledger. Enter a stable account identifier, preferably the IBAN or the same account label used for every export, then choose one CSV, JSON, CAMT/XML, or MT940 file up to 4 MB. FAB validates the format and financial columns, keeps legitimate repeated rows, treats exact re-imports as duplicates, and starts local reconciliation only when new rows were added. The result shows new, already-present, and skipped rows plus the explicit external-submission state. This workflow does not connect to a bank, upload the original statement to Wave, or submit anything externally.

### 4.3. `[google_drive]` Section

Configuration for fetching documents from Google Drive.

*   `enabled`: Opt in to connector intake. Defaults to `false`.
*   `credentials_file`: Path to your Google API credentials JSON file. (e.g., `credentials/drive_credentials.json`)
*   `token_file`: Path to a token created during supervised OAuth setup. Workers do not open an OAuth browser by default.
*   `download_dir`: Directory to save downloaded Drive files. (e.g., `downloads/drive`)
*   `folder_id`: The ID of the specific Google Drive folder to monitor. You can find this in the URL when viewing the folder in Google Drive.
*   `file_types`: Comma-separated list of file extensions to download (e.g., `pdf,jpg,png`).

### 4.4. `[freshdesk]` Section

Configuration for fetching documents from Freshdesk.

*   `enabled`: Opt in to connector intake. Defaults to `false`.
*   `api_key`: Your Freshdesk API key.
*   `domain`: Your Freshdesk domain (e.g., `yourcompany.freshdesk.com`).
*   `api_url`: Optional repository-025 compatibility alternative to `domain`; FAB accepts a tenant root or an API path ending in `/api`, `/v2`, or `/api/v2`.
*   `download_dir`: Directory to save downloaded Freshdesk attachments. (e.g., `downloads/freshdesk`)
*   `financial_filter_enabled`: Enables the consolidated repository-025 financial-ticket profile.
*   `financial_keywords`: Comma-separated subject/description terms used to admit financial tickets.
*   `ticket_statuses`: Freshdesk status IDs searched by the profile; defaults to open and pending (`2,3`).
*   `include_ticket_description`: Retains normalized ticket text as non-posting supporting evidence.
*   `pdf_only`: Requires attachment filename, MIME type, and binary signature to identify a PDF.
*   `require_https_attachments`: Retained for configuration compatibility; attachment downloads always require HTTPS.
*   `attachment_allowed_hosts`: Optional comma-separated exact hostnames for Freshdesk attachment CDNs. The configured API hostname is allowed automatically; redirects and IP-literal URLs are rejected.

The Freshdesk connector is read-only. It never closes tickets and does not copy
files to Drive; matched evidence enters FAB's immutable local ledger directly.

### 4.5. `[google_photos]` Section

Google Photos is a supervised source. Google no longer permits the previous whole-library background read model, so FAB never starts or completes a Picker session from the worker.

*   `enabled`: Enable user-owned Picker sessions in Sources. Defaults to `false`.
*   `mode`: Must be `picker` for new installations.
*   `credentials_file`: Path to Google Photos Picker credentials.
*   `picker_token_file`: Path to JSON Picker authorization state. Pickle token files are rejected.
*   `download_dir`: Private local destination for selected receipt images.
*   `picker_autoclose`: Append Google's `/autoclose` behavior to the user-facing picker link. Default: `true`.
*   `max_pages`, `max_items`, `max_media_bytes`, `request_timeout_seconds`: Completeness, file-size, and network bounds for supervised imports.

After enabling the Picker API and creating an OAuth desktop client with the `photospicker.mediaitems.readonly` scope, run this once from a local terminal:

```bash
python -m src.run_photos_picker_auth
```

The command opens Google's user-owned OAuth flow and stores only the resulting JSON token at `picker_token_file`. It does not run in the worker or accept credentials from the dashboard. The local API exposes `GET/POST /api/sources/google-photos/sessions`, `GET /api/sources/google-photos/sessions/{id}`, and explicit `POST .../{id}/collect` and `POST .../{id}/cancel` actions. Successful collection pages through every selected item, downloads only HTTPS Google-hosted photos with a bearer token, applies file-size limits, registers evidence through duplicate/revision controls, and deletes the provider session. Videos are skipped rather than entering the receipt OCR pipeline.

### 4.6. `[document_processing]` Section

Settings for document processing.

*   `primary_ocr_method`: Primary OCR engine. The supported default pipeline accepts `tesseract` or explicitly configured `vision`.
*   `enable_line_item_extraction`: Enable or disable line-item extraction.
*   `enable_template_matching`: Enable or disable user-owned vendor rules.
*   `template_matching_templates_dir`: Optional directory containing JSON vendor definitions. Files load in filename order.
*   `vendor_templates_file`: Optional JSON file containing vendor definitions; it overrides same-named directory definitions.
*   `vendor_templates`: Optional inline JSON object; it overrides same-named file definitions. Invalid files, schemas, and regular expressions are isolated and do not abort OCR.
*   `tesseract_cmd`: Path to the Tesseract executable (if not in system PATH). (e.g., `/usr/local/bin/tesseract`)
*   `tesseract_lang`: Default Tesseract language (e.g., `eng`, `nld`).
*   `dutch_ocr_lang`: Language for Dutch OCR processor (e.g., `nld`).
*   `handwritten_model_path`: Path to the trained model for handwritten recognition.

### 4.7. `[categorizer]` Section

Settings for document categorization.

*   `categorization_rules`: Path to a JSON file defining rule-based categorization rules. (e.g., `config/categorization_rules.json`)
*   `default_fallback_category`: The category to assign if no other categorization method yields a confident result. (e.g., `Manual Review`)
*   `ml_confidence_threshold`: Minimum confidence score for ML categorizer to accept a prediction (0.0 to 1.0).
*   `fab_auto_apply_trusted_category_suggestions`: Defaults to `true`. Applies only FAB's fixed, exact-normalized-vendor taxonomy automatically; it does not accept fuzzy or model-generated categories and never closes validation, duplicate, sensitive-document, credit-note, posting-approval, attachment, or archive gates.
*   `fab_trusted_category_suggestion_min_confidence`: Defaults to `0.95` and cannot weaken the built-in 0.95 policy floor.
*   `ml_model_path`: Path to the trained ML categorization model. (e.g., `models/ml_categorizer_model.joblib`)
*   `ml_vectorizer_path`: Path to the TF-IDF vectorizer used by the ML model. (e.g., `models/tfidf_vectorizer.joblib`)

### 4.8. `[mijngeldzaken]` Section

Settings for data entry into mijngeldzaken.nl.

*   `username`: Your mijngeldzaken.nl username.
*   `password`: Your mijngeldzaken.nl password.
*   `login_url`: The login page URL for mijngeldzaken.nl.
*   `import_url`: The URL for the CSV import page on mijngeldzaken.nl.
*   `csv_template`: JSON string defining the CSV structure and mapping from extracted data. Example: `{"columns": ["Date", "Description", "Amount", "Category"], "mapping": {"Date": "extracted_data.transaction_date", "Description": "extracted_data.description", "Amount": "extracted_data.total_amount", "Category": "category"}, "delimiter": ";"}`
*   `category_mapping`: JSON string mapping internal categories to mijngeldzaken.nl categories. Example: `{"Personal": "Huishouden", "Business": "Zakelijk"}`

### 4.9. `[waveapps_business]` Section

Settings for data entry into Waveapps Business account.

*   `access_token`: Your Waveapps Business API access token.
*   `business_id`: Your Waveapps Business ID.
*   `category_mapping`: JSON string mapping internal categories to Waveapps Business categories. Example: `{"Business": "Office Supplies", "Travel": "Travel Expenses"}`

### 4.10. `[waveapps_personal]` Section

Settings for data entry into Waveapps Personal account.

*   `access_token`: Your Waveapps Personal API access token.
*   `personal_id`: Your Waveapps Personal ID.
*   `category_mapping`: JSON string mapping internal categories to Waveapps Personal categories. Example: `{"Handicaps": "Medical Expenses", "Personal": "Household"}`
*   `handicap_tag`: A tag to append to descriptions for handicap-related expenses. (e.g., `#handicap`)

### 4.11. `[validation]` Section

Settings for data validation.

*   `receipt_validation_required_fields`: Comma-separated list of fields that must be present in extracted data for a receipt to be considered valid. (e.g., `vendor_name,total_amount,transaction_date`)
*   `btw_number_pattern`: Regular expression pattern for validating BTW (VAT) numbers. (e.g., `NL\d{9}B\d{2}`)

### 4.12. `[budget]` Section

Settings for budget management.

*   `budget_file`: Path to the JSON file storing budget definitions. (e.g., `data/budgets.json`)

### 4.13. `[banking]` Section

Settings for banking API integration.

*   `banking_api_endpoint`: URL of the banking API endpoint.
*   `banking_api_credentials`: JSON string with banking API credentials. Example: `{"client_id": "your_client_id", "client_secret": "your_client_secret"}`

### 4.14. `[reconciliation]` Section

Settings for automated reconciliation.

*   `reconciliation_threshold`: The maximum allowable difference between transaction amounts for them to be considered a match. (e.g., `0.05` for 5 cents)
*   `reconciliation_match_threshold`: Minimum combined amount, date, and vendor confidence required for an automatic match. Defaults to `0.9`.
*   `reconciliation_date_tolerance_days`: Maximum number of days between the bank transaction and document date. Defaults to `0`.
*   `reconciliation_use_absolute_amounts`: Match negative bank expenses with positive receipt totals. Defaults to `true`.

### 4.15. `[manual_review]` Section

Settings for manual review interface.

*   `manual_review_queue_file`: Path to the JSON file storing documents awaiting manual review. (e.g., `data/manual_review_queue.json`)

### 4.16. `[backup]` Section

Settings for backup and restore.

*   `backup_base_dir`: Base directory where backups will be stored. (e.g., `backups`)
*   `backup_paths`: Comma-separated list of files/directories to include in backups. (e.g., `data,config/config.ini`)
*   `backup_config`: JSON string defining backup type. Example: `{"type": "zip"}`

The local operations dashboard has a recovery flow under `[operations] backup_dir`. Version 2 recovery packages contain a SQLite snapshot plus content-addressed copies of every safe source document, with manifest counts and SHA-256 verification for the ledger and each unique source file. `worker_create_scheduled_backups=true` checks the schedule during each worker cycle; `backup_schedule_interval_hours` defaults to 24. Keep `backup_require_complete_source_evidence=true` so missing, unsafe, or checksum-drifted evidence fails closed instead of producing a false-complete package. The React operator dashboard shows redacted package and recovery readiness without exposing filesystem paths or confirmation phrases.

On Windows, stop normal operation and launch `Start-FAB-Maintenance.cmd`. Maintenance mode is local-only: the worker does not start, HAI execution is disabled, and every normal mutation is locked. Open **Advanced recovery**. Ledger-only recovery requires `RESTORE FAB LOCAL LEDGER`. A source-complete version 2 package also offers full recovery with `RESTORE FAB LEDGER AND SOURCE EVIDENCE`; FAB restores source bytes without overwriting an existing tree, rewrites recovered ledger paths, validates the live ledger and each file, and rolls the ledger back if validation fails. When finished, run `Stop-FAB.cmd` and then `Start-FAB.cmd` to resume automation. Rehearse this procedure on a copy before relying on it for production disaster recovery.

### 4.17. `[error_handling]` Section

Settings for error handling and recovery.

*   `error_recovery_max_retries`: Maximum number of retries for failed operations.
*   `error_recovery_retry_delay_seconds`: Delay between retries in seconds.
*   `email_notifications_enabled`: `true` or `false`. Enable/disable email notifications for critical errors.

### 4.18. `[workflow]` Section

Settings for autonomous progress tracking and restart safety.

*   `workflow_state_enabled`: Enable persistent source-document checkpoints. Defaults to `true`.
*   `workflow_state_file`: Path to the atomic JSON checkpoint file. Defaults to `data/workflow_state.json`.
*   `workflow_checkpoint_autosave`: Persist every terminal document transition immediately. Defaults to `true`. Disable only when batched disk writes are preferable and replay risk after a process crash is acceptable.
*   `workflow_checkpoint_fail_closed`: Block processing when existing checkpoint JSON is unreadable or structurally invalid. Defaults to `true`. Disable only for a deliberate checkpoint reset after preserving the damaged file.
*   `workflow_checkpoint_skip_statuses`: Optional comma-separated override for statuses skipped on later runs.
*   `workflow_known_documents_limit`: Maximum duplicate fingerprints retained in checkpoint state. Defaults to `1000`.
*   `workflow_run_lock_enabled`: Prevent overlapping workflow runs on the same host. Defaults to `true`.
*   `workflow_run_lock_file`: Optional lock-file path. Defaults to `<workflow_state_file>.lock`.
*   `workflow_run_lock_stale_seconds`: Recover abandoned locks older than this duration. Defaults to `21600` (six hours).
*   `duplicate_similarity_threshold`: Minimum evidence-weighted fuzzy duplicate score. Defaults to `0.9`.
*   `duplicate_amount_tolerance`: Maximum amount difference for duplicate comparison. Defaults to `0.02`.

Duplicate detection only suppresses documents with sufficient populated accounting evidence. Reused filenames or missing dates alone are not treated as duplicate proof.

## 5. Running the Application

### 5.1. Running the Workflow Locally

To execute the main automated bookkeeping workflow, navigate to the project's root directory and run:

```bash
python -m src.main
```

This runs one ownership-checked cycle of the authoritative ledger worker. It
performs only the configured governed stages and exits without starting a
second worker when the recurring Windows or container worker already owns the
checkout. Use `python -m src.run_worker` for an intentionally long-running
worker; the legacy checkpoint pipeline remains disabled unless its migration
compatibility switch is explicitly enabled.

### 5.2. Running Tests

It's highly recommended to run the tests to ensure everything is set up correctly and functioning as expected. From the project's root directory:

```bash
python -m unittest discover tests
```

### 5.3. Building Deployment Packages

The `package.py` script creates checksum-bound source archives for the supported Windows 11 standalone and Docker Compose environments. It must run from a clean committed checkout and never packages local configuration, tokens, credentials, financial data, logs, backups, test fixtures, or build output.

To build packages:

```bash
python package.py --target windows
python package.py --target compose
```

This creates `fab-windows-*.zip` or `fab-compose-*.zip` plus a `.zip.sha256` sidecar in `dist/`. Every archive includes `FAB/RELEASE-MANIFEST.json`; the builder verifies every member before reporting success. Cloud archives are for the authenticated Compose contract described in `deployment_guide.md`, not Google Cloud Functions.

## 6. Advanced Usage and Customization

### 6.1. Customizing Categorization Rules

Edit the JSON file specified in `[categorizer] categorization_rules` to define or modify your rule-based categorization logic. This file typically contains rules based on keywords, vendors, or other extracted data points.

### 6.2. Training ML Categorizer

Manual category corrections are persisted in the operations ledger and create reviewable vendor/category rule suggestions through `CorrectionLearningService`. FAB never fabricates OCR text or silently retrains a model from synthetic feedback. Rules become effective through the governed correction and review path.

### 6.3. Extending Document Fetchers/Processors/Data Entry Handlers

The system is designed with a modular architecture. You can extend its capabilities by creating new modules that adhere to the `base.py` interfaces in `src/document_fetchers`, `src/document_processors`, and `src/data_entry`. Refer to `module_interfaces.md` for detailed API specifications.

### 6.4. Manual Review Interface

Documents that cannot be automatically processed or categorized with high confidence are flagged for manual review. The local dashboard's Manual Review panel exposes document and linked bank evidence, correction controls, approval/rejection/resolution actions, and suggested-rule feedback. Provider-side source revisions also appear here so changed evidence cannot silently replace an earlier document.

## 7. Troubleshooting

*   **Check Logs**: Always start by examining the `app.log` file (or the path specified in `config.ini`) for error messages and warnings.
*   **Configuration Errors**: Double-check your `config.ini` file for typos, incorrect paths, or missing credentials. Ensure sensitive information is correctly set via environment variables if you choose that method.
*   **Dependency Issues**: If you encounter `ModuleNotFoundError` or similar, ensure all dependencies are installed (`pip install -r requirements.txt`) and your virtual environment is activated.
*   **Google API Authentication**: Create or refresh Gmail/Drive token files during a supervised setup run with `interactive_auth=true`, verify readiness, then restore `interactive_auth=false` for workers. Never delete a valid token as a first troubleshooting step; inspect the recorded connector error and credential/token paths. Google Photos authorization uses `python -m src.run_photos_picker_auth`; its JSON token must include the Picker read-only scope, and selection remains a dashboard-initiated user action.
*   **Supervised provider access**: FAB prepares reviewable artifacts. Complete any provider-owned browser step in a user-controlled session; FAB does not store provider passwords or silently drive that session.

## 8. Support

For further assistance, please refer to the `technical_reference.md` and `deployment_guide.md` documents, or contact the development team.


