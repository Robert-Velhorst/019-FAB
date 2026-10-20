# Codex Worklog

Historical record: Ngrok support was retired on 2026-10-19. Entries below that
describe Ngrok document the former implementation and its verification only.

## 2026-10-16 - Confirm hosted CI and refresh PR security checkpoint

- Verified PR #125 remains open at `6a3bf9bc249b1b6049ee3428c99892c564d3e30e`;
  the isolated worktree is clean. Both private FAB GHCR packages already grant
  `Robert-Velhorst/019-FAB` Actions Write; this was verified in both package
  settings, so no access change was necessary.
- Hosted FAB verification run `38966840477` completed successfully across all
  nine jobs. Linux backend: 1,919 passed, 44 skipped, 219 subtests. Four Windows
  shards: 1,985 passed, 15 skipped, 219 subtests total. Frontend: 471 passed,
  5 skipped. MySQL integration: 5 passed. Both production container builds
  passed without publishing images.
- CodeQL run `38966836975` has four language jobs but was still queued at the
  latest check. The latest default-branch analysis is at `6445f876`; its open
  findings are not attributed to this PR head, and no findings were dismissed.
- Refreshed `CODEX_CHECKPOINTS.md` and `GOAL_COMPLETION_MATRIX.md` so hosted CI
  status is accurate. Provider writes, live ngrok exposure, target-host cloud
  deployment, and human/accountant acceptance remain separate unverified gates.

## 2026-10-16 - Verify full CI and CodeQL on refreshed PR head

- FAB verification run `38968352699` passed all nine jobs on commit
  `3a47906537f1e59a5191e2a81dba6fcc286fa236`. This re-ran Linux and four
  Windows backend shards, frontend checks/build, MySQL integration, and both
  build-only container jobs after the checkpoint update.
- CodeQL run `38968348453` passed Python, JavaScript/TypeScript, Actions, and
  C# analysis. The aggregate PR check states: "No new alerts in code changed by
  this pull request." This does not clear the existing default-branch alert
  backlog or replace a whole-repository security assessment.
- The PR remains open. No external package, provider, tunnel, or deployment
  mutation occurred.

## 2026-10-16 - Verify intake hardening and refresh release/security checkpoint

- Verified the isolated PR #125 checkout is clean at `1dee2d8` on
  `codex/fab-followup-20261015`. Regression tests for local intake, connector
  intake, and Drive relay passed (`40 passed, 2 skipped`); Python compilation
  and `git diff --check` passed.
- The FAB verification run `38964035980` and CodeQL run `38964032687` are both
  queued for this exact commit. The earlier CodeQL run reported six high-severity
  path-flow alerts; latest hosted security status therefore remains unresolved.
- Confirmed the two FAB GHCR packages remain private and associated with
  `Robert-Velhorst/019-FAB`. Their Actions Write access was already verified in
  package settings on 2026-10-16 after the user's authorization; no additional
  permission change or package publication was needed for this update.
- Updated `CODEX_CHECKPOINTS.md` to replace stale PR heads and CI references.
  No live provider data, Drive files, release, or package contents were changed.

## 2026-10-16 - Recheck hosted security and whole-repository alert inventory

- CodeQL run `38964032687` completed successfully for PR code head `1dee2d8`;
  its pull-request check explicitly reports no new alerts in code changed by the
  PR. This is not a clean bill for all existing code-scanning alerts.
- Queried GitHub's current default-branch code-scanning inventory. At the
  observed analysis commit `6445f876`, 100 alerts remain open across 11 rules,
  including 22 high-severity path-injection findings and 63 medium stack-trace
  exposure findings. These findings are on `refs/heads/main`; do not attribute
  them to the current PR head or imply that this PR resolves them wholesale.
- FAB verification run `38964460719` completed for `f50b3a8`: all four Windows
  shards, frontend, MySQL integration, and both container builds passed. Linux
  reported `1,918 passed, 44 skipped, 219 subtests passed` and one failing
  traversal-filename expectation because Linux treats backslashes differently
  from Windows. The stored file remained contained; the failure exposed a
  cross-platform filename-normalization mismatch.
- A native Codex Security Standard scan was started for the isolated PR
  checkout, but its progress update was rejected because another continuation
  owns that scan. No findings or coverage from it are claimed. No code-scanning
  alert was dismissed or suppressed.
- The active CI run surfaced Node.js 20 deprecation warnings from the pinned
  Docker build actions. A newer Node.js 24-native release was tested locally
  against workflow YAML, but a repository settings check later showed the
  selected-actions allowlist permits only the existing Docker action SHAs.
  Restored those approved pins instead of broadening repository permissions;
  a future action upgrade requires adding reviewed exact SHAs to the allowlist.
- Updated `CODEX_CHECKPOINTS.md` to distinguish the PR CodeQL result, the
  default-branch alert backlog, and unfinished full CI.
- Fixed Drive relay basename handling to interpret both slash styles as
  separators before Werkzeug sanitization. Focused relay tests now pass
  (`5 passed`); Python compilation and `git diff --check` pass. Updated the
  checkpoint with the exact failed CI result. Pushed the code and workflow
  updates as `9fa48a2`.
- The FAB verification trigger for `9fa48a2` ended as `startup_failure` before
  any job was created; there is no run log, and GitHub says the run cannot be
  retried. CodeQL run `38966062508` is queued for the same head. A fresh PR
  event is required to verify the updated code and action pins.

## 2026-10-16 - Validate the full intake integration slice locally

- Ran the broader local regression group across folder intake, connector intake,
  Drive relay, photo-picker intake/client, and the local operations API:
  `174 passed, 2 skipped, 2 subtests passed` in 116.59 seconds.
- Confirmed three PR-triggered FAB runs and a workflow-dispatch run fail with
  `startup_failure` before any jobs are created. CodeQL remains queued; no
  hosted verification is inferred from the local tests.

## 2026-10-16 - Refresh the current verification checkpoint

- Refreshed `CODEX_CHECKPOINTS.md` to PR head `4c8b5a8` and added the latest
  `startup_failure` run `38966565138`. GitHub still creates no jobs for the
  FAB workflow, while local integration coverage remains green. No provider
  writes or code-scanning alert suppressions were performed.

## 2026-10-16 - Align Docker action pins with repository policy

- Queried the repository's selected-action policy and confirmed it allows only
  the prior exact Buildx/build-push SHAs, not the newly pinned Node 24 releases.
  Restored the approved pins in CI and release workflows without changing
  repository permissions.
- The run failures began after the unallowlisted references were introduced;
  this policy conflict is a likely cause, but requires a fresh hosted run to
  confirm. The older pins emitted Node 20 deprecation warnings but built both
  containers successfully on the earlier CI run.

## 2026-10-16 - Correct Drive archive recovery state and review PR security gate

- Fixed a false `externalSubmission: not_executed` result after Drive had already
  moved a verified file but the local archive-ledger commit failed. The result
  is now `not_executed` only when restore is positively confirmed; failed or
  ambiguous compensation reports `unknown`, with a regression covering disk-full
  ledger failure and a lost Drive restore response.
- Verification: the combined Drive/Wave and HAI regression group passed
  (`141 passed, 40 subtests`); `py_compile` and `git diff --check` passed.
  No live Drive or Wave mutation was performed.
- Refreshed PR #125 checks for its then-current head `592de35`: FAB verification
  passed on Linux and all four Windows shards, frontend, MySQL integration, both
  container builds, and CodeQL analysis language jobs passed. GitHub Advanced
  Security's aggregate `CodeQL` check nevertheless failed with six new alerts:
  five high-severity path-flow findings in `local_intake.py` and one medium
  stack-trace exposure in `local_api.py`. This is an active security blocker;
  the passing analysis jobs are not a clean security result. Triage and fix the
  findings before describing the PR as security-clean.
- The Drive recovery correction is pushed as `11d124b`; intake path confinement
  and non-reflective rule errors were pushed as `f3b17c3` and `4d8f1c0`.

## 2026-10-16 - Confine local intake paths after symlink resolution

- Hardened single-file and shared intake registration to resolve candidate and
  source-root paths before checking containment. Files resolving outside the
  configured root, including through a symlinked parent, are rejected before
  they are statted or hashed. Added a parent-symlink escape regression with a
  Windows fallback that simulates the resolved target.
- Verification at the time: local intake suite passed (`17 passed, 2 skipped`);
  combined intake, connector, Drive relay, photo picker, and upload API tests
  passed (`44 passed, 2 skipped`). Python compilation and `git diff --check` passed.
  Ruff is not installed in this checkout runtime.
- The then-current PR head `11d124b` still had five high path-flow and one
  medium exception-exposure CodeQL alerts. The fixes were pushed afterward and
  require hosted reassessment.

## 2026-10-16 - Stop reflecting vendor-rule exception details

- Vendor-rule API and form handlers now return a fixed conflict message instead
  of reflecting exception text. The valid conflict remains understandable to
  the operator without exposing internal exception details.
- Verification: vendor-rule resolution route tests passed (`3 passed`), local
  intake suite passed (`17 passed, 2 skipped`), and Python compilation plus
  `git diff --check` passed. The full intake/upload/connector group passed
  (`44 passed, 2 skipped`) after the path-boundary changes. These fixes are
  pushed at `4d8f1c0`; fresh hosted checks were pending at the last poll.

## 2026-10-16 - Bind recurring intake hashes to configured roots

- The shared hash helper now accepts the configured root and revalidates
  canonical containment before stat/open and after opening, including in
  recurring pending-change checks. This protects both import and change-detection
  paths when a candidate changes after directory walking.
- Verification: combined intake, connector, Drive relay, photo picker, upload,
  and vendor-rule route tests passed (`48 passed, 2 skipped`); compilation and
  `git diff --check` passed. This follow-up is local and needs hosted retesting.

