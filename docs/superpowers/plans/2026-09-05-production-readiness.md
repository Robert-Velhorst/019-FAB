# Windows and VM Production Implementation Plan

**Goal:** Execute the approved single-business Windows and Docker/VM hardening design.
**Spec:** ../specs/2026-08-30-fab-production-readiness-design.md
**Architecture:** Retain Python/SQLite, its owned worker, and the React/Express gateway. Add explicit deployment validation, managed operator authentication, and reproducible deployment checks to these existing entrypoints.

## Constraints and Decisions

- No provider writes, source archival, credential rotation, live restore, commit, or publication during implementation.
- Preserve the current local workflow. Explicit `windows` and `vm` deployment profiles enforce additional production requirements; `local` remains the compatibility default.
- VM runs one business on one host. Use a separately configured operator secret and signed browser session without requiring the optional SaaS database.
- Host volume encryption is an operator attestation, never an inferred fact. Recovery rehearsal uses synthetic source files in a disposable directory.
- Use a feature branch and preserve the existing untracked approved specification.
- Parallel workers own disjoint web startup and authentication files; parent owns Python, Compose, documentation, and integration.

## Tasks

- [x] Refresh tracked repository state and read approved specification.
- [x] Python: implement shared secret-file loading and deployment preflight; enforce it before API and worker startup, expose redacted findings through readiness/doctor.
- [x] Web startup: explicit proxy trust, validated exact production binding, bounded shutdown, secret files, and profile validation.
- [x] Remote operator: reachable login, signed bounded sessions, logout, CSRF protection, and gateway authentication for the VM deployment.
- [ ] Deployment: resource-bounded VM Compose profile, durable data/secrets, health checks, proxy configuration, Windows integration and rotation instructions.
- [x] Recovery: exercise source-complete backup and restore in isolation; verify evidence bytes and ledger integrity.
- [ ] Verify Python suite, web tests/typecheck/build, dependencies, container profile and browser smoke. Fix failures with focused regressions.
- [ ] Review combined implementation; document measured evidence and unresolved external acceptance gates.

## Verification Commands

Run the project Python 3.13 executable with `-m pytest -q -p no:cacheprovider -p no:stepwise`; use test-scoped synthetic data only. Run `pnpm.cmd --dir web check`, `pnpm.cmd --dir web test`, `pnpm.cmd --dir web build`, and `pnpm.cmd --dir web audit --audit-level=high`. Parse the VM Compose configuration with synthetic environment values. Start isolated services on unused loopback ports for HTTP/browser checks. Treat unavailable Docker engine or provider credentials as missing evidence, never passing checks.

## Progress

2026-09-05: Branch `hardening/windows-vm-production` created from `5002f3a`. Approved specification remains present. Two independent web tasks dispatched; Python implementation and deployment integration remain on the parent thread.

2026-09-05: Windows and VM configuration, startup/authentication integration,
synthetic recovery and focused regression checks implemented. Deployment and
verification tasks remain open for actual host/browser/container acceptance and
the broader storage/session/security gaps listed in `docs/production-verification.md`.
No publication, live restore or provider mutation was performed.

2026-09-06: Backup inspection now separates metadata from byte verification,
bounds manifest parsing/cache retention, and revalidates the reopened restore
archive. The complete local Python suite passed: 953 tests, one skipped test and
59 subtests, exit 0. The synthetic recovery CLI passed all 13 checks. Earlier web
verification remains recorded separately. Actual Windows/VM/ngrok deployment,
provider acceptance and the remaining security/resource boundaries keep the
broader deployment/review tasks open; no publication or live restore occurred.

2026-09-06: Fixed the ngrok start/verifier credential mismatch using a shared
read-only resolver; configured values/files now take precedence without creating
replacement credentials. Windows dashboard startup now retains an explicitly
configured public ledger URL while its internal API stays on loopback. Original
regressions failed before the fixes. The focused Windows/configuration/secret/
preflight suite passed 94 tests and 18 subtests. Actual tunnel/host acceptance is
still open; this increment did not start services, expose an endpoint or change
provider records. The earlier full Python suite is a separate snapshot.

