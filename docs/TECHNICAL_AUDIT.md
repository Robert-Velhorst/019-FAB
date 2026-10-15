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
- Corrected Dutch bank-export normalization for `Datum`, `Boekingsdatum`,
  `Valutadatum`, `Valuta`, and counterparty-name headers. Invalid dates or
  missing amounts can no longer create ledger rows or false completed imports.
- Made generated bank identities occurrence-aware. Two legitimate identical
  rows in one statement are both retained, while replaying the same statement
  recognizes both identities and creates no additional rows.
- Corrected the dashboard's unreconciled-value read: it no longer sends the
  invalid `status=unreconciled` filter and excludes only finalized
  reconciliation states from the local amount projection.

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
| Medium | Image optimization | The OCR/PDF-capable API image baseline is 1.52 GB. Pandas has now been removed after replacing its runtime uses; rebuild and measure before claiming an image-size reduction. A separate API/OCR worker image remains a candidate if transfer or cold-start cost is still material. |
| Medium | Performance baseline | Run sustained idle-host and concurrent-refresh tests, and track cold backup-integrity scan time separately from warm bounded-health latency and payload size. |
| Medium | Recovery rehearsal | Exercise the documented schema rollback and full source-evidence recovery process on a production-sized copy before unattended upgrades. |
| Medium | Legacy compatibility retirement | External and internal duplicate workflow entrypoints and placeholder helpers are retired. Continue removing retained `posting_attempts` compatibility only after any deployed pre-ledger database has been migrated. |
| Medium | Privacy governance | Complete a signed DPIA and data-processing inventory before multi-user production use. |
| Low | Public product shell | Keep public deployment, capability, and billing text synchronized with the operator product before each release. |
