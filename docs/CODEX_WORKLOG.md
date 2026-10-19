# Codex Worklog

Historical record: Ngrok support was retired on 2026-10-19. Entries below that
describe Ngrok document the former implementation and its verification only.

## 2026-08-13 - Bounded recurring autonomy and canonical operator tickets

- Audited the production ledger after unattended operation. The 150-document database had accumulated 250,572 audit events, 60,153 workflow steps, and 166,347 repeated `local_export_attempt.prepared` events because unchanged drafts and not-due work were repeatedly materialized.
- Replaced the bounded list scan for existing export attempts with indexed identity lookups and route-without-export queries. Existing protected attempts are preserved without timestamp or audit churn, empty preparation batches are no-ops, and the worker no longer prepares the same exports twice in one cycle.
- Made projection and daily Wave planning genuinely due-based: the master projection runs only when its bounded 500-row checksum changes, and a Wave daily plan runs once per local date. Routes already represented by export attempts no longer wake either the plan or the worker.
- Changed recurring worker persistence to materialize only runnable steps during normal operation while retaining complete failure and emergency-stop boundaries. Unchanged worker stage summaries are coalesced with a daily heartbeat; state changes and failures still write immediately.
- Canonicalized Base64URL ticket components before operator-session signature verification. Alternate final characters that decoded to identical bytes are now rejected, closing an ambiguity in the short-lived authenticated ledger handoff.
- Two live five-minute cycles added one workflow run and one step per steady cycle, did not create or update any of the 31 export attempts, and did not repeat the master projection. A copied-production steady pass completed in 367 ms with only `rescan_intake` runnable and `externalSubmission=not_executed`.
- Verification passed 94 focused tests, all four CI-equivalent backend shards (`814 passed`, `4 skipped`), all 182 web tests, TypeScript checking, production build budgets, dependency and peer audits, Python compilation, PowerShell parsing, and standard/maintenance Compose parsing. The Windows API, worker, and dashboard restarted from current source with empty process logs.
- Browser acceptance rendered the live desktop dashboard without horizontal overflow or console warnings/errors. The same-origin handoff reached the populated FAB Operations ledger at `http://127.0.0.1:5001/` without a token prompt. No provider record, review decision, source file, Drive archive, or external submission was changed.
- Implementation commit `c18b2ae` is on `origin/main`; GitHub Actions run `31678667271` passed the frontend, Linux backend, and all four Windows backend jobs.

## 2026-08-09 - Authenticated operator ledger handoff

- Found that every advanced ledger, evidence, work-order, report-artifact, recovery, and connector-contract link left the authenticated React dashboard and opened the token-protected Flask service directly. A non-technical operator was redirected to a token-entry page even though the launcher deliberately keeps that token in DPAPI-backed server state and out of browser code.
- Added a same-origin Express handoff for loopback operators and authenticated administrators. It creates a 45-second HMAC ticket containing only a bounded relative target, actor, issue/expiry time, and random nonce; the long-lived local API token never enters browser HTML, JavaScript, query parameters, audit records, or response content.
- Added a Flask bootstrap that independently verifies signature, audience, version, time bounds, actor, nonce, and redirect target; consumes each nonce once under a process lock; rotates the existing session; sets `HttpOnly`, `SameSite=Lax`, and HTTPS-only `Secure` policy; records a redacted local audit event; and rejects replays, tampering, encoded redirect escapes, absolute URLs, and bootstrap recursion.
- Reused one operator-access resolver for the handoff and verified source-preview gateway. Converted every operator-facing direct service link across overview, exceptions, review, activity, recovery, reports, backups, delivery, connections, and the receipt-executor drawer. Machine-facing HAI/executor URLs remain direct and continue to omit browser session state.
- Verification passed all four CI-equivalent backend shards (`808 passed`, `4 skipped`), all 182 web tests across 22 files, TypeScript checking, production build budgets, the high-severity dependency audit, and peer checks. New tests cover local/admin authorization, target validation, token absence, public HTTPS origins, signature contents, expiry, secure cookies, authenticated continuation, audit redaction, tampering, and one-time replay rejection.
- The exact production build restarted on Windows at `http://127.0.0.1:3005/admin/operations`. Live Browser acceptance followed the handoff to `http://127.0.0.1:5001/#audit`, found the complete FAB Operations ledger with no token prompt, and found no console warnings/errors in fresh dashboard or ledger tabs. Desktop and narrow measurements both reported page width equal to viewport width.
- Implementation commit `6e9dad0` is on `origin/main`; GitHub Actions run `31337125650` passed the frontend, Linux backend, and all four Windows backend jobs. No provider record, review decision, financial submission, attachment, source file, or Drive archive was changed.

## 2026-08-09 - Guided activation and review draft continuity