## 2026-10-16 - Refresh PR state and continue security review

- Revalidated the protected canonical checkout at `dbe3a8a6383c88ad926a707ff7d8975561c99327`; its extensive staged user work remains untouched. The isolated PR #125 worktree is clean at `9571430366725dfe13f028cd975c8f1b5cacdc09` before this documentation correction.
- PR #125 remains open. Linux backend, frontend, MySQL integration, and both container builds pass on the recorded head; four Windows shards and CodeQL are pending. Private GHCR Actions Write access for both FAB packages was already granted.
- Continued source review of Flask auth/HAI, Drive-to-Wave archive gating, and backup restore/path boundaries. Focused Windows suites passed: `103 passed, 40 subtests` across HAI, Drive/Wave delivery, and backup inspection.
- Saved a provisional Codex Security Standard threat model and partial coverage checkpoint for the recorded PR head. The canonical scan draft remains incomplete; a low-severity HAI false-completion defect was validated, fixed, and preserved in a supplemental security-audit artifact. This is not a clean or complete repository-wide result. Independent review was unavailable because the agent thread limit was reached.
- Corrected stale C0/C7/C8 state in `CODEX_CHECKPOINTS.md` to distinguish the canonical checkout from the isolated PR worktree and reflect current check/security status. `git diff --check` passed. No provider writes, live data, or public tunnel were used.
- Pushed the checkpoint-only commit `9d921d2d4f27167ea22e478bbd62e6e99d11e20b` to PR #125. Refreshed GitHub state confirms the PR is open at that head; all new CI and CodeQL checks are pending. The previous code commit's Linux, frontend, MySQL, and container checks passed; Windows and CodeQL were pending.
- Deep HAI/API regression verification passed after the executor-result fix (`208 passed, 10 subtests`). The narrower HAI module passed (`25 passed, 8 subtests`). Ruff is not installed in the isolated venv; `git diff --check` and Python compilation remain the applicable local static checks.
- The Drive/Wave delivery regression suite passed with the fix (`52 passed, 32 subtests`). Pushed `e43e9e0e808a1f7a33934028f4ce7bcdfbe4124c` to PR #125; the fresh FAB verification run `38940384831` and CodeQL run `38940379696` were queued/pending at the latest check. No live archive operation was performed.

## 2026-10-16 - Revalidate published PR checkout on Windows

- Verified PR #125 is open at `04a006d059ad235ec0c03611f1b5691738f65a35`;
  its working tree is clean. PR #118 remains open. Release
  `v0.1.0-preview.1` exists with Windows and Compose ZIPs plus SHA-256 files;
  private GHCR packages `019-fab-api` and `019-fab-dashboard` each have a
  `0.1.0-preview.1` image tag. The repository has Actions Write access on both.
- On the PR checkout, full Windows Python verification passed (1,984 passed,
  6 skipped, 219 subtests) in 769.33 seconds. The web suite passed (471 passed,
  5 skipped across 40 files); `tsc --noEmit`, production Vite/server/standalone
  build and bundle budgets passed. The synthetic Windows dashboard/API/SQLite
  stack rehearsal passed all checks, including HAI auth/scope, audit replay,
  reconciliation, exception recovery, and owned-child shutdown.
- Three 30-request, six-client warm-cache synthetic control-center runs measured
  median/p95 latencies of 70.17/96.50 ms, 36.44/46.99 ms, and 37.89/45.82 ms.
  Each returned 57,768 bytes. Sampled combined API/dashboard working-set peaks
  were 191,123,456, 192,811,008, and 193,351,680 bytes. The first run was an
  outlier; this is diagnostic evidence on a one-document fixture, not a
  production SLA or scaled/cold/sustained resource baseline. The synthetic
  2,000x400 sparse reconciliation benchmark measured 2.662 s unprepared and
  40.23 ms indexed, with identical results; traced allocation peaks were
  651,704 and 848,007 bytes respectively.
- `git diff --check` passed. Build/dependency artifacts are ignored in this PR
  clone; no tracked source changes, provider writes, or public tunnel were used.
  A dependency setup attempt through a temporary junction was blocked by Windows
  `EPERM`; dependency installation and builds then succeeded inside this clone,
  and the canonical checkout's pnpm metadata and Vite entrypoint remain present.
  GitHub Linux, frontend, MySQL, and container checks pass; Windows and CodeQL
  checks remain queued, leaving PR #125 blocked.

## 2026-10-16 - Measure rehearsal process resources

- Added sampled CPU-time and working-set metrics for the isolated Python API and Node dashboard children to `scripts/run_stack_rehearsal.py`; unsupported/exited processes are reported as unavailable, not zero.
- The final Windows 30-request / 6-client warm-cache rehearsal passed all synthetic end-to-end checks. At 50 ms sampling, six samples measured a 189,747,200-byte combined peak working set (API 76,197,888 bytes; dashboard 113,549,312 bytes), dashboard CPU 0.359375 seconds, and API CPU 0.0 seconds at the reported precision. Dashboard latency was 49.56 ms median / 66.27 ms p95; response body was 57,768 bytes.
- The one-document fixture, warm cache, four-sample window, coarse sampling, and rate-limit-bounded request burst mean these figures are diagnostic only, not a scaled, cold, sustained, or application-wide resource baseline. An 80-request burst hit the existing rate limit; the combined rehearsal now rejects values above 30 before launching children.
- Verification: resource sampler tests passed (`4 passed`), including a real Windows process-counter read and CLI rate-limit-bound regression; the full synthetic Windows stack rehearsal passed and reported `ownedChildrenStopped: true`; the full Python suite passed (`1,980 passed, 6 skipped, 219 subtests`) in 723.75 seconds; `git diff --check` passed.

## 2026-10-16 - Precompute leave-one-out anomaly statistics

- Profiling still showed repeated sorting/median absolute deviation work for
  eligible amounts. Each vendor/category group now parses and sorts amounts
  once, then calculates exact leave-one-out medians and deviations using
  sorted order statistics. Duplicate IDs retain a brute-force compatibility
  path; ties and the existing anomaly scoring/output are preserved.
- A deterministic differential regression compared every precomputed
  leave-one-out result with brute force across 60 randomized rows, including
  duplicate IDs, invalid/zero/negative amounts, ties, and outliers. Controlled
  interleaved 15-run measurements against the original checkout implementation
  on 400 synthetic rows produced identical issue output; median fell from
  42.022 ms to 9.719 ms and max from 64.491 ms to 11.207 ms. With 100
  below-threshold rows, amount parsing fell from 20,000 calls to 200. These are
  service-level synthetic measurements, not whole-app CPU/RSS or throughput.
- Verification: full Python suite passed (`1,976 passed, 6 skipped, 219
  subtests`) in 748.66 seconds; control-center batch API integration tests
  passed. No provider actions or user-ledger access occurred.

## 2026-10-16 - Reduce repeated low-value anomaly analysis

- A profile of the uncached control-center batch with 400 synthetic
  bookkeeping records and 2,000 synthetic bank transactions showed anomaly
  analysis dominating the request. Low-value records were rebuilding vendor
  and category histories despite being below the configured anomaly threshold.
  They now return before history construction; anomaly scoring and evidence for
  eligible amounts are unchanged.
- Red-capable regression first failed at 20,000 amount-parser calls for 100
  below-threshold records; after the change it passes at no more than 200, while
  a high vendor outlier retains its prior median, sample count, ratio, and issue
  identity. A same-process, interleaved 15-run comparison against the prior
  implementation on 400 synthetic rows produced identical issue output and
  reduced anomaly-service median from 85.377 ms to 66.14 ms (max 130.187 ms to
  107.402 ms). This service-only fixture does not measure process CPU/RSS or
  establish an application-wide speedup.
- Verification: full Python suite passed (`1,975 passed, 6 skipped, 159
  subtests`) in 781.64 seconds. The control-center resource manifest also
  matches across frontend and backend (26 batch resources plus two direct
  reads). No provider actions or user ledger access occurred.

## 2026-10-16 - Triage dynamic SQL heuristics and lock down report exports

- Reviewed every B608 finding from a fresh Bandit scan. The 26 remaining hits
  are in `local_ledger.py`: query text is fixed or assembled from internal
  predicates, generated placeholders bind user/data values, and dynamic
  identifiers are syntax-validated or selected from fixed maps. No
  request/document-controlled SQL syntax path was found. Replaced the legacy
  reporting service's interpolated table name with fixed per-table SQL
  statements and added hostile-name/allowed-name regression tests.
- Verification: Bandit over `src` reported 0 high, 29 medium, and 16 low
  heuristic findings (26 B608, 2 B104, 1 B113). Manual review confirmed the
  B104 wildcard binds are explicit VM/container deployment paths gated by
  strong-token preflight, and the B113 Photos request supplies a request
  timeout. These are bounded heuristic matches, not a penetration-test result.
  Focused SQL tests passed (`5 passed`, 3 subtests); Ruff passed on the touched
  Python module and test; Bandit reported no finding in the reporting module.
  A fresh full Python run passed (`1,976 passed, 6 skipped, 159 subtests`) in
  776.57 seconds.

## 2026-10-16 - Preserve HAI external-operation outcomes

- Corrected HAI completion and idempotent replay responses to preserve the
  executor's validated `externalSubmission` state instead of reporting
  `not_executed` after a Drive archive may have run.
- For policy-gated commands, executor exceptions and executions lacking a
  durable outcome now report `unknown`; local-only commands remain
  `not_executed`. Added regression coverage for success, replay, and exception
  paths without performing a live Drive operation.
- Verification: the full Python suite passed (`1,955 passed, 5 skipped, 159
  subtests`); the web suite passed (`459 passed, 5 skipped`); direct TypeScript
  checking, Vite production build, configured production bundle budgets, both
  production server bundles, Python compilation, and `git diff --check` passed.
  The standard `pnpm check` wrapper hit a Windows `EPERM` while refreshing
  `.package-map.json`; running the installed TypeScript/Vitest binaries directly
  succeeded without changing the lockfile. No live Drive operation was performed.

