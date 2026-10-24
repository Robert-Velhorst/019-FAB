# Technical Audit

Audit date: 2026-08-13

## Baseline

- Branch and default branch: `main`.
- Starting commit: `8a2b43d`.
- Runtime: Python local API/worker plus a React/Express operator dashboard.
- Persistence: SQLite operational ledger with ordered checksum-bound schema history and verified pre-upgrade snapshots; optional MySQL/Drizzle storage for the web application.
- Delivery: Windows launch scripts, CI on Linux and Windows, and explicit API/worker/dashboard container services.
- Local financial data, credentials, tokens, logs, uploads, databases, and generated support bundles are ignored by Git.

## Confirmed strengths

- The critical path is implemented across intake, OCR/extraction, validation, categorization, duplicate handling, manual review, routing, approval-gated exports, reconciliation, reporting, backup, retention, and audit history.
- Google and Wave readiness is capability-based. OAuth `invalid_grant` is an authorization blocker, not a retry loop.
- Drive source archival is gated on Wave record and attachment readback evidence.
- External submissions are represented separately from local preparation and require supported capabilities and approvals.
- Worker leases, idempotency keys, rate limits, recovery candidates, and dead-letter-style export deferral are present.

## 2026-10-17 Follow-up

- Drive archival compensation now revalidates the active lease before moving a
  source back. If ownership has transferred or cannot be confirmed, FAB avoids
  a competing provider mutation, marks the result `unknown`, and keeps the
  document in review. A failed local audit commit also reports an unknown
  external state when source restoration is unconfirmed.
- Deterministic lease-transfer and aggregate-status regressions failed before
  their fixes. The complete Drive-to-Wave delivery module then passed 55 tests
  and 35 subtests.
- Intake upload completion now catches dashboard refresh failures after the
  files have been registered and displays a recovery message instead of
  leaving an unhandled promise rejection. Full web verification passed 478
  tests (6 skipped), TypeScript, and the production client/server bundles;
  four local API upload-boundary tests passed.
- MySQL migration `0007` now uses the schema's own collation consistently for
  source identity grouping, dependent-reference remapping, duplicate removal,
  and the unique index. A case-variant duplicate regression failed against
  MySQL 8.4 before the fix and passed after it, verifying all migrated
  references. All tracked migrations then applied to a fresh disposable MySQL
  8.4 database and all six database integration tests passed. The default web
  suite passed 478 tests with 6 skipped; the added database integration test
  is skipped there when no MySQL URL is configured.
- Path and error-response boundaries were rechecked against the PR review
  findings. Intake uses canonical-root containment and symlink/race checks;
  backup restore validates archive members before staged writes; unexpected
  API exceptions return a generic correlated response. Regressions now verify
  traversal rejection and traceback/path redaction. The full local API module
  passed 119 tests and 2 subtests; related intake/backup suites passed 45 tests
  with 4 environment-dependent skips. This is not a new hosted CodeQL scan.
- A fresh Windows production-bundle rehearsal passed all 71 synthetic checks
  across the dashboard, SQLite ledger, managed session handoff, source-byte
  verification, and HAI authorization/replay boundaries. One bounded 30-request,
  six-client warm-cache run measured 51.25 ms median, 69.86 ms p95, 70.63 ms
  max, and 57,768 response bytes; sampled peak combined working set was
  195,387,392 bytes. No provider writes or ngrok tunnel were involved.

## Remediated in this audit

- Replaced repeated export-attempt list scans and no-op updates with indexed identity lookups, route-without-export queries, explicit changed-state results, and no-op batch behavior.
- Made master-ledger projection and Wave daily planning due-based, preventing identical checksum projections and same-day plans from running every five minutes.
- Reduced recurring workflow persistence to runnable steps while preserving complete failure and emergency-stop boundaries; coalesced unchanged worker summaries with bounded daily heartbeats.
- Added canonical Base64URL enforcement to short-lived operator-session tickets so equivalent noncanonical encodings fail before signature acceptance.
- Verified on a copied production ledger that a steady pass completed in 367 ms with one runnable action, zero export writes, and no external submission. Live recurrence reduced normal cycle persistence from 13 workflow steps to one without deleting historical audit evidence.

