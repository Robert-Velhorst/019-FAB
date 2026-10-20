# Codex Checkpoints

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