## 2026-10-16 - Preserve Wave references for uncertain writes

- Wave's published GraphQL schema documents transaction creation but does not
  expose a business transaction list/readback or an `externalId` lookup; the
  returned Transaction object exposes only its ID. Automated provider absence
  and attachment verification therefore remain unsupported and fail closed.
- Ambiguous business/personal Wave writes and executor exceptions now retain
  the exact `fab:<operation>` external reference sent with the request. The
  export attempt stores it in execution metadata and shows it in the dashboard
  only as a search aid, never as a confirmed Wave transaction ID.
- Fixed the personal handler to classify missing/incomplete GraphQL mutation
  results as `outcome_unknown`; only an explicit provider rejection is treated
  as a definitive failure. Unknown attempts remain terminal and non-retryable.
- Verification: the full Python suite passed with `1,953 passed, 5 skipped,
  159 subtests` in 527.08 seconds, including the timeout → exact request
  reference → ledger persistence → no-retry API integration test. Python
  compilation and `git diff --check` passed. Live provider readback, attachment
  readback, automated reconciliation, and retry recovery remain
  unverified/unsupported.

## 2026-10-16 - Full-suite regression and security evidence

- A fresh full Python run initially exposed repeated autonomy workflow creation
  for an empty watched folder. Restored content-aware intake change detection
  and added a no-extra-hash fast path for empty folders; this preserves content
  hash checks for populated folders while avoiding empty-cycle workflow/audit
  growth.
- Full Python suite after the fix: `1,950 passed`, `5 skipped`, `159 subtests`
  in 554.30 seconds. Affected intake/worker modules passed `31 passed, 1
  skipped`; complete web suite passed `459 passed, 5 skipped`; TypeScript,
  production build/budgets, PowerShell AST parsing, Python compilation and
  `git diff --check` passed.
- Completed Codex Security diff scan `dacda496-dffd-4eed-9671-ff8730e566f3`
  against the exact working-tree snapshot. All 17 changed-file inventory items
  were reviewed; zero reportable findings. This did not cover live providers,
  ngrok, HAI runtime, or deployment acceptance. Report:
  `C:\Users\NO\.codex\state\plugins\codex-security\scans\019-FAB\dbe3a8a6383c88ad926a707ff7d8975561c99327_20261016T085229Z_atjno6di\report.md`.
- Read-only GitHub recheck: PR #118 remains open; its 13 displayed checks pass.
  The preview release and both private GHCR images are present; both grant the
  repository Actions Write. No commit, push, merge, release, provider mutation,
  or local user-ledger startup was performed.
- At the initial audit baseline the local API was not running. The checkout's
  `data/` contains real user-state files, including a 237 MB ledger; it remained
  untouched. Isolated runtime and browser acceptance performed afterward are
  recorded below.

## 2026-10-16 - Isolated Windows production acceptance

- Created a disposable source copy with its own Python 3.13.14 environment,
  empty intake folder, isolated SQLite ledger, and no copied provider credentials
  or user ledger. Hash-locked `requirements-local.lock` installation passed.
- The isolated production client/server/standalone build passed, including the
  2,432-module and bundle-size budgets. Production launcher acceptance
  initially exposed a Windows-specific bug: esbuild marks output artifacts
  hidden, `Test-Path` found them, but `Get-Item` without `-Force` treated them
  as absent. The launcher now reads hidden outputs and retries transient file
  metadata races before treating a build as stale. A regression test covers
  both hidden current artifacts and genuinely missing artifacts; the Windows
  launcher suite passed 16 tests.
- Production `Start-FAB.ps1 -NoBrowser` started API, dashboard, and worker.
  Authenticated `/api/live` returned `fab-ledger-api` status `ok`; dashboard
  returned HTTP 200; authenticated HAI status and manifest returned `ready`
  with 14 allowlisted commands and external submission not executed. Browser
  checks at 1280x720 and 390x844 found no horizontal overflow or console errors.
  Startup correctly reported unconfigured Gmail, Drive, and Wave credentials.
- `Stop-FAB.ps1` released the local-autonomy worker lease. All recorded service
  PIDs exited and test ports 3001/5001 were released. The isolated ledger only
  contained test/runtime state; the 237 MB project ledger was not opened. No
  live provider, tunnel, or cloud operation was performed.

## 2026-10-15 - Remove pandas from the bookkeeping runtime

- Replaced pandas CSV loading with the existing delimiter-aware standard-library
  reader, added read-only streaming `.xlsx` import through openpyxl, and removed
  the unsupported `.xls` file claim from the local importer.
- Reimplemented expense summaries, monthly trend aggregation, and forecasts
  without pandas; reports retain JSON-friendly `YYYY-MM` trend keys.
- Removed pandas from the runtime requirements and regenerated both hash
  locks. Each lock is 72 lines shorter; no OCR, Google, API, or provider
  functionality was removed.
- Focused verification after the day-first parser regression: 28 passed,
  2 skipped, 3 subtests; dependency-lock verification passed. The complete
  backend suite passed immediately before that parser-only addition
  (`1,932 passed`, `6 skipped`, `159 subtests`). Production image size
  verification remains pending.

## 2026-10-15 - Bound raster OCR memory and verify package workflow access

- Added shared raster edge/pixel limits before OpenCV decoding and bounded
  Google Vision uploads to 20 MB before provider requests. Tesseract image
  pages now use the shared dimension limit and reject multi-frame input.
- Routed Tesseract and OpenCV image decoding through the shared bounded
  validation path. Sanitized local image-open failures so returned OCR errors
  do not disclose receipt paths; size-limit errors remain specific.
- Focused document/OCR verification passed 43 tests and 16 subtests, Python
  compilation, and `git diff --check` for the touched paths.
- Full Windows Python suite then passed: 1,901 tests, 7 skips, and 152
  subtests in 1,490 seconds.
- A source sweep found raw chart config interpolation in an exported React
  `<style>` component. Extracted CSS generation into a pure helper, constrained
  chart IDs/series identifiers/color syntax, and added a malicious-input
  regression test. The complete web suite passed 450 tests with 5 skips;
  TypeScript and all production client/server/standalone bundle builds passed.
- Codex Security Deep Scan was requested for the whole worktree but did not
  start: its coordinator requires a managed filesystem permission profile,
  which this session could not grant. No security-scan result is claimed;
  local targeted source review and test/build verification continued instead.
- GitHub's Code Scanning API returned 101 open alerts on `refs/heads/main` at
  `8fbfeaebe305dd467a5eb8d179cc64f2ad3c2e24` (90 error-severity, 11 warning).
  Several findings have corresponding local hardening in this checkout, but
  the remote default-branch findings remain until changes are delivered and
  CodeQL reruns. Dependabot alerts are zero; this does not substitute for a
  worktree security scan.
- Added an API response-boundary guard that withholds diagnostics containing
  local absolute paths or Python traceback frames while preserving ordinary
  validation messages. The complete local-operations API module passed 112
  tests and 2 subtests after this change.
- Inspected both private GHCR package settings directly. `019-FAB` already has
  Write under Manage Actions access for `019-fab-api` and
  `019-fab-dashboard`; no external package permission mutation was needed.
- Read-only GitHub checks confirmed vulnerability alerts and private
  vulnerability reporting are enabled; CodeQL default setup is configured for
  Actions, C#, JavaScript/TypeScript, and Python; Dependabot security updates,
  secret scanning, and push protection are enabled. `FAB main protection` and
  `Restrict version tags` rulesets are active. Non-provider secret patterns
  and validity checks remain disabled and were not changed.
- Confirmed the public repository About description/topics are populated and
  the `v0.1.0-preview.1` prerelease already contains checksum-sidecar Windows
  and Compose bundles plus private API/dashboard images.
- These changes remain local and uncommitted. Hosted MySQL for the current
  uncommitted CI changes and live provider/deployment acceptance remain pending.

## 2026-10-15 - Verify GitHub branch CI and runtime contract

- Queried GitHub directly: `hardening/windows-vm-production` is at
  `c3ed1599b3807a4c900af4ed5b3aa8772f5a1a48`, 20 commits ahead of this
  checkout's `adc2aa8` base. Its Actions run 92 completed successfully across
  Linux, all Windows backend shards (including the Windows stack rehearsal),
  and frontend audit/type/test/build. This run predates the current uncommitted
  dashboard-polling and MySQL integration additions and does not verify them.
- The checked-in CI currently pins Node 22 and pnpm 11.20. Local default tools
  are Node 25/pnpm 11.25, while the bundled Codex tools are Node 24/pnpm 11.25;
  latest local build results therefore are not an exact Node 22 CI replay.
- No remote branch, PR, or workflow was changed or triggered.

## 2026-10-15 - Revalidate Windows cloud and HAI paths

- Re-ran the synthetic dashboard-to-Python-API-to-SQLite stack rehearsal against
  the current production bundle. It passed with loopback-only networking,
  synthetic records, provider writes disabled, and all owned child processes
  stopped. The checks exercised dashboard ledger mutations, bank import and
  reconciliation, authentication boundaries, HAI command/actor constraints,
  and stale-approval handling.
- Focused Windows ngrok/cloud-access and HAI/gateway regressions passed:
  41 tests and 6 subtests. These validate local scripts/contracts, not a public
  ngrok tunnel or live HAI/provider connectivity.

## 2026-10-15 - Pause operator polling while hidden

- Replaced the Operations dashboard's fixed query interval with the shared,
  tested visibility-aware poller. It keeps the existing 60-second refresh
  cadence while visible, pauses requests in hidden tabs, and refreshes on
  return; cleanup cancels timers and prevents component-unmount polling.