- Added a persistent, audited autonomy emergency stop. It is checked before every workflow step and only an operator can clear it with an exact confirmation phrase.
- Added sanitized doctor output and support ZIP generation that excludes financial documents, OCR, ledger rows, filenames, amounts, paths, configuration values, and credentials.
- Wired support bundle creation and emergency controls through the local API and operator dashboard.
- Removed invented testimonials, numeric outcome claims, and public claims of unsupported direct MijnGeldzaken, PSD2, SVB, biometric, and unrestricted Wave automation.
- Added traceability, acceptance, security, API/UI audit, operator, checkpoint, and verification documents.
- Replaced the Flask development runtime with bounded Waitress threads, enabled SQLite WAL plus bounded lock waiting, limited dashboard fan-out, and paginated the document-review workspace.
- Added three-service Docker/Compose definitions with non-root containers, health checks, durable runtime volumes, read-only intake, configurable loopback-published ports, and opt-in Docker bridge-gateway trust restricted to private `.1` gateways and loopback hostnames.
- Self-bundled the production web server so its runtime image needs no dependency tree, added the Node ESM compatibility bridge required by bundled CommonJS packages, and removed fixed Docker subnet assumptions that could collide with existing projects.
- Added stable correlated JSON error envelopes across the local API, sanitized unexpected-error handling, and bounded HAI request identifiers without provider-detail leakage.
- Added versioned SQLite migration history, fail-closed history validation, automatic integrity-checked pre-upgrade snapshots, restore-based rollback guidance, and schema status in health/doctor output.
- Replaced per-failed-workflow recovery lookups in deep health with one bounded bulk query.
- Bounded API and support health-detail serialization without weakening full-set status, metrics, counts, next actions, notifications, exception queues, close readiness, or autonomy gates.
- Coalesced identical deep-health HTTP reads through a short bounded in-process cache while retaining `no-store` responses and uncached internal safety decisions.
- On the real 441-issue ledger, bounded serialization reduced health payload bytes by 84.3% and doctor bytes by 68.3%; short concurrent acceptance improved health throughput 8.3x with exact counts in every response.
- Added queue-specific Drive/Wave and master-ledger summary views. The live 150-work-order response fell from 988,287 to 141,040 bytes, while the ledger dashboard read fell from 202,949 to 918 bytes and retained the same checksum.
- Added a two-second, mutation-invalidated control-center single-flight cache, HTTP compression above 1 KiB, and longest-read-first scheduling within the existing four-request bound. Live cold refresh fell from 2.04-2.64 seconds to 1.35-1.65 seconds across the complete optimization and dependency-upgrade runs, immediate refresh fell to 36-54 ms, and the 508 KB browser response transfers as about 78 KB with gzip.
- Replaced eager `os.walk` directory enumeration in local intake with a deterministic `scandir` traversal capped at 120,000 filesystem entries per scan. This bounds memory spent listing irrelevant files and directories before the existing document, byte, and directory limits apply; overflow fails closed as `attention_required` without importing a partial manifest.
- Upgraded the complete web dependency surface, including Express, Vite, Vitest, Recharts, Drizzle, Axios, AWS SDK, Tailwind, PostCSS, and the rate limiter. `pnpm audit` fell from 86 findings (25 high, 53 moderate, eight low) to zero and the peer-dependency check reports no issues.
- Removed the obsolete JSX-location plugin and migrated the shared chart wrapper to the current Recharts tooltip and legend contracts, including the full-payload formatter contract.
- Found and fixed an Express 5 production-only SPA fallback incompatibility. A real TCP regression test now proves nested routes such as `/admin/operations` resolve to the built application.
- Vite 8 reduced the production client graph from 6,221 to 2,426 transformed modules. The Operations chunk fell from about 285 KB to 192.70 KB (50.26 KB gzip), the largest public chunk is 480.76 KB, and the former greater-than-500-KB chunk warning is gone.
- Removed the Manus development/session instrumentation from the production dependency graph and HTML. A build gate now rejects developer markers, an HTML shell above 32 KiB, or any JavaScript asset above 512 KiB; the verified shell is 2,031 bytes and the largest JavaScript asset is 480,764 bytes.
- Added production CSP, clickjacking/MIME/referrer protections, conditional HTTPS-only HSTS, immutable caching for hashed assets, and no-cache delivery for the application shell. Real TCP tests and live native/container checks cover the header contract without breaking loopback HTTP.
- Made optional Stripe billing fail closed behind `FAB_BILLING_ENABLED`, removed public checkout claims/actions from the local product shell, and exposed the deployment-controlled disabled state to account/admin screens.
- Added a bounded request identifier and sanitized stable error envelope to the authenticated server operations bridge, then wired its server-only token through both the Windows launcher and Compose.
- Replaced the Windows worker lock file with a project-scoped named mutex. Forced worker termination can no longer leave a file handle that blocks cleanup or the next isolated test run; Unix deployments retain `flock`.
- Made the Windows launcher provision and validate a project-local Python 3.13 `.venv`, preserving the system Python and installing only FAB's local requirements into the isolated runtime.
- Added dependency vulnerability and peer-contract checks to the web CI gate.
- Replaced the documented one-shot legacy controller entrypoint with one owned
  cycle of the authoritative ledger worker and retired the duplicate controller.
  A stale opt-in setting now fails startup with a migration message.
