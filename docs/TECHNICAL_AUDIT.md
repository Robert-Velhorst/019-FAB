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

## Remediated in this audit

- Background refresh of loaded review pages is bounded to four logical reads
  rather than one request per loaded page at once. Superseded/error batches stop
  queued work and preserve ordered, all-or-nothing result merging. Synthetic
  Chromium checks against the real temporary stack observed six additional
  page reads with peak four while retaining 175 loaded reviews. Desktop mobile
  navigation-control visibility, collapsed-sidebar mobile reopening and narrow
  table-value placement were corrected and checked at 1280x800 and 390x844.
  This is not a whole-app resource or complete browser/device acceptance claim.

- Category candidate discovery no longer takes only the newest 5,000 global
  reviews. SQL selects documents with open category gates, and a stable
  timestamp/ID cursor traverses 100-document pages with complete open review
  groups. This avoids unrelated payload hydration and duplicate/normalized
  context loading while preserving existing category and posting eligibility.
  A synthetic 2,000-document/2,000-review fixture with 20 category reviews and
  8 KiB OCR/review payloads returned identical category context: median
  624.82 ms / 18,046,561 peak traced Python bytes for the former global-window
  reference versus 10.12 ms / 482,243 bytes for the filtered cursor, across five
  interleaved warm samples. The reference covers this entire bounded fixture;
  separate regressions prove discovery behind 5,001 unrelated reviews. These
  are read-path measurements, not process RSS, cold-cache or whole-app claims.
  Reproduce with `python scripts/benchmark_planning_reads.py --category-reviews`.

- Replaced eager `os.walk` directory enumeration in local intake with a deterministic `scandir` traversal capped at 120,000 filesystem entries per source scan. This bounds memory spent listing irrelevant files and directories before the existing document, byte, and directory limits apply; overflow fails closed as `attention_required` without importing a partial manifest.

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

- Fresh complete Python suite on the canonical working tree passed 1,982 tests, 6 skipped, and 219 subtests in 1,270.51 seconds; the filesystem-entry cap regression passed. Python lock verification and its regression test also passed.
- PR #125 head `cd5889f` passed hosted Linux/Windows backend, web, isolated MySQL, both container-build, and four-language CodeQL checks. The bounded folder traversal is a newer change and needs a fresh hosted run.

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
- The fresh Windows production-stack rehearsal passed 30 concurrent authenticated warm-cache `fab.controlCenter` reads at concurrency 6. Removing the unused HAI manifest projection reduced the uncompressed response from 67,105 to 56,750 bytes (15.4%); post-change latency was 31.20 ms median and 37.26 ms p95 on a one-document synthetic ledger. Largest remaining projected fields were `autonomy` (12,081 bytes) and `health` (5,213 bytes). The direct HAI manifest endpoint remains available and passed the rehearsal. The latest 30-request run measured 49.56 ms median / 66.27 ms p95 and 57,768 response bytes on its fixture. Its 50 ms sampler captured six process samples: peak sampled combined API/dashboard working set 189,747,200 bytes (76,197,888 / 113,549,312), dashboard CPU 0.359375 seconds, and API CPU 0.0 seconds at reported precision. The one-document warm-cache, six-sample measurement is diagnostic only and does not measure cold refreshes, scaled-ledger behavior, sustained load, or a representative whole-app resource baseline. The 80-request burst was rate-limited by the existing tRPC policy; the combined rehearsal CLI is now bounded to 30 requests.

## Planning Read Verification (2026-10-20)

Autonomy batch counters now use bounded SQL counts instead of materializing OCR
and review evidence merely to compute list lengths. Status filters and the
existing 1-to-500 batch clamp are preserved. Duplicate-cycle detection now
streams all confirmed links using only document IDs and link IDs, without a
newest-document cutoff. A regression covers a cycle older than 5,001 newer
linked documents, across multiple 500-row cursor batches. Explicit caller-
supplied diagnostic limits retain their bounded-window behavior; autonomous
planning and repair use the full-history default. Graph memory scales with
confirmed links rather than document payload size.

Each repair rechecks its selected links in bounded SQL batches after taking
the writer lock. Changed cycles are deferred rather than undoing an intervening
correction. Document links/status, review gates, normalized bookkeeping records,
and the per-cycle audit commit together or roll back. Backup happens before
the writer transaction; checksum-bound backup and retained-source gates remain.

