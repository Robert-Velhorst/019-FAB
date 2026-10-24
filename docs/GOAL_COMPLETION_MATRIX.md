# Goal Completion Matrix

Status meanings: **Implemented** is reachable, wired, tested, and documented in the repository; **Partial** has real behavior but remaining scope; **Blocked** needs external authorization, deployment, or human validation; **Not applicable** is intentionally absent and not presented as a product capability.

| Phase | Status | Evidence or remaining work |
| --- | --- | --- |
| 000 Repository integrity and true starting point | Implemented | Baseline in `TECHNICAL_AUDIT.md`; branch and commit recorded. |
| 001 Complete file and dependency audit | Implemented | Python/web/CI/scripts/docs/runtime surfaces inventoried. |
| 002 Product definition and user outcome contract | Implemented | `fab_comprehensive_scope.md`, critical path, and truthful UI copy. |
| 003 Critical path definition and smoke test | Implemented | `CRITICAL_PATH.md` and cross-stage tests. |
| 004 Architecture decision and current stack validation | Implemented | Existing Python/SQLite plus React/Express architecture retained. |
| 005 Data model, ownership, and persistence design | Implemented | Local ledger schema and master-ledger projection. |
| 006 Configuration validation and startup guards | Implemented | `LocalReadinessService`, loopback/token checks, launch scripts. |
| 007 Authentication model and session security | Partial | Web operator auth and the local API token exist. Advanced ledger/evidence links now use a 45-second, one-time, relative-target HMAC handoff that keeps the token server-only, rotates the Flask session, and records redacted actor/path evidence. Production identity deployment remains unverified. |
| 008 Authorization and resource ownership | Partial | Operator/admin and HAI bounds exist; full team/tenant ownership is not complete. |
| 009 API contract and error envelope | Implemented | Typed gateway plus correlated machine-readable JSON errors preserve route details and hide unexpected exception content. Catch-all route failures now record `externalSubmission: unknown` rather than falsely asserting no provider effect occurred. |
| 010 Frontend architecture and navigation model | Implemented | Functional operator shell and panels. |
| 011 Core workflow vertical slice | Implemented | Intake through recovery covered by ledger services and tests. |
| 012 External provider reality review | Implemented | Capability states distinguish live, supervised, and blocked. |
| 013 Compliance and platform policy boundaries | Partial | Guardrails exist; legal/accountant and provider-policy sign-off remains external. |
| 014 No fake success and no mock production behavior | Implemented | Supported entrypoints use the authoritative ledger worker and explicit execution/readback states. Empty or fully invalid bank imports are recorded as `empty`, never `completed`; malformed/binary statement files are rejected. Fake-success cloud/mobile/root/checkpoint entrypoints, synthetic learning, placeholder performance/migration helpers, and the dummy tax export are removed; tests prevent their accidental return. |
| 015 Storage, files, uploads, and media safety | Implemented | Hashing, path validation, ignored runtime roots, recovery evidence, and bounded local-folder traversal (120,000 filesystem entries per source scan) with a regression for fail-closed overflow. |
| 016 Background jobs, schedulers, and workers | Implemented | Worker, schedules, leases, recovery, reports, and notifications. Worker intervals are clamped to 10 seconds through 24 hours (invalid values default to 300 seconds) to prevent hot loops and unreasonable sleep delays. Generic/HAI safe autonomy can prepare exports but cannot submit them; Wave/MijnGeldzaken execution stays on the dedicated supervised export-worker path. Historical high-risk workflow steps require manual approval and are not retried automatically. |
| 017 Idempotency and duplicate action prevention | Implemented | Content fingerprints, duplicate candidates, operation IDs, runtime leases, bounded compact duplicate reassessment without per-pair full-history reads, and occurrence-aware generated bank identities preserve legitimate repeated rows while exact re-imports remain no-op duplicates. Changed financial facts under a stable bank transaction ID are retained as a review conflict and cannot overwrite prior reconciliation evidence. |
| 018 Rate limits, cooldowns, and provider quotas | Implemented | Shared limiter state and operational health issues. |
| 019 Audit logging and event history | Implemented | Persistent audit events across decisions and controls; legacy backup/restore audit projections omit filesystem paths, archive names, and source filenames while retaining outcome and source-count evidence. |
| 020 User-facing dashboard and next-action design | Implemented | Operator control center with blockers, next actions, and a local bank-statement drawer reachable from both Overview and Connections. Advanced ledger, evidence, report-artifact, delivery, recovery, and connector-contract links now open through the authenticated one-time session handoff instead of prompting a non-technical operator for the hidden API token. |
| 021 Forms, validation, and autosave behavior | Partial | Validation is wired. Source-backed review corrections now save in tab-scoped browser storage, restore only for the exact unchanged review/source identity, expire after seven days, clear after approval, and support explicit discard. Universal draft autosave across every low-risk form remains broader follow-up work. |
| 022 Search, filters, sorting, and pagination | Partial | The review queue now pages complete document groups on the server and incrementally loads the entire filtered-open queue while preserving client triage/search over loaded records. Existing exception and delivery pagination remain; not every low-volume table paginates. |
| 023 Import and export workflows | Partial | File/provider intake, operator-authenticated 4 MB CSV/JSON/CAMT/MT940 bank uploads, automatic local reconciliation after new rows, approval-gated exports, canonical-only Wave result transitions, and artifact-hash-bound MijnGeldzaken operator attestations. Approval, rejection, and supervised attestation are serialized and atomic; Wave approvals bind to current source/review/provider mapping. Indeterminate Wave writes are redacted, terminal, non-retryable, and now retain/display the exact FAB external reference sent to Wave. The public Wave schema does not expose a transaction list/readback or externalId lookup; automated provider/attachment verification and reconciliation remain incomplete. |
| 024 Templates, presets, and reusable user defaults | Partial | Rules/mappings/preferences exist; broader reusable presets remain. |
| 025 AI/provider abstraction and deterministic fallback | Implemented | OCR/category providers with deterministic rules and review fallback. |
| 026 Human review queue and approval gates | Implemented | Review workspace, corrections, routing/export approvals, complete document-group paging, and queue-wide posting/evidence safety totals. |
| 027 Notifications and reminders | Implemented | Preference/status/audit writes and health refresh commit atomically; dashboard totals use an indexed SQL aggregate rather than the 500-row inbox page, with a 501-row regression test. Due/exception notifications remain local unless an explicitly authorized delivery connector is configured. |
| 028 Privacy controls and data deletion | Partial | Retention/export controls exist; end-user erasure workflow needs governance validation. |
| 029 Security headers and web security | Implemented | Restrictive production CSP, conditional HTTPS HSTS, standard browser protections, request tracing, auth/sanitization, hashed server-registered legacy dashboard sessions with persisted logout revocation, bounded peer-based login throttling, and real TCP/live header tests are wired; independent penetration testing remains a separate external gate. |
| 030 Secrets management and credential rotation | Partial | Local encrypted Wave store and OAuth reauthorization state; full rotation drill remains. |
| 031 Local development one-command experience | Implemented | `Start-FAB.ps1/.cmd` provisions an isolated Python 3.13 `.venv`, starts API/worker/production dashboard with checkout-scoped instance identity, and `Stop-FAB.ps1/.cmd` stops only this checkout. Readiness identity-verifies the selected dashboard port. `python -m src.main` uses the same owned worker path for one cycle. Managed ngrok start/stop wrappers verify ownership and never pool or stop another project's endpoint. A fresh Windows production-bundle/API/SQLite rehearsal passed the complete synthetic integration flow and verified owned child processes stop. |
| 032 Docker and deployment readiness | Implemented | API/worker/web Compose, both non-root images, configurable loopback ports, explicit host-reachable API/dashboard projections, authenticated health, HAI/cloud status, and local-operator acceptance passed. Clean-source Windows/Compose release archives are source-commit and checksum bound. A dedicated production endpoint is still an infrastructure activation gate. |
| 033 Database migrations and rollback safety | Implemented | Ordered checksum-bound migration history, fail-closed validation, verified pre-upgrade snapshots, and restore-based rollback guidance. |
| 034 CLI and doctor/self-diagnostic command | Implemented | `python -m src.run_fab_doctor`. |
| 035 Observability, health, and readiness endpoints | Implemented | Constant-time liveness, deep health, settings, doctor, metrics, audit, workflow state, project-owned cloud status, identity-verified operator/API access metadata, and profiled autonomy latency. |
| 036 Admin/operator diagnostics | Implemented | Dashboard diagnostics and sanitized support bundle. |
| 037 Demo mode with explicit labelling | Not applicable | No production demo state is substituted for live financial data. |
| 038 Fake provider lab for tests only | Partial | Mocks/fixtures are test-scoped; no dedicated provider simulator UI. |
| 039 Test-data factories and fixtures | Implemented | Synthetic isolated ledgers and provider mocks across suites. |
| 040 Backend test suite | Implemented | Broad Python test suite and CI. |
| 041 Frontend and component test suite | Implemented | Vitest gateway/shared/UI behavior coverage. |
| 042 Worker/job test suite | Implemented | Autonomy, recovery, schedules, and leases covered. |
| 043 End-to-end workflow tests | Implemented | Cross-stage local workflow tests and the Windows production dashboard/API/SQLite rehearsal; live-provider E2E remains separately blocked. |
| 044 Acceptance test matrix | Implemented | `ACCEPTANCE_TESTS.md`. |
| 045 Adversarial break-the-app tests | Partial | Failure, duplicate, traversal, auth, and ambiguity tests exist; no external red team. |
| 046 Cross-user isolation tests | Partial | Operator procedures are role-gated; multi-tenant isolation is not a complete product model. |
| 047 File safety and path traversal tests | Implemented | Upload/intake/backup/provider credential path tests. |
| 048 Provider failure simulation | Implemented | OAuth, API, rate, retry, ambiguous, and attachment failure tests. |
| 049 Accessibility review | Partial | Semantic states and keyboard-native controls; the bank-import dialog traps focus, closes on Escape, and restores the invoking control. Full WCAG audit remains. |
| 050 Responsive and browser compatibility | Partial | Current-source in-app Browser QA rendered the live 119-document review queue and authenticated ledger handoff at desktop and a narrow responsive breakpoint. Fresh dashboard/ledger console reads were empty and both desktop/narrow page width equaled viewport width. Prior delivery and bank-import acceptance remains green; a broad browser/device matrix remains. |
| 051 Performance baseline and indexing | Partial | SQLite WAL/busy waiting, query-only compound-read snapshots, batched line-item/exception hydration, bounded dashboard fan-out, lazy OCR/ML loading, bounded projections, and short single-flight coalescing are verified. The Operations dashboard polls at 60-second intervals only while visible and immediately refreshes on return. Live exceptions fell from 308.57 ms to 85.28 ms median, 150-work-order reads from 222.81 ms to 125.52 ms, and the complete cold control center to 709.21 ms median with a 31.61 ms warm median. Readiness uses app-owned dependency/runtime caches; settings remain 2.48 ms median on the 100-read baseline. Earlier isolated Windows rehearsal measured 30 authenticated warm-cache dashboard reads at six concurrent clients: 31.20 ms median, 37.26 ms p95, and 56,750 response bytes on a one-document synthetic ledger. Removing the unused HAI manifest projection reduced the before-run's 67,105-byte response by 10,355 bytes (15.4%); HAI status remains in the dashboard, and the direct HAI manifest endpoint remains available. An earlier 40-request/16-client run measured 125.49 ms median, 139.01 ms p95, and 57,768 response bytes after the fixture changed; it is not directly comparable to the six-client run. An earlier 30-request / 6-client run measured 49.56 ms median / 66.27 ms p95. Three new 30-request / 6-client warm-cache runs returned a stable 57,768-byte body but varied in latency: medians 61.89-105.67 ms, p95 81.34-221.80 ms, max 81.99-248.59 ms. At 50 ms sampling, peak combined working set ranged 189,505,536-196,145,152 bytes, dashboard CPU 0.328125-0.5625 seconds, and API CPU 0.0 seconds at reported precision. The 2026-10-17 scaled Windows rehearsal passed the built standalone/API/SQLite flow with 2,005 synthetic ledger documents; 30 warm-cache requests at concurrency 8 measured 68.60 ms median, 84.19 ms p95, 87.55 ms max, and 70,446 response bytes. Six 50 ms samples observed 181,149,696 bytes combined peak working set, 0.390625 seconds dashboard CPU, and 0.0 API CPU seconds at reported precision. These are short synthetic warm-cache measurements, not cold, sustained, production-scale, or whole-app resource guarantees. The 2026-10-15 matcher benchmark measured 1.91 s unprepared versus 33.79 ms indexed on a 2,000x400 sparse synthetic case. A fresh 2026-10-16 run measured 1.395 s versus 19.78 ms (about 70x faster) with identical results; traced Python peak was 651,704 versus 848,007 bytes, so these synthetic matcher allocations are not a whole-app memory claim. On a separate 400-row synthetic anomaly service benchmark, precomputed leave-one-out statistics preserved identical issue output and reduced median from 42.022 ms to 9.719 ms, without process CPU/RSS measurement. Sustained host load and cold backup-integrity timing remain. |
| 052 Large dataset and pagination testing | Partial | Review paging has complete-group boundary tests and live `50 -> 100 -> 119` acceptance; bounded batch/limit tests cover other queues. Sustained production-scale testing remains. |
| 053 Backup and restore procedures | Implemented | V3 source-complete recovery packages authenticate the manifest with HMAC-SHA-256, bind the ledger/evidence hashes, fail closed on a missing or wrong key, and require explicit legacy confirmation for V1/V2 checksum-only archives. The 13-check synthetic recovery rehearsal passed; off-host key custody and production restore acceptance remain operator gates. |
| 054 Data reconciliation and repair commands | Implemented | Reconciliation, reprocessing, recovery, and close-readiness services. |
| 055 Product analytics local-first design | Partial | Operational aggregate metrics exist; no product telemetry pipeline is enabled. |
| 056 SaaS readiness without forced billing | Partial | Web shell/billing code exists; local FAB operation does not require billing. |
| 057 Internationalization and Dutch/English readiness | Implemented | English/Dutch public and operator surfaces. |
| 058 Feature flags and rollout controls | Partial | Config capability switches exist; no unified feature-flag registry. |
| 059 Formal state machines | Implemented | Explicit document, review, export, workflow, provider, and delivery states. |
| 060 Domain model specification | Implemented | Ledger schema and technical/module documentation. |
| 061 Data invariants and constraints | Implemented | Validation, foreign keys/state checks, hashes, approval and archive gates. |
| 062 Pre-action safety review screen | Implemented | Review/export/delivery controls show target, evidence, and blockers. |
| 063 Provider credential verification checklist | Implemented | Gmail/Drive authorization and Wave setup/readiness drawers. |
| 064 Threat model and security design review | Implemented | `SECURITY.md` plus existing `security_approach.md`. |
| 065 Privacy impact assessment | Partial | Data boundaries documented; signed DPIA remains external. |
| 066 Supply chain and dependency review | Implemented | Frozen web lockfile, pinned CI actions, production bundle budgets, high-severity dependency audit, peer-contract checks, tracked-only release manifests, and retired unused Cloud Function dependencies run locally and in CI; license/legal review remains phase 067. |
| 067 License and third-party service review | Partial | License/dependency docs exist; legal review remains external. |
| 068 CI/CD quality gates | Partial | The workflow defines Linux/Windows backend plus web audit, peer check, type check, test, production build-budget, isolated MySQL 8.4 migration/transaction integration, and container-build jobs. On PR #125 head `a3d1a36f9b12d7cc071781400ee96ed5cb5c61c7` (2026-10-17), workflow-dispatch run `39012513626` passed all nine jobs: four Windows backend shards, Linux backend, frontend, MySQL integration, and API/dashboard container builds. CodeQL run `39012198239` also passed. On current PR #125 head `8742943d5a2fa874d9a59963dab1adb35982e9c5`, run `39172131938` passed Linux backend/recovery rehearsal, frontend, MySQL 8.4 migrations and database integration, and both container builds; four Windows shards were queued and CodeQL checks remained pending at 2026-10-19 00:40 Europe/Amsterdam. The 2026-10-18 local Python run skipped database integration because no disposable MySQL URL was configured. The checkout remains 45 commits behind its configured remote with uncommitted work; hosted results do not validate these local edits, and the local result does not validate the remote PR. Live OAuth/provider use, public ngrok routing, deployment, and recovery remain separate acceptance gates. |
| 069 Release process, canary, and rollback | Partial | Checksum-bound releases plus quiescent ledger/source recovery and automatic ledger rollback are implemented; a production canary environment and signed recovery rehearsal remain external. |
| 070 Operator runbook | Implemented | `OPERATOR_RUNBOOK.md`. |
| 071 User guide and help system | Implemented | `user_guide.md`, dashboard next actions, setup drawers. |
| 072 Troubleshooting guide and error catalog | Implemented | `ERROR_CATALOG.md` centralizes startup/deployment, provider, API/gateway, HAI, review/reconciliation, backup/restore, and evidence-delivery failures with safe next actions; the operator runbook and readiness checks link into it. |
| 073 UI action audit | Implemented | `UI_ACTION_AUDIT.md`. |
| 074 Backend endpoint usage audit | Implemented | `API_USAGE_AUDIT.md`. |
| 075 Documentation truthfulness audit | Implemented | Unsupported claims and invented testimonials removed. |
| 076 Technical debt register | Implemented | `TECHNICAL_AUDIT.md`. |
| 077 Bug hunt log | Implemented | Worklog records emergency/support/truthfulness defects and fixes. |
| 078 Red-team review loop one | Partial | Static safety/provider boundary audit complete; no independent assessor. |
| 079 Red-team review loop two | Partial | Secret/runtime and fake-success review complete; no independent assessor. |
| 080 Red-team review loop three | Partial | Browser/action/API review complete; no penetration test. |
| 081 Non-technical user simulation | Partial | Activation checklist and browser walkthrough; named user acceptance remains. |
| 082 Autonomy-first product review | Implemented | Eligible local work runs autonomously with review/external safety gates. |
| 083 Value review | Implemented | Dashboard prioritizes exceptions, decisions, delivery, and recovery. |
| 084 Product realism review | Implemented | Live/supervised/blocked connector truth is explicit. |
| 085 Requirements traceability | Implemented | This matrix and critical-path evidence. |
| 086 Task graph and dependency map | Implemented | `TASK_GRAPH.md`. |
| 087 Codex worklog and checkpoints | Implemented | `CODEX_WORKLOG.md` and `CODEX_CHECKPOINTS.md`. |
| 088 Context-loss resume safety | Implemented | Checkpoint file identifies first pending gate and evidence. |
| 089 Progressive stabilization gates | Implemented | Baseline, focused tests, full verification, browser, hygiene, delivery checkpoints. |
| 090 No vanity work rule | Implemented | Changes address safety, diagnostics, truthfulness, and operations. |
| 091 Feature-level definition of done | Implemented | Status definition and acceptance contract in this matrix. |
| 092 Fresh-clone dry run | Implemented | Clean clone of `76e5fff` passed exact remote identity, clean status, Python compilation, Compose parsing, frozen install, dependency audit, TypeScript, all 161 web tests, and production build budgets. |
| 093 Manual verification evidence | Partial | Local browser, Windows, HAI, container, and isolated recovery evidence passed; production-sized recovery rehearsal and live provider acceptance remain external. |
| 094 Final no-excuses search | Implemented | Code search, staged runtime-path scan, secret-pattern scan, release archive inventory/checksum verification, and `git diff --check` passed. |
| 095 Completion matrix | Implemented | This document includes every phase. |
| 096 Final verification report | Implemented | `FINAL_VERIFICATION_REPORT.md` records local, browser, container, fresh-clone, safety, and provider-boundary results. |
| 097 Final response requirements | Partial | Can be complete only in the delivery response. |
| 098 Post-completion maintenance plan | Implemented | Technical debt priorities and operator verification cadence documented. |
| 099 Roadmap and blocked items | Implemented | Exact provider/deployment/privacy blockers listed in audit and verification docs. |
| 100 Real-provider cleanup and account safety | Partial | No real mutation in this audit; live account acceptance requires the owner. |
| 101 Support/debug bundle design | Implemented | Sanitized CLI/API/dashboard ZIP with privacy tests. |
| 102 Data retention and archival policy | Implemented | Retention/compliance services and evidence-gated Drive archival. |
| 103 Migration from prototype to production | Partial | Local operational product, maintenance recovery, and supported release packages work; duplicate prototype launchers and placeholder helpers are retired. Live deployment, provider, accountant, and legal acceptance gates remain. |
| 104 Operator safety stop and emergency controls | Implemented | Persistent audited stop, per-step checks, dashboard/API/HAI policy. |
| 105 User onboarding and first-run wizard | Implemented | The operator dashboard derives a dependency-ordered five-step activation path from authoritative Drive, Gmail, Wave, receipt-executor, and queue-wide review readiness. It shows exact progress, highlights one current step, opens the matching existing setup/review surface, advances only when live readiness changes, and never treats setup progress as provider execution. |
| 106 Role-based settings and team permissions | Partial | Admin/operator gating exists; granular multi-user team permissions remain. |
| 107 Quality scoring and confidence display | Implemented | Confidence, evidence, extraction and review status are retained/displayed. |
| 108 Human decision minimization | Implemented | Eligible local repair and processing automate; queue focuses exceptions. |
| 109 Exception-based workflow dashboard | Implemented | Exceptions, reviews, blockers, recovery, and next actions are first-class. |
| 110 Safe retries and recovery strategy | Implemented | Backoff, leases, idempotency, stale-run finalization, source-proven dependency continuation, and backup/restore. Recovery cannot replay approved exports or unrelated queued work. |
| 111 Ambiguous external action resolution | Implemented | Ambiguous/supervised/export/attachment states fail closed. |
| 112 Versioning and changelog discipline | Partial | Git history and worklog exist; formal release changelog/version automation remains. |
| 113 Regression baseline | Implemented | CI and local backend/web suites. |
| 114 Maintenance and refactoring review | Implemented | External-looking unsafe entrypoints, duplicate checkpoint orchestration, synthetic learning, generic performance/migration wrappers, dummy tax output, and unused dependencies are retired. Retained posting compatibility is explicit and fail-closed. |
| 115 Final human-operator readiness test | Blocked | Requires owner-run live provider acceptance and explicit human sign-off. |