- Replaced source-adjacent, no-op image preprocessing with bounded denoising,
  measured deskew correction, private temporary output, unconditional cleanup,
  and sanitized preprocessing evidence in document metadata and audit events.
- Prevented local-operator requests from needlessly verifying unrelated browser
  cookies and made the Windows launcher provision a stable encrypted dashboard
  signing secret through the current-user DPAPI-backed FAB secret store.
- Added a quiescent maintenance runtime across Windows and Compose. It omits the
  worker, locks normal mutations, disables cloud access and HAI commands, and
  keeps only the authenticated API/dashboard recovery surface available.
- Replaced ledger-file-copy recovery with SQLite backup-based ledger recovery
  and complete v2 source-evidence recovery. The latter validates archive
  topology, manifest/ledger coverage, immutable content-addressed targets,
  rewritten paths, every restored byte, database integrity, and audit state.
- Bound recovery to the authoritative worker ownership lock, exact confirmation,
  a verified source-complete pre-restore package, rollback state, and fail-closed
  tamper, collision, reparse-point, concurrent-owner, and final-verification
  behavior.
- Split private service traffic from browser-facing API origins. Docker's
  internal `api` hostname is accepted only server-side; operator links allow
  loopback HTTP or a clean HTTPS origin and reject credentials, paths, queries,
  fragments, and insecure non-loopback hosts.
- Unified runtime access identity across Windows readiness, doctor/support
  diagnostics, API settings, Compose, and the operator dashboard. Launcher
  metadata is accepted only after a bounded direct loopback identity probe
  proves the service, checkout, routes, and exact API origin; a short
  signature-bound cache avoids repeated probes under dashboard refresh load.
- Removed duplicate dependency discovery inside readiness summaries and the
  per-route readiness-service lifecycle that defeated caching. All API,
  dashboard, HAI, autonomy, recovery, doctor, and support paths now share one
  app-owned service. Its thread-safe five-second dependency cache returns deep
  copies and preserves prompt change detection. One hundred live authenticated
  settings reads improved from 39.41 ms median / 54.81 ms p95 to 2.48 ms /
  3.02 ms after the production restart.
- Removed the duplicate-reassessment N+1 path that loaded two complete document
  histories for every open candidate pair. A bounded, chunked snapshot now
  loads only core document rows and selected open reviews through one SQLite
  connection. On the live ledger, reassessment improved from about 206 ms to
  22.95 ms median, autonomy counts from 252.56 ms to 118.67 ms, and the
  restarted authenticated autonomy endpoint measured 217.94 ms median while
  retaining every duplicate, review, approval, and external-submission gate.
