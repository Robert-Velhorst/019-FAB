# FAB - Financial Automation Bookkeeper

FAB is a local-first bookkeeping automation system for collecting financial
documents, extracting bookkeeping facts, routing them through review and
approval, preparing downstream accounting actions, reconciling evidence, and
keeping an auditable local ledger.

The intended operating model is:

```text
End user / operator <-> FAB <-> supervised or API-backed bookkeeping providers
```

FAB is built to become the primary source of truth for the operator's financial
document workflow. It does not treat external systems as a place to blindly push
data. Every meaningful action is tracked locally first, and high-risk provider
changes remain gated by capability checks, explicit approval, idempotency,
readback evidence, and recoverable audit trails.

## Current Status

Latest full-suite snapshot (retained-bank identity stage, 2026-10-01): Python
passed **1,483 tests and 83 subtests**, with one Windows directory-symlink test
skipped for unavailable privilege. Dashboard verification passed **379 tests
across 32 files**, its type check and its production build/size budgets. Both
**59-check** rebuilt dashboard/backend rehearsals passed on Node 24/25 using
synthetic data. These results are not live-provider or deployment certification;
[verification notes](docs/production-verification.md) record the scope and gates.

This full-suite snapshot includes the direct-input guards, account/external-
reference isolation, prepared matching and retained-bank reference fixes.
Earlier per-phase results remain in the notes as historical evidence.

Subsequent request-boundary hardening passed 350 focused Python tests and both
61-check real-HTTP rehearsals on Node 24/25. The full-suite snapshot above predates
this change; it is not a fresh full-suite result for the request-boundary stage.
Reconciliation POST bodies are now capped at 5 MiB before JSON/form decoding,
after authentication, while retaining stricter configured limits. Excessively
nested JSON returns a client error rather than a server error. Valid 4 MiB bank
files retain their separate upload allowance. This application-level bound does
not prove limits on upstream server/proxy buffering or whole-process memory.

Document-selection hardening subsequently passed 381 focused Python
tests and both 65-check real-HTTP rehearsals. Explicit `documentIds: []` selects
no documents instead of silently searching the ledger. Selections are captured
before matching, accept at most 500 positive integer references, reject booleans
and fractions before database lookups, and process duplicate IDs only once.
Missing selected documents reject the entire run instead of producing misleading
missing-receipt results, including references beyond the candidate-count limit.
Omitted/null selection retains automatic candidate discovery. The existing
candidate-count limit still applies independently; no provider posting occurs.

Document eligibility is also enforced for explicit selections: imported,
review-blocked, failed, duplicate and unknown-status documents cannot bypass
their processing gates. Automatic discovery excludes linked duplicates before
the candidate limit, preserving valid candidates behind them. Completed/ignored
documents remain excluded. This stage passed 407 distinct focused tests and both
67-check real-HTTP rehearsals; these are synthetic local checks, not live
provider or deployment acceptance.

The dashboard gateway preserves structured backend rejection codes for
validation, authentication/permission, missing records, conflicts, oversized
uploads, unprocessable input, rate limiting and service unavailability instead
of reporting them all as internal failures. Messages remain sanitized and
bounded; the gateway does not retry mutations. This stage passed **390 web
tests across 32 files**, type checking, production build/size budgets and both
**68-check** rebuilt real-HTTP rehearsals on Node 24/25. The existing Python
full-suite snapshot remains historical; these checks are not deployment or
rendered-browser acceptance.

Failed batch reads no longer fan out into individual resource requests on
authentication, rate-limit, service or transport errors. Five synthetic failure
scenarios reduced requests per refresh from 30 to 3 (90% fewer requests for those
scenarios, not 90% less whole-app CPU/RAM). A real loopback HTTP 503 check also
observed only three requests. Structured 404/405 unsupported-batch responses
retain bounded legacy fallback; successful incompatible batch schemas retain
the existing fallback. This stage passed **399 web tests**, type checking,
production build/size budgets and both **68-check** rebuilt HTTP rehearsals.

Overlapping refreshes now reject superseded dashboard reads before they can
overwrite newer cached resource history. Mutations invalidate snapshots both
before submission and when the request finishes, including unreadable/uncertain
responses, so a snapshot loaded during a pending change is not reused afterward.
Normal cached polling and visibly stale fallback data remain supported. This
stage passed **403 web tests**, type checking, production build/size budgets and
both **68-check** rebuilt HTTP rehearsals; no whole-app performance or deployment
claim follows from these checks.