### Current Verification Addendum (2026-10-17)

- Cookie-backed tRPC mutations now require an exact same-origin `Origin` and
  reject cross-site Fetch Metadata in both server entrypoints. Legacy OAuth JWTs
  now use durable hashed session records and logout revocation; migration
  `0009_revocable_oauth_sessions` must be applied before deployment, and old
  unregistered tokens require sign-in again. The live MySQL migration remains
  unverified.
- The latest 505-document Windows stack rehearsal refreshes and verifies the
  dashboard snapshot after seeding before benchmarking; see
  `production-verification.md` for current measurements and scope limits.

### Current Verification Addendum (2026-10-19)

- Required MySQL integration passed on PR #125 head
  `8742943d5a2fa874d9a59963dab1adb35982e9c5`: tracked migrations applied and
  MySQL-backed audit/transaction tests passed. The main-branch ruleset now
  requires this status check and still requires zero approvals.
- Focused local verification passed 64 Drive archive/archiver tests with 35
  subtests, 29 Windows launcher/ngrok tests, and 27 HAI connector tests with 10
  subtests. These are subsystem checks, not whole-system or live-provider
  acceptance.
- The same PR run's four Windows shards were still queued and its CodeQL checks
  pending at the last read. The checkout has staged and unstaged changes and
  trails its configured remote by 45 commits; those changes have not been
  rebased, committed, or pushed. The requested repository-wide Codex Security
  scan remains unrun because this session cannot provide its required managed
  filesystem profile.