- Added a context-local, query-only SQLite snapshot for compound health,
  exception, and delivery reads. It provides one consistent database view,
  collapses repeated Windows connection setup/teardown, rejects accidental
  writes, and remains isolated per request. Normalized record line items and
  exception entities now hydrate in bounded bulk queries instead of N+1 reads.
- Reused trusted Gmail scanner containment validation only inside the current
  delivery snapshot. Individual reads still validate the source and archival
  execution retains all provider identity, source/destination, and pre/post
  hash checks. Live exception latency fell from 308.57 ms to 85.28 ms median,
  150-work-order latency from 222.81 ms to 125.52 ms, and the complete cold
  control center to 709.21 ms median with a 31.61 ms immediate-repeat median.
- Replaced the descriptive activation checklist with a tested, dependency-ordered
  five-step flow. Progress comes from live Drive, Gmail, Wave, receipt-executor,
  and queue-wide review state; the current action opens the existing guarded
  setup surface and cannot bypass any provider or approval gate.
- Added tab-scoped review draft recovery for source-backed correction forms.
  Drafts are schema-bounded, exact-review/source-identity-bound, seven-day
  expiring, cleared after successful approval, explicitly discardable, and
  never written to the authoritative ledger before the operator submits.
- Wired local bank-statement file import through the authenticated React
  dashboard, typed server router, bounded gateway, Flask API, normalizer,
  SQLite ledger, audit trail, and automatic local reconciliation command.
  CSV, JSON, CAMT/XML, and MT940 files are limited to 4 MB; binary content,
  mismatched extensions, malformed XML/JSON/base64, and CSV files without a
  recognizable transaction date plus amount/debit/credit header fail closed.
- CAMT entries containing multiple transaction details are split only when
  every detail has a parseable amount in the entry currency and the signed
  detail amounts exactly reconcile to the entry total; otherwise FAB retains
  the original entry-level amount rather than inventing a transaction split.
- Corrected Dutch bank-export normalization for `Datum`, `Boekingsdatum`,
  `Valutadatum`, `Valuta`, and counterparty-name headers. Invalid dates or
  missing amounts can no longer create ledger rows or false completed imports.
- Made generated bank identities occurrence-aware. Two legitimate identical
  rows in one statement are both retained, while replaying the same statement
  recognizes both identities and creates no additional rows.
- Corrected the dashboard's unreconciled-value read: it no longer sends the
  invalid `status=unreconciled` filter and excludes only finalized
  reconciliation states from the local amount projection.

## Verification snapshot (2026-10-17)

- Latest complete Python suite on the current working tree: 1,993 passed, 6 skipped, and 222 subtests passed in 934.92 seconds. This includes source-root containment, HAI result validation, backup archive traversal, and exception path/traceback redaction regressions.
- Fresh web verification passed TypeScript, all 478 Vitest tests (6 skipped), Vite production build and asset budgets, and general plus standalone server bundles via `npm run build`. All six MySQL integration tests also passed after applying the complete migration chain to a fresh disposable MySQL 8.4 database. `pnpm run build` failed before running the build because Windows denied writing `web/node_modules/.package-map.json`; the equivalent npm build completed without downloading packages.
- Follow-up verification: the complete Python suite passed 1,996 tests, skipped 8, and passed 222 subtests in 824.74 seconds. The two initial failures were incomplete `fstat` test doubles; fixtures now preserve the real file device/inode while simulating size changes, and the entire suite passed on rerun. The web suite passed 479 tests (6 skipped), TypeScript and production build/bundle checks passed, and the synthetic Windows stack rehearsal passed with authenticated dashboard document upload, source hash verification, idempotent replay, exactly-one-row persistence, processing, dashboard refresh, and owned-child cleanup. No MySQL integration URL was configured for this follow-up, so MySQL migration integration was not reverified in that run.
- MySQL follow-up on 2026-10-17: all tracked migrations applied successfully to a new disposable MySQL 8.4 `fab_test` database, then all six MySQL integration tests passed. The test used a fixed loopback port after Docker Desktop's dynamically assigned localhost port timed out; the disposable container was stopped and removed afterward. `pnpm exec` attempted dependency installation and hit a Windows `EPERM`, so the already-installed Drizzle and Vitest launchers were invoked directly. This is local verification, not a hosted CI result.
- PR #125 is open at live head `892f790`. CodeQL passed for that head. Its FAB verification workflow failed to start; manual rerun `38985828203` remains queued with zero jobs. Earlier successful hosted runs were attached to prior PR commits and are not current-head verification.
- Read-only GitHub audit on 2026-10-17: all 101 open code-scanning alerts are attached to `refs/heads/main` at commit `6445f876`; PR #125's head `892f790` has a passing CodeQL check, but that does not clear the main-branch backlog. Manual FAB verification run `38985828203` is still queued with zero jobs, so hosted FAB tests have not executed on this head.