- Replaced the descriptive activation checklist with a dependency-ordered five-step flow over Google Drive, Gmail, Wave, the supervised Wave receipt session, and queue-wide posting decisions. The dashboard now shows authoritative progress, highlights one current step, and opens the matching guarded setup surface.
- Kept activation truthful: progress advances only when live connector or review state changes, and no setup action is represented as transaction execution, attachment verification, or Drive archival.
- Added source-backed review draft recovery in tab-scoped browser storage. Drafts restore after drawer close or dashboard reload only for the exact unchanged review/source identity, reject malformed, oversized, stale, or future-dated state, expire after seven days, and clear after successful approval or explicit discard.
- Live Browser acceptance showed `3/5 ready`, selected the stale Wave receipt session as the next action, opened its drawer, and found no console warnings/errors. A review note survived close/reopen and full reload; explicit discard restored the original form. No review was submitted.
- Narrow activation geometry stayed within the component and page bounds, and the desktop view retained a readable progress/action hierarchy.
- Verification passed all four backend shards (`801 passed`, `4 skipped`), all 174 web tests across 19 files, TypeScript checking, production build budgets, and the production dependency audit. Implementation commit `cc718f3` is on `origin/main`; GitHub Actions run `31333616656` passed the frontend, Linux backend, and all four Windows backend jobs.
- No provider record, review decision, source file, Drive archive, or external submission was changed.

## 2026-08-09 - Consistent ledger snapshots and control-center latency

- Reprofiled every one of the 29 operator control-center resources against the live 150-document ledger. Exception enrichment was the slowest remaining independent resource, and delivery work orders repeated trusted Gmail scanner path resolution while building one immutable response.
- Added a context-local, query-only SQLite read snapshot for compound health, exception, and delivery projections. A compound read now uses one consistent database view and one connection; any accidental write inside the snapshot fails closed. Normal writes remain independently committed after the snapshot closes.
- Replaced normalized bookkeeping-record line-item N+1 hydration with one bounded bulk query, and replaced per-exception full entity histories with one compact multi-entity snapshot. Source-connector exceptions now include a useful redacted source-account summary instead of a null entity and never expose the source identifier.
- Reused the candidate source-verification result only within the same delivery read snapshot. A trusted Gmail scanner path is resolved once per work-order read, while individual work orders still validate once and every archival operation still rechecks policy, provider identity, source hash, destination, and post-move hash.
- Direct real-ledger exception reads improved from 122.84 ms to 51.73 ms median; 150-work-order reads improved from 151.61 ms to 81.32 ms. After the production restart, authenticated HTTP exceptions improved from 308.57 ms to 85.28 ms, work orders from 222.81 ms to 125.52 ms, close readiness from 185.02 ms to 72.59 ms, and autonomy from 300.73 ms to 223.66 ms.
- Five cold complete dashboard reads measured 612.64-858.23 ms with a 709.21 ms median; immediate cached reads measured 31.61 ms median. Gzip reduced the 418,948-byte control-center response to 59,729 bytes. All 29 resources returned HTTP 200.
- Verification passed 93 focused health/ledger/exception/delivery tests, 801 backend tests with four optional-runtime skips, 165 web tests, TypeScript checking, production build budgets, dependency and peer audits, Python compilation, six PowerShell parsers, both Compose modes, and a read-only live-ledger integrity and foreign-key check.
- Rebuilt the exact current API, worker, and dashboard images in an isolated standard Compose project on loopback ports 5512/3512. API/dashboard identity matched, all 29 control-center resources were live with zero partial errors, HAI was ready with 14 commands and eight resources, all services ran as non-root users, and no fatal log pattern appeared. The project containers, network, volumes, and images were removed afterward.
- The restarted Windows runtime matched the current source fingerprint, all six process logs remained empty, HAI reported ready with 14 manifest commands, 13 currently allowlisted commands, and eight resources, and cloud access remained safely inactive with `endpoint_required`.
- Fresh desktop and narrow in-app Browser acceptance showed connected live data, no horizontal or off-screen control overflow, and no new console warnings/errors. No provider record, review decision, source file, Drive archive, or external submission was changed.
- Implementation commit `e55e7b2` is on `origin/main`; GitHub Actions run `31331321548` passed the frontend, Linux backend, and all four Windows backend jobs.

## 2026-08-09 - Compact duplicate reassessment and autonomy latency

