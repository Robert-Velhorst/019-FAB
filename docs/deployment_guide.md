# Deployment Guide for Automated Bookkeeping Solution

This guide covers Windows 11 local operation and Docker Compose development. Production deployment is targeted to Hetzner.

## 1. Local Deployment

Local deployment is suitable for development, testing, and running the solution on a dedicated machine or server within your own infrastructure.

### 1.1. Prerequisites

*   **Python 3.13** installed (the Windows launcher can provision a project-local runtime).
*   **`pip`** (Python package installer).
*   **`git`** (if cloning from repository).
*   **Tesseract OCR**: Install Tesseract OCR engine and language packs (`eng`, `nld`) on your system. Refer to Tesseract's official documentation for installation instructions specific to your OS.
    *   **Ubuntu/Debian**: `sudo apt-get update && sudo apt-get install -y tesseract-ocr tesseract-ocr-eng tesseract-ocr-nld`
    *   **macOS (Homebrew)**: `brew install tesseract && brew install tesseract-lang`
    *   **Windows**: Download installer from [Tesseract-OCR GitHub](https://tesseract-ocr.github.io/tessdoc/Downloads.html).
*   **Playwright Browsers**: MijnGeldzaken remains supervised. Where that workflow is enabled, install Chromium after the Python dependencies with `python -m playwright install chromium`.

### 1.2. Deployment Steps

1.  **Obtain the Project Files:**

    *   **From a Zip Archive**: Extract the verified `fab-windows-<UTC timestamp>.zip` or `fab-compose-<UTC timestamp>.zip` release and retain its `.sha256` sidecar for verification.
    *   **From Git Repository**: Clone the repository:
        ```bash
        git clone https://github.com/Robert-Velhorst/019-FAB.git
        cd 019-FAB
        ```

2.  **Set up Python Environment:**

    It is highly recommended to use a Python virtual environment.
    ```bash
    python3.13 -m venv .venv
    source .venv/bin/activate  # On Windows, use `.venv\Scripts\activate.bat`
    ```

3.  **Install Python Dependencies:**

    Navigate to the project root directory and install the required Python packages:
    ```bash
    pip install -r requirements.txt
    ```

4.  **Configure the Application:**

    Copy the `config_template.ini` to `config.ini` and edit it with your specific settings and credentials. Refer to the `user_guide.md` for detailed configuration instructions.
    ```bash
    cp config/config_template.ini config/config.ini
    # Edit config/config.ini
    ```
    **Security Best Practice**: For sensitive information (API keys, passwords), use the operator dashboard's encrypted local setup or environment variables. The `ConfigLoader` will automatically pick up environment variables prefixed with `APP_` (e.g., `APP_GMAIL_CLIENT_ID` will override `gmail.client_id` in `config.ini`). Local operations settings also accept direct `FAB_LOCAL_*` variables such as `FAB_LOCAL_LEDGER_PATH`, `FAB_LOCAL_API_HOST`, `FAB_LOCAL_API_PORT`, and `FAB_LOCAL_API_TOKEN`. On Windows, **Connections > Wave - Noodzakelijk Online** encrypts the Wave token locally and protects its encryption key with current-user DPAPI. `Start-FAB.ps1` also provisions the dashboard signing secret in this encrypted current-user store unless a strong `JWT_SECRET` environment value is supplied. Managed deployments can instead use `FAB_WAVEAPPS_BUSINESS_ACCESS_TOKEN`, `FAB_WAVEAPPS_BUSINESS_ID`, `FAB_WAVEAPPS_PERSONAL_ACCESS_TOKEN`, and `FAB_WAVEAPPS_PERSONAL_ID`; environment values take precedence and do not need to be written to `config.ini`.

    For local-first operation on Windows 11, keep the FAB operations ledger enabled and store it in a private local data folder:
    ```ini
    [operations]
    local_ledger_enabled = true
    ledger_path = C:\Users\<you>\AppData\Local\FAB\fab_operations.sqlite3
    api_host = 127.0.0.1
    api_port = 5001
    api_token = choose-a-long-random-token-before-remote-exposure
    local_intake_paths = C:\Users\<you>\Google Drive\sort out
    local_intake_extensions = pdf,jpg,jpeg,png,heic,tif,tiff,txt,csv
    backup_dir = C:\Users\<you>\AppData\Local\FAB\backups
    backup_restore_source_root = C:\Users\<you>\AppData\Local\FAB\restored-source-evidence
    worker_create_scheduled_backups = true
    backup_schedule_interval_hours = 24
    backup_require_complete_source_evidence = true
    categorization_review_confidence_threshold = 0.7
    waveapps_default_account = Uncategorized
    review_stale_hours = 48
    document_stale_hours = 24
    routing_stale_hours = 24
    workflow_stale_hours = 6
    source_stale_hours = 24
    health_api_issue_limit = 50
    support_health_issue_limit = 100
    health_cache_ttl_seconds = 2
    worker_sync_source_connectors = true
    worker_source_connectors =
    enabled = false
    ```
    This SQLite ledger records workflow runs and ordered step evidence, document statuses, normalized bookkeeping records and line items, review items, routing attempts, export attempts, bank statement imports, bank transactions, reconciliation matches, and audit events without requiring the web database/API to be online. Local intake stores file metadata and SHA-256 duplicate fingerprints, not raw document bytes. The worker creates an atomic recovery package on the configured interval containing a verified SQLite snapshot and every source document that has a safe, checksum-matching ledger path. With `backup_require_complete_source_evidence=true`, any missing or changed source blocks the package instead of reporting a false-success backup. Keep the ledger and backup directory outside Git when they contain real financial data.

5.  **Run the Application:**

    You can run exactly one governed worker cycle manually:
    ```bash
    python -m src.main
    ```
    This uses the authoritative operations ledger and the same runtime ownership
    lock as the recurring worker. It fails closed when another worker owns the
    checkout. The retired checkpoint controller is not part of this command.
    For continuous operation, run the recurring worker rather than repeatedly launching the one-shot command:
    ```bash
    python -m src.run_worker
    ```
    The worker first runs durable connector intake for explicitly enabled Gmail, Google Drive, and Freshdesk sources, then runs the policy-gated local autonomous cycle, scheduled reports, provisional VAT/retention assessment, notification refresh, operations-ledger exports, and optional compatibility retries as independent audited stages. Connector intake records one timed workflow step per selected source; local autonomy records every executable action, including skipped, failed, and `not_run` downstream boundaries. From the Runs panel, a failed run can be retried only when its current recovery plan permits it: connector retries remain read-only and autonomous retries are restricted to the exact failed low-risk step. Each attempt is a new linked run, and approved export execution is never selected. `GET /api/workflows/{id}/recovery-plan` is read-only; authenticated `POST /api/workflows/{id}/retry` performs the governed retry. One failed connector, report, compliance, or notification stage does not suppress the remaining local bookkeeping stages. `worker_sync_source_connectors=false` disables connector intake; `worker_source_connectors` can restrict a cycle to named enabled sources. The retired checkpoint controller cannot be enabled; a stale `worker_run_legacy_workflow=true` setting fails startup with a migration message instead of creating a second processing path. Worker OAuth is non-interactive: prepare and validate Google token files during a supervised setup run, then leave `interactive_auth=false`. Google Photos whole-library background access is unavailable; use `python -m src.run_photos_picker_auth` once, then start and complete each user-owned receipt selection from Sources. The worker never creates, opens, polls, or cancels Picker sessions. `worker_interval_seconds` controls the interval; `worker_run_once=true` is useful when Windows Task Scheduler supplies the recurrence. A SQLite runtime lease prevents the worker, Task Scheduler, and `/api/autonomy/run` from overlapping the local autonomous cycle; a per-source-run lease prevents duplicate concurrent recovery attempts. Scheduled reports use a separate unique database slot, so overlapping worker launches cannot create duplicate report artifacts. Compliance assessments and notification events use source checksums/fingerprints so repeated cycles do not create noise. Expired autonomy leases recover after `fab_autonomy_lease_seconds`.

    The Python Docker image installs `config_template.ini` as its baseline `config.ini`, so the same reviewed fail-closed defaults apply even when no host configuration file is mounted. Compose environment variables continue to override runtime paths and secrets. Optional connectors remain disabled until explicitly configured, and the removed checkpoint workflow cannot be enabled. The web image uses a frozen, audited dependency lockfile and serves compressed responses above 1 KiB.

    On Windows Task Scheduler, set **Program/script** to the virtual environment's `python.exe`, **Add arguments** to `-m src.run_worker`, and **Start in** to the repository directory. Use either one long-running worker with restart-on-failure or `worker_run_once=true` with a recurring task, not both recurrence models at once.

6.  **Run a Local Recovery Rehearsal:**

    Do not restore while the standard runtime is active. On Windows, launch `Start-FAB-Maintenance.cmd`. The launcher stops the owned standard services when switching modes, starts API and dashboard on loopback without the worker, disables HAI execution, and locks every normal mutation. Use **Advanced recovery** to inspect a package before entering either exact confirmation phrase. Full ledger-and-source recovery is available only for a source-complete version 2 package. It creates a verified pre-restore package, refuses source-tree overwrite or checksum drift, and restores the prior live ledger if final validation fails.

    After the rehearsal or recovery:
    ```powershell
    .\Stop-FAB.cmd
    .\Start-FAB.cmd
    ```
    Confirm `/api/live` reports `maintenanceMode: false` and that the worker is present before resuming normal operation. Rehearse against an isolated copy first; do not use the live ledger for acceptance testing.

7.  **Run the Local Operations API (optional):**

    The local API exposes the authoritative SQLite operations ledger for dashboard, review, routing, and export execution tooling:
    ```bash
    python -m src.operations.local_api
    ```
    Open `http://127.0.0.1:5001/` for the local dashboard. The VAT & Compliance panel creates provisional current-quarter evidence, shows blocking/review findings, and tracks seven-year source-document retention. Configure `worker_assess_compliance`, `allowed_vat_rates`, `vat_rate_tolerance_percentage_points`, and `document_retention_years`; this never files a return or authorizes deletion. The Notification Center stores preference-controlled health alerts, compliance findings, and Wave invoice deadlines with read, acknowledge, and resolve actions. Set `worker_refresh_notifications=true`, `notification_minimum_severity`, and `invoice_due_soon_days` as needed; per-event preferences live in the ledger. External notification delivery remains disabled, so FAB never sends reminders or financial details from this stage. The Financial Reports panel and `/api/reports` expose provisional accrual/cash P&L, VAT, cash movement, and expense breakdowns from normalized records, with currency separation and completeness gates. The scheduled-report block shows the current deterministic slot, next due time, retry or review state, and checksum-verified artifacts. Configure `report_dir`, `report_schedule_frequency`, `report_schedule_timezone`, `report_schedule_period_mode`, and related `[operations]` settings; keep the report directory outside Git because artifacts contain financial data. `worker_generate_scheduled_reports=true` runs the due check during each worker cycle. Generation is local-only and never emails, files, or submits a report. The Export Attempts panel is the posting source of truth: it requires `APPROVE FAB EXPORT DRAFT`, uses an atomic execution claim and pre-execution backup, and requires `RECORD FAB EXPORT RESULT` for supervised outcomes. Verified Wave expense execution requires an explicit Business/Personal target and current account-ID mappings; its external Wave ID is displayed in the export ledger. The Wave Control Center can perform a read-only paginated mirror sync for customers, products/services, and invoices. Sync runs and downstream presence are stored locally so stale or removed Wave IDs are visible before later mutations use them. The autonomous cycle refreshes configured Wave mirrors only when due; use `wave_entity_sync_stale_hours`, `wave_entity_sync_retry_hours`, and `fab_autonomy_sync_wave_entities` to control that behavior. Missing configuration or unsupported actions become `attention_required`, while quota throttles become `deferred` until `nextRetryAt`. MijnGeldzaken execution writes a local checksum-bound artifact and remains `supervision_required` until the user-owned session result is recorded. `/api/health` reports stuck execution claims, failed or review-required report runs, due deferred exports, Wave mirror drift, compliance findings, and pending supervision. The React/Express operator dashboard on the URL printed by `Start-FAB.ps1` uses compact checksum-preserving queue reads, short mutation-invalidated request coalescing, and response compression for local clients; detailed evidence endpoints remain complete. Keep the host on 127.0.0.1 for local use. Public production hosting is targeted to Hetzner behind authenticated TLS ingress; configure the API token before any non-loopback exposure. The complete endpoint inventory is available from the dashboard and technical reference.
    The Sources panel and `/api/sources` list observed folder or connector sources with status, last scan time, counters, and source identifiers. `GET /api/sources/readiness` shows configured/enabled/mode status and `POST /api/sources/sync` runs the same durable source-only intake used by the worker. Provider revisions create review work instead of overwriting prior evidence. Secret-looking connector metadata and errors are redacted before persistence.

### 1.3. Running as a System Service (Linux example with systemd)

1.  **Create a systemd service file** (e.g., `/etc/systemd/system/bookkeeping.service`):
    ```ini
    [Unit]
    Description=Automated Bookkeeping Service
    After=network.target

    [Service]
    User=your_username
    WorkingDirectory=/path/to/your/019-FAB
    ExecStart=/path/to/your/019-FAB/.venv/bin/python -m src.run_worker
    Restart=on-failure
    EnvironmentFile=/etc/fab/fab.env

    [Install]
    WantedBy=multi-user.target
    ```
    *   Replace `your_username` with your actual username.
    *   Replace `/path/to/your/019-FAB` with the actual path to your project directory.
    *   Store required environment values in a root-owned `0600` environment file or a reviewed secret manager; never place credentials directly in the unit file.

2.  **Reload systemd, enable, and start the service:**
    ```bash
    sudo systemctl daemon-reload
    sudo systemctl enable bookkeeping.service
    sudo systemctl start bookkeeping.service
    ```

3.  **Check service status:**
    ```bash
    sudo systemctl status bookkeeping.service
    ```

## 2. Docker Deployment

The supported container deployment is the repository's three-service Compose stack: authenticated API, worker, and production dashboard. It uses named data/output volumes, mounts intake read-only, runs both images as non-root users, and publishes only to host loopback.

1. Generate separate long random values for `FAB_LOCAL_API_TOKEN` and `FAB_WEB_JWT_SECRET` in your secret manager or shell environment. Compose also applies the local API token to the server-only operations bridge so the Python services, dashboard, and bounded HAI integration share one authenticated local trust boundary without exposing the token to browser code. Do not put either secret in Compose, `.env`, logs, URLs, or Git.
2. If ports `5001` or `3000` are already occupied, set `FAB_API_HOST_PORT` and `FAB_WEB_HOST_PORT` to free loopback ports. Compose keeps `FAB_LOCAL_API_URL=http://api:5001` private for service traffic and derives `FAB_LOCAL_API_PUBLIC_URL` from `FAB_API_HOST_PORT` for operator links; do not replace the public value with Docker's internal hostname.
3. Build and start the stack:

```powershell
function New-FabSecret {
    $bytes = New-Object byte[] 48
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
    [Convert]::ToBase64String($bytes)
}
$env:FAB_LOCAL_API_TOKEN = New-FabSecret
$env:FAB_WEB_JWT_SECRET = New-FabSecret
$env:FAB_WEB_HOST_PORT = '3001' # optional
docker compose up -d --build api worker web
```

4. Open `http://127.0.0.1:<FAB_WEB_HOST_PORT>/admin/operations`. Check `docker compose ps` and `docker compose logs --tail 100 api worker web` if a service is unhealthy.
5. Stop FAB with `docker compose stop`. Named volumes are retained. Do not use `down --volumes` against financial data unless a separately verified recovery package exists.

For container recovery, stop the standard stack and start the maintenance override. The override omits the worker and keeps API/dashboard ports published only on host loopback:

```powershell
docker compose stop
docker compose -f docker-compose.yml -f docker-compose.maintenance.yml up -d --build api web
```

Perform only the reviewed local recovery action. Then stop the maintenance services and restart the standard stack:

```powershell
docker compose -f docker-compose.yml -f docker-compose.maintenance.yml stop
docker compose up -d api worker web
```

`docker-compose.maintenance.yml` is a recovery override, not a remotely exposed deployment mode. Do not place TLS ingress in front of it.

The web service's Docker gateway trust is intentionally narrow: it applies only in explicit local-operator mode, to private bridge gateway addresses ending in `.1`, and with a loopback hostname. Put an authenticated reverse proxy in front and disable local-operator mode for a remotely reachable deployment. In a managed remote deployment, set `FAB_LOCAL_API_PUBLIC_URL` to the clean external HTTPS origin that a browser can reach; credentials, paths, queries, fragments, and non-loopback HTTP values are rejected. Advanced operator links enter that origin through a 45-second one-time HMAC handoff. The remote dashboard request must already belong to an authenticated administrator; the long-lived API token remains server-only, and HTTPS makes the resulting Flask session cookie `Secure`.

## 3. Cloud Deployment Boundary

No unauthenticated Google Cloud Function deployment is supported. For cloud use, deploy the same Compose services or equivalent images behind private networking, TLS, an authenticated reverse proxy, a managed secret store, encrypted persistent volumes, monitored backups, and provider egress controls. A cloud deployment is not accepted until restore, provider sandbox, authorization-expiry, and attachment-readback tests pass in that exact environment.

Build a tracked-source-only Compose release from a clean committed checkout with `python package.py --target compose`. Verify the emitted `.zip.sha256` sidecar before transferring it. The archive manifest binds every included file to its size and SHA-256 checksum; it never includes local secrets or runtime financial data.