2026-09-06: Extended Windows failure rollback across service spawning, dashboard
build/readiness and runtime registration. New workers wait for API/dashboard
identity endpoints; prior runtime metadata is retained through atomic replacement.
Review-driven regressions fixed rollback targeting a different dashboard listener
and strict warning preferences interrupting cleanup. The combined focused suite
passed 116 tests and 18 subtests, including real metadata filesystem tests on both
PowerShell versions. Real process-tree termination, host restart, provider access
and broader production acceptance remain unverified; no live service was launched.

2026-09-06: Startup rollback now uses original process instances and validates
discoverable descendants by creation time; 8 actual disposable-process cases
passed across PowerShell versions. Worker readiness now checks matching live
runtime registration, ancestry and late-result rejection, with configured CIM
operation timeouts. Combined verification passed 133 tests and 18 subtests, with
one strict expected failure proving an unresolved detached-descendant gap.
Launch-time Job Object containment and equivalent normal-stop hardening are the
next Windows lifecycle requirements, not waived acceptance items. No bookkeeping
services, provider calls, commits or deployments occurred.

2026-09-06: Implemented native Windows job containment for newly started API,
dashboard and worker processes. Atomic JOB_LIST assignment closes the suspended
create/assign crash window; a non-inheritable root-owned keeper allows launcher
exit without a resident supervisor and kills descendants on root exit. Failed or
cancelled starts unwind through guarded finally before disposing launcher handles.
The departed-intermediate regression now passes without an expected-failure marker.
Runtime fingerprints include the launcher helpers. Native crash and cancellation
regressions both failed before their fixes; the follow-up passed 33 tests before
adding Windows PowerShell cancellation coverage to the final combined run.
Normal Stop-FAB ownership, hard multi-service handoff crash reconciliation and
actual Windows/VM host acceptance remain open. No financial/provider services,
commits, pushes, live restores or deployments were performed.

2026-10-01: Hardened normal Windows shutdown with recorded named native process
groups, exact root start-time identity, and per-service containment coverage.
Unresponsive services no longer depend on endpoint discovery for native cleanup.
Partial/unreadable/foreign records and unverified legacy descendants cannot
authorize clearing runtime metadata or leases. Foreign folder-prefix matching,
recycled dashboard ancestry, discovery short-circuiting, and virtual-environment
Python redirector ownership were corrected. Combined focused verification passed
101 tests and four subtests; see the dated production-verification record for
scope and remaining acceptance gates. Production host/provider acceptance and
hard multi-service handoff reconciliation are not complete.

2026-10-01: Fixed non-atomic force-release auditing and the 500-HAI-command
shutdown cutoff. Lease deletion and each audit now share one scoped SQLite
transaction with bounded keyset pages; audit/owner failure rolls back every page.
Windows lifecycle mutex serialization and worker maintenance ownership cover
cleanup and old runtime-record removal, including missing worker registration.
Loopback-only identity probes reject URL credentials and block redirects on both
PowerShell versions, verified with real disposable HTTP servers. The final
combined focused run passed 154 tests and four subtests; eight overlapping lease
tests passed again after fixture teardown tightening. No provider writes,
financial-data changes, live service launches, commits, or deployments occurred.
Unmanaged APIs/cross-session coordination and the wider production acceptance
requirements remain open, not waived by these tests.

2026-10-01: Production packaging now uses portable Node preloads, secret-file-aware
Python health checks and restricted source-only Docker contexts, with seven
packaging/probe regressions passing against synthetic data and real disposable
local scratch images. Remote operator secret parsing now reuses bounded strict
deployment parsing after three malformed-input regressions reproduced failures.
Both production web entrypoints pass loopback HTTP startup/authorization smoke
checks. Final web verification passed 335 tests, type checking and production
build budgets. Full application container/host acceptance remains open.

2026-10-01: Fresh complete Python run finished with 1112 passed, one failed,
one skipped and 81 passed subtests. The failing legacy inline-cleanup assertion
was updated to the audited batch helper; a strengthened regression also exposed
and fixed recorded-process termination happening before tunnel shutdown. Wider
post-fix Windows regressions passed 88 tests and 22 subtests; 14 overlapping
checks include a new simulated tunnel-failure retention test. The complete
Python run predates that shutdown-order fix, so whole-suite acceptance remains
open. No real tunnels, provider mutations or publication occurred.