- Profiled all 29 operator control-center resources against the live 150-document ledger. The autonomy plan was the dominant warm path at 485.89 ms because its count projection reassessed duplicate candidates by loading two complete document histories for every open pair.
- Added a bounded, chunked ledger snapshot that loads only the requested document rows and selected review items through one SQLite connection. Duplicate reassessment now uses that snapshot instead of repeated `get_document` calls that also loaded groups, extracted fields, routing/export history, reconciliation, corrections, bookkeeping lines, and every document audit event.
- Preserved duplicate identity decisions, canonicalization, open-review ordering, review gates, and the permanent `externalSubmission=not_executed` planning boundary. Added tests that prove review filtering/ordering and fail if the planning path returns to full-history N+1 reads.
- On the real ledger, duplicate reassessment improved from about 206 ms to 22.95 ms median, autonomy counts from 252.56 ms to 118.67 ms, and the complete direct autonomy plan from 485.89 ms to 336.69 ms. After the Windows production restart, 15 authenticated autonomy HTTP reads returned 200 at 217.94 ms warm median and 262.25 ms p95.
- Verification passed 91 focused ledger/processing/autonomy tests, 797 backend tests with four optional-runtime skips, 165 web tests, TypeScript checking, production build budgets, dependency and peer audits, Python compilation, five PowerShell parsers, and standard/maintenance Compose parsing. API, worker, and web error logs remained empty; HAI reported 14 commands and eight resources.
- The dashboard remained live at `http://127.0.0.1:3005/admin/operations`. The in-app Browser listed that tab but returned a stale tab identifier when asked for a repeat DOM snapshot, so the latest backend-only change relies on the unchanged 165-test/build frontend contract and live HTTP identity rather than claiming a new manual browser capture.
- Implementation commit `a6ce7c4` is on `origin/main`; GitHub Actions run `31329640244` passed the frontend, Linux backend, and all four Windows backend jobs. No provider record, review decision, source file, Drive archive, or external submission was changed.

## 2026-08-09 - App-owned readiness lifecycle and dependency efficiency

- Found that each readiness summary discovered Python modules, Tesseract languages, Poppler, and optional model files twice: once for dependency output and again while deriving OCR source readiness.
- Reused one immutable dependency snapshot throughout each summary and added a thread-safe, single-flight five-second dependency cache. Every caller receives a deep copy, so response mutation cannot corrupt the cached state and dependency changes remain visible after the bounded interval.
- Found a second lifecycle defect after live profiling: settings, health, dashboard, HAI, autonomy, workflow recovery, doctor, and support routes constructed a new readiness service per request, defeating both dependency and runtime-identity caches. `create_app` now owns one readiness service and one support service shared by every route.
- Direct 50-summary profiling reduced dependency discoveries from 100 to 50 and total time from 755.67 ms to 432.58 ms before caching. With the bounded cache, 100 summaries needed one discovery and measured 1.55 ms median and 3.18 ms p95 after the cold call.
- After a production Windows restart, 100 authenticated persistent `/api/settings` reads all returned 200 and improved from 39.41 ms median / 54.81 ms p95 to 2.48 ms median / 3.02 ms p95. The runtime source fingerprint matched the working tree and API, worker, and dashboard error logs remained empty.
- Verification passed 18 focused readiness/support tests, 795 backend tests with four optional-runtime skips, 165 web tests, TypeScript checking, and production build budgets. No provider record, review decision, source file, or external submission was changed.
- Implementation commit `04a2ad4` is on `origin/main`; GitHub Actions run `31328111774` passed the frontend, Linux backend, and all four Windows backend jobs.

## 2026-08-09 - Authoritative runtime access identity

- Fixed readiness and doctor output that advertised the legacy ledger dashboard on port 5001 even while the supported React operator dashboard was running on the launcher's selected port 3005.
- Added bounded launcher-runtime discovery that accepts an operator URL only after a direct loopback, no-proxy, no-redirect identity probe proves the FAB service, checkout instance ID, exact API origin, and expected routes. Invalid, stale, oversized, mismatched, or remote runtime metadata falls back to the ledger dashboard.
- Separated `dashboardUrl`, `ledgerDashboardUrl`, and `apiBaseUrl` in readiness and sanitized support diagnostics, and exposed the non-secret dashboard identity source.
- Added a two-second signature-bound single-flight cache so concurrent readiness refreshes do not repeatedly probe the dashboard identity. On the final restarted runtime, twenty live settings reads completed with a 33.56 ms minimum, 40.45 ms median, and 51.83 ms maximum.
- Scoped `FAB_INSTANCE_ROOT` into the API, worker, and dashboard child environments on Windows and restored the caller environment afterward. Compose now supplies the exact instance root plus host-reachable API and operator dashboard URLs.
- Live Windows acceptance made doctor output and authenticated `/api/settings` agree on `http://127.0.0.1:3005/admin/operations`, retained the ledger dashboard at `http://127.0.0.1:5001/`, kept HAI ready with 14 governed commands, and found all three managed processes alive with zero-byte error logs.
- Verification passed 18 focused readiness/support/launcher tests, 792 backend tests with four optional-runtime skips, 165 web tests, TypeScript checking, production build budgets, Python compilation, PowerShell parsing, and Compose configuration. No provider record, review decision, source file, or external submission was changed.
- Implementation commit `588179c` is on `origin/main`; GitHub Actions run `31326518357` passed the frontend, Linux backend, and all four Windows backend jobs.

## 2026-08-09 - Authoritative review queue pagination

