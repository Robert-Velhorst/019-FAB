# FAB | Financial Automation Bookkeeper

FAB is a local-first bookkeeping operations platform. It collects financial documents, extracts and validates bookkeeping data, prepares reviewable records, and coordinates approved work with supported accounting services. Its local operations ledger links source evidence, decisions, provider operations, and reconciliation history.

    You and your team <-> FAB (local operations ledger and review) <-> accounting providers

FAB is designed to reduce repetitive bookkeeping without hiding uncertainty. Configured local intake, extraction, matching, reporting, and other low-risk work can run automatically. External financial changes remain capability- and approval-gated; a prepared action is not an executed action, and source archival requires positive provider readback.

## Status

This repository contains a runnable Windows-oriented application, Docker Compose deployment profiles, a React operator dashboard, a Python bookkeeping engine, automated tests, and a source-package builder. FAB is under active development; it is not a certified accounting product or a fully unattended replacement for professional oversight.

Check the repository's Actions page for the current main-branch CI status. A green run is evidence only for its commit and configured checks; it does not prove a user's installation, provider credentials, live bookkeeping, tax treatment, deployment, or recovery plan. See the production verification record and goal-completion matrix for detailed evidence and outstanding gates.

| Area | Current support | Boundary |
| --- | --- | --- |
| Documents | Local upload/folder intake, Gmail and Drive connectors, OCR, extraction, validation, duplicate/revision handling | Real provider use needs owner-authorized OAuth and configuration. |
| Review and ledger | Exception queue, corrections, category suggestions, audit history, local operational ledger | Uncertain or invalid evidence remains for review. Local records are not official filings. |
| Wave | Account/category discovery and mapping, supported draft/export flows, approval tracking, supervised receipt attachment work where required | Public API coverage is incomplete for some receipt operations; verify the configured executor and actual record/attachment readback. |
| Banking and reports | Bank-file import, reconciliation, local reports, provisional VAT/retention evidence, scheduled local work | Direct PSD2 feeds and tax-return submission are not implemented. |
| MijnGeldzaken | Checksum-bound artifacts for supervised handling | FAB does not store DigiD credentials or claim direct account mutation. |
| HAI | Bounded status, resource discovery, and governed local commands | HAI cannot approve provider exports, clear the emergency stop, restore backups, change access, or bypass review gates. |
| Recovery | Local ledger/evidence backups, integrity checks, restore rehearsal and maintenance mode | Off-host recovery, host encryption, and restore acceptance must be verified on the target host. |

Direct SVB submissions, official tax filing, complete multi-tenant permissions, and automatic bank feeds are not represented as implemented. Drive source archival is allowed only after the configured flow proves the exact downstream record and stored attachment; a visible attachment icon alone is not proof.

### Preview release