2026-10-01: Shared Python secret loading now validates the opened descriptor,
uses bounded binary/nonblocking reads where available, and rejects malformed
file bytes instead of silently normalizing them. Runtime fingerprints include
web startup/build scripts. Red regressions preceded both fixes; 76 focused tests
and 24 subtests passed. Actual local Python image builds and offline non-root
native OCR/PDF/API/SQLite/HAI scope smoke checks passed before and after removing
the unnecessary graphics dependency. Docker-reported image size fell 15.99
percent with matching Python version inventories. All disposable containers and
unique image tags were cleaned up. See production-verification for exact scope;
reproducible dependencies and full stack/host/provider acceptance remain open.

2026-10-01: Reproduced and fixed non-ASCII request credentials causing API/login
500s, plus gateway/backend token-format mismatches. All profiles now validate
shared API tokens before ledger access; web backend credentials share bounded
ASCII/size/absolute-file rules without changing JWT/operator contracts. Wider
Python regressions passed 202 tests; 56 overlapping follow-up tests include real
Waitress HTTP authorization and accepted size boundaries. Web tests (341), type
checking and production build passed. Full target-host/provider acceptance and
a refreshed complete Python suite remain open; no external mutations occurred.

2026-10-01: Gateway transport regressions reproduced redirect replay and false
success from malformed JSON/body aborts before correction. Gateway and source
previews now refuse redirects; response reads enforce streamed byte limits.
Gateway cancellation and timeout validation are preserved, strict UTF-8 object
responses are required, and uncertain mutations invalidate cached snapshots
without retry. All 366 web tests, type checking and production build budgets
passed, including disposable real HTTP redirect/stalled-body checks. No live
financial data or external writes were used. Full host/provider acceptance and
a refreshed complete Python suite remain open.

2026-10-01: Preview HTTP regressions reproduced missing disconnect cancellation
and unbounded concurrency. Downloads now abort on disconnect, have a four-slot
per-instance ceiling through response finish/close, reject excess reads without
queuing, validate limits and distinguish timed-out from unrelated aborts. All
378 web tests, type checking and production build budgets passed. The approved
dependency audit found 19 advisories; selected same-major dependency updates
reduced the fresh audit to no known vulnerabilities. Full host/provider acceptance
remains open; no external records or deployment changed.

2026-10-01: The complete Python run finished with 1138 passed, one failed,
one skipped and 83 passed subtests. Its sole failure was a 15-second Docker
information-probe timeout before build-context assertions. The fixture now pins
all commands to its verified local context and has a bounded cold-start probe
allowance without relaxing exclusion checks or skipping timeouts. Eight focused
Docker context/health/startup tests passed afterward. A fresh complete rerun and
full target-host/provider acceptance remain open, not inferred from these checks.

2026-10-01: Added a reusable actual production-dashboard/Waitress/SQLite rehearsal.
All 23 synthetic-only HTTP checks passed on Windows with Node 24 and 25, including
persisted corrections, checked source bytes, handoff replay denial and cross-service
logout revocation. Isolated environment, existing-ledger refusal and child cleanup
guards pass. Prepared Linux and single-shard Windows CI wiring and updated README;
hosted CI, Node 22, browser rendering and real TLS/ngrok/provider acceptance remain
open. Targeted local Docker-builder inspection replaces unrelated enumeration;
11 combined guard/context tests passed. No publication or provider writes occurred.

2026-10-01: The next complete Python run ended with 1139 passed, one failed,
one skipped and 83 passed subtests. The web-context Docker build exceeded its
90-second deadline. That run predates the targeted-builder refinement and new
guard tests; the later eleven-test focused pass does not replace full acceptance.
The build deadline and exclusion requirements were not weakened. A subsequent
container inventory probe was cancelled while unresponsive; image inventory
finished empty. Docker engine/container acceptance and a clean full rerun remain
open. No shared engine restart, live provider action or publication occurred.

2026-10-01: Hardened retained-source previews against disappearance after the
path check, non-regular opened descriptors, opened-size substitutions and growth
beyond the 25 MiB cap. Binary bytes are preserved and descriptor closure is
verified, including wrapper failure. Six new regressions are included in a
137-test API/auth/session pass; nine source/rehearsal guards passed separately.
Two stack attempts timed out on review correction; bounded redacted RPC status
diagnostics and synthetic stalled-thread tracing were added without increasing
the mutation deadline or replaying writes. Two subsequent Node 24 runs and one
Node 25 run each passed all 23 actual dashboard/API/SQLite checks. The earlier
timeout cause remains unresolved, and there is no hard OS-level filesystem
deadline or total-memory claim. Full production acceptance remains open; no
provider action, commit, publication or deployment occurred.