- Replaced the review API's raw-row slice with complete document-group paging, so multiple open decisions for one document cannot be split across pages or counted as separate dashboard records.
- Added queue-wide posting-blocked, evidence-only, duplicate, suggestion, document, decision, and oldest/newest timestamp totals. Activation readiness now uses the complete queue instead of the first 200 rows.
- Added a composite review index and lean continuation-page mode. Page 2 and later skip full summary/category-catalog recomputation while still returning an authoritative total and compact redacted work items.
- Wired bounded review pages through the token-holding Express gateway and operator-only tRPC route. The dashboard starts with 50 document reviews, reports loaded versus total state, merges subsequent pages, and keeps triage/search behavior over the loaded set.
- Live Windows acceptance loaded the real queue `50 -> 100 -> 119`; the final page contained 19 document groups. Desktop/responsive DOM checks found no horizontal or control-text overflow and no new console warnings/errors after the final reload and page fetch.
- Corrected the English review-age unit from Dutch `u` to `h` and clarified document-review terminology.
- Final local verification passed `793` backend tests with four optional-runtime skips, `165` web tests, TypeScript checking, and production build budgets. No review was resolved, no provider record was changed, no Drive source was archived, and no external submission was performed.
- Implementation commit `53d8e21` is on `origin/main`; GitHub Actions run `31324705111` passed the frontend, Linux backend, and all four Windows backend jobs.

## 2026-08-09 - Quiescent maintenance and full evidence recovery

- Added an explicit maintenance runtime that keeps only the authenticated API and operator dashboard online. The recurring worker is absent, normal mutations return `423 Locked`, cloud access is disabled, and HAI exposes no commands while recovery is possible.
- Implemented ledger-only and complete ledger-plus-source-evidence recovery from verified v2 packages. Complete recovery validates archive topology, manifest/ledger coverage, every evidence checksum, immutable content-addressed targets, rewritten live paths, and the final database/filesystem state.
- Bound recovery to the same cross-process ownership lock as the worker, exact typed confirmations, a source-complete pre-restore package, a private ledger rollback snapshot, audited results, and fail-closed collision, reparse-point, tamper, and final-verification handling.
- Added `Start-FAB-Maintenance.cmd`, `Start-FAB.ps1 -Maintenance`, and a Compose maintenance override. Mode changes restart only checkout-owned processes; normal Windows startup restores the API, dashboard, and recurring worker.
- Added a read-only HAI recovery-status resource while permanently excluding restore execution from HAI. The dashboard redacts backup paths, restore roots, and confirmation phrases, and disables ordinary backup/support actions during maintenance.
- Separated the private service API URL from the validated browser-facing API origin. Compose now uses its internal `api` hostname only for server traffic while every operator link resolves to the loopback host port.
- Live isolated Compose acceptance passed with only healthy non-root API/web services, maintenance state, zero HAI commands, cloud disabled, ordinary mutation blocked, a working public recovery link, clean desktop/narrow browser geometry, and no console errors. Exact test containers, volumes, and network were removed afterward.
- Native Windows acceptance entered maintenance with no worker, verified complete-recovery readiness and mutation blocking, then returned to standard mode with the worker alive and all 13 governed HAI commands restored.
- Final local verification passed `788` backend tests with four optional-runtime skips, `164` dashboard tests, TypeScript checking, production build budgets, dependency integrity, Python compilation, and zero known web vulnerabilities.
- Implementation commit `312ec34` is on `origin/main`; GitHub Actions run `31322371840` passed the frontend, Linux backend, and all four Windows backend jobs.
- No restore was executed against the live ledger, no provider record was changed, no Drive source was archived, and no external submission was performed.

## 2026-08-09 - Dependency-safe autonomous workflow continuation

- Replaced failed-step-only autonomy recovery with an explicit local dependency graph. A retry can now continue only through descendants that were recorded as not run because the source cycle aborted after that exact failed step.
- Added independent executor validation for the exact retry action, selected descendants, current low-risk policy, safe local mode, and the permanent exclusion of `execute_approved_exports`.
- Bound every continuation to output created by the current recovery. Existing unrelated documents, routing drafts, export attempts, or close work cannot make a continuation run.
- Persisted retry and continuation roles, dependency edges, exact attempts, and source-run linkage in the workflow ledger. A failed continuation stops the path, leaves later steps not run, and becomes the next exact recovery point instead of reporting false completion.
- Exposed the complete safe path in the API dashboard and React operator queue, with responsive ellipsis behavior and a 68-pixel stable row height.
- Verification passed 777 backend tests with three optional-runtime skips, 163 dashboard tests, TypeScript checking, production build budgets, dependency and peer audits, Windows API/dashboard/worker/HAI acceptance, and clean desktop/narrow in-app Browser geometry and console checks.
- Exact-source Compose acceptance passed with healthy non-root API, worker, and web services, matching API/dashboard instance identities, HAI ready with 14 commands, and no fatal log matches. The isolated containers and volumes were removed afterward.
- No recovery was executed against the live ledger, no provider record was changed, no Drive source was archived, and no external submission was performed.
- Implementation commit `3f2d5c6` is on `origin/main`; GitHub Actions run `31319135326` passed the frontend, Linux backend, and all four Windows backend jobs.

## 2026-08-09 - Authoritative worker and placeholder retirement

