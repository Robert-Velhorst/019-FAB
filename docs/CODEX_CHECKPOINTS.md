# Codex Checkpoints

2026-10-24 cleanup/publication follow-up: bank-record preparation retries now
check review/status gates, operation identity, source snapshot and target before
reusing a draft. Unchanged retries retain approval and metadata; stale or unbound
prepared records require review. Lookup filters by record before its bounded
limit. Final routing/export/reconciliation/records/autonomy/MijnGeldzaken checks
passed 144 tests and 20 subtests. The prior interrupted full-disk run is not a
verdict. Exact-head CI is required after publication; adfea9b completed all nine
verification jobs and CodeQL. Owner-authorized local cleanup preserves financial
data, credentials, backups and unique source. The separate local-profile
pre-write low-storage guard remains an identified, unimplemented gap.

Latest prepared-draft retry follow-up: unchanged document preparation returns
the same local draft without rewriting normalized approval, timestamps or audit.
Reuse requires the operation, source snapshot (excluding machine processing
status), target and an existing normalized record. Generic aliases remain
equivalent; explicit Wave business/personal targets remain distinct. Changed,
unbound legacy or missing-record drafts require review, not silent replacement.
Autonomous batch selection still excludes already-prepared documents. Final
routing/export/reconciliation/records/autonomy checks passed 121 tests and
20 subtests; the built synthetic dashboard/API/SQLite/auth/HAI rehearsal passed
with no provider writes and owned children stopped. No new browser/layout
acceptance is claimed for this Python-only change. Prior 798515e passed all
nine CI jobs and CodeQL; this follow-up needs fresh exact-head checks. No cleanup
was performed; host storage and live provider/cloud/HAI-peer gates remain open.

Latest routing follow-up: document and bank-record draft preparation each use
one writer transaction, including source validation, review/blocked branches,
routing attempt, document/normalized export state and completion audit. Four
red Wave/MijnGeldzaken cases reproduced partial persistence after normalization
or audit failure. Final routing/export/reconciliation/records/autonomy checks
passed 98 tests and 20 subtests, including nested rollback and approval/no-execute
guards. C: exhausted during an expanded run; source writes stopped, recovery
copies were hash-verified on D:, and final tests used offline locked dependencies
with D: fixtures and bytecode disabled. No cleanup was performed. C: later
regained space without this task deleting files. Keep host-storage headroom as
an unresolved operational gate; no live financial/provider data was touched.
Prior 7f451da CI and CodeQL passed; this follow-up needs fresh exact-head checks.

Latest retry-admission follow-up: failed-document retries now compare their
starting core snapshot under the ledger writer lock and atomically persist
the imported transition, bounded retry history and retry-start audit. Stale
claims are skipped, not counted as retried; admission avoids full history
hydration. Three red regressions reproduced partial writes, overwritten newer
status and competing callers. Expanded checks passed 277 tests and 8 subtests;
10 final focused cases include single/batch authenticated API skip semantics,
same-timestamp evidence drift, post-insert audit failure and fast re-failure.
The built synthetic dashboard/API/HAI rehearsal passed and stopped children.
OCR/file work runs after the claim transaction; this is not a global processing
lease or exactly-once guarantee. Fresh exact-head CI and live production gates
remain open. Ngrok stays retired and no live financial data changed.

Latest queue race follow-up: incremental review loads now retain their starting
queue generation and cannot append rows, offsets, pagination or stale errors
after a newer manual/background refresh. Every first-page effect and cleanup
invalidates old loads, including when no additional pages were previously
loaded; a synchronous in-flight ref prevents duplicate incremental requests.
An actual Chromium held-response regression failed before the fix (25 reset
rows became 50), then passed for manual and background first-page refreshes.
A fresh load still advanced 25 to 50. Full web suite: 490 passed, 7 skipped;
TypeScript/build budgets and synthetic dashboard/API/HAI rehearsal passed.
Desktop/mobile checks passed at 1280x800 and 390x844 using the previously approved
Playwright fallback. No live financial/provider data changed. Exact-head CI,
broader UI races and production/provider acceptance still require evidence.

Latest dashboard follow-up: background review-page refreshes run at most four
logical reads at a time, preserve offset order and stop queued dispatch after
supersession or failure. The Operations effect passes its live sequence/cleanup
guard. Corrected desktop/mobile navigation cascade and mobile table value
columns. Full web suite: 490 passed, 7 skipped; TypeScript and production build
budgets passed. Owner-approved Playwright fallback used a temporary real
dashboard/API/SQLite stack after the in-app browser failed to initialize.
175 reviews across seven loaded pages refreshed six additional pages at a
measured logical peak of four; desktop/mobile navigation, no-overflow, console
and screenshot checks passed. All rehearsal children stopped; no provider writes
or live ledger access. Prior 9967ba0 CI and CodeQL passed. This follow-up needs
fresh exact-head CI; full production acceptance and wider UI race audits remain.