2026-10-01: Failure injection proved partial commits in review corrections and
direct normalized document/bank record creation/resolution. Added a shared local
write transaction with writer reservation, nested savepoints, rollback, context
cleanup and read-only escalation rejection. Wired review decisions, exact-vendor
propagation and normalized records to it. The measured review fixture now opens
one connection rather than 17; no total-RAM or throughput claim is made. Nineteen
transaction regressions pass, including competing decisions, external readers,
batch rollback, interrupts and separate redacted API error auditing. The wider
226-test/12-subtest and separate 63-test runs pass. Final real stack rehearsals
passed all 23 checks on Node 24 and 25. Timeouts remain uncertain outcomes, not
automatic retry permission. Updated README and error guidance. Full-suite and
real host/container/ngrok/provider acceptance remain open; no publication or
external financial mutation occurred.

2026-10-01 reconciliation follow-up audit items (not completion evidence):
- Verify missing-receipt-only resolution and reopening where there is no linked
  document; document-linked review tests do not prove that path.
- Test evidence changes between matching computation and result persistence;
  atomic persistence alone is not proof of snapshot freshness.
- Review the one-open-review-per-document/reason policy when multiple bank
  candidates point at a document. Do not silently discard a candidate or bypass
  financial/duplicate/provider approval gates to achieve closure.

2026-10-01: Reconciliation result persistence and match resolution now share the
ledger write transaction; matching computation precedes this service's writer
reservation. Failure injection proved partial commits before repair. Corrected
document review closure by exact match ID, `needs_review` reopening/preservation,
normalized gate refresh order, 50-row lookup cutoffs and false created-review
counts. Malformed/boolean/fractional links remain pending. Seventeen regressions
pass; the final wider run passed 224 tests and three subtests. Expanded the real
stack rehearsal to synthetic dashboard bank import, reconciliation and approval;
all 31 checks passed on Node 24 and 25. Missing-receipt-only, snapshot-freshness
and multi-candidate review audits listed above remain open, alongside full
host/container/ngrok/provider acceptance. No external financial mutation,
commit, push or deployment occurred.

2026-10-01: Addressed the missing-receipt-only and run-time freshness follow-ups
with local failure/race evidence. Documentless dispositions close/reopen only
their own gates; unsupported final confirmation returns 400 without mutation.
Bounded keyset lookup avoids document histories and growing offsets. Read-snapshot
fact hashes and writer-locked revalidation reject changed selected facts,
changed default pools and malformed/conflicting/duplicated stored-bank references.
SQL excludes completed documents before applying the limit, preventing a later
run from undoing completed reconciliation or starving older open candidates.
The final wider run passed 247 tests and three subtests; 24 focused checks pass.
The expanded 39-check real stack rehearsal passes on Node 24 and 25, including
actual HAI financial-disposition denial. No provider, archival, deployment or
publication action occurred.

Remaining reconciliation acceptance includes approval-time staleness (after
candidate creation, before a later human decision), the multi-bank-candidate
policy and larger/long-running workload acceptance. Selected-row/default-pool
run-time proofs do not establish every freshness invariant. Full target-host,
container, rendered-browser, ngrok/provider and complete-suite gates remain open.

2026-10-01: Reproduced and repaired final-approval staleness, nested review failure
commits, duplicate-status normalization bypasses, malformed/mismatched review
links and double confirmation against one document. New candidates retain local
fact hashes; writer-locked pre/post-edit checks reject stale confirmation and
roll back nested review edits/learning. Legacy candidates remain recoverable via
matching refresh. JSON/form routes and the dashboard gateway expose conflicts
without replay. Final wider verification passed 271 Python tests / three subtests;
28 focused checks, 379 web tests with two workers, type checking and production
build budgets pass. The rebuilt 41-check synthetic stack passes on Node 24/25.
The initial unrestricted web test run timed out in one startup check; the bounded
rerun passed without changing its deadline. This is not unrestricted-concurrency
acceptance. Remaining work includes non-confirmation staleness policy, richer
multi-candidate/partial-payment selection, complete suites, larger workloads and
the original target-host/container/provider/recovery acceptance gates. No live
provider write, archival, publication or deployment occurred.