- Removed the opt-in checkpoint controller so connector intake, local autonomy, reporting, compliance, export handling, and verified archival have one operations-ledger owner. A stale `worker_run_legacy_workflow=true` setting now fails startup with a clear migration error.
- Removed orphaned checkpoint storage, synthetic learning, generic cache/batch/performance, and interactive migration helpers plus tests that only certified their initialization or fabricated data.
- Preserved and documented the live correction-learning service, lazy OCR/ML loading, bounded worker/API data paths, authenticated historical imports, reconciliation, and checksum-bound provisional VAT reporting.
- Consolidated vendor-template extraction into the active processor pipeline. It now loads directory, file, and inline definitions deterministically; normalizes dates, amounts, VAT, currency, and line items; emits field confidence/evidence; and isolates malformed user templates.
- Removed both duplicate file-backed review queues and the orphaned retry/SMTP facade. Review, exceptions, retries, notifications, and correction evidence remain owned by the operations ledger, authenticated API, and dashboard.
- Exact-source Compose acceptance exposed and fixed a dashboard identity mismatch: the bundled server previously resolved two levels above `/app/dist` to `/` while the API identified `/app`. Compose now supplies an explicit `/app` instance root, and tests plus live container acceptance require matching API/dashboard identities.
- Removed the dummy tax-export method rather than allowing an unverified file to resemble a statutory filing.
- Closed child-process streams in the Windows worker ownership test after the focused run exposed a `ResourceWarning`.
- Final verification passed 771 backend tests with three optional-runtime skips, 163 dashboard tests, TypeScript checking, production build budgets, dependency/peer audits, Python compilation, PowerShell parsing, Compose configuration, authenticated Windows API/dashboard/HAI acceptance, and worker ownership rejection. No provider operation or financial source file was changed.
- Implementation commit `e58e1b6` is on `origin/main`; GitHub Actions run `31316715894` passed the frontend, Linux backend, and all four Windows backend jobs.

## 2026-08-09 - Release safety and unsupported entrypoint retirement

- Removed duplicate Google Cloud Function handlers that could return success after OCR/categorization without durable ledger, review, export, or provider-readback evidence.
- Removed the unauthenticated standalone mobile Flask uploader; local/mobile uploads now use the authenticated operations intake contract and authoritative ledger path.
- Removed the old root `main.py` launcher so the only documented one-shot entrypoint is the ownership-checked `python -m src.main` worker cycle.
- Removed `functions-framework` and `google-cloud-storage` from the production dependency set because no supported runtime imports them.
- Bound the project-local Windows `.venv` to the exact `requirements-local.txt` checksum. A changed dependency contract now stops FAB and rebuilds only the verified, non-linked environment before recording the new checksum, preventing retired packages from accumulating across upgrades.
- Live upgrade acceptance exposed and fixed a Windows bootstrap edge case: an unavailable `py -3.13` registration no longer prevents fallback to an installed `uv`-managed Python 3.13 runtime. Native probes now use explicit exit codes, and incomplete environments are removed only through the same path and reparse-point guard.
- Rebuilt `package.py` around clean committed Git source. Windows and Compose ZIPs exclude tests, CI files, secret/runtime paths, credential-like files, and untracked state; each archive includes a per-file SHA-256 manifest and archive hash sidecar.
- Added atomic package output, failed-build cleanup, path/link/duplicate/member-count and compressed/uncompressed size bounds, dirty-tree refusal, required-runtime inventories, exact source-commit binding, and complete post-build verification.
- Added regression coverage for successful Windows/Compose builds, secret and retired-entrypoint rejection, tamper detection, oversized manifests, dirty sources, and cleanup after final verification failure.
- Verification passed 822 backend tests plus 38 subtests, 163 web tests, TypeScript checking, production build budgets, checksum-bound Windows dependency reconciliation, authenticated local API/HAI acceptance, and real Windows/Compose archive verification. No provider operation or runtime financial file was included or changed.

## 2026-08-09 - Production entrypoint consolidation

- Routed `python -m src.main` through one ownership-checked cycle of the
  authoritative ledger worker instead of the legacy checkpoint controller.
- Reused the recurring worker's Windows mutex/runtime descriptor and returned a
  concise nonzero result when another worker already owns the checkout.
- Changed the scheduler's missing-configuration fallback so the legacy workflow
  is disabled unless explicitly enabled.
- Corrected Windows/Linux/operator documentation and downgraded the broad
  no-placeholder matrix claim until disabled prototype helpers are retired.
- Replaced the old source-adjacent preprocessing copy with real bounded image
  denoising, deskew correction, and binarization in a private temporary file.
- Added unconditional derived-file cleanup on both OCR success and failure, and
  persisted only sanitized preprocessing evidence in document/audit records.
- Added template defaults and real OpenCV regression coverage for skew
  correction, second-pass stability, source preservation, and cleanup.
- Fixed noisy zero-length session-key verification found during live restart:
  loopback operator requests bypass irrelevant SaaS cookie checks, while the
  launcher provisions a stable signing secret in the encrypted DPAPI-backed
  local store for session routes that are used.