HAI command inputs are validated before executor calls, leases or command audit
writes. Limits/document IDs require integer values without coercing booleans,
floats (including whole-valued floats) or numeric strings. Local document IDs
are bounded to 1 through 2^63-1, and replay keys must be strings matching the
existing request-ID pattern. Attachment attestation requires both `documentId`
and `evidence`; accepting that envelope does not establish attachment integrity
or authorize archival. That stage passed **193 focused Python tests** and both
**70-check** local HTTP rehearsals.

HAI now captures an independent JSON snapshot of command metadata and gives
executors a separate copy. Changes to caller/executor dictionaries cannot alter
the recorded request or its replay identity. Metadata must be finite JSON,
at most 1 MiB in canonical UTF-8, with depth at most 32 and 10,000 values.
Authenticated plan/execute HTTP envelopes are capped at 2 MiB before decoding;
stricter configured limits still apply, and receipt/bank upload limits are
unchanged. The latest stage passed **209 focused Python tests** and both
**71-check** local HTTP rehearsals on Node 24/25. Earlier full-suite results
remain historical; these checks do not establish live-provider, deployment,
rendered-browser or whole-process resource acceptance.

HTTP HAI command audit attribution now comes from the server-verified credential
class, not a caller-supplied actor label. This stage passed **220 focused Python
tests** and both **73-check** local HTTP rehearsals. These results supersede the
previous focused snapshot, not the historical full-suite/deployment evidence.

Wave evidence validation and work-order gating were subsequently tightened to
reject equally invalid fields and require VAT readback from normalized records
as well as source documents. This separate scope passed **240 Python tests and
14 subtests across eight modules**, plus both existing **73-check** local HTTP
rehearsals. Neither result is a fresh full-suite or live-provider certification.

Subsequent byte-size hardening passed **298 Python tests and 20 subtests across
nine modules**, plus both existing **73-check** local HTTP rehearsals. Invalid
or conflicting source sizes now block receipt work orders/readback, and current
Drive size must match before and after an archive move. These are isolated
checks, not evidence of live provider recovery or whole-app resource savings.

Delivery JSON and line-item hardening subsequently passed **356 Python tests
and 32 subtests across ten modules**, plus both existing **73-check** Node 24/25
HTTP rehearsals. Malformed expected values and line-item numbers no longer leak
non-finite numbers through the delivery handoff; explicit field diagnostics hold
affected records for processing and block readback approval/archival, even after
earlier verification. Original ledger values, source files and prior audits are
retained. These focused checks do not establish tax arithmetic correctness,
app-wide JSON safety, live-provider acceptance or production deployment.

Direct reconciliation keeps identical bank references from different accounts
separate, rejects duplicate references within one account's batch before saving,
and refuses conflicting account aliases. Account identifiers remain exact: FAB
does not guess which account an unscoped legacy record belongs to.
Explicit `id`/`transaction_id` references must be non-empty strings or integers,
and both aliases must identify the same reference. Invalid explicit references
are rejected rather than silently replaced with an inferred reference.
Stored bank-reference aliases that are null, malformed, conflicting, or disagree
with the selected bank record stop reconciliation without overwriting history.
These guards do not automatically repair ambiguous legacy ownership.

Matching prepares amounts, dates and vendor text once per run and avoids fuzzy
comparison for identical normalized vendors. Two synthetic 500-bank/100-document
workloads measured approximately 2.6-8x faster matching with identical results.
The prepared features add about 28-29 KB to the measured Python allocation peak;
this is not a whole-app speed or RAM claim. Reproduce with
`python scripts/benchmark_reconciliation.py`; detailed measurements are in
[the benchmark record](.ecc/benchmarks/reconciliation-2026-10-01.json).

The repository contains a working local application, not only a prototype:

- Python 3.13 bookkeeping engine, local operations API, recurring worker, OCR
  pipeline, connector intake, SQLite operations ledger, backup and recovery
  services.
- React/Node operator dashboard for health, activation, intake, review,
  automation, reconciliation, reporting, recovery, exports, Wave setup, Google
  setup, and HAI control.
- Windows 11 launcher scripts, Docker Compose runtime, managed ngrok helpers,
  packaging, GitHub Actions CI, and broad Python/web regression tests.
- Guarded HAI connector surfaces for bounded control and status, without
  granting HAI authority to approve exports, clear emergency stops, restore
  backups, change permissions, or submit downstream bookkeeping changes.

Important live-provider limits are intentional and must not be hidden:

- Google Gmail and Drive require owner OAuth consent before FAB can read real
  mailbox/folder sources.