Latest continuation: category-review discovery now uses filtered SQL and
100-document timestamp/ID cursor pages instead of the newest 5,000 global
reviews. Complete open review groups and all prior eligibility gates remain.
Expanded ledger/processing/autonomy/API/records/health/benchmark checks passed
305 tests and 8 subtests; the synthetic built dashboard/API/SQLite/HAI rehearsal
passed with no provider writes and owned children stopped. Sparse 2,000-row
category-context measurements are in the technical audit; they do not describe
whole-app resource consumption. Prior 1c75fec CI (all nine jobs) and CodeQL are
green. This new change still needs fresh exact-head hosted checks. Ngrok stays
retired; live provider/cloud/HAI-peer acceptance remains unverified.

Latest continuation: trusted-vendor category automation now fingerprints its
original candidates, releases that snapshot before backup, revalidates under
the writer lock, and defers changed documents. Accepted category/field/review/
normalized-record/audit mutations share one batch transaction. Expanded checks
passed 289 tests and 8 subtests; final focused checks passed 17 tests including
a separate-connection operator correction and real synthetic authenticated
backup. The built dashboard/API/SQLite/HAI synthetic rehearsal passed and its
children stopped. Prior e344700 completed all nine CI jobs and four-language
CodeQL; its focused managed security review had zero reportable findings, not
an independent or whole-repository clearance. This new follow-up requires fresh
exact-head CI. No live provider, ledger, cloud host or external HAI peer changed.

Latest safety follow-up: duplicate-candidate reassessment now fingerprints and
revalidates evidence under the writer lock, defers changed/operator-confirmed
pairs, and commits accepted candidate/link/review/document/normalized-record/
audit changes atomically. Expanded checks passed 264 tests and 5 subtests;
nine focused cases include a real synthetic authenticated backup. Prior
0052ef6 CI and CodeQL passed; this follow-up needs its own checks/review.

Latest continuation: full-history confirmed-link cycle scans now replace the
default recent-row window. Per-cycle writer transactions revalidate links and
atomically retain review/normalization/audit changes, deferring intervening
corrections. Current focused checks passed 63 tests; the wider changed-service
group passed 134. See the latest worklog entry for expanded tests, benchmark
scope, and pending exact-head CI. Live cloud/provider/HAI-peer gates remain open.

## Current Continuation (2026-10-20)

Worktree `C:\Users\NO\Documents\Codex\2026-06-03\019-FAB-pr125-next`,
branch `codex/fab-durable-telemetry-identities`, draft PR #146. Preserve the
original checkout's user-owned source changes; do not reset or clean it.
Ngrok remains retired by explicit user decision.

Local full-suite evidence before the latest quarantine follow-up: 1,989 Python
tests and 239 subtests passed, 8 skipped; 486 web tests passed, 7 skipped;
TypeScript, production build, and bundle budgets passed. The schema-v4 follow-up
passed 283 expanded migration/backup/API/readiness tests and 34 subtests, with
2 skips, plus 8 stack-rehearsal tests. The built dashboard/API/SQLite/HAI
synthetic rehearsal passed with provider writes disabled and children stopped.
These are local/synthetic checks, not live deployment or provider acceptance.

Hosted checks on `67fbc78` passed frontend, all Windows shards, MySQL, both
containers, and CodeQL; Linux stopped at the newly reported certifi advisory.
The follow-up pins certifi 2026.10.19 in the affected hash locks; lock consistency
and the combined Python vulnerability audit pass locally. Exact-head hosted
verification must be refreshed after the follow-up is pushed. Historical
unmapped telemetry and unknown write outcomes still require verified recovery;
the broader production objective is not complete.

The newer backup-publication follow-up passed 202 backup/recovery/API/health
tests and 2 subtests, with 2 skips. Frozen-clock regressions reproduced old
recovery points being overwritten on success or deleted on failure. New
packages use unique names, exclusive publication, attempt-owned cleanup,
streaming source-size limits, and pre-compression package budgets. See
`CODEX_WORKLOG.md` for exact evidence boundaries. CI on prior commit `79cafdc`
passed Linux, frontend, MySQL, containers, CodeQL, and three Windows shards;
one Windows shard was still running when checked. The backup follow-up itself
requires fresh exact-head hosted verification.

The latest opt-in VM HAI wiring adds an API-only dedicated secret and restricted
default commands through `docker-compose.vm.hai.yml`; it is not applied to a
live host. Its expanded configuration/authentication/release checks passed
152 tests and 15 subtests, including real Compose rendering and ConfigLoader
secret-file/API/SQLite checks. Fresh source-package verification follows the
commit. Host/domain identification, target TLS/permissions/encryption, and
an external HAI peer remain unverified. Ngrok stays retired.

## Historical Checkpoint Table (2026-10-17)