2026-10-01: Reproduced and repaired stale stored-bank exception disposition,
old-match takeover of newer/completed decisions, cross-account external-ID
collisions and open bank-work starvation behind a completed batch. Bank evidence
refresh updates its retained review snapshot as well as the match; competing
batch selection preserves earlier pending candidates. Existing scoped SQL and
indexes avoid loading full reconciliation histories and filter open bank rows
before their limit. Final focused verification passed 101 tests; the expanded
43-check built stack passes on Node 24/25. An earlier reconciliation timeout
remains unexplained, with fixture-only tracing broadened for future diagnosis.
Full final-code suites, workload/resource benchmarks, target-host/container/
provider/recovery gates, richer selection and superseded exception cleanup
remain open. No external financial mutation, archival or publication occurred.

2026-10-01: Added atomic, auditable supersession of open missing-receipt exceptions
after confirmation against the same imported bank row. Original snapshots and
closed human decisions remain intact; another account or an unconfirmed candidate
cannot trigger closure. Bounded primary-key pages withstand second-page failure
with full rollback. Strict shared reference checks preserve ambiguous reviews,
and native filtering returns one relevant row instead of a 206-row backlog in
the measured fixture. Integer-string compatibility is tested. Fourteen dedicated
supersession tests and the 94-test focused set pass; final wider verification
passed 309 Python tests and three subtests. Both Node runtimes pass the expanded
49-check real built dashboard/API/SQLite rehearsal, including retained synthetic
bytes, arrival, approval, history links and queue refresh. No provider/archive
authority follows from local supersession. Full suites, target-host/container/
rendered-browser/provider/recovery acceptance, resource/latency benchmarks,
legacy resolved-match/open-review repair and richer candidate selection remain
open. No external financial mutation, archival, commit, push or deployment occurred.

2026-10-01: Added two rebuildable reconciliation lookup indexes with shared,
JSON-validity-guarded query expressions. Four tests verify actual indexed plans,
keyset ordering without temporary sorting, row preservation over legacy damaged
JSON, integrity and equivalent results with reduced SQLite instruction work.
The synthetic 2,001-review lookup decreased from 28,054 instructions to 53;
this is not a whole-app resource or speed guarantee. Both Node 24/25 built-stack
rehearsals pass all 49 checks. Initial index-build locking/latency, index disk and
write costs and production workload benchmarks remain acceptance gates. Existing
approval, strict reference validation and downstream archive boundaries remain.
Final selected regression run passed 358 tests and three subtests, with one
skipped test, in 220.21 seconds across 22 modules including backup/recovery.
This is not full-suite or live-provider acceptance; the goal remains active.

2026-10-01: Added evidence-checked identical-status confirmation acknowledgment
and atomic repair of exactly linked completed/superseded open review tasks through
the existing dashboard review action. Original final decisions and supersession
history stay unchanged; normal approval retains reconciliation reference metadata.
Changed evidence, missing hashes, conflicting pointers and financial corrections
during repair remain blocked. This is operator-triggered, not blanket legacy
cleanup or autonomous approval. Provider/archive, full-suite, real-host, rendered
UI, ngrok/container/recovery and workload acceptance remain open.
Final code passed 378 selected Python tests and three subtests (one skipped),
including 20 new retry/repair cases, in 590.22 seconds across 23 modules. Both
Node 24/25 real built-stack rehearsals pass all 51 checks. These are synthetic
local acceptance results, not full-suite, provider or deployment certification.

2026-10-01: Fixed false stale-evidence rejection for ordinary cent values by
comparing canonical Decimal amounts on both sides. Added finite numeric guards
across bank import, ledger writes, VAT/receipt/confidence validation and matcher
configuration, retaining invalid-row skips and explicit-field precedence. Final
focused verification passes 103 tests; the 51-check built-stack rehearsal uses
EUR 4.28 and passes on both Node versions. Final full Python regression passed
1,386 tests and 83 subtests, with one native directory-symlink check skipped for
unavailable Windows privilege, in 1,273.76 seconds. Both real Docker context
checks passed; this is not container deployment proof. A fresh backup/rehearsal
guard run passed 20 tests with the same skip. Older Python failure/selected-suite
snapshots no longer describe this local acceptance state.
No live financial writes, archival, shared restart or publication is authorized
by these local results. Fixed-point storage, complete currency/locale matching
and workload/deployment/provider acceptance remain open.