Candidate reassessment uses compact canonical plan fingerprints and releases
the initial payload snapshot before backup/revalidation. It recomputes evidence
under the writer lock, defers changed pairs, and applies accepted changes and
completion audit in one transaction. Regressions reproduce operator-confirmed
candidates being rejected by a stale plan and partial writes after normalization
or audit failure. Same-timestamp evidence edits, replacement candidate/review
failures, false transition results, mixed fresh/deferred pairs, and a real
authenticated pre-mutation recovery package are covered. Expanded checks passed
264 tests and 5 subtests; nine final focused cases passed. No operator data or
external provider records were used. This is not independent penetration testing.

`python scripts/benchmark_planning_reads.py` uses only a new disposable synthetic
SQLite ledger, never operator configuration or provider accounts. Its default
fixture has 2,000 documents and 2,000 reviews, 8,192-byte OCR/review payloads,
500-row batches, and five alternating warm samples per implementation. Both
versions returned identical counts and 500-row graph projections in the initial
`3e46b72` benchmark:

| Read Path | Full-Payload Median | Lightweight Median | Full-Payload / Lightweight Peak Traced Python Bytes |
| --- | --- | --- | --- |
| Document/review batch counters | 164.67 ms | 4.58 ms | 7,186,967 / 3,822 |
| 500-row duplicate graph projection | 88.74 ms | 21.08 ms | 7,186,939 / 170,268 |

These are read-path timings and Python allocation peaks, not process RSS, total
CPU, cold-cache throughput, or a whole-app resource-reduction percentage. The
fixture remains bounded to 128 MiB of generated payload and removes its temporary
data. Expanded ledger/API/health/autonomy/processing checks passed 298 tests and
5 subtests; a further 47-case focused group includes the large-queue clamp check.
The built dashboard/API/SQLite/HAI synthetic rehearsal passed with no provider
writes and both children stopped. Its tiny warm-cache result is not a scaled
application baseline. Fresh hosted CI is required for this follow-up.

The subsequent full-history follow-up adds `--confirmed-cycle` to seed a cycle
spanning every synthetic document. A fresh 2,000-link, five-sample run returned
the same cycle as the complete diagnostic-window reference: 28.36 ms median /
1,149,040 peak traced Python bytes for that reference versus 12.87 ms / 583,036
bytes for full-history confirmed-link streaming. Both implementations cover the
whole fixture in this comparison; separate regression tests prove coverage
beyond the old window. This remains a graph-path microbenchmark, not a process
RSS, whole-app, or live-ledger performance claim.

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
| High | Host storage headroom | The Windows source volume exhausted free space during synthetic verification. Keep adequate free space for ledger WAL, evidence and authenticated recovery packages before unattended operation; alternate exFAT storage with large allocation units is inefficient for dependency caches and test checkouts. No user data or backups were deleted to recover space. |
| High | Remote telemetry recovery and acceptance | Destination-scoped durable identity links, local-first persistence, translated references/updates, and conflict quarantine address the reproduced numeric-ID collision. Schema v4 also quarantines rejected local participants without a link, preventing resubmission after restart; schema v2/v3 upgrade fixtures verify recovery snapshots and retained v3 links. Historical unmapped rows and unknown create outcomes still need verified readback/backfill; replacing a remote database at the same URL requires a new identity_scope generation label. Exercise these recovery boundaries against the intended deployment before calling dual-store synchronization production-ready; exactly-once delivery is not implemented. |
| Medium | Image optimization | Removed unused OpenCV and its NumPy dependency from the core lock after confirming no source imports. A fresh current dirty-checkout image measured 140,805,983 bytes with ML disabled, versus 242,575,002 bytes for tagged revision `3e74dbcf5bb4158888ef550797c79bc564e5d291` (about 42% smaller). This is not a controlled comparison because the source revisions and Python base versions differ; clean-release, registry-transfer, and process CPU/RSS measurements remain. |
| Medium | Performance baseline | Initial synthetic concurrent hot-cache dashboard timings are now recorded. Still measure cold refresh, sustained idle/concurrent host load, process CPU/RSS, and cold backup-integrity duration separately; the current one-document fixture is not representative of a populated production ledger. |
| Medium | Recovery rehearsal | Exercise the documented schema rollback and full source-evidence recovery process on a production-sized copy before unattended upgrades. |
| Medium | Legacy compatibility retirement | External and internal duplicate workflow entrypoints and placeholder helpers are retired. Continue removing retained `posting_attempts` compatibility only after any deployed pre-ledger database has been migrated. |
| Medium | Privacy governance | Complete a signed DPIA and data-processing inventory before multi-user production use. |
| Low | Public product shell | Keep public deployment, capability, and billing text synchronized with the operator product before each release. |