- Final verification passed 815 backend tests plus 38 subtests, 163 web tests,
  TypeScript, production build budgets, dependency/peer audits, Python
  dependency integrity, source compilation, and Compose configuration.
- A stopped-state one-shot worker cycle completed in 23 seconds with no
  external submission; the restarted recurring worker then refused a second
  owner. The latest export cycle prepared 0 new and recognized 25 existing
  approval-gated attempts.
- Current-source in-app Browser QA exercised Connections and a document Review
  action, verified a 480-pixel-effective layout without horizontal overflow,
  and found no console warning/error. No provider record or Drive source was
  changed or archived.
- Implementation commit `b228fdc` is on `origin/main`; GitHub Actions run
  `31310667870` passed the frontend, Linux backend, and all four Windows backend
  jobs.

## 2026-08-09 - Managed FAB cloud access and runtime efficiency follow-up

- Added project-owned `Start-FAB-Ngrok.cmd` / `Stop-FAB-Ngrok.cmd` lifecycle scripts that expose only the authenticated FAB API, use a private inspector port, verify the remote FAB and HAI identities, and refuse to pool, stop, or reuse an unrelated ngrok endpoint.
- Added a secret-safe `/api/cloud/status` contract and dashboard connection card. Runtime metadata must match this checkout, its stable instance ID, the exact HTTPS origin, private inspector, tunnel name, and loopback API target before FAB reports cloud access as active.
- Kept the dashboard on loopback and kept remote access disabled when a strong API token, a dedicated endpoint, or exact runtime ownership cannot be proved.
- Lazy-loaded OCR/ML dependencies so ordinary API startup no longer imports NumPy, SciPy, pandas, OpenCV, scikit-learn, joblib, or the processor pipeline. Measured API import wall time fell from roughly 7 seconds to 1.1 seconds.
- After a real control-center load, measured native working sets fell from about 212 MB to 97 MB for the API and from about 190 MB to 120 MB for the worker; the production dashboard used about 124 MB.
- Added signature-bound, bounded backup-manifest caching. Warm unchanged manifest reads fell from roughly 1.4-1.6 seconds to 26-30 ms while any size/time/signature change forces reinspection.
- Added a summary review contract for the dashboard while retaining complete review evidence on the default API. The source API payload fell from 434,794 to 234,340 bytes on the live ledger.
- Verification passed 793 backend tests plus 38 subtests, 161 web tests, TypeScript checking, production build budgets, the production dependency audit, PowerShell parsing, live Windows/HAI/cloud checks, desktop/narrow in-app Browser DOM interaction, and exact-source non-root Compose acceptance.
- The in-app Browser exposed complete desktop and 520-pixel DOM state with the cloud card and no application errors, but its screenshot function returned blank frames; no screenshot claim is made for this follow-up.
- The unmanaged ngrok endpoint on port 4040 remained untouched. FAB still needs a separately reserved HTTPS endpoint before managed cloud access can be accepted.
- Implementation commit `76e5fff` is on `origin/main`; GitHub Actions run `31305186684` passed every Linux, Windows, dependency, test, type, and build job. A separate clean clone of that commit passed source compilation, Compose parsing, frozen install, dependency audit, TypeScript, all 161 web tests, and production build budgets.
- No provider record was changed, no Drive source was archived, and no external submission was performed.

## 2026-08-09 - Production runtime, security, and truthful product follow-up

- Removed production Manus instrumentation and its dependency; the generated HTML shell fell from roughly 369 KB to 2,031 bytes.
- Added enforceable production budgets for the HTML shell and JavaScript assets. The largest JavaScript asset is 480,764 bytes and the build rejects regressions above 512 KiB.
- Added a restrictive production CSP, conditional HTTPS-only HSTS, immutable hashed-asset caching, no-cache SPA delivery, and real TCP header tests.
- Made optional Stripe billing disabled by default, removed active public checkout behavior from the local deployment shell, and derived account/admin billing state from the backend capability gate.
- Rewrote unsupported public security, provider, automation, pricing, SLA, mobile, compliance, and outcome claims in the English/Dutch product content.
- Added request correlation and generic secret-safe unexpected-error handling to the authenticated server operations bridge.
- Wired the operations service token into the Windows and Compose runtimes so the local API, dashboard, worker telemetry boundary, and future HAI control plane use the same server-only local trust boundary.
- Fixed Windows worker restart/test cleanup with a project-scoped named mutex instead of a lock file that survived forced process termination.
- Updated the Windows launcher to create and use a project-local Python 3.13 `.venv` without modifying global Python packages.
- Added web dependency vulnerability and peer-contract checks to CI.
- Verification passed 780 backend tests plus 38 subtests, 161 web tests across 16 files, TypeScript checking, production build budgets, dependency/peer audits, live Windows startup, HAI read-only command planning, desktop/narrow browser interaction and console checks, and exact-source non-root Compose acceptance.
- GitHub Actions run `31301840108` passed Linux, all four Windows shards, frontend dependency/peer gates, tests, and build. A separate clean clone of `2f70668` repeated exact identity, Python compile/mutex tests, frozen install, dependency gates, all 161 web tests, type check, production build budgets, and Compose parsing.
- The isolated ngrok verifier again failed closed with `ERR_NGROK_334`; it did not modify the unrelated endpoint already online.
- No provider record was changed, no Drive source was archived, and no external submission was performed.