The first development preview, [`v0.1.0-preview.1`](https://github.com/Robert-Velhorst/019-FAB/releases/tag/v0.1.0-preview.1), provides checksum-verified Windows and Docker Compose source bundles. Each archive contains a `RELEASE-MANIFEST.json` with per-file hashes and has a matching `.sha256` sidecar. These are source packages, not signed installers or prebuilt container images. Verify the checksum before extracting and read the security policy before connecting live financial sources.

## Operator Quick Start (Windows 11)

Requirements: Git, Python 3.13 (the launcher may provision it using the Windows Python launcher or uv), Tesseract with Dutch and English language data, and Poppler for PDF OCR. The launcher installs dependencies in a project-local environment.

    git clone https://github.com/Robert-Velhorst/019-FAB.git
    Set-Location 019-FAB
    .\Start-FAB.cmd

Complete dashboard activation and configure only the sources/providers you intend to use. Provider credentials and OAuth consent are not included. Add files through a configured intake folder, an authorized source, or dashboard upload. Review exceptions and the evidence before approving downstream actions.

Stop only services started by this checkout with .\Stop-FAB.cmd. Use .\Start-FAB-Maintenance.cmd for maintenance and recovery; follow the operator runbook. Do not share a live ledger between checkouts or Windows sessions unless worker exclusion has been verified.

## Docker Compose

The standard Compose profile runs the Python API, recurring worker, and production dashboard. Set strong unique secrets outside source control:

    $env:FAB_LOCAL_API_TOKEN = "<long random token>"
    $env:FAB_WEB_JWT_SECRET = "<long random secret>"
    docker compose up --build

Published ports bind to loopback by default. State is in persistent Compose volumes and configured mounts. Remote deployment additionally needs reviewed TLS/reverse-proxy configuration, managed secrets, access controls, monitoring, backup/restore drills, and provider acceptance. See the VM deployment and deployment guidance. A successful Compose config check does not certify a host deployment.

## How the Workflow Works

    intake -> preserve and hash source -> OCR/extract -> validate -> categorize
    -> deduplicate/group -> review exceptions -> local ledger -> prepare draft
    -> explicit approval -> supported execution -> provider readback
    -> reconcile/report -> backup or verified archival

Preparation is not execution; approval is not completion until the result is verified. A timeout or missing provider response may be ambiguous. Check FAB's operation history and the provider before retrying. Never archive source evidence because an export was merely queued or a record appears to exist.

## Capabilities

- Intake: configured local folders, authenticated dashboard upload, Gmail scanner/source sync, Google Drive folders, read-only Freshdesk ticket attachments, and supervised Google Photos Picker imports. The useful scanner-mailbox behavior from Noodzakelijk-Online/025-Scan-to-folder-automation is integrated as guarded FAB intake; it does not grant live Gmail consent.
- Document processing: Tesseract OCR with Dutch/English data, optional Google Vision, Poppler PDF rendering, field extraction for vendor/date/amount/VAT/currency/references/line items, confidence/provenance, vendor templates, and duplicate/document-group checks.
- Categorization and review: deterministic/vendor rules, optional ML/hybrid categorization, explainable review-based rules, a review queue, and approval-gated routing. Confidence never overrides validation or approval.
- Wave: local configuration, identity/account/category discovery, category mapping, read-only mirrors for selected customer/product/invoice surfaces, supported approved operations, and supervised receipt upload/readback where API capability is insufficient.
- Banking/reporting: local CSV/JSON and supported bank-statement imports, matching/reconciliation, unmatched and missing-receipt review, provisional reports, scheduled generation, notifications, and provisional Dutch VAT/retention evidence.
- MijnGeldzaken: supervised checksum-bound CSV/JSON artifacts; the operator completes and records account-side actions.
- Recovery/audit: SQLite operations ledger, integrity checks, backup/restore flows, maintenance mode, source hashes, operation history, and sanitized support bundles.
- HAI: bounded API resources and governed local actions with server-attributed credential classes. HAI is not an unrestricted agent and does not get approval or recovery authority.

These functions support bookkeeping workflows; they do not submit official returns, provide legal/accounting sign-off, or replace professional review.

## Architecture

- src/: Python 3.13 services for document intake/processing, OCR, categorization, local API, SQLite ledger, reconciliation, provider adapters, worker, audit, backups, and recovery.
- web/client/: React operator dashboard.
- web/server/: Express/tRPC gateway and dashboard server. The browser does not receive the backend API token; the gateway validates and forwards bounded requests.
- web/shared/: shared types and provider definitions.
- config/: safe template; live settings belong in ignored local files or environment variables.
- deploy/, Dockerfiles, Compose files, and PowerShell launchers: local/VM runtime profiles.
- tests/: Python and web regression/safety suites.
- docs/: operator, developer, deployment, security, workflow, and acceptance references.

Operational state (database, source evidence, credentials, logs, backups) is local and excluded from Git/release packages. Never commit or publish financial documents, ledger databases, OAuth files, provider tokens, encryption keys, support bundles, or backups.

## Developer Setup and Checks

Prerequisites: Python 3.13; Node.js 22 and pnpm 11.20.0; Tesseract with eng and nld language data; Poppler for PDF OCR; Docker only for Compose work.

    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install --disable-pip-version-check --require-hashes -r requirements-test.lock
    pnpm.cmd --dir web install --frozen-lockfile
    Copy-Item config\config_template.ini config\config.ini

Set service secrets through environment variables or the dashboard's local encrypted setup; do not put them in tracked source. Use Start-FAB.cmd for the integrated Windows runtime. The local API module is python -m src.operations.local_api; do not start a second worker if the normal launcher already owns the runtime lease.

Run the relevant checks before submitting changes:

    python -m pytest -q -p no:cacheprovider -p no:stepwise
    pnpm.cmd --dir web check
    pnpm.cmd --dir web test
    pnpm.cmd --dir web build
    python scripts/run_stack_rehearsal.py

The stack rehearsal uses synthetic data and a temporary loopback ledger. It does not prove public TLS, live provider submissions, attachment archival, or production deployment. CI also checks dependencies, lockfiles, Linux/Windows backend behavior, and frontend build. Do not combine separate test runs into a made-up total; use dated verification notes.

## Configuration and Providers

Start from config/config_template.ini. It documents operations/worker scheduling, document processing, Gmail, Drive, Freshdesk, Photos Picker, Wave business/personal mappings, the supervised Wave receipt executor, and MijnGeldzaken artifact handling. Environment values can override local configuration. Keep credentials, OAuth consent artifacts, account IDs, and business identifiers private.

Google sources require owner-authorized OAuth and explicit folder configuration. Wave needs a valid user-owned token, the correct business, verified account/category mappings, and a supervised receipt executor for API gaps. MijnGeldzaken remains supervised. Read the Drive-to-Wave delivery contract and scanner migration notes before configuring automated source archival or scanner intake.

## Packaging

From a clean, committed checkout, build both source distributions:

    python package.py --target all

The tracked-source-only builder fails closed on dirty tracked files, forbidden runtime/secret paths, unsafe links, missing required files, or size limits. Each ZIP contains RELEASE-MANIFEST.json with per-file SHA-256 values and a .zip.sha256 archive checksum. The builder verifies each archive and member before success; compare the sidecar after downloading. These are source bundles, not signed installers or prebuilt images.

## Security and Privacy

FAB handles sensitive financial evidence. Intended defaults include loopback-only local services, authentication for non-loopback API use, server-only API credentials, bounded/sanitized diagnostics, audit events, idempotency, worker leases, an emergency stop, approval gates, and maintenance mode for restore operations. Windows OAuth tokens use current-user DPAPI where supported; filesystem, host, backup, and secret-management controls remain deployment responsibilities.

1. Keep config/config.ini, .encryption_key, credentials/, tokens/, data/, downloads/, logs/, output/, backups, and source documents out of Git and public support requests.
2. Use provider MFA and grant only necessary access.
3. Revoke/rotate credentials after suspected disclosure. Push protection does not remove a secret already exposed in Git history.
4. Verify backup integrity and rehearse restores on the target host. A configuration flag is not proof of encryption.
5. Have a qualified bookkeeper/accountant review accounting and tax treatment.

See SECURITY.md, docs/SECURITY.md, and docs/security_approach.md. FAB is not independently penetration-tested or certified as compliant with Dutch tax, privacy, or financial regulations.

## Documentation

- docs/user_guide.md and docs/OPERATOR_RUNBOOK.md: daily operation, setup, exception handling, stop/recovery.
- docs/GOAL_COMPLETION_MATRIX.md: implemented, partial, blocked, and intentionally absent scope.
- docs/CRITICAL_PATH.md and docs/ACCEPTANCE_TESTS.md: invariants and acceptance contract.
- docs/production-verification.md: dated checks and remaining acceptance gates.
- docs/technical_reference.md, docs/API_USAGE_AUDIT.md, docs/UI_ACTION_AUDIT.md: architecture and surface inventories.
- docs/windows-production.md, docs/vm-production.md, docs/deployment_guide.md, docs/ERROR_CATALOG.md: deployment and troubleshooting.
- docs/drive_wave_delivery.md and docs/scanner_mailbox_migration.md: external document flows.

## Contributing and License

Use a feature branch and pull request for code changes. Keep changes scoped, add regression tests, preserve fail-closed provider gates, and report local, CI, live-provider, and deployment verification separately. Avoid broad staging when runtime data or credentials exist in the checkout.

The web package declares MIT, but there is no repository-level LICENSE file. The overall repository license is therefore not stated; do not assume the web package declaration covers every file.

For diagnostics, python -m src.run_fab_doctor --support-bundle creates a sanitized archive. Review it before sharing; do not include private documents or credentials.