- Wave requires a valid user-owned Wave token, business ID, verified account
  mappings, and in some cases a supervised receipt executor because Wave's
  public API does not cover every receipt attachment/readback action FAB needs.
- MijnGeldzaken is handled as supervised export artifacts. FAB does not store
  DigiD credentials and does not claim direct MijnGeldzaken account mutation.
- Direct PSD2 bank feeds, SVB submissions, tax filings, and legal/accountant
  sign-off are not implemented. Bank statement import and provisional VAT
  evidence are local bookkeeping aids, not official filings.
- Google Drive source archival is disabled until FAB verifies the exact
  downstream Wave record and the actual stored attachment through binary
  readback. A matching record or visible icon is not enough.

See [docs/GOAL_COMPLETION_MATRIX.md](docs/GOAL_COMPLETION_MATRIX.md) for the
full implemented/partial/blocked matrix.

Reconciliation lookup indexes reduce database work for missing-receipt review
links and imported bank-row history. A synthetic 2,001-review lookup used 53
SQLite instructions instead of 28,054, returning identical results. This is not
a whole-application speed or memory guarantee. The indexes are rebuildable,
preserve ledger rows, and require additional storage and initial creation work.
See [production verification](docs/production-verification.md) for test scope
and remaining acceptance gates.

Completed imported-bank confirmations can be retried with the same status when
their original approval hashes, current financial facts and final owner links
still agree. The original decision is preserved. Existing dashboard review
actions can also repair exactly linked orphaned review tasks; ambiguous links,
changed facts and corrections to already-confirmed evidence remain blocked.
This does not authorize external posting or source archival.

Bank evidence checks compare both amounts as decimals so ordinary cent values
such as EUR 4.28 are not falsely rejected as changed. Bank import, receipt
validation and ledger numeric writes reject non-finite, boolean and overflowing
amounts. Present invalid VAT and invalid confidence scores block validation;
an invalid primary amount is not replaced silently by an alternate field.
These controls preserve historical rows and do not claim a complete currency,
exchange-rate or fixed-point ledger migration.

Direct reconciliation accepts at most 500 rows per batch and bounds its captured
bank JSON to 4 MiB, 32 nesting levels and 50,000 values. Invalid requests are
rejected as a whole, without truncating transactions or creating partial review
records. Larger workloads should use bank import and bounded processing batches.

### Production Deployment Profiles

The explicit `windows` and `vm` profiles add startup checks for strong credentials,
storage, backups, and network settings. The default `local` profile preserves the
existing desktop workflow; it is not a production certification.

- [Windows setup](docs/windows-production.md): single-account launcher, external
  ledger/backup paths, shared service credentials, and safe start/stop requirements.
- [Docker/VM setup](docs/vm-production.md): single-business stack, persistent
  storage, mounted secrets, HTTPS proxy, and a separate administrator login.
- [Error catalogue](docs/ERROR_CATALOG.md): blocked configuration and recovery steps.
- [Verification and acceptance](docs/production-verification.md): checks actually
  performed and remaining deployment, provider, and security gates.

Run `python -m src.run_deployment_preflight` to check effective configuration
without opening a ledger. Run `python -m src.run_recovery_rehearsal` to exercise
backup and full restore with disposable synthetic evidence only. Neither command
authorizes changing real financial records. Actual host encryption, off-host
recovery, provider consent/readback, and operator acceptance remain separate.

## Who This Is For

For non-technical operators, FAB is the control room for a bookkeeping process:

- Put receipts, invoices, scans, bank statements, and supporting files into the
  configured sources.
- Let FAB collect, OCR, classify, validate, group, deduplicate, and prepare the
  bookkeeping work.
- Review only the exceptions, uncertain fields, duplicate candidates, and
  high-risk provider actions.
- Approve or reject drafts with visible evidence.
- Keep source files, local records, external operations, Wave readback evidence,
  reports, and recovery packages traceable.

For developers, FAB is a Python + SQLite + Flask/Waitress backend, a React +
Express/tRPC frontend, and a set of local automation services organized around
an operations ledger. The codebase is intentionally conservative about external
automation: local computations can run autonomously, while provider mutations
must pass explicit safety gates.

## Core Workflow

FAB's protected path is:

```text
source intake
-> immutable evidence
-> OCR and field extraction
-> validation
-> categorization
-> duplicate and document-group handling
-> manual review where needed
-> local bookkeeping record
-> routing draft
-> explicit approval
-> supported provider execution
-> provider readback and attachment verification
-> reconciliation
-> reports
-> verified backup or export
```