- Full web TypeScript checking passed and Vitest passed: 448 tests, 5 skipped.
  Prettier passed for the poller and its unit tests. The poller tests cover
  hidden/visible transitions, overlap prevention, callback errors, and cleanup.
- The production Vite build and asset budgets passed, along with both the
  Express server bundle and standalone FAB bundle.

## 2026-10-15 - Exercise dashboard tRPC through MySQL

- Extended the disposable-MySQL integration suite through the authenticated
  dashboard `bookkeeping.registerDocument` tRPC procedure, then read back the
  persisted document and actor-bound audit event. The test uses synthetic
  records and the existing loopback `fab_test` guard.
- Direct TypeScript checking passed. Focused Vitest passed 13 mocked database
  tests; all 5 real-MySQL cases skipped because no opt-in MySQL instance is
  available locally. The pnpm wrapper hit `EPERM` while writing its ignored
  generated package map, so verification used already-installed binaries.
- The hosted MySQL integration job has not run yet; no local or user database
  was accessed.

## 2026-10-15 - Worker interval resource bound

- Bounded worker cycle intervals to 10 seconds through 24 hours; invalid and
  non-finite values fall back to 300 seconds, preventing configuration mistakes
  from creating a hot loop or excessively delayed worker cycles.
- Added regression coverage for minimum/maximum clamping, invalid values, and
  the ten one-second sleep slices used for cooperative shutdown.
- Verification passed 62 worker/runtime tests and then 34 adjacent
  configuration/component/worker tests, with 2 skips and 11 subtests in the
  latter run. No provider operations or financial records were involved.

## 2026-10-15 - Source-of-truth and Windows cloud-access audit

- Added a separate CI job with a disposable MySQL 8.4 service that applies the tracked Drizzle migrations and exercises idempotent document registration, dashboard tRPC-to-MySQL persistence, workflow metadata compare-and-swap, review/audit transactions, and rollback on audit failure. The opt-in test refuses non-loopback hosts and databases other than `fab_test`. Full Vitest initially passed (`448 passed`, `4 skipped`); after adding the tRPC database case, the focused suite passed 13 mocked DB tests and skipped all 5 real-MySQL cases because this machine has no loopback MySQL listener. The hosted MySQL job has not yet run.
- Fresh full Windows backend suite after the connector path and ledger SQL identifier hardening passed: `1,886 passed`, `5 skipped`, and `137 subtests passed` in 887.15 seconds. This is local backend regression evidence for this checkout; frontend, live provider, ngrok, HAI-client, host deployment and external security-scan acceptance remain separate.
- Hardened the operations ledger's generic SQL helpers: dynamic table/column identifiers are now syntax-checked and quoted before insert, update, count, schema migration, or status-filter SQL construction; parameter values remain bound. Malformed identifier regressions and the existing ledger, operations API, and gateway contract suites passed (`155 passed`, `6 subtests`). The app-wide Codex Security Deep Scan was retried from the current workspace but its worker still refused to start without a managed filesystem permission profile; no Deep Scan report was produced.
- Hardened the legacy Banking API adapter by applying a finite 5-second connect / 20-second read timeout to transaction and balance requests. New focused regression tests plus the existing component test passed (`3 passed`); this guards against a stalled configured endpoint but does not establish live bank connectivity or PSD2 support.
- Removed Unix `/tmp` connector download fallbacks that could break standalone Windows runs or store source evidence outside the app data tree. Gmail, Drive, Freshdesk, Photos, and the base fetcher now resolve configured paths first and otherwise use `data/source_downloads[/<source>]`; configured paths are normalized with environment/user expansion. Fetcher and banking regressions passed (`24 passed`, `4 subtests`). A fresh Bandit medium/high scan of 63,033 Python lines reported zero high-severity findings and 29 medium candidates (down from 34); the remaining Photos B113 is a false positive because the request supplies an explicit timeout. SQL-builder candidates remain manually reviewable, not adjudicated vulnerabilities.
- Current verification refresh: after the reconciliation candidate-index change, the full Python suite passed (`1,887 passed`, `7 skipped`, `137 subtests passed`, 965.55 seconds). The first full Vitest run had three tests exceed the default timeout under concurrent CPU load; each passed in isolation and the full rerun passed (`448 passed`, 35 files). TypeScript checking passed. The Vite production build, asset budgets, Express server bundle, and standalone bundle passed. A fresh `scripts/run_stack_rehearsal.py --bundle-dir web/dist` passed all cross-layer checks using synthetic data, loopback-only networking, a simulated trusted proxy, provider writes disabled, and all owned child processes stopped.
- Reduced reconciliation scans for sparse amount ranges using a per-call sorted amount index while retaining the existing scorer, candidate ordering/tie behavior, custom-scorer fallback, dense-range fallback, and a full scan when Decimal range arithmetic could round at the active precision. A reproduced large-decimal boundary mismatch now has a regression test. Focused matcher/financial/component checks passed (`65 passed`). Synthetic benchmark medians were 4.17 ms versus 86.27 ms for 500x100 different amounts, 14.64 ms versus 107.11 ms for dense ties, and 33.79 ms versus 1.91 s for 2,000x400 sparse rows; all results were identical. The compared baseline is the unprepared scorer. `tracemalloc` peak rose by about 193 KB on the larger fixture; no whole-app memory or production SLA claim is made.
- Verified both private GHCR packages (`019-fab-api` and `019-fab-dashboard`) in their GitHub package settings: `Robert-Velhorst/019-FAB` already has **Write** under **Manage Actions access** for each. No permission change was needed; repository Actions access is confirmed, while a new release/tag and an Actions publish run have not been performed in this verification pass.
- Added a Python dependency vulnerability gate to CI using the official pip-audit action pinned to commit `1220774d901786e6f652ae159f7b6bc8fea6d266`. A local strict audit of `requirements.lock`, `requirements-local.lock`, and `requirements-test.lock` with pip-audit 2.10.1, `--require-hashes`, and `--disable-pip` found no known vulnerabilities. Workflow YAML parsed successfully; the linter reported only existing line-length warnings.
- Standard and VM Docker Compose configuration parsed successfully using process-scoped synthetic values only. PowerShell's AST parser accepted `Start-FAB.ps1`, `Stop-FAB.ps1`, `Start-FAB-Ngrok.ps1`, `Stop-FAB-Ngrok.ps1`, and `scripts/Windows-Profile.ps1`. No FAB service, ngrok tunnel, or external provider operation was started by these checks.
- `pnpm.cmd --dir web check` materialized the locked dependencies but failed before type-checking while writing ignored `web/node_modules/.package-map.json` (`EPERM`). Direct execution of the installed TypeScript, Vitest, Vite, production-budget, and esbuild binaries completed successfully. This environment-specific pnpm wrapper failure is not represented as a code-test failure.
- The Codex Security Deep Scan could not start because its worker requires a managed filesystem permission profile unavailable to this session. No repository-wide security scan or security finding set was produced; local tests and the synthetic rehearsal do not substitute for that scan or for live provider/deployment acceptance.

- Deep Windows standalone rehearsal uncovered and fixed three startup/shutdown defects: hidden dashboard inputs are now included in build-freshness checks; scoped service credentials remove empty `*_FILE` variables (including the operations token file) rather than passing invalid empty paths; and worker runtime PID is now distinct from the contained process-root PID in ownership metadata. Added regression coverage for environment restoration, hidden-input launcher behavior, and worker descendant/root validation.
- Focused Windows launcher and shutdown tests passed (`64 passed`). An isolated tracked-source copy with a hash-checked Python 3.13 runtime started in the Windows profile on loopback-only synthetic paths; the authenticated API identified `fab-ledger-api`, the dashboard returned HTTP 200 with matching instance identity, and normal `Stop-FAB.ps1` shutdown removed runtime metadata and closed both listeners with no recorded worker process remaining.
- The rehearsal disabled all provider connectors and did not mutate Wave, Drive, Gmail, or other external records. To exercise startup beyond the deployment preflight, only the temporary test process used `FAB_STORAGE_ENCRYPTION_CONFIRMED=true`; this is not evidence that the host volume is encrypted. Provider acceptance and host encryption remain unverified.
- Fixed a control-center failure-path inefficiency: if the shared metrics projection failed, the compliance resource was omitted and retried through the generic per-route fallback, which depends on the same failed metrics path. The batch now emits an explicit failed compliance envelope and skips that redundant retry. Route-contract tests passed (`3 passed`); the existing shared-projection/compliance API tests passed (`2 passed`).
- Replaced the website admin overview's optional-MySQL "Review Backlog" count with a direct link to the live FAB Operations console. The active console is wired through the web gateway to the Python local API and SQLite ledger; the legacy MySQL bookkeeping procedures remain but have no current frontend callers, and `OperationsClient` has no production caller. No data or legacy endpoint was removed.
- TypeScript `tsc --noEmit` passed when invoked directly. `pnpm.cmd check` could not complete because pnpm received `EPERM` writing ignored `web/node_modules/.package-map.json`; it had materialized the locked dependencies before failing. `git diff --check` passed.
- Managed ngrok regression module passed (`6 passed`), and PowerShell parsed the ngrok start/stop/verifier plus main FAB start/stop scripts without syntax errors. This verifies local script contracts only; no live ngrok tunnel, public endpoint, or cloud deployment was started.
- HAI connector, authenticated-actor, integer-contract, and payload-snapshot regressions passed (`80 passed`, `6 subtests`). Source review confirmed Windows startup provisions separate encrypted operator and HAI tokens, and HAI requests are route-allowlisted. This is local synthetic evidence only; no remote HAI client connected.
- Verified both private GHCR package settings directly: `019-FAB` is listed under each package's "Manage Actions access" with `Write`; no permission change was needed. GitHub warns that public-repository forks may then access private packages. The release workflow remains local and has not been published to GitHub `main`.
- Added `--bundle-dir` to the stack rehearsal runner so production bundles can be built outside the existing `web/dist` artifacts. A fresh Vite/esbuild build in a unique temporary directory passed all 67 synthetic end-to-end checks, including auth/HAI scopes, source-byte integrity, ledger persistence, review, bank import/reconciliation, and owned-process shutdown. The run used loopback only and made no provider writes; `web/dist` was not changed.
- Refreshed frontend verification on current source: `tsc --noEmit` passed and Vitest passed (`448 passed`, 35 files). The complete Python suite initially found a Windows Docker teardown timeout at the 30-second cleanup bound; increasing test-owned container/image cleanup timeouts to 90 seconds resolved it. The focused Docker/build-context module passed (`9 passed`), then the complete backend suite passed (`1,872 passed`, `5 skipped`, `130 subtests`, 747.63 seconds). The synthetic container and image from the failed run were removed after verifying their unique test names, and a follow-up read-only inventory found no `fab-context-test-*` containers or images. Local checks only; live providers and production deployment were not exercised.
- Refreshed CI dependency checks: `pnpm audit --audit-level=high` found no known vulnerabilities and `pnpm peers check` found no peer issues. Production budget verification now accepts an optional public-output directory, retaining the original `web/dist/public` default; the isolated fresh build passed at 2,031-byte HTML and 475,938-byte largest JavaScript asset (512 KiB ceiling).