- Latest full Python suite on the current worktree: 1,976 passed, 6 skipped, and 219 subtests passed in 748.66 seconds. The changed API, Drive archive, autonomy, processing, report-export, and anomaly-analysis areas were covered by this run.
- Python lock regeneration/verification passed for all four hash-locked sets. `pip-audit` reported no known vulnerabilities for the production, local, ML, and test locks.
- Web TypeScript check passed. The latest Vitest run: 39 files passed, 1 skipped; 471 tests passed, 5 skipped. The Vite production build and production asset budget check passed, as did both web-server bundles (general and standalone).
- The `pnpm check` wrapper attempted dependency installation and hit `EPERM` writing pnpm's generated `web/node_modules/.package-map.json`; the already-installed TypeScript, Vitest, Vite, and esbuild binaries were invoked directly. No tracked dependency manifests were changed.
- Codex Security diff review completed for 27 changed code files with zero reportable findings. Coverage is partial: no live Google/Wave account, public ngrok deployment, external HAI peer, production-sized restore, or independent penetration test was exercised. Report: `C:\Users\NO\.codex\state\plugins\codex-security\scans\019-FAB\dbe3a8a6383c88ad926a707ff7d8975561c99327_20261016T115022Z_5npeimmp\report.md`.
- Fresh synthetic 2,000-bank-row x 400-document reconciliation benchmark: 1.395 seconds unprepared versus 19.78 ms indexed, with identical results. Traced Python peak was 651,704 versus 848,007 bytes; this matcher-only allocation metric is not process RAM or an application-wide resource result.
- A controlled service-level anomaly comparison used identical 400-row synthetic input and interleaved 15-run measurements against the original checkout implementation. Results were identical; median decreased from 42.022 ms to 9.719 ms and max from 64.491 ms to 11.207 ms. For 100 below-threshold rows, amount-parser calls fell from 20,000 to 200. This does not measure control-center throughput or process CPU/RSS.
- A separate 40-request/16-client synthetic warm-cache dashboard rehearsal passed at 125.49 ms median and 139.01 ms p95 with a 57,768-byte JSON body. This fixture and concurrency differ from the prior 30-request/6-client run, so the results are not a controlled speed comparison and do not measure CPU/RSS.
- Local Bandit scan reported 0 high, 29 medium, and 16 low heuristic findings across `src`. All 26 B608 findings are in `local_ledger.py`; manual source tracing found fixed query templates, internally built filters, identifier validation, and bound values rather than request/document data used as SQL syntax. The legacy `ReportingService` CSV export now selects from fixed SQL statements, with regression tests rejecting hostile table names before database access. The two B104 findings are the explicitly configured VM/container wildcard-bind paths; remote binding requires a strong API token in deployment preflight. The B113 finding is a Photos download request with `timeout=self._request_timeout()`. These were manually reviewed as bounded heuristic matches, not automatically suppressed. The managed Codex Security Deep Scan could not start because this session lacks its required filesystem permission profile. Do not treat these checks as a clean penetration-test result.
- Unexpected Flask route exceptions now record `externalSubmission: unknown`; the regression test simulates a provider-side effect before a route throws and verifies the audit does not falsely assert `not_executed`.
- After restoring intake root containment and HAI malformed-result handling, three separate Windows stack rehearsals each passed the synthetic integration checks and 30 concurrent authenticated warm-cache `fab.controlCenter` reads at concurrency 6. The response was consistently 57,768 bytes. Latency varied materially: median 61.89-105.67 ms, p95 81.34-221.80 ms, and max 81.99-248.59 ms. Seven to thirteen 50 ms samples measured combined API/dashboard peak working set 189,505,536-196,145,152 bytes; dashboard CPU 0.328125-0.5625 seconds and API CPU 0.0 seconds at reported precision. This one-document, warm-cache sample is diagnostic, noisy, and not a cold, scaled, sustained or whole-app baseline. The 80-request burst was rate-limited by the existing tRPC policy; the combined rehearsal CLI is bounded to 30 requests.