The key invariants are documented in
[docs/CRITICAL_PATH.md](docs/CRITICAL_PATH.md). In short: source bytes are
hashed before processing, uncertain data pauses for review, duplicates cannot
post twice, preparing an external operation is not the same as executing it,
and archival requires proof that the external attachment and bookkeeping fields
match the retained source evidence.

## Main Capabilities

### Intake and Source Evidence

- Local folder intake from configured folders such as `downloads/sort-out`.
- Authenticated browser uploads through the operator dashboard.
- Gmail connector with an optional strict scanner-mailbox profile. This ports
  the useful behavior from `Noodzakelijk-Online/025-Scan-to-folder-automation`
  into FAB directly: trusted sender, PDF filename/MIME/signature validation,
  content-addressed local evidence, provider checkpointing, and no source email
  mutation.
- Google Drive connector for configured folders, including source provenance,
  duplicate/revision evidence, and optional move-only archival after Wave
  verification.
- Freshdesk financial-ticket intake profile for read-only ticket and PDF
  attachment evidence. FAB never closes tickets or copies evidence to Drive as
  a side effect.
- Supervised Google Photos Picker intake. The worker does not scan whole Google
  Photos libraries.

### Document Understanding

- Tesseract OCR with Dutch and English language support.
- Optional Google Cloud Vision OCR provider when configured.
- Image preprocessing through private temporary copies: grayscale, denoising,
  deskew, and binarization.
- PDF-to-image conversion through Poppler for OCR.
- Dutch/English language-aware processing.
- Financial field extraction for vendor, date, amount, VAT/BTW, currency,
  references, line items, category evidence, and confidence/provenance.
- Vendor templates and deterministic extraction rules.
- Duplicate detection using content, provider identity, validated references,
  and document grouping.

### Categorization and Learning

- Fixed conservative vendor taxonomy for trusted exact-vendor suggestions.
- Rule-based, ML, fallback, and hybrid categorizer modules.
- Review-based learning: explicit approved corrections can create explainable
  vendor/category rules.
- FAB does not fabricate training data and does not treat model confidence as
  permission to bypass validation, duplicate review, external approval, or
  archival gates.

### Review, Routing, and Export Control

- Review queue with source-backed correction handling.
- Draft routing into target systems such as Wave Business, Wave Personal, or
  MijnGeldzaken.
- Approval-gated export attempts with operation IDs, idempotency keys, approval
  status, execution state, redacted results, and audit history.
- Pre-execution backups for approved batches.
- Quota/throttle deferral instead of silent failure.
- Supervised completion tracking for artifact-based flows.

### Wave Support

FAB models Wave as a downstream bookkeeping surface, with local FAB records as
the decision source:

- Store Wave business/account setup locally through encrypted settings or
  environment variables.
- Validate Wave identity and read account/category data.
- Map FAB category intents to Wave chart-of-account IDs.
- Mirror customers, products/services, and invoices read-only for routing and
  drift detection.
- Prepare Wave operations only when required fields and account mappings are
  present.
- Execute supported Wave money-transaction actions only after approval and
  capability checks.
- Coordinate a supervised receipt executor for attachment upload/readback when
  the public Wave API cannot provide the required receipt workflow.
- Require binary readback evidence before Drive archival.

Wave field comparison requires valid values on both sides. Invalid dates,
non-finite/unparseable amounts, blank normalized text and non-string text values
cannot match merely because both sides normalize identically. Otherwise-ready
work orders with missing or invalid required fields stay at `needs_processing`.
VAT from either
the source document or normalized bookkeeping record requires matching tax
readback. Valid cent precision, supported date formats and text case/spacing
normalization remain unchanged; exact attachment bytes and all other archive
gates still apply independently.

Source receipt sizes must be positive whole-byte counts: native integers or
trimmed strings of 1-19 ASCII digits, bounded to 2^63-1. If both intake and
provider sizes are recorded, they must agree; an invalid declared size cannot
be hidden by a fallback. Existing Wave file-size limits still apply. Missing or
invalid sizes block upload work orders and full readback verification. Drive
archival requires a valid, matching current provider size before and after the
move; a failed post-move check follows the existing rollback path. Earlier audit
records and source bytes are preserved, but old verification cannot override
freshly invalid source metadata.

The Drive-to-Wave contract is documented in
[docs/drive_wave_delivery.md](docs/drive_wave_delivery.md).

### MijnGeldzaken Support

FAB prepares checksum-bound CSV/JSON artifacts for supervised MijnGeldzaken
handling. The operator completes the account-side action in a user-owned
session and records the result back in FAB.