## 2026-08-09 - Reporting and compliance operator workspace

- Added compact gateway reads for scheduled report runs and compliance assessments, including explicit live, stale, empty, and external-submission states.
- Projected only operator-safe fields: report periods, row/blocker counts, artifact availability, filing state, finding totals, and evidence checksums. Local artifact paths and private metadata are excluded from the browser contract.
- Added a Reporting navigation target and a complete operator section with schedule state, report evidence downloads, provisional Dutch compliance findings, retention status, and governed local command entry points.
- Kept tax filing and external artifact submission explicitly disabled from this workspace; the new controls prepare local evidence and assessments only.
- Browser QA exposed a clipped report status at common desktop widths. The reporting ledgers now stack before their complete table columns would be constrained, while mobile tables switch to labeled records without horizontal overflow.
- Live acceptance found two prepared report runs and five compliance assessments with one open finding. TypeScript checking, all 163 web tests, production build budgets, clean-console in-app browser checks, and desktop/mobile Playwright captures passed.
- No provider record was changed, no Drive source was archived, and no external submission was performed.

## 2026-08-09 - Control-center data path and dependency hardening

- Added compact Drive-to-Wave queue projections that preserve exact stage, review, retention, archive, and external-submission decisions while leaving complete evidence available from the default and per-document work-order endpoints.
- Added an exact-checksum master-ledger summary response for dashboard reads; complete JSON and CSV projections remain unchanged.
- Added two-second control-center request coalescing with generation-safe invalidation after writes, kept stale-resource fallback behavior, and scheduled the heaviest reads first within the four-request ceiling.
- Enabled compression for JSON/static responses above 1 KiB.
- On the live 150-document ledger, Drive/Wave list traffic fell from 988,287 to 141,040 bytes and master-ledger traffic from 202,949 to 918 bytes with the same checksum.
- Cold control-center refresh fell from 2.04-2.64 seconds to 1.35-1.65 seconds across the complete optimization and dependency-upgrade runs; an immediate repeat completed in 36-54 ms. Gzip reduced the roughly 508 KB browser response to 78,294 bytes.
- Upgraded the web dependency graph and removed the obsolete JSX-location plugin. The package audit fell from 86 findings to zero, with no peer-dependency errors and a reproducible frozen-lockfile install.
- Migrated the chart wrapper to current Recharts contracts and fixed its formatter payload so consumers receive the complete tooltip payload array.
- Fixed Express 5 SPA fallback routing after a production restart exposed the old bare-wildcard incompatibility; a real TCP test now protects nested dashboard routes.
- Vite 8 reduced transformed client modules from 6,221 to 2,426, reduced the Operations chunk to 192.70 KB, and eliminated the greater-than-500-KB public chunk warning.
- Verification passed 780 backend tests, 155 web tests, TypeScript checking, production builds, native and container runtime checks, dependency/peer audits, and desktop/mobile browser geometry and console inspection.
- No provider record was changed, no Drive file was archived, and no external submission was performed.

## 2026-08-09 - Reliability, migration, and API contract follow-up

- Normalized every JSON error below `/api/` to a correlated machine-readable envelope while preserving route-specific details.
- Added bounded request IDs, generic unexpected-error responses, and sanitized persistent failure audit events without exception messages or provider data.
- Hardened HAI request identifiers and executor failures so command responses and ledger audit events cannot leak executor exception details.
- Added ordered checksum-bound SQLite migration history with future/unknown/incomplete/tampered-history refusal.
- Added an automatic SQLite backup, integrity check, atomic SHA-256 manifest, and private file permissions before any populated legacy ledger is upgraded.
- Exposed persisted schema/backup status consistently through API health, doctor, and support diagnostics, including from a separate process.
- Replaced the failed-workflow health N+1 lookup with one bounded bulk query.
- Bounded serialized health/support issue details while retaining complete status, metrics, severity/type totals, next actions, notification materialization, exception queues, and autonomy/close safety decisions.
- Added a bounded two-second single-flight cache for identical read-only health projections; browser/proxy caching stays disabled and all financial safety paths remain uncached.
- Added configurable API/support diagnostic windows and fixed a mojibake separator in the legacy Wave operation table.
- Verified the real 180,838,400-byte pre-upgrade ledger snapshot independently: manifest hash matched and both integrity checks returned `ok`.
- Final verification passed 779 backend tests, 143 affected-path tests, 153 web tests, TypeScript checking, production web build, API image build, non-root Compose acceptance, and the native Windows runtime.
- Live native measurements: liveness 45 ms, five deep-health calls 259-412 ms, dashboard 310 ms, all with 441 real backlog issues retained as blockers rather than hidden.
- Real-ledger payload verification reduced default health JSON from 118,102 to about 18,505 bytes (84.3%) and doctor JSON from 77,448 to 24,544 bytes (68.3%) while preserving all 441 issue totals and on-demand full detail.
- Four-worker/20-request health acceptance improved from 3.14 to 26.11 requests/second; median latency improved from 1,006.6 to 8.1 ms and p95 from 2,226.5 to 703.5 ms, with one cache miss, 19 hits, and exact counts in every response.
- Fixed the Python container runtime baseline so the reviewed fail-closed defaults are loaded even without a mounted host config; Docker acceptance confirmed that optional Freshdesk intake and the legacy workflow stay disabled and produced no error log matches.
- No Wave/Google/provider record was changed, no Drive file was archived, and no external submission was performed.