| Checkpoint | State | Resume evidence |
| --- | --- | --- |
| C0 Baseline | Current | As of 2026-10-17, the canonical checkout at `C:\Users\NO\Documents\Codex\2026-06-03\pull-noodzakelijk-online-019-fab-https\019-FAB` remains at `dbe3a8a6383c88ad926a707ff7d8975561c99327` with 64 pre-existing staged files plus local-only lock-verification and bounded folder-traversal fixes; preserve it. PR #125 is isolated in `C:\Users\NO\AppData\Local\Temp\019-fab-pr-publish-20261016`, branch `codex/fab-followup-20261015`, based at the verified `cd5889f` head before this update. |
| C1 Requirements map | Complete | 116 phase rows in `GOAL_COMPLETION_MATRIX.md`. |
| C2 Critical safety gaps | Complete | Persistent emergency stop and sanitized support bundle implemented and tested. The legacy Flask dashboard now stores only a hash of each random browser-session ID, checks its expiration and revocation on authenticated requests, and revokes that ID on logout; copied cookies remain invalid after logout and app restart. Its login form also enforces a bounded per-peer failure window without trusting forwarded-IP headers. |
| C3 Product truthfulness | Complete | Unsupported public claims and invented testimonials removed in English and Dutch. |
| C4 Focused verification | Complete | Export idempotency, route due-state, projection/daily-plan scheduling, sparse recurring workflow persistence, audit coalescing, and canonical operator-ticket tests pass. The latest combined export/API/worker/autonomy/HAI/recovery group passed (`210 passed`, `11 subtests`): generic and HAI safe cycles cannot execute provider exports; manual Wave result transitions are blocked; supervised MijnGeldzaken results require the stored artifact hash; historical high-risk attempts remain approval-required. Approval, rejection, and supervised operator-attestation transitions are serialized in ledger write transactions with rollback and race regressions. Wave export approval binds current source/review/provider mapping; indeterminate Wave responses are redacted, terminal on the attempt, and counted for operator attention. Provider-side readback/reconciliation after an indeterminate response remains a limitation. |
| C5 Full verification | Partial | Fresh complete Python run on the canonical working tree passed `1,982 passed`, `6 skipped`, and `219 subtests` in 1,270.51 seconds, including the bounded directory-entry regression. The lock verifier and its regression test passed. The web suite passed `471 passed`, `5 skipped`; TypeScript, production Vite/server/standalone builds, and bundle budgets passed. Python and production web dependency audits reported no known vulnerabilities. PR #125 head `cd5889f` passed hosted CI and CodeQL; the new folder-traversal fix is not in that run and awaits its own hosted checks. Local tests do not prove live provider, tunnel, cloud deployment, or operator acceptance. |
| C6 Browser/runtime acceptance | Partial | A disposable Windows local-profile production run on 2026-10-16 used an isolated ledger and fresh credentials, with provider credentials absent and connector sync/export execution disabled. `Start-FAB.ps1 -NoBrowser` started API, production dashboard, and worker; authenticated `/api/live` returned `fab-ledger-api` status `ok`, dashboard returned HTTP 200, and authenticated HAI status/manifest returned `ready` with 14 allowlisted commands and no external submission. Browser verification found no horizontal overflow at 1280x720 or 390x844 and no console errors. The fresh 2026-10-17 synthetic stack rehearsal after the bounded intake change passed the local API/dashboard/SQLite flow, HAI allow/deny/replay checks, ledger/reconciliation gates, and child shutdown; it reported `syntheticOnly=true`, `loopbackOnly=true`, `providerWrites=false`. The one-document fixture and warm-cache resource timings are not a sustained/scaled/cold baseline. No provider writes occurred and the 237 MB user ledger was not used. Live provider, public tunnel, cloud deployment, and user/accountant acceptance remain separate gates. |
| C7 Source hygiene | Partial | The protected canonical checkout still contains broad user-owned staged and unstaged content. Do not clean, reset, or package that tree as a release snapshot until its changes are classified and a clean-source artifact is independently verified. The isolated PR #125 worktree is `C:\Users\NO\AppData\Local\Temp\019-fab-pr-publish-20261016`, branch `codex/fab-followup-20261015`, clean at `3a47906537f1e59a5191e2a81dba6fcc286fa236` when checked on 2026-10-16; recheck before delivery. |
| C8 Delivery/security | Partial | PR #118 remains open and unmerged; release `v0.1.0-preview.1` and private GHCR images are published. Both packages show `019-FAB` with Actions Write. PR #125 (`codex/fab-followup-20261015`) was at `cd5889f` before this update; hosted CI run `38978111933` and CodeQL run `38978107784` passed. This update adds the bounded intake traversal fix and requires a fresh hosted run. The PR remains open and unmerged; GitHub reports merge state `BLOCKED`. Whole-repository Codex Security scanning remains incomplete; the earlier diff scan was tied to a stale working-tree digest and did not cover the current change. Independent review and external provider/deployment acceptance remain outstanding. |

On context loss, re-run `git status --short` before editing. Next: continue profiling the whole app and resolving remaining partial phases in `GOAL_COMPLETION_MATRIX.md`. Ngrok support is retired; Hetzner hosting, live providers, and human/accountant acceptance still require separate evidence. Do not describe the application as production-ready until all remaining gates are verified.