FAB intentionally does not store MijnGeldzaken passwords, DigiD details, or
unattended browser credentials.

### Banking, Reconciliation, Reports, and Compliance

- Local bank statement import for supported CSV/JSON/CAMT/MT940-like data.
- Reconciliation between imported bank rows and bookkeeping documents.
- Missing receipt and unmatched transaction review handling.
- Provisional financial reports with checksum-bound JSON/CSV artifacts.
- Scheduled local report generation.
- Provisional Dutch VAT and seven-year source-retention evidence.
- Notification center for health, due work, compliance findings, and Wave
  invoice deadlines.

These features support bookkeeping control and review. They do not file tax
returns, submit to authorities, or replace professional advice.

### Backup, Recovery, and Auditability

- SQLite operations ledger with WAL, migrations, integrity checks, and
  migration snapshots.
- Manual review decisions (including exact-vendor propagation) and normalized
  record creation/resolution each commit their local changes as one transaction.
  Failures roll back the financial changes; the API can still record a separate
  redacted error event. A timed-out client request is not proof of rollback.
- Reconciliation result persistence and match resolution also use local
  transactions. Document-linked reviews close only for their recorded match;
  `needs_review` preserves or reopens the document review gate. Normalized records refresh after
  the review decision, and older reviews are checked through bounded pages.
- Missing-receipt-only decisions close or reopen their own review gate, but
  cannot confirm a bank match without a linked document. Matching rechecks
  selected document and stored-bank facts before persistence and rejects stale
  or conflicting references. Completed documents stay out of later automatic
  candidate pools; the filter runs before the batch limit.
- Final reconciliation approval rechecks source/financial document hashes and
  stored bank facts while holding the same local transaction as the decision.
  Changed or incomplete evidence returns a conflict; rejected nested decisions
  roll back review edits and learning. A document cannot confirm a second bank
  candidate once reconciled. Older candidates without approval hashes must be
  refreshed through matching, not silently approved or deleted. Financial
  corrections must be saved separately and rematched before confirmation.
- Ignoring/rejecting/resolving a stored-bank exception also rechecks its bank
  facts and decision ownership. An older match cannot downgrade another completed
  document match or take over a newer bank candidate. Missing-receipt refreshes
  preserve review IDs and show the refreshed bank facts. Matching lookups separate
  imported bank-row identities across accounts; open bank rows are filtered in
  SQL before the batch limit, so completed rows cannot hide older open work.
- Confirming a receipt against its imported bank row resolves that row's open
  missing-receipt exceptions and linked reviews in the same transaction. Original
  snapshots remain available with a link to the confirming local match; closed
  human decisions and other accounts are preserved. Unconfirmed candidates do
  not close exceptions. This local update is not provider attachment verification
  and never grants permission to archive source files.
- Source-complete recovery packages with manifest-bound SHA-256 checksums.
- Lightweight dashboard checks read archive metadata only. Deep inspection and
  restore still verify receipt/ledger bytes and database integrity; a
  `manifest_valid` result is not proof that the saved financial bytes are intact.
- Local maintenance mode for restore operations. The worker, normal mutations,
  ngrok, and HAI command execution are locked during maintenance.
- Pre-restore package creation, source-byte verification, immutable source
  recovery tree, ledger path rewriting, and rollback after failed final checks.
- Sanitized support bundles that exclude credentials, raw documents, OCR text,
  filenames, local paths, and amounts.
- Audit events with sensitive fields redacted before persistence.

Recovery inspection rejects manifests above 8 MiB, JSON nesting beyond 32
container levels, and non-finite numbers. Existing limits remain 10,000 archive
members, 250 MiB per evidence file and 20 GiB total declared uncompressed bytes;
size checks run before member decompression. Existing rejected packages are not
deleted. Preserve them and investigate rather than bypassing validation.

### HAI Connector

The HAI connector exposes bounded discovery, status, resources, and governed
commands under `/api/hai/*`.

For HTTP command execution, FAB ignores the caller's `actor` label and records
the server-verified credential class as `fab_hai_api:hai` or
`fab_hai_api:operator`. Token-free loopback mode records `fab_hai_api:loopback`;
that label is not an authenticated personal identity. These labels identify
credential classes, not individual humans. Trusted in-process calls retain
their existing actor attribution. Other API routes are not changed by this
command-specific policy.

HAI can help inspect status and trigger low-risk local work such as intake,
processing, reconciliation, due reports, compliance assessment, notification
refresh, and emergency stop. It cannot:

- approve export drafts;
- execute provider submissions by itself;
- clear emergency stop;
- restore backups;
- change access controls or secrets;
- bypass review, duplicate, attachment, or archive gates.