## 2026-10-14 - Current worktree verification refresh

- Final post-change full backend suite passed: `1,853 passed`, `6 skipped`, and `124 subtests passed` in 1,419.26 seconds. The Wave exception-boundary changes redact provider/request details and classify transport/parse failures as `external_submission=unknown`, making the same failed attempt non-retryable; the dashboard attention aggregate counts these outcomes. Focused data-entry/export/executor/ledger coverage passed (`77 passed`, `18 subtests`), and the added end-to-end unknown-outcome regression passed separately. Python compilation and `git diff --check` passed. This is local evidence only; no live Wave/Gmail call or current browser/Windows runtime acceptance occurred.
- Fixed stale export-review recovery by creating an idempotent review item for failed/review-blocked processing and recomputing Wave review eligibility from current source data while excluding only the same attempt's Wave review. Gmail consent-marker cleanup retries transient Windows sharing violations and fails closed if cleanup remains blocked. Focused export/Gmail/OAuth tests passed (`32 passed`, `15 subtests`).
- Fixed a downstream status fidelity bug found by the complete backend run: a supervised MijnGeldzaken result recorded as `submitted` was projected as `executed`. The result ledger now preserves `submitted` distinctly (including its audit-facing message), and the master-ledger regression exercises the persisted-artifact SHA-256 attestation, verifies that the unsupported `queued` result is rejected, and checks the final projection. Full backend suite passed: `1,847 passed, 4 skipped, 120 subtests` in 827.32 seconds; focused export/master-ledger suite passed: `23 passed, 11 subtests`.
- Removed `execute_approved_exports` from the generic safe-autonomy action graph and runner, so neither the FAB local safe cycle nor HAI `run_safe_cycle` can invoke Wave/MijnGeldzaken export execution even when `fab_autonomy_execute_approved_exports` is enabled. The flag remains documented for the dedicated operations export worker. Historical workflow-recovery records for this high-risk action still surface as approval-required and are not retried automatically. Regression tests cover both generic and HAI entrypoints with an approved synthetic export; the autonomy, HAI, recovery, and worker suites passed (`81 passed`). This is local and uncommitted; GitHub issue #103 remains open until the fix is delivered and verified there.
- Added a target-specific manual-result boundary: Wave/unknown targets cannot be marked successful or failed through the generic result API/form; MijnGeldzaken attestation requires `supervision_required` and the exact persisted artifact SHA-256. Only a fixed source label and artifact hash are retained as result evidence, external references are bounded, and the approved Wave UI no longer exposes a manual success form. Approval, rejection, and supervised-attestation transitions now run in serialized ledger write transactions; rollback fault injection and a result-versus-rejection race test cover atomicity. Export API actor labels now pass through principal-aware audit attribution. Focused export/API/worker/autonomy/HAI/recovery coverage passed (`210 passed`, `11 subtests`). Issue #102 remains incomplete: canonical Wave provider-result finalization and deterministic Wave approval-state binding are still required.
- Fixed a path-disclosure issue in the legacy authenticated backup routes: audit `entity_id` and `after_json` previously persisted absolute archive/source/restore paths, while failed backup results could persist raw filesystem errors. Audits now record fixed entity IDs, safe outcome status, and (for successful creation only) the number of included sources; API responses and restore behavior remain unchanged. Added a route-level synthetic backup/restore regression that first failed on the stored absolute path, then passed while also verifying restored bytes. Legacy dashboard auth tests passed (`13 passed`); combined backup manager and dashboard regression selection passed (`35 passed`, `2 skipped`, `3 subtests`, 235.38s). Python compilation and scoped `git diff --check` passed.
- Extended the backup audit regression to inject a failure result containing an absolute path and prove that the path is not copied into the audit ledger. The final legacy dashboard suite passed again (`13 passed` in 132.59s) with the success, restore-integrity, and failure-redaction assertions active.
- Re-ran the HAI connector and managed Windows ngrok wrapper suites on the current tree: `20 passed` in 27.62s. This covers dedicated HAI bearer-token scoping, denied unlisted routes, fail-closed command allowlists, and project-owned ngrok start/stop behavior. No live HAI command, tunnel, or provider operation was started.
- Added a dynamic authentication contract for every non-public GET route in the legacy Flask dashboard. The regression enumerates the actual Flask route map, denies each sensitive read without a session/token, and confirms the intentionally public health response remains minimal. The exhaustive route test passed (`1 passed` in 2.42s); no real ledger data was read.
- Bounded raster OCR inputs to 25 million pixels and reject multi-frame images rather than allocating unbounded image memory or silently processing only the first frame. Added oversized-image and multi-frame regressions; OCR processor, resource-bound, and Poppler suites passed (`49 passed`, `9 subtests passed`, 69.90s). This is an input/resource safety limit, not a throughput benchmark.
- Added an end-to-end ledger regression proving oversized-image rejection preserves source bytes, creates a processing-failed review, leaves export blocked, and never calls OCR; combined integration and resource-bound check passed (`15 passed`, `9 subtests passed`).
- Windows PowerShell's AST parser accepted all six root `.ps1` entrypoints, including hidden files. Focused Windows launcher, lifecycle-lock, and managed-ngrok tests passed: `25 passed` in 47.24 seconds. The ngrok endpoint/Basic Auth YAML shape matches the vendor's documented pattern ([ngrok Basic Auth example](https://ngrok.com/docs/gateway/examples/ollama)); this is configuration-shape evidence only, not a live tunnel acceptance test.
- Hardened local API audit attribution: HAI requests retain the server-assigned HAI identity, while operator-supplied actor labels are marked as claims under the authenticated operator principal and control characters are normalized. This prevents a caller from making an operator API action appear to originate from HAI or forging multiline audit labels. Local API and HAI connector suites passed: `122 passed` in 169.98 seconds.
- Re-ran the complete Python suite on branch `hardening/windows-vm-production` at base commit `adc2aa8`: `1,828 passed`, `6 skipped`, `118 subtests passed`, and one timeout in the PowerShell Job Object rollback fixture after 24m13s. No process from the failed fixture remained. The exact rollback case passed for both PowerShell shells on isolated rerun, and the full containment module passed (`18 passed` in 46.87s) with pytest cache disabled; the full run also reported `.pytest_cache` access denied.
- Closed the same actor-spoofing path across JSON mutation routes that passed actor labels directly to services: the request boundary now binds submitted labels to the authenticated principal and writes the sanitized identity back into the parsed payload. Added direct-route regression coverage for notification refresh, and updated legacy audit assertions to require explicit `unverified_claim` provenance for tokenless loopback requests. HAI connector and local operations API suites passed: `123 passed` in 158.40 seconds; the six changed audit cases passed again (`6 passed`), Python compilation passed, and `git diff --check` was clean.
- Fixed the Node gateway's message-only upstream error handling: FAB Operations responses use `message` with `errorCode`/`requestId`, while the gateway previously surfaced only `error` and replaced these with a generic HTTP status. The gateway now falls back to the sanitized `message` field without allowing body codes to override HTTP classification. `fabLocalGateway.test.ts` passed (`64 tests`) and `tsc --noEmit` passed.
- Added a Python/Flask-to-TypeScript gateway contract test covering all control-center read paths, all operator command paths and methods, and the actual batch-resource projection. This catches drift before it reaches runtime; 41 unique gateway paths currently resolve, and both contract tests passed.
- Re-ran the web tests from the materialized locked dependency tree after the concurrency fix: `428 passed` across 35 files. `tsc --noEmit` passed. The isolated Vite production build passed the same HTML/JavaScript budgets (2,031-byte shell; largest JavaScript asset 475,938 bytes), and both Express server entrypoints bundled successfully to temporary output without replacing the running dashboard's `dist` files.
- Standard Compose and the maintenance override both validated when invoked as a layered project with synthetic-only validation secrets; the override is not a standalone Compose project.
- An initial ad-hoc route probe was discarded after finding it misread Werkzeug's `Rule.build()` return tuple and therefore did not exercise the intended paths. Added durable Flask route-map regression tests for missing operator credentials and the HAI credential's exact eight-rule allowlist. They exercise every registered API method, check login/bootstrap exceptions, fail if API coverage drops below 100 routes, and skip allowlisted writes. The focused HAI/operator/dashboard auth suite passed: 32 passed in 50.52 seconds.
- The normal pnpm test command could not complete its dependency-status check because Windows returned `EPERM` while opening `web/node_modules/.package-map.json`; no packages were downloaded. Direct execution of the already materialized Vitest, TypeScript, Vite, and esbuild binaries passed.
- Fresh local runtime inspection found no FAB API listener on ports 5001/8001 and no FAB dashboard listener on 3005. Port 8001 returned `Python/3.11 SimpleHTTPServer` 404 for `/api/live`; port 3000 is owned by `com.docker.backend` but returned no HTTP bytes. `com.docker.service` was stopped and a read-only Docker CLI inventory hung. Only the CLI process started by this audit was stopped; no service or container was changed, so live UI acceptance remains blocked pending an operator-approved runtime start.
- GitHub PR #118 (`codex/first-fab-release-package`, head `19aa325`) has all reported Actions checks passing. The existing preview release is `v0.1.0-preview.1`; no GHCR images were published, and the local Docker builders were unreachable at that checkpoint.
- Fixed the admin tRPC bookkeeping path so workflow-run updates reject empty payloads and return `NOT_FOUND` when the target run is missing. Redacted workflow metadata/error text, source-document IDs, and free-form review reasons from audit details; audit records retain changed field names and safe classification fields.
- Replaced Gmail, Google Drive, and Wave executor status intervals with a shared visibility-aware poller. Polls do not overlap, pause while the browser tab is hidden, resume immediately when visible, and surface refresh errors in the drawer. This avoids invalidating the cached control center every 2.5-5 seconds during long authorization or executor sessions.
- Moved document registration/updates, workflow-run creation/updates, review-item creation, routing attempts, and reconciliation-match creation behind database transactions that insert their audit event before commit. Wired the same transaction boundary through the tRPC and HAI/service paths, including autonomous run creation/claiming, executor cycles, executor loops, operation updates, and autonomous-review exceptions. Missing-row updates produce no audit event, and audit storage failures roll back the corresponding financial operation.
- Added an optimistic compare-and-swap predicate on the prior workflow metadata for autonomous operation claims, cycles, loops, and status updates. A stale concurrent worker now receives a conflict rather than overwriting another worker's operation state; unsuccessful compare-and-swap updates emit no audit event.
- Verification for this follow-up passed the full 428-test web suite, `tsc --noEmit`, `git diff --check`, and a temporary production Vite build within budgets (2,031-byte HTML shell; largest JS asset 475,938 bytes). Both Express server entrypoints bundled successfully to temporary output. Database tests verify audited writes share a transaction, missing-row writes do not leave orphan audit rows, audit failure propagates so the DB rolls back, and the generated MySQL predicate compares expected prior metadata. A route test verifies stale claims return `CONFLICT`. The redundant Python rerun was stopped at 31% because this follow-up changed no Python files; the prior full Python result above remains the available evidence. The mysql2 default `FOUND_ROWS` flag was inspected directly, disproving a suspected no-op-update/404 defect; no change was made for that non-issue.
- This checkpoint verifies local code and build behavior only. Live Google/Wave acceptance, a dedicated ngrok endpoint, multi-tenant authorization, formal penetration/DPIA/accountant review, and the managed-profile security scan remain unverified or externally blocked. No provider records or Drive files were changed.
- Made Wave attachment evidence, document lifecycle metadata, review resolution, and their audit event atomic in the local ledger. Made Drive archive lifecycle metadata and archive audit atomic; on a local commit failure FAB attempts to restore the Drive source and records a blocked review state. Lease-heartbeat exceptions now immediately mark ownership uncertain so post-move verification fails closed and triggers source restoration. Added failure-injection and rollback regressions; the complete Drive/Wave delivery module passed (`49 passed`, `32 subtests passed`, 29.40s). No live provider records or files were changed.
- Refreshed the broad frontend verification: the first complete Vitest run finished with `428 passed` and one timeout in `server/_core/deploymentStartup.test.ts` while waiting for the production runtime identity endpoint. The exact test file passed alone (`8 passed`), confirming a timing-sensitive readiness limit under suite load. Increased the bounded wait from 20 to 45 seconds and the enclosing test timeout from 30 to 60 seconds; the complete rerun then passed (`429 passed`, 35 files, 42.42s). `tsc --noEmit` passed after the adjustment.
- Confirmed PR #118 is open and unmerged and all currently reported GitHub Actions/CodeQL checks pass. Refreshed the active GitHub CLI credential after the user's device-flow approval; `gh auth status` now reports exactly the requested additional `write:packages` scope. The existing `v0.1.0-preview.1` release tag resolves to `main` commit `3e74dbcf5bb4158888ef550797c79bc564e5d291`.
- Attempted the private GHCR bootstrap from a separate clean checkout of that release tag, leaving the dirty working tree untouched. GitHub still reports no `fab-api` or `fab-dashboard` package. Docker's `default` and `desktop-linux` builders both timed out at the engine ping (`context deadline exceeded`), so the build/publish command was stopped before an image was published. Only the audit's own stalled CLI commands were canceled; no Docker service or existing container was changed. No package visibility or Actions-access setting was changed.
- Rechecked local listeners for runtime acceptance: `127.0.0.1:3000` is occupied by a non-FAB service whose HTTP root identifies `LARO API`; no listeners were found on 3005, 5001, or 8001. FAB's Windows launcher explicitly rejects occupied production ports and does not silently choose another port or stop an unknown process. Preserve the other app; use an explicit unused `PORT` (3005 was free at this observation) only after runtime startup is authorized. No other app or service was modified.
- Verified the Windows launcher conflict policy using the PowerShell AST/launcher checks and isolated startup preflight: `4 passed` in 19.78 seconds. These checks confirm startup validation precedes service creation and the Windows profile rejects invalid/changed ports; they do not constitute a live FAB startup or browser acceptance test.

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

## 2026-10-16 - Windows stack rehearsal and dependency-audit coverage

- Re-ran the built production dashboard, Flask API, and SQLite ledger together
  on Windows using `scripts/run_stack_rehearsal.py` and isolated synthetic data.
  The complete rehearsal passed, including managed login/session revocation,
  HAI credential scoping and actor binding, verified source-byte delivery and
  tamper rejection, statement import, reconciliation approvals, stale-evidence
  rejection, and owned-child process shutdown.
- The rehearsal explicitly reports `syntheticOnly`, `loopbackOnly`, and
  `providerWrites: false`; it is local integration evidence, not public ngrok,
  live-provider, or cloud-host acceptance.
- Found that CI's Python dependency audit omitted `requirements-ml.lock`.
  Updated the existing audit command to cover all four lock sets and regenerated
  the stale `platformdirs` pin in `requirements-test.lock` with the repository's
  pinned lock compiler.
- Verified all Python lockfiles with `scripts/compile_python_locks.py --verify`
  and audited all four sets with hash enforcement; no known vulnerabilities
  were reported. `git diff --check` passed with only existing line-ending
  normalization warnings.
- Fresh focused regressions passed: 101 Python tests, 8 subtests, and 2
  optional skips across document processing, intake, cloud access, HAI, and
  master-ledger suites; 54 web tests passed across auth/logout, managed auth,
  public-form rate limiting, and Stripe customer ownership.
- No application provider writes, live tunnel changes, commit, or push were
  performed.

## 2026-10-16 - Stripe customer lookup fail-closed

- Traced a duplicate-customer risk in the checkout path: transient failures
  retrieving a stored Stripe customer or searching by email were caught and
  followed by a new customer creation. The search warning also logged the
  customer's email address.
- Added tests that reproduced duplicate creation on both failure paths. The
  helper now propagates transient lookup failures and only replaces a stored
  customer when Stripe returns HTTP 404 with `resource_missing`; removed email
  from lookup warning context.
- Regression verification passed: `stripe.customer.test.ts` (5 tests) and
  `tsc --noEmit`. The focused test was observed failing against the old behavior
  before the fix and passing afterward.
- After the fix, the full web suite passed (468 tests, 5 skipped); the Vite
  asset-budget check and both general/standalone server bundles built. The
  Windows production-stack rehearsal passed again against these freshly built
  artifacts and stopped all owned child processes.
- A whole-repository Codex Security Deep Scan did not start: the scanner
  requires a managed read-only filesystem profile, and the scoped permission
  request returned no filesystem grant. The earlier diff scan remains partial;
  no whole-repository scan result is claimed.

## 2026-10-16 - Reconciliation allocation experiment

- Re-ran the synthetic reconciliation benchmark on Python 3.13.14; the indexed
  2,000x400 sparse case remained about 19-20 ms median with identical output.
- Tested replacing the per-row candidate-index sort with a lazy generator and
  explicit original-index tie-breaking. The 24 reconciliation equivalence and
  tie-order tests passed, but the sparse-case median regressed to 37.91 ms and
  traced peak allocation did not improve (`848,199` versus `848,007` bytes).
- Reverted that experiment. The original candidate ordering and faster indexed
  path are retained; no matcher code change is part of this experiment.

## 2026-10-16 - Stripe concurrent-customer idempotency

- Added a stable per-user Stripe idempotency key to customer creation. A
  regression simulates two concurrent callers that both see an empty customer
  search and verifies the same key is sent for both requests; it failed before
  the change and passes afterward.
- Replaced the first-page-only email lookup with the SDK's streaming
  `autoPagingEach` and early stop when `metadata.user_id` matches. A regression
  with 100 unrelated matches before the owned customer reproduced the previous
  false miss and now passes without creating a new customer.
- Stripe's official API reference documents idempotent POST result reuse for
  matching parameters and notes keys may be pruned after at least 24 hours. This
  reduces short-window duplicates but is not permanent database uniqueness or
  provider readback; no live Stripe request was made.
- Verification passed: all 470 web tests (5 skipped), TypeScript check, and
  production client/general-server/standalone-server build with asset budgets.

## 2026-10-16 - Reject nested encoding in public tRPC paths

- The public-form limiter decoded `req.originalUrl` once. A direct request to
  `/api/trpc/contact%252Esubmit` therefore evaded its standard 20-per-15-minute
  limiter and passed to downstream routing; the regression returned HTTP 200
  instead of rejecting the ambiguous path.
- Added a fail-closed 400 response when a tRPC path still contains a percent
  escape after canonical decoding. Double- and triple-encoded procedure names
  are now rejected before either the public form limiter or tRPC routing.
- The regression failed before the code change and passed afterward. Full web
  verification passed: 471 tests, 5 skipped; TypeScript check and client,
  general-server, and standalone-server production build with asset budgets.
- No live provider or ngrok request was made.

## 2026-10-16 - Concurrent Windows dashboard baseline

- Extended the isolated Windows production-stack rehearsal with opt-in
  `--benchmark-requests` and `--benchmark-concurrency` arguments. It measures
  authenticated full control-center reads, reports median/p95/max latency and
  response size by projected field, and closes its per-thread HTTP sessions.
  The default integration rehearsal remains unchanged when the option is not
  supplied.
- Rehearsed the final harness against the production dashboard bundle, real
  Flask API, and disposable synthetic SQLite ledger: all end-to-end checks
  passed, provider writes were disabled, and owned child processes stopped.
- Thirty warm-cache control-center reads at six concurrent clients measured
  33.17 ms median, 50.34 ms p95, and 67,105 uncompressed response bytes. The
  largest projected sections were autonomy (12,081 bytes) and HAI (10,226
  bytes). This one-document integration fixture does not establish cold-cache,
  scaled-ledger, CPU/RSS, or sustained-load performance.
- No real user ledger, provider account, or public ngrok tunnel was used.

## 2026-10-16 - Trim unused HAI manifest from dashboard refresh

- The measured warm control-center response included a full HAI manifest even
  though the operator UI only needs HAI connection status; its connector drawer
  opens `/api/hai/manifest` directly. The manifest therefore duplicated data
  on every dashboard refresh.
- Removed that manifest from the dashboard gateway type/projection and the
  Flask control-center batch. HAI status remains available in Connections, and
  the standalone manifest route and HAI authentication contract are unchanged.
- A regression first failed because the manifest was still projected; after
  the change, all 64 `fabLocalGateway` tests passed. Python route-contract and
  local API suites passed (121 tests, 2 subtests); the full web suite passed
  (471 tests, 5 skipped), TypeScript check and production builds passed.
- Repeated the isolated Windows production-stack benchmark with 30 concurrent
  warm-cache refreshes at concurrency 6. The response fell from 67,105 to
  56,750 bytes (15.4%); post-change median/p95 were 31.20/37.26 ms. This is a
  one-document synthetic workload, not a scaled or sustained performance
  claim. All rehearsal checks passed and owned processes stopped; no real user
  ledger, provider, or public tunnel was used.

## 2026-10-16 - Bound master-ledger lookup for older SQLite builds

- The projection's latest-export lookup used two bound SQL variables per
  selected record. A 500-record projection exceeded SQLite builds configured
  with the legacy 999-variable limit. A regression test constrained the live
  SQLite connection to 999 and reproduced `too many SQL variables` before the
  fix.
- The lookup now processes at most 400 records per query on one reused
  connection. Typical small projections remain one query; larger projections
  stay below the legacy SQLite parameter cap without changing output ordering.
- Verification passed: `tests/test_local_master_ledger.py` and
  `tests/test_local_operations_ledger.py` (47 tests, 3 subtests). The Windows
  production-stack rehearsal also passed all checks, with 30 concurrent warm
  dashboard reads at concurrency 6 (33.03 ms median, 51.60 ms p95, 56,750
  response bytes). It used a synthetic one-document ledger; no provider writes,
  live ledger, or public tunnel were used.

## 2026-10-16 - Full current-worktree verification

- The complete Python suite passed on the current worktree: 1,965 tests passed,
  6 skipped, and 159 subtests passed in 719.36 seconds. The current web suite
  passed 471 tests with 5 skipped across 39 passing files and 1 skipped file.
- TypeScript checking and the Vite, production-budget, general-server, and
  standalone-server builds passed. The full pnpm advisory audit and its
  production-only form reported no known vulnerabilities. The synthetic
  Windows production-stack rehearsal passed its complete end-to-end checks.
- A repository-wide Codex Security Deep Scan did not start because its worker
  requires a managed read-only filesystem profile that this session could not
  obtain. The local `.venv` also does not contain the `pip-audit` module, so no
  fresh Python advisory scan is claimed here. No live provider, account, or
  public tunnel was exercised.

## 2026-10-16 - Bound Drive archive hashing memory

- `DriveArchiveClient.download_sha256()` used `BytesIO` with the Google client
  library's 100 MiB default download chunk, retaining each complete receipt in
  memory. Archival hashes the source before and after the move, so large
  documents could cause repeated high transient memory use.
- Replaced the full in-memory buffer with an incremental SHA-256 sink, set the
  downloader chunk size to 8 MiB, and enforce the existing 100 MiB source
  document limit while bytes arrive. This bounds retained application memory
  and rejects an unexpectedly oversized provider response before proceeding
  with archive verification.
- Verification passed: Drive archiver and Drive-to-Wave suites (56 tests, 32
  subtests). No live Google Drive request or archive move was made.

## 2026-10-16 - Isolated Python dependency advisory audit

- The repository `.venv` did not include `pip-audit`, so ran the pinned
  `pip-audit==2.10.1` through `uvx` rather than modifying the FAB environment.
  Audited all four hash-locked sets (`requirements.lock`, `requirements-local.lock`,
  `requirements-ml.lock`, and `requirements-test.lock`) with dependency
  resolution disabled and hashes required.
- Result: no known vulnerabilities found. This checks package advisories for
  the locked Python dependencies; it does not replace source security review or
  live deployment testing.

## 2026-10-16 - Recover ambiguous Drive archive move responses

- The archive service marked a move as started only after `move_file()`
  returned. If Drive applied the move but the response timed out, the caller
  skipped compensation and reported `not_executed`, despite an unknown remote
  state. Regressions reproduced both the missing restore and false status.
- The service now records the attempt before calling Drive and tries an
  idempotent restore after every ambiguous move failure. A verified restore
  reports `not_executed`; absent/failed/unverified compensation adds
  `archive_source_restore_failed` and reports `unknown` in the API response and
  audit event, while opening the review gate.
- The Flask archive route had separately hardcoded every failed result to
  `not_executed`; an API regression reproduced this state loss. The route now
  preserves the service's `unknown` result and defaults unexpected outcomes to
  `unknown`, without exposing raw failure details.
- Verification passed: `tests/test_local_operations_api.py`,
  `tests/test_drive_archiver.py`, and `tests/test_drive_wave_delivery.py` (177
  tests, 34 subtests). No live Drive move was attempted.

## 2026-10-16 - Bind Windows ngrok verification to the FAB checkout

- The temporary Windows ngrok verifier checked only the API service name before
  exposing the local port. A different FAB instance could therefore satisfy the
  preflight without proving it was the checkout being verified. The verifier
  now requires strong runtime credentials, validates a plain HTTP loopback API
  origin, and matches both local and tunneled liveness identity/authentication
  to the current checkout before reporting success.
- Quoted both ngrok config paths passed through `Start-Process`, so user-profile
  or temporary-directory paths containing spaces remain single arguments.
- Verification passed: `tests/test_ngrok_windows_scripts.py` and
  `tests/test_local_cloud_access.py` (31 tests). The initial combined run emitted
  a pytest cache permission warning; tests themselves passed. No public tunnel
  was started.

## 2026-10-16 - Remove unused OpenCV from the core runtime

- Repository search found no OpenCV imports; core OpenCV was retained only by
  a stale requirement comment. The previously tagged API image's installed
  `cv2` and OpenCV native-library directories occupied about 153 MB. Removed
  OpenCV from `requirements.txt`, regenerated the hash locks, and updated the
  dependency guide. NumPy is no longer pulled into core/test locks and remains
  isolated in the optional ML lock.
- `scripts/compile_python_locks.py --verify` passed. The current dirty-checkout
  ML-disabled container built successfully at 140,805,983 bytes versus
  242,575,002 bytes for the tagged older image (about 42% smaller; not a
  controlled comparison because the source and Python base revisions differ).
  Container checks confirmed core/OCR/intake imports work without OpenCV or
  NumPy, and authenticated `/api/live` returned `ok`. The temporary container
  used an isolated `/tmp` SQLite ledger and was stopped; no project data volume
  was mounted. Focused OCR/intake tests passed (54 tests, 2 skipped, 9
  subtests).

## 2026-10-16 - Preserve CAMT batch transaction amounts

- Deep import-path review found CAMT parsing reduced each `<Ntry>` to a single
  row, even when it contained multiple `<TxDtls>` with separately identified
  transaction amounts. This could hide the underlying ledger detail.
- The parser now splits only when every detail amount is present, each detail
  currency agrees with the entry currency, and signed detail totals exactly
  match the statement entry. Missing, mixed-currency, or unreconciled detail
  totals retain the entry-level aggregate row rather than fabricate splits.
- Verification passed: bank-import/reconciliation unit suite (21 tests) and
  bank-import/reconciliation API coverage (10 tests). The Python environment
  emitted a pytest-cache permission warning; tests passed. No live bank/provider
  data was imported.

## 2026-10-16 - Preserve returned HAI executor failures

- A regression against an executor that returned `success: false` reproduced a
  contract error: FAB recorded `hai.command.completed` and returned
  `success: true`, so a replay claimed the failed command had executed.
- Returned non-success results now create a durable `hai.command.failed` event,
  return `success: false`, preserve an executor's `blocked`/`needs_review`
  status, retain its result and external-submission state, and replay as
  `previously_failed` without re-running the executor. Structured blocks return
  HTTP 409; unexpected executor exceptions remain HTTP 500.
- The first complete Python run surfaced a regression where a normal blocked
  result was mapped to HTTP 500. The route/result mapping was corrected, and
  focused HAI, Drive/Wave, and bank/reconciliation cases passed.
- Final full Python verification passed: 1,974 passed, 6 skipped, and 159
  subtests in 678.06 seconds. Python compilation and `git diff --check` passed.
  The returned-failure regression failed before its fix. No provider action was
  run.
- Web verification also passed: TypeScript, all 471 Vitest tests (5 skipped),
  Vite production build and asset budgets, and both general and standalone
  server bundles. `pnpm check` again failed before TypeScript because Windows
  denied writes to generated `web/node_modules/.package-map.json`; the installed
  local binaries ran directly, with no tracked package or lockfile changes.
- Extended the isolated Windows stack rehearsal to send a deliberately
  incomplete attachment attestation through the live synthetic HAI API. The
  API returned HTTP 409 with `blocked`, recorded a durable failed outcome,
  reported `externalSubmission: not_executed`, and replayed as
  `previously_failed` without another executor call. The full built-dashboard,
  API, and SQLite rehearsal passed with `syntheticOnly: true`,
  `loopbackOnly: true`, `providerWrites: false`, and owned child processes
  stopped. The complete HAI connector test file passed (24 tests, 8 subtests).

## 2026-10-16 - Preserve indeterminate provider outcomes in generic API errors

- Audit review found the Flask catch-all handler recorded
  `externalSubmission: not_executed` for every unexpected route exception. A
  provider can accept a write and the route can fail before recording its
  result, so that audit state was not defensible.
- Catch-all failures now record `unknown`. The regression test simulates a
  provider-side effect before raising, and verifies the correlated audit event
  preserves uncertainty without disclosing exception content.
- The local-operations API suite passed (119 tests, 2 subtests). The separate
  full Python suite passed before this narrow fix (1,974 passed, 6 skipped,
  159 subtests); the synthetic Windows stack rehearsal passed 75 checks with
  provider writes disabled and owned children stopped.
- Removed an unused `os` import and two dead local assignments found by Ruff;
  the relevant Drive archive/autonomy/processing tests passed (75 tests), and
  Ruff's undefined/unused-name rules passed on those files.
- A 40-request/16-client warm-cache dashboard run passed at 125.49 ms median,
  139.01 ms p95, and 57,768 response bytes. It uses a changed fixture and
  concurrency relative to the prior six-client result and is not a controlled
  speed comparison or CPU/RSS measurement.
- Read-only GitHub state shows PR #125 is open with merge state `DIRTY`; this
  worktree remains unpushed. Local Bandit reported heuristic findings
  (0 high, 30 medium, 16 low), which remain under review. The managed Deep
  Security Scan did not start because the required filesystem permission
  profile is unavailable in this session.

## Explicitly not performed

- No live provider record was created, modified, or deleted during the code audit.
- No Drive source was archived.
- No credentials, OAuth tokens, financial files, or runtime database were added to Git.
- No direct MijnGeldzaken, PSD2, or SVB automation was invented to satisfy a checklist.

## 2026-10-16 - Verify PR CI and private GHCR Actions access

- PR #125 is open at `b664468a3df3cbde40553f553901c06fcd891006`.
- Hosted FAB verification run `38961436355` completed successfully: Linux backend,
  all four Windows backend shards, MySQL integration, frontend, and both
  production-container build jobs passed. Container jobs built without publishing.
- GitHub Advanced Security CodeQL check `117178222073` still fails with six
  high-severity path-flow alerts in `src/operations/local_intake.py`. The earlier
  medium exception-exposure alert is no longer reported. Security remains open;
  no alert was suppressed or dismissed.
- Verified both private GHCR packages, `019-fab-api` and `019-fab-dashboard`,
  already grant `019-FAB` Actions Write access. No permission change was needed.
- GitHub warns that granting a public repository access to private packages can
  make them accessible to fork-triggered workflows. The container publishing
  workflow itself is tag-push-only, checks the tagged commit is on `main`, and
  scopes `packages: write` to its publish job; review any future workflow that
  consumes these private images separately.
- Release `v0.1.0-preview.1` is already published as a prerelease with Windows
  and Compose archives and SHA-256 sidecars. This verification did not publish
  another release or container image.
- No live provider operation, cloud deployment, public tunnel, or accountant
  acceptance was performed.

## 2026-10-20 - Validate HAI results and reduce dispatch allocations

- Reproduced malformed executor dictionaries being marked completed, including
  string/integer success flags and non-finite or non-JSON values. A list-valued
  failure status also raised an unhandled type error.
- Validate executor output before recording completion: optional success/status
  fields have strict types, and results must be finite JSON within the same
  1 MiB, depth-32, 10,000-value limits as command metadata. The manifest now
  advertises these result limits. Invalid output records a durable failure,
  preserves uncertain provider effects, and cannot execute again under the
  same request ID.
- Normalize command input once while retaining both policy checks and the
  separate copy passed to the executor. In a synthetic 25-iteration benchmark
  using a 500,063-byte payload and an in-memory ledger stub, median dispatch
  latency changed from 21.654 ms to 8.241 ms; p95 changed from 45.792 ms to
  15.541 ms. Traced Python peak changed from 2,512,303 to 2,007,170 bytes, with
  identical result SHA-256. These figures concern this synthetic dispatch path,
  not whole-app RAM, sustained load, or provider throughput.
- HAI and local API suites passed: 147 tests and 17 subtests in 334.15 seconds,
  including authenticated API failure/replay and oversized-output regressions.
  No provider write or live HAI controller was exercised.
- PR #125 was merged into main during this work. Its nested-database backup and
  case-fold migration fixes are already upstream. The owner confirmed that the
  newer ngrok retirement remains in effect; Windows local operation and the
  supported cloud deployment profiles remain the current deployment direction.

## 2026-10-20 - Bound worker telemetry transport

- Reproduced infinite/excessive configured timeouts, redirects reported as
  successful writes, and unbounded eager response buffering in OperationsClient.
- Disable redirects, reject redirect responses, stream decoded response bodies
  with a 1 MiB cap, and always close the response. Invalid/non-finite timeouts
  fall back to five seconds; valid connect/read timeouts cap at 120 seconds.
  These are not total wall-clock deadlines. No automatic retry was added.
- A real loopback HTTP regression proves a redirected document POST is not
  forwarded and the source identity is persisted in the local SQLite ledger.
  Additional tests cover response limits/closure and invalid timeout values.
- Operations-client and workflow-recovery suites passed on Windows: 32 tests
  and 6 subtests in 15.86 seconds. The initial integration-test cleanup failure
  was fixed by closing its own SQLite connection explicitly.
- Corrected stale gateway audit documentation to match the eight-read pool,
  65-second snapshot cache, and mutation invalidation already in the code.
- All hosted checks passed for the preceding HAI commit 317492ee, including
  Linux/Windows backend, frontend, MySQL, container builds, and CodeQL. That
  result does not validate this subsequent transport change until CI reruns.
  No live provider write, deployment, or live HAI acceptance was performed.

## 2026-10-20 - Keep HAI failure classification on replay

- Store `invalid_executor_result` as the audit error code and replay the original stored failure code. Historical invalid-result records without that field retain their classification through their existing error type; ordinary executor exceptions retain `executor_failed`.
- Added assertions for authenticated API replay, all seven malformed-result fixtures, and the durable audit code. Local connector suite: 27 tests and 15 subtests passed. No provider execution or deployment was performed.

## 2026-10-20 - Complete, compact bank dashboard totals

- Found the dashboard deriving gross unreconciled bank amounts from only its
  latest 250 transaction rows. Replaced that partial window with a complete
  SQLite aggregate, wired through Flask's individual and batched resources to
  the dashboard gateway. Shared filters preserve the default detail route.
- Summary reads omit private transaction rows and limit recent statement
  imports to five. Approved/reconciled/ignored entries do not contribute.
  Missing amounts or overflow mark the summary incomplete. The gateway shows
  unavailable amounts for absent, incomplete, malformed, or unsupported-currency
  aggregates instead of silently calculating partial legacy totals.
- Reproduced the API detail-load bug and six gateway failures before fixes.
  Final checks: 146 Python tests and 7 subtests passed across API, bank import,
  reconciliation, SQL safety, and new summary coverage; 71 gateway tests passed.
  Type check and production build/budget verification passed.
- Actual HTTP smoke check on this Windows host: built standalone dashboard
  using the windows deployment profile, temporary Flask backend, fresh random
  secrets, and a synthetic SQLite ledger. The tRPC control-center response
  reported 301 transactions, EUR 602 exposure, and a live bank resource. The
  dashboard HTML returned 200; an anonymous backend summary request returned
  401. All temporary processes stopped and temporary data was removed. This
  was not browser rendering, installer acceptance, or live provider acceptance.
- Final-code synthetic benchmark: 2,000 transactions with 4 KiB metadata each,
  ten Flask test-client reads after warmup, under tracemalloc. Detail-window
  response: 1,127,049 bytes, 62.437 ms median, 4,772,111-byte traced Python peak.
  Complete-summary response: 209 bytes, 11.449 ms median, 38,444-byte traced peak.
  The summary asserted all 2,000 transactions and EUR 2,500 exposure. These are
  scoped endpoint measurements, not whole-app memory or sustained-load claims.
- Hosted checks for the preceding transport commit 0e18e590 passed, including
  Linux/Windows backend, frontend, MySQL, container builds, and CodeQL. The new
  dashboard-summary commit requires its own CI verification.
- Preserved concurrent remote HAI replay-classification commit 3d58a92 through
  a non-force rebase; the only conflict was adjacent work-log entries, both
  retained. The combined HAI/summary check then passed 29 tests and 17 subtests.