### Current Verification Addendum (2026-10-20)

- A full local backend run passed 2,022 tests, skipped 9, and passed 224
  subtests in 1,302.23 seconds. It used an OS-temp basetemp outside the checkout
  with pytest cache disabled. A prior attempt using a checkout-local basetemp
  produced setup errors because cleanup tests removed its parent; that run was
  discarded as invalid environment setup. The full pass preceded the nested
  SQLite backup fix below.
- Fixed nested SQLite backup handling: direct and recursive references now
  reuse one consistent database snapshot; transient `-wal`/`-shm` files are
  excluded from directory archives and cannot be backed up independently. The
  new open-WAL regression plus backup-manager tests passed: 11 passed, 2
  skipped, 9 deselected, and 3 subtests.
- The existing local, unstaged correction to migration
  `0007_idempotent_document_registration.sql` aligns canonical remapping joins
  with the source-column collation used for grouping and the unique index. Its
  case-insensitive `ABC`/`abc` cleanup was executed against disposable MySQL
  8.4 and correctly retained the canonical document, remapped references, and
  updated audit identity. The full Drizzle host-to-container migration command
  timed out despite a reachable TCP port; the complete MySQL integration suite
  is not claimed as locally verified.
- Current local web validation passed TypeScript, 502 Vitest tests (7 skipped),
  and the production build with bundle budgets. The remote PR #125 head
  `8742943d5a2fa874d9a59963dab1adb35982e9c5` now has every required check and
  CodeQL green, but remains blocked by two unresolved review threads covering
  the nested SQLite backup and MySQL collation findings. The corresponding
  fixes are only in this local working tree; the PR has not been updated.
- The checkout remains heavily staged/unstaged and 45 commits behind its
  configured remote. No rebase, commit, or push was performed. The managed
  Codex Security Deep Scan remains unavailable without its required filesystem
  permission profile; passing CI and CodeQL are not substitutes for that scan
  or an independent penetration test.