## 2026-08-08 - Giant goal implementation audit

- Recorded `main` at starting commit `8a2b43d`; `origin/HEAD` also targets `main`.
- Inventoried the Python, React/Express, SQLite, worker, provider, CI, script, test, and documentation surfaces.
- Parsed the 116-phase implementation brief and mapped it against existing product behavior.
- Added a persistent `runtime_controls` ledger model and audited autonomy emergency-stop service.
- Checked the stop before each autonomous step, exposed API/dashboard controls, and allowed HAI to stop but never resume.
- Added sanitized doctor and support-bundle services, CLI, API, dashboard action, privacy contract, and tests.
- Removed fabricated testimonials, numeric outcomes, and unsupported direct-provider/security claims from English and Dutch public copy.
- Added required technical audit, critical path, acceptance, completion, verification, API/UI, security, operator, task graph, worklog, and checkpoint artifacts.
- Added production Waitress serving, authenticated constant-time liveness, SQLite WAL/busy waiting, bounded gateway concurrency, reduced read payloads, and review pagination after browser QA exposed lock contention and excessive mobile DOM height.
- Removed the N+1 trusted-category plan scan and reused one master-ledger/health evidence snapshot across autonomy, exception, and close decisions.
- Added non-root API/worker/web containers, Compose isolation, and a fail-closed ngrok verifier that does not disturb unrelated tunnels.
- Verification commands and final results are maintained in `FINAL_VERIFICATION_REPORT.md`.
- Final local verification passed 768 backend tests, 153 web tests, TypeScript checking, the production build, production Start/Stop, and a 26-resource API profile without SQLite lock errors.
- Built and executed both production containers. Compose passed authenticated API liveness, dashboard HTML, tRPC local-operator authorization, health checks, and non-root execution on configurable loopback ports.
- Removed the fixed Compose subnet after acceptance exposed a collision with an unrelated Docker network; added narrowly scoped Docker bridge-gateway trust and regression tests.
- Fixed a production-only bundled-server crash (`Dynamic require of "fs" is not supported`) with an ESM `createRequire` bridge and re-ran container acceptance.
- Verified implementation commit `c5e42aa` from a separate clean clone: source compilation, Compose parsing, clean status, and 123 focused tests passed.
- Scanned the staged implementation snapshot for runtime paths and high-confidence credential patterns; none were present.

## 2026-08-09 - Safe operator bank-statement import

- Wired a complete local bank-import vertical slice from the React operator
  drawer through the role-gated tRPC router, bounded server gateway, Flask API,
  normalization service, SQLite ledger, audit event, and existing local
  reconciliation command.
- Added 4 MB CSV, JSON, CAMT/XML, and MT940 file policy with filename/format
  agreement, base64 bounds, text/binary checks, strict CSV financial headers,
  and malformed XML/JSON rejection.
- Added Dutch bank-column normalization and CP1252 support for Windows exports.
  Invalid dates and missing amounts are skipped and cannot create false
  completed imports.
- Preserved legitimate identical rows through occurrence-aware generated
  transaction identities while keeping exact statement replay idempotent.
- Corrected the control-center unreconciled-value query and projection so
  finalized bank rows are excluded without using a nonexistent status value.
- Added Overview and Connections entry points, recent-import evidence,
  import/duplicate/skipped results, explicit external-submission state,
  focus trapping, Escape/focus restoration, and responsive drawer styling.
- Local verification passed 806 backend tests with four optional-runtime skips,
  179 web tests, TypeScript checking, production build budgets, peer checks,
  dependency audit, source compilation, production Windows restart, and live
  desktop/narrow browser acceptance with an empty console.
- Implementation commit `2356177` is on `origin/main`; GitHub Actions run
  `31335374005` passed the frontend, Linux backend, and all four Windows jobs.
- No real bank statement was selected in browser QA, no provider record was
  changed, no Drive source was archived, and no external submission occurred.

## Explicitly not performed

- No live provider record was created, modified, or deleted during the code audit.
- No Drive source was archived.
- No credentials, OAuth tokens, financial files, or runtime database were added to Git.
- No direct MijnGeldzaken, PSD2, or SVB automation was invented to satisfy a checklist.