## Remaining risks

- Live Google and Wave acceptance depends on owner authorization and current provider state.
- MijnGeldzaken remains a supervised master-ledger export, not an authenticated write connector.
- Direct PSD2 bank feeds and SVB submissions are not implemented.
- Bank-file import is production-wired, but direct PSD2 account connectivity
  remains unavailable; statement files must be exported by the account owner.
- Compose configuration, both images, authenticated service health, dashboard access, a complete 29-resource control-center response, local-operator authorization, server-operations authentication, production headers, compression, and non-root execution are locally verified. Live cloud-host acceptance remains environment-specific.
- Recovery is implemented and locally verified without touching the live ledger. A production-sized restore rehearsal, recovery-time objective measurement, and accountant-approved evidence sampling remain required before unattended upgrades rely on it.
- Formal penetration testing, DPIA approval, accountant validation, and production disaster-recovery exercises remain external work.
- Production-looking Google Cloud Function, root workflow, standalone mobile
  upload, checkpoint controller, synthetic learning, generic performance,
  interactive migration, duplicate review/error queues, and duplicate vendor
  template paths have been removed. The active correction-learning, processing,
  template, performance, import, reporting, review, and compliance behavior is
  owned by the operations-ledger services. The dummy
  tax-export method was removed; provisional VAT artifacts come only from the
  checksum-bound reporting service and are never represented as a filing.

## Technical debt register

| Priority | Item | Required action |
| --- | --- | --- |
| High | Live provider acceptance | Reauthorize Google, validate the Wave business/token/mapping, and run a synthetic receipt through attachment readback before enabling archival. |
| Medium | Image optimization | Removed unused OpenCV and its NumPy dependency from the core lock after confirming no source imports. A fresh current dirty-checkout image measured 140,805,983 bytes with ML disabled, versus 242,575,002 bytes for tagged revision `3e74dbcf5bb4158888ef550797c79bc564e5d291` (about 42% smaller). This is not a controlled comparison because the source revisions and Python base versions differ; clean-release, registry-transfer, and process CPU/RSS measurements remain. |
| Medium | Performance baseline | Initial synthetic concurrent hot-cache dashboard timings are now recorded. Still measure cold refresh, sustained idle/concurrent host load, process CPU/RSS, and cold backup-integrity duration separately; the current one-document fixture is not representative of a populated production ledger. |
| Medium | Recovery rehearsal | Exercise the documented schema rollback and full source-evidence recovery process on a production-sized copy before unattended upgrades. |
| Medium | Legacy compatibility retirement | External and internal duplicate workflow entrypoints and placeholder helpers are retired. Continue removing retained `posting_attempts` compatibility only after any deployed pre-ledger database has been migrated. |
| Medium | Privacy governance | Complete a signed DPIA and data-processing inventory before multi-user production use. |
| Low | Public product shell | Keep public deployment, capability, and billing text synchronized with the operator product before each release. |