## Architecture

### Backend

The backend is Python 3.13. Major areas:

```text
src/operations/          Local API, ledger, readiness, autonomy, exports,
                         review, recovery, HAI, Wave/Drive delivery
src/worker/              Recurring authoritative worker
src/document_fetchers/   Gmail, Drive, Freshdesk, Photos Picker, local folder
src/document_processors/ OCR, preprocessing, extraction, templates, line items
src/categorizers/        Rule, ML, hybrid, fallback categorization
src/data_entry/          Wave, MijnGeldzaken, safe posting, provider surfaces
src/reconciliation/      Transaction/document matching
src/backup/              Backup and restore support
src/security/            Encryption, OAuth token storage, local secret store
src/workflow/            State, safety, logging, autonomous playbook
tests/                   Python regression and safety suite
```

The authoritative local API is `python -m src.operations.local_api`. It serves
authenticated JSON endpoints under `/api/*`, uses Waitress in supported local
runtime, and stores operational truth in `data/fab_operations.sqlite3` by
default.

The recurring worker is `python -m src.run_worker`. It runs connector intake,
local autonomy, scheduled backups, reports, compliance checks, notifications,
export execution gates, Drive archival checks, and recovery handling as
isolated audited stages.

### Frontend and Gateway

The web app lives in `web/`:

```text
web/client/              React operator dashboard
web/server/              Express/tRPC gateway and standalone server
web/shared/              Shared types and provider surface definitions
web/drizzle/             Web database schema/migrations
web/server/lib/          Logging, loopback checks, rate limiting, sanitization
```

The browser never receives the hidden local API token. The Express gateway
adds it server-side, calls fixed local endpoints, validates origins, applies
timeouts and bounded projections, compresses large responses, and keeps
operator links protected through short-lived one-time handoff tickets.

The main operator page is:

```text
http://127.0.0.1:<dashboard-port>/admin/operations
```

The launcher records the actual ports in `data/fab-runtime.json` after proving
that the API, worker, and dashboard belong to this checkout.

### Storage

Runtime data is intentionally local and ignored by Git:

```text
config/config.ini
credentials/
tokens/
data/
downloads/
logs/
output/
web/node_modules/
.venv/
```

Do not commit real financial files, ledgers, tokens, support bundles, or
provider credentials.

## Quick Start for Operators on Windows 11

1. Install Git. Install Python 3.13 if the launcher cannot provision it through
   the Windows Python launcher or `uv`.
2. Clone the repository:

   ```powershell
   git clone https://github.com/Robert-Velhorst/019-FAB.git
   cd 019-FAB
   ```

3. Start FAB:

   ```powershell
   .\Start-FAB.cmd
   ```

   The launcher creates the project-local `.venv`, installs missing Python and
   dashboard dependencies, builds or starts the dashboard, checks Tesseract and
   Poppler, starts the local API and worker, selects safe loopback ports, and
   opens the dashboard.

4. In the dashboard, use **Finish activation** and **Connections** to configure
   Gmail, Google Drive, Wave, OCR, intake folders, and review settings.

5. Add files through:

   - the configured local intake folder;
   - Google Drive folder sync;
   - Gmail scanner/source sync;
   - Freshdesk source sync;
   - dashboard **Add receipts** upload.

6. Resolve review items and only approve external drafts after checking the
   evidence shown by FAB.

To stop only this checkout's FAB services:

```powershell
.\Stop-FAB.cmd
```

For maintenance/recovery:

```powershell
.\Start-FAB-Maintenance.cmd
```

See [docs/OPERATOR_RUNBOOK.md](docs/OPERATOR_RUNBOOK.md) and
[docs/user_guide.md](docs/user_guide.md).

## Developer Setup

### Prerequisites

- Python 3.13.
- Node.js 22.
- pnpm 11.20.0.
- Tesseract OCR with `eng` and `nld` language data.
- Poppler PDF tools for PDF OCR.
- Docker Desktop or Docker Engine if using Compose.