2026-10-01: Added independent captured bank JSON for direct reconciliation,
finite amount/date row checks and bounded 500-row/4 MiB/32-level/50,000-value
processing. Mutated matching input, non-finite nested metadata, non-string keys,
cycles and oversized batches are rejected before financial writes. Explicit
null/false arrays no longer silently become empty batches; JSON/form API paths
use the same service guard. Final selected verification passed 455 tests and
three subtests across 22 modules (27 new input cases). Both Node versions pass
the expanded 53-check real stack, including malformed HTTP rejection; three
fresh rehearsal guard tests pass. A client-side negative-fixture encoding error
was reproduced and corrected before the final real HTTP checks. The previous
full Python snapshot is historical; new-code full-suite, raw HTTP limits,
ad-hoc account identity, historical JSON, workload and live acceptance remain
open. No financial provider writes, archival, commit, push or deployment occurred.

2026-10-01: Reproduced eleven direct-account identity failures, then separated
ad-hoc matching history by exact account and bank reference. Conflicting account
aliases/non-string labels and same-account within-batch duplicates now reject
without financial writes; unscoped legacy ownership is not inferred. The new
derived partial lookup index is exercised by the real query and installs on
reopen without rewriting historical rows, including malformed JSON. Final
selected verification passed 473 tests and three subtests across 24 modules in
353.12 seconds. A separate ledger/backup regression passed 76 tests with one
Windows privilege-dependent skip. Both Node versions passed 56 real built-stack
checks, including operator-authenticated account separation and duplicate
rejection; fresh rehearsal guards passed three tests. No provider mutations,
archival, shared restart, commit/push or deployment occurred. Full-suite and
live production acceptance, historical unknown ownership and workload/resource
profiling remain open; the full production goal is not complete.

2026-10-01: Reproduced 18 inconsistent-reference failures, including imported
bank aliases selecting one identity for freshness and another for recording,
and a legacy candidate incorrectly confirming that mismatch with a retained
hash. Direct input now validates exact external references and all account
aliases before matching; bank freshness, approval, disposition, retry and
history reuse reject inconsistent identities. Final selected verification
passed 496 tests and three subtests across 25 modules in 271.57 seconds. Both
Node versions passed 58 built-stack checks, including authenticated invalid-
reference rejection without financial writes; fresh isolation guards passed
three tests. No web source changed in this phase, no provider writes/archival
or publication occurred. Full-suite revalidation, anonymous/legacy identity
repair, measured workload performance and live acceptance remain open.

2026-10-01: Prepared matcher fields once per run and avoided fuzzy matcher
allocation for identical normalized vendors, retaining custom scorer contracts
and exact result ordering/scoring. A deterministic fixture reduces amount
parsing from 16,000 calls to 180. Two repeated synthetic 500-bank/100-document
benchmarks show 2.6-3.4x and about 8x matching speedups, with identical results;
per-run traced allocation peaks increase by 28,243/29,366 bytes. This is a CPU/
bounded-storage tradeoff, not whole-app RAM reduction or production latency
proof. The reusable benchmark and JSON phase record are saved in the repo.
Final selected regression passes 521 tests and three subtests across 28 modules
in 456.73 seconds; both Node versions pass the 58-check built stack. No live
provider mutation, archival, shared restart or publication occurred. Full-suite,
process-RAM/mixed-workload profiling and live acceptance remain open; the full
production goal remains active.

2026-10-01: Preserved selected ambiguous retained bank history instead of
overwriting it or creating replacement records. Shared validation rejects
present null/malformed/conflicting persisted-bank aliases; strict bank identity
disagreement with SQL's numeric prefilter also blocks the operation. Sixteen
new cases cover these failures plus valid positive string forms. Focused
verification passes 139 tests. Fresh complete Python verification passes 1,483
tests and 83 subtests, with one native directory-symlink case skipped for
confirmed WinError 1314, in 933.05 seconds; both real Docker context checks pass.
Fresh frontend verification passes 379 tests across 32 files, type checking and
production build/size budgets. Both Node versions pass all 59 application checks
again after rebuilding. Earlier full-suite snapshots are historical, not current
acceptance gaps for this Python source. No live financial mutation, archival,
shared restart or publication occurred. Unselected legacy corruption, rendered
UI, process-RAM/workload and real production/provider/attachment/archive/recovery
acceptance remain open; the full goal remains active.