### Python backend

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --disable-pip-version-check -r requirements.txt pytest
python -m src.operations.local_api
```

The local API defaults to:

```text
http://127.0.0.1:5001
```

Set a strong `operations.api_token` in `config/config.ini` or set
`FAB_LOCAL_API_TOKEN` before exposing anything beyond loopback.

### Web dashboard

```powershell
Copy-Item web\.env.example web\.env
pnpm.cmd --dir web install --frozen-lockfile
pnpm.cmd --dir web dev
```

For manual development, set these in `web/.env`:

```text
FAB_LOCAL_API_URL=http://127.0.0.1:5001
FAB_LOCAL_API_PUBLIC_URL=http://127.0.0.1:5001
FAB_LOCAL_API_TOKEN=<same long token as the Python API>
FAB_OPERATIONS_SERVICE_TOKEN=<same long token as the Python API>
JWT_SECRET=<long random secret>
FAB_OPERATOR_LOCAL_MODE=true
```

Use `pnpm.cmd` on Windows when PowerShell cannot resolve the `pnpm` shim.

### One-shot workflow cycle

Run exactly one governed local cycle:

```powershell
python -m src.main
```

If the recurring worker already owns the runtime lease, the command exits
without starting a duplicate cycle.

## Configuration

Start from:

```powershell
Copy-Item config\config_template.ini config\config.ini
```

The template documents all major sections:

- `[operations]`: local ledger, API host/port/token, HAI allowlist, intake
  paths, backup/report directories, autonomy, worker schedule, health limits,
  reports, notifications, VAT/retention settings.
- `[document_processing]`: OCR method, Tesseract, Poppler, preprocessing,
  template matching, line items, VAT extraction safety.
- `[gmail]`: Gmail source, scanner mode, trusted senders, limits, OAuth paths.
- `[google_drive]`: Drive intake folder, archive folder, relay size, OAuth
  paths, archival gates.
- `[freshdesk]`: read-only financial-ticket profile and attachment policy.
- `[google_photos]`: supervised Picker settings.
- `[waveapps]`, `[waveapps_business]`, `[waveapps_personal]`: Wave GraphQL URL,
  business IDs, category mappings, account IDs, and token settings.
- `[wave_receipt_executor]`: supervised receipt upload/readback coordination.
- `[mijngeldzaken]`: supervised export artifact settings.

Environment variables override config values. Keep credentials out of Git and
prefer the dashboard's encrypted local setup or environment variables for
secrets.

## Testing and Verification

Run the backend suite:

```powershell
python -m pytest -q -p no:cacheprovider -p no:stepwise
```

Run the web checks:

```powershell
pnpm.cmd --dir web check
pnpm.cmd --dir web test
pnpm.cmd --dir web build
```

Run dependency checks used by CI:

```powershell
pnpm.cmd --dir web audit --audit-level=high
pnpm.cmd --dir web peers check
```

After building the web bundles, exercise the dashboard, API and SQLite together:

```powershell
python scripts/run_stack_rehearsal.py
```

This rehearsal creates a fresh temporary ledger and synthetic receipt, launches
the production dashboard and real API only on loopback, checks authentication,
review correction, bank-statement import, reconciliation approval, source
checksums, missing-receipt disposition/reopening, later-run preservation,
signed session handoff, logout revocation
and HAI route permissions, then stops its child processes and removes its data.
It does not use your configured accounts or ledger. HTTPS proxy headers are
simulated; passing this check does not prove real TLS, ngrok, provider posting,
Drive archival or rendered-browser acceptance. `--node` can select an explicit
Node.js executable. Missing builds or failed checks produce a nonzero exit.

The GitHub Actions workflow is configured for:

- backend on Linux;
- backend on Windows across four shards;
- web frozen install, high-severity audit, peer check, TypeScript check, Vitest,
  and production build;
- synthetic dashboard/API/ledger rehearsal on Linux and one Windows shard.

Current hardening evidence and unresolved acceptance gates are kept in
[docs/production-verification.md](docs/production-verification.md). The earlier
[final verification report](docs/FINAL_VERIFICATION_REPORT.md) remains available
as a historical snapshot. Local workflow edits do not establish a hosted CI pass.

## Docker Compose

Set required secrets and start the three-service stack:

```powershell
$env:FAB_LOCAL_API_TOKEN = "<long random token>"
$env:FAB_WEB_JWT_SECRET = "<long random secret>"
docker compose up --build
```

Compose runs:

- `api`: Python local operations API;
- `worker`: Python recurring worker;
- `web`: production React/Express dashboard.

The stack binds published ports to loopback by default and stores persistent
state in Compose volumes plus the mounted local intake folder. Remote or cloud
deployment must add TLS, an authenticated reverse proxy, managed secrets, and
provider acceptance checks. Unauthenticated Cloud Function deployment is not
supported for financial data.

## Packaging

Create clean, checksum-bound release archives from a committed checkout:

```powershell
python package.py --target windows
python package.py --target compose
```

Packaging refuses dirty tracked files, tests, runtime data, credential-like
paths, unsupported old entrypoints, and oversized/unsafe archive contents. Each
ZIP includes a `RELEASE-MANIFEST.json` and a `.zip.sha256` sidecar.

## Local Cloud Access with ngrok

FAB can expose only the authenticated API/HAI surface through managed ngrok.
The operator dashboard stays local.

```powershell
.\Start-FAB-Ngrok.cmd
```

If another ngrok endpoint is already online, FAB refuses to pool, stop, or
reuse it. Reserve a dedicated FAB endpoint and pass it explicitly:

```powershell
.\Start-FAB-Ngrok.cmd -Url https://your-reserved-endpoint.example
```

See [docs/local_windows_ngrok_setup.md](docs/local_windows_ngrok_setup.md).

## Security and Privacy Model

FAB processes high-risk financial evidence. Its defaults are designed to fail
closed:

- API and dashboard bind to loopback by default.
- Non-loopback API access requires a strong bearer token.
- The browser never receives the hidden API token.
- Provider credentials are stored in ignored local files, encrypted local
  settings, or environment variables.
- Readiness, health, errors, logs, support bundles, and audit records redact
  secrets and bound provider diagnostics.
- Runtime leases prevent overlapping autonomous cycles and duplicate external
  actions.
- Export execution is separate from draft preparation and approval.
- Emergency stop blocks new autonomous work until an operator clears it with
  the exact confirmation flow.
- Drive archival requires Wave transaction, field, and attachment evidence.
- Maintenance mode disables normal mutations and HAI execution before restore.

See [docs/SECURITY.md](docs/SECURITY.md) and
[docs/security_approach.md](docs/security_approach.md).

## Documentation Map

- [docs/user_guide.md](docs/user_guide.md): operator guide.
- [docs/OPERATOR_RUNBOOK.md](docs/OPERATOR_RUNBOOK.md): daily operation,
  emergency stop, provider activation, diagnostics, recovery.
- [docs/technical_reference.md](docs/technical_reference.md): module and data
  flow reference.
- [docs/API_USAGE_AUDIT.md](docs/API_USAGE_AUDIT.md): local API, web gateway,
  provider API, and error-contract audit.
- [docs/UI_ACTION_AUDIT.md](docs/UI_ACTION_AUDIT.md): dashboard action
  inventory.
- [docs/ACCEPTANCE_TESTS.md](docs/ACCEPTANCE_TESTS.md): release acceptance
  contract.
- [docs/GOAL_COMPLETION_MATRIX.md](docs/GOAL_COMPLETION_MATRIX.md): implemented,
  partial, blocked, and intentionally absent capabilities.
- [docs/FINAL_VERIFICATION_REPORT.md](docs/FINAL_VERIFICATION_REPORT.md):
  current verification evidence and provider-live blockers.
- [docs/scanner_mailbox_migration.md](docs/scanner_mailbox_migration.md):
  consolidation of repository 025 scan-to-folder behavior.
- [docs/drive_wave_delivery.md](docs/drive_wave_delivery.md): high-assurance
  Drive/Gmail source to Wave attachment delivery.
- [docs/local_windows_ngrok_setup.md](docs/local_windows_ngrok_setup.md):
  Windows and managed ngrok setup.
- [docs/deployment_guide.md](docs/deployment_guide.md): deployment and
  operations notes.
- [docs/TECHNICAL_AUDIT.md](docs/TECHNICAL_AUDIT.md): technical audit and debt
  record.

## Repository Hygiene

Before publishing changes:

```powershell
git status --short
git diff --check
python -m pytest -q -p no:cacheprovider -p no:stepwise
pnpm.cmd --dir web check
pnpm.cmd --dir web test
pnpm.cmd --dir web build
```

Stage only intended files. Do not use broad staging commands when runtime data
or credentials may exist locally.

## Contributing

1. Create a feature branch.
2. Keep changes scoped to the affected runtime, module, or documentation area.
3. Add or update tests for behavior changes.
4. Preserve fail-closed provider behavior and truthful capability states.
5. Run the verification commands above.
6. Open a pull request with clear local, CI, browser, packaging, and provider
   acceptance evidence where relevant.

## License

`web/package.json` declares the web package as MIT, but this repository does
not currently contain a repository-level `LICENSE` file. Treat the repository
license as unset until a top-level license file is added.

## Support

For operational support, generate a sanitized support bundle from the FAB
dashboard or with:

```powershell
python -m src.run_fab_doctor --support-bundle
```

Review the bundle before sharing. It is designed to exclude source documents,
OCR text, financial identifiers, local paths, credential values, and tokens.
