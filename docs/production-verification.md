# Production Hardening Verification

Date: 2026-09-05. Branch: `hardening/windows-vm-production`, based on `5002f3a`.
These are development checks, not a claim that an installed business system has
passed production acceptance. No real ledger was restored, provider record changed,
Drive source archived, or credential rotated during this work.

## Implemented

- Explicit Windows/VM startup validation before API/worker ledger access.
- Bounded mounted-secret inputs, strong-secret checks and redacted failures.
- Exact production ports, explicit proxy IP trust and bounded web shutdown.
- Single-administrator VM sign-in, HTTPS/origin checks, short signed sessions,
  logout/restart revocation, and authorization across gateway/handoff/preview paths.
- Managed-mode denial of the unrelated legacy OAuth callback.
- Windows launcher credential-file normalization and production preflight.
- Read-only configured/stored API and HAI credential resolution for ngrok scripts;
  Windows dashboard startup preserves the configured public ledger origin.
- Windows startup rollback covers service/build/readiness/registration failures;
  new workers wait for API/dashboard endpoints, and runtime publication is atomic.
- New Windows service roots use launch-time Job Objects for rollback/lifetime
  management, with no resident supervisor. Legacy cleanup retains process handles
  and checks creation times. Worker startup requires matching live registration and
  rejects results arriving after its acceptance deadline.
- Standalone VM Compose configuration with resource limits, durable ledger,
  recovery, provider-credential and OAuth-token volumes, and loopback publication.
- Disposable full-ledger/source recovery rehearsal; CI configuration/recovery steps.
- PDF OCR rendering and subprocess limit hardening, tracked separately below.

See [Windows setup](windows-production.md), [VM setup](vm-production.md), and
[error catalogue](ERROR_CATALOG.md) for operator instructions.

## Captured Evidence

| Check | Observed result |
| --- | --- |
| Atomic native containment and startup cancellation follow-up, 2026-09-06 | 33 passed in 106.62 seconds, exit 0. Includes 14 native cases across PowerShell 7/Windows PowerShell, including forced launcher death immediately after creation; 19 startup tests include real runspace cancellation with synthetic services. A subsequent regression run includes cancellation on Windows PowerShell too. |
| Windows native containment, lifecycle wiring and launcher/fingerprint checks, 2026-09-06 | 48 passed and 3 subtests passed in 73.89 seconds, exit 0. Includes 12 actual disposable native-process tests across both PowerShell versions and extracted startup paths with synthetic service/network boundaries. No actual FAB service or provider calls. |
| Earlier Windows process cleanup, worker registration and surrounding launcher/configuration regressions, 2026-09-06 | 133 passed, 18 subtests passed and 1 expected failure in 157.01 seconds, exit 0. That snapshot exposed the detached-descendant gap addressed by the later native containment follow-up below. Includes 8 real disposable process cases across PowerShell 7/Windows PowerShell. No bookkeeping services or provider calls were started. |
| Additional registration deadline coverage, 2026-09-06 | 4 tests passed in 17.02 seconds after adding delayed-ancestry rejection and actual helper-to-CIM timeout-argument assertions across both PowerShell versions. No production code changed after the combined run above. |
| Windows startup rollback, metadata, ngrok, configuration and secret regressions, 2026-09-06 | 116 passed and 18 subtests passed in 81.12 seconds, exit 0. Includes 16 extracted-script startup cases with synthetic process/network functions and 6 real temporary-file metadata cases across PowerShell 7 and Windows PowerShell. No real bookkeeping services or tunnels were started. |
| Windows/ngrok credential and public-origin integration, 2026-09-06 | 94 passed and 18 subtests passed in 106.65 seconds, exit 0. Includes actual isolated PowerShell credential/startup blocks, both Windows script parsers, secret-file/configuration/preflight regressions. No actual service or tunnel was started. This is a focused run after the full-suite snapshot below. |
| Web gateway, handoff and deployment regression checks after Windows URL wiring, 2026-09-06 | 91 tests passed across 4 files, exit 0. Existing gateway/session/deployment test fixtures, not a new live browser or tunnel acceptance test. |
| Independent Windows/ngrok credential and public-URL review, 2026-09-06 | No actionable findings in the scoped implementation/tests and immediate dependencies. Not a whole-repository security audit. |
| Complete Python suite after backup inspection hardening, 2026-09-06 | 953 passed, 1 skipped, 59 subtests passed in 2,149.31 seconds (35 minutes 49 seconds), exit 0. This supersedes the earlier non-green baseline below; it is local Windows evidence, not target-host/provider acceptance. |
| Broad Python suite before final integration | 858 passed, 1 failed, 1 skipped, 38 subtests. Failure was a new readiness-test assertion using `id` on existing issue objects without that field; corrected and rerun in the focused suite. Not a final full-suite green claim. |
| Final deployment, launcher, recovery and backup tests | 59 passed, 1 skipped (Windows symlink privileges). |
| Configuration, worker, export-worker, secrets, readiness and health regression tests | 76 passed. |
| Document processors and OCR resource bounds | 27 passed, 9 subtests passed. Synthetic converter/timeout fixtures, not native-parser isolation proof. |
| Web suite after authentication and browser form-policy fix | 306 passed across 30 files. |
| TypeScript | Passed after web integration and Windows loopback enforcement. |
| Production build | Passed; bundle-budget checks passed. Largest client JavaScript file 480,764 bytes; entry HTML 2,031 bytes. |
| Web dependency audit, all dependencies | No known vulnerabilities reported by the package audit at execution time. Not proof of absence of vulnerabilities. |
| Synthetic recovery CLI | All 13 checks passed, including source bytes, checksum, ledger rows, SQLite integrity, safety backup and cleanup. |
| VM Compose | Configuration parse passed. Docker engine runtime was not established. |
| Isolated HTTPS browser smoke | Login fits desktop 1440x1000 and mobile 390x844. Wrong password rejected; correct password accepted; secure cookie attributes checked; logout clears cookie; logged-out cookie replay denied. Real ledger deliberately absent. |

Test counts above are separate runs and must not be added into a unique-test total.
The 2026-09-06 full backend suite took about 36 minutes on this Windows machine.
Later dated runs are recorded in the follow-ups below; these are not controlled
comparative performance measurements. No claim of
90% resource reduction or 100% speed improvement is made.

The browser smoke initially found a form-policy regression: `no-referrer` on the
authentication page interfered with browser-generated form Origin checks. The
form now uses `same-origin`; null and cross-origin requests remain denied. The
regression test failed before the change, the final 306-test suite passed, and the
real browser workflow subsequently completed without assertion failures.

## Remaining Acceptance Gates

- Prove normal Stop-FAB and actual host lifecycle acceptance on the intended
  installation. Local process-instance hardening is implemented and tested in
  the dated follow-ups. Launch-time Job Objects address detached-descendant rollback
  gap, but adopted/legacy roots are not retroactively contained, and hard crashes
  during multi-service commit/publication still need reconciliation.
- Run the actual Windows launcher and the VM stack on their intended hosts.
  Validate permissions, restart, monitoring, TLS/proxy configuration, mounted
  secret access, volume persistence and an isolated upgrade/rollback rehearsal.
- Verify encryption for every sensitive location, including OS temporary storage,
  migration snapshots, logs, credentials and evidence. The preflight encryption
  flag is only an operator attestation and is not a startup-blocking host check.
- Complete Windows relocation/ownership review for logs, credentials, restored
  evidence and checkout-scoped worker coordination. Ledger/backup overrides do
  not automatically migrate these resources. Do not share a ledger between
  separate checkouts or Windows sessions without verified worker exclusion.
- Provision owner-authorized provider credentials and prove actual Google/Wave
  intake, approvals, downstream attachment readback and recovery on controlled
  records. VM desktop Google consent routes remain loopback-only.
- Managed dashboard logout now revokes linked Flask ledger access in local tests
  and a real two-service browser check (see follow-up below). The actual target
  host must still prove validation connectivity, restart and rotation behavior.
- The optional legacy SaaS OAuth flow now has browser-bound, one-use state (see
  follow-up below). Its real identity portal's state round-trip and registered
  callback URI still require provider acceptance; managed mode keeps it disabled.
- PDF rendering now calls Poppler directly for each page, avoiding pdf2image's
  untimed internal metadata/version probes. Rendering is sequential with a
  3,000-pixel longest-edge limit, bounded raster bytes, and a 30-second child
  timeout. A shared Tesseract document budget (300 seconds, configurable downward)
  is propagated through metadata, rendering and both OCR attempts. Late/partial
  output is rejected. These controls are not an OS sandbox or a hard deadline for
  in-process image preprocessing; native memory confinement remains host-owned.
- Security review is partial. Scanner sender authentication, backup decompression,
  other parsers, provider execution and unreviewed paths need further coverage.
  Findings and architecture were saved as a resumable audit draft; the separate
  security workbench report was not sealed because final artifact retrieval was
  unreliable. This document does not represent a completed whole-repository scan.
- Verify off-host recovery and business/accounting/privacy acceptance separately.

No commit, push, merge, deployment or external financial submission is implied
by these local results. The approved production design remains the full target;
this implementation does not redefine its unfinished acceptance work as complete.

## Follow-up: Windows Startup Rollback

Eleven initial regression cases reproduced startup failures leaving newly created
services running, deleting prior metadata or letting a cleanup error hide the
original failure. A separate ordering regression showed that the worker started
before API/dashboard readiness. The launcher now covers startup through runtime
registration with one rollback boundary, attempts each newly started service's
cleanup independently, retains prior metadata and delays a new worker until both
identity endpoints respond.

Review found two further issues, both reproduced before fixing: a discovered
dashboard PID could replace the launcher's own rollback target, and strict warning
settings could interrupt cleanup. The original spawned dashboard process reference
is retained independently, a mismatched listener blocks startup, and cleanup diagnostics are
explicitly nonterminating. Existing adopted services are not rollback targets.

Runtime registration writes a same-directory temporary file and publishes it with
File.Replace/Move. Actual tests across both Windows PowerShell versions covered
first creation, replacement and a locked destination; failed publication retains
the prior bytes and removes the temporary file. This is atomic publication, not
an assertion of power-loss durability. Later isolated process tests exercise real
discoverable-child termination and native containment; live worker progress,
production-host restart and provider acceptance remain unproven.

## Follow-up: Process Ownership and Worker Registration

The rollback helper now takes the original System.Diagnostics.Process object and
retains handles while checking discovered children's creation times. It scans
before and after stopping the parent, handles already-exited parent processes,
continues cleanup when a child fails, and reports discovery/termination failures.
Eight actual disposable-process cases passed across both PowerShell versions:
ordinary and already-exited parents, successful and deliberately failed discovery,
and an unrelated control process left running until fixture cleanup. These are
synthetic Python programs, not FAB's API, worker or provider executors.

An independent review identified the vanished-intermediate ownership gap. The
original strict expected-failure test demonstrated a surviving descendant that
discovery could not find. Its regression now launches through the native job helper
and checks the real startup rollback function without an expected-failure marker.
Uncontained legacy discovery retains this limitation. The normal Stop-FAB path was
not changed in this increment.

The native launcher creates suspended roots atomically inside the job before
allowing service code to run, restricts inherited handles to standard streams,
and retains original process instances. Uncommitted disposal and explicit job termination stop all normal job
members. A committed root retains a non-inheritable, zero-rights lifetime handle;
tests confirm the service survives its launching PowerShell exit and its remaining
child exits after natural root shutdown. Tests also cover departed intermediates,
unrelated process preservation, argument quoting, scoped environment, log output
and native creation failure releasing log handles on both PowerShell versions.
There is no persistent helper process. This is not a hostile-code sandbox or
evidence of live FAB/provider acceptance. See [Windows setup](windows-production.md)
for host-policy, adopted-runtime and crash-reconciliation limitations.

Review also exposed cancellation bypassing catch after keeper transfer and the
separate create/assign crash window. A real PowerShell runspace cancellation test
failed with no services stopped before moving guarded rollback into finally.
A disposable source-copy fault injection pauses immediately after native creation
and force-kills its launcher; both PowerShell versions left a suspended orphan
before switching to atomic JOB_LIST assignment. Fixtures cleaned their own orphan
processes. Final combined verification must include both regressions; neither test
starts financial services or touches provider data.

Worker startup now waits for a live, checkout-matching registered PID with the
expected process ancestry before publishing the main runtime record. Missing,
foreign, exited and late registrations fail startup. A late-success regression
failed before adding post-inspection deadline checks. The shared monotonic budget
also controls ancestry traversal and the requested CIM operation timeouts; this
does not hard-cancel blocking filesystem/OS calls or prove ongoing scheduler health.

## Follow-up: PDF Processing

The final renderer follow-up passed 88 tests and 9 subtests across the renderer,
Tesseract, document processors, processor pipeline, lazy imports, executable
resolution and local ledger processing. This includes a real ordinary-PDF Poppler render, a real child-process
timeout, temporary-file cleanup, oversized raster rejection, sequential cleanup,
and a shared deadline that rejects partial/late OCR text. No financial source was
modified. A pipeline-to-ledger test also verifies that render failure creates a
review item, blocks export and leaves the original source bytes unchanged.
Earlier audit-draft metadata-timeout findings describe the superseded
pdf2image rendering path; they are not evidence about this replacement path.

An isolated three-round Windows benchmark rendered the same synthetic PDF at
1500x900 pixels using both implementations. Median render time was 0.316 seconds
for the previous pdf2image path and 0.234 seconds for the bounded Poppler path.
Median traced Python peak allocation was 4,193,455 versus 152,035 bytes. These
allocation measurements exclude native subprocess memory and C image buffers;
the small, fixed-order synthetic sample is not a whole-app performance or RAM
reduction claim. Rendering fewer helper processes and avoiding the Python raster
pipe are the concrete changes, not a promised performance percentage.

## Follow-up: Source Failure and Export Dispatch

Regression tests reproduced an older prepared route masking the source's current
failure/duplicate/review block. A separate reproduction showed a previously
approved Wave draft reaching the fake executor after its source had failed.
Current source blocks now take precedence in the normalized ledger and its
`readyForWaveDraft` metadata. Approval and execution re-read the source document;
execution checks it after acquiring the export claim. Failed, duplicate or
`needs_review` documents stop dispatch and move the attempt to
`attention_required`, requiring approval again instead of remaining in the
automatic retry queue. Missing referenced documents also fail closed. Original
draft payloads and routing history are retained, and the block is audited.

Verification: 96 tests and 14 subtests passed across bookkeeping records, local
exports, export workers, routing and processing. Separately, eight existing API
export tests and one new API regression passed. The new API test verifies HTTP
400, the persisted attention/block states and no provider-executor call. The
worker regression verifies that a blocked MijnGeldzaken export is absent from
the next batch and produces no provider artifact. Tests use temporary ledgers,
synthetic evidence and fake/provider-patched execution only.

This is not a general Wave payload freshness guarantee or a transaction spanning
SQLite and the provider: concurrent source edits after the last check and
checksum-bound Wave draft approval still require broader acceptance coverage.
No UI build, whole-backend suite, live provider or deployment run is implied by
these focused checks.

## Follow-up: Legacy OAuth Login State

The old callback accepted an arbitrary nonempty state value before exchanging
an authorization code and issuing a session. The regression reproduced that
path before implementation. Sign-in links now navigate to FAB's
`/api/oauth/start`, which creates independent random state and browser-binding
values. The HttpOnly binding cookie uses SameSite=Lax for top-level OAuth return;
HTTPS uses a Secure `__Host-` cookie. HTTP is restricted to an explicitly
configured loopback callback. Callback requests must match the configured origin
through Express's explicit proxy trust and the server-issued browser binding.
Transactions expire after ten minutes and are consumed before any asynchronous
exchange, including when that exchange fails. The existing SDK still receives
base64 of the stored, exact callback URI, never the untrusted incoming nonce.

Pending transactions are capped at 1,000 without evicting existing valid logins;
initiation is limited to 20 requests per client IP per ten minutes. Trusted
proxies must overwrite forwarded headers. These are application bounds, not
protection against distributed denial of service. A live pending browser binding
is reused for another login tab and expires naturally, so finishing one tab does
not invalidate the other. Explicit default ports are normalized before origin
comparison. These three behaviors were independently flagged during review,
reproduced by failing tests and corrected.

Verification on the final candidate:

- `pnpm.cmd --dir web check`: passed.
- `pnpm.cmd --dir web test`: 323 tests passed across 31 files, including 21
  focused OAuth/link tests. Unbound state, wrong browser, expiry, replay,
  duplicate query/cookie fields, concurrent callbacks, failed exchanges,
  loopback cookies, proxy/host checks, two tabs and capacity controls are covered.
- `pnpm.cmd --dir web build`: passed, including both server entrypoints and
  bundle-budget checks (largest client JS 480,764 bytes; HTML 2,031 bytes).
- An isolated Chrome/Playwright check passed eight assertions against the real
  OAuth routes with a local synthetic provider on a different site
  (`localhost` versus `127.0.0.1`). Both tabs completed using the browser's actual
  cookie jar; replay returned HTTP 400 with no third exchange. SDK/account
  persistence were synthetic. No real provider or financial database was used.
  Ignored artifacts are under `output/playwright/oauth-smoke/`.

Configuration migration for the optional full-server SaaS flow is documented in
`web/.env.example`: set `VITE_APP_ID`, `VITE_OAUTH_PORTAL_URL`, `OAUTH_SERVER_URL`
and the exact `OAUTH_REDIRECT_URI` in the server environment. Restart invalidates
outstanding login transactions; users restart sign-in. The local transaction
store requires one application process or sticky routing; replicated shared
OAuth state is not implemented. Standalone and managed login do not enable this
legacy flow. Existing legacy session lifetime/revocation and the separate Flask
session lifecycle remain outside this specific state-binding fix.

The boundary follows [RFC 9700 section 4.7.1](https://www.rfc-editor.org/rfc/rfc9700.html#section-4.7.1).
This is local verification, not identity-provider acceptance or a sealed
whole-repository security report.

## Follow-up: Managed Ledger Session Revocation

Managed Node handoffs now carry a signed version-2 ticket binding the issuing
session ID and expiry. Flask validates that authority on bootstrap and every
cookie-authenticated request. The backend-only Node status endpoint uses a
purpose-derived HMAC credential; it never treats browser cookies, the raw API
token, or HAI credentials as status authority. Node consumes handoff nonces in its
bounded in-memory authority store, preventing reuse across Flask-only restarts.
The session store remains single-process. Logout, expiry, web restart and
operator/signing-secret rotation revoke subsequent linked requests.

Managed deployments reject legacy boolean-only cookies and unbound version-1
tickets. Deliberate master-token ledger login and API bearer access remain
independent. HAI bearer evaluation precedes cookies and retains route restrictions.
There is no positive validation cache and no retroactive cancellation of an
already authorized operation. Each linked browser request adds one private HTTP
round-trip and a short-lived watchdog; bearer worker/HAI requests do not.

The read-only review identified three additional edge cases. Regression tests
reproduced all five parameterized cases before the corrections:

- Managed local configuration now requires a strong API token before ledger
  creation, not just the Windows/VM profiles.
- Five seconds of issuance-clock tolerance is preserved without accepting an
  expired ticket/session or changing the absolute expiry.
- Direct status transport has a watchdog that interrupts slow response headers
  and bodies at its three-second budget, rather than relying only on inactivity
  timeouts. The response is capped at 4 KiB; compression and redirects are denied.
  Connect timeout is one second and idle read timeout at most two seconds. OS DNS
  resolution is not an interruptible hard wall-clock bound; prefer the configured
  local/Compose service address. TLS verification remains enabled for HTTPS.

Verification on 2026-09-05:

| Check | Observed result |
| --- | --- |
| Parent validation, preflight and Flask API regressions | 146 passed, including slow streamed headers/body, clock tolerance, bootstrap/revocation, replay after Flask reconstruction and HAI separation. |
| Complete web test suite | 329 passed across 31 files; includes both logout routes, parent expiry/rotation and consumed handoff rejection. |
| Type check and production build | Passed. Bundle budgets unchanged: entry HTML 2,031 bytes; largest client JavaScript 480,764 bytes. |
| Real Chrome with Node and Flask on distinct local HTTPS origins | Eight checks passed: handoff opens real disposable ledger, Secure/HttpOnly cookie, ticket removed from final URL, consumed handoff rejection, dashboard cookie removal, linked ledger denial after logout, saved-cookie replay denial and no JavaScript runtime exceptions. |
| VM Compose configuration | `docker compose -f docker-compose.vm.yml config --quiet` passed using synthetic non-operational settings. No containers started. |

The browser check used real authentication/handoff/Flask routes and a synthetic
dashboard link, not the complete operations UI. The only initial console error
was the isolated harness's missing favicon; subsequent HTTP 401 responses were
expected denial checks. The Browser skill was unavailable, so the installed
Playwright CLI drove a separate Chrome session. Temporary artifacts are outside
Git in `%TEMP%/fab-parent-session-smoke-20260905/`. No real business ledger,
financial document, provider account or production service was changed.
The temporary Node/Flask harness and its separate Chrome session were stopped
after verification; the disposable ledger directory was removed by its fixture.

VM Compose now supplies the private validation URL. Windows managed access needs
an explicit URL using its actual Node port; see the updated runbooks. These tests
do not prove target-host deployment, real ngrok/proxy behavior or live providers.

## Follow-up: Backup Inspection Resource Use

On 2026-09-06 the existing manifest-only path was found to invoke the source
checksum verifier, decompressing every receipt despite reporting a lightweight
check. Both inspection paths also read unbounded manifest JSON before checking
the aggregate uncompressed size. Fourteen focused regressions reproduced the
initial behaviors before changes.

Manifest-only inspection now checks structure, metadata, counts and declared
sizes without opening ledger or receipt members. Its result remains
`manifest_valid` with deep verification explicitly not executed. Deep inspection,
restore planning, package creation and scheduled verification retain SHA-256
checks of source evidence plus ledger checksum and SQLite integrity checks.
Manifest and deep caches remain separate. API tests show that metadata-only
success cannot authorize corrupt evidence: deep inspect, restore plan and the
confirmed restore request still reject it without a restore audit or source edit.

Both paths check the existing declared-size budgets before opening any archive
member. Manifests have an 8 MiB header and bounded-read limit, 32-container nesting
limit and finite-number requirement. A reviewer reproduced `1e400` overflow
bypassing JSON constant rejection; five additional regressions failed before the
finite-float check and then passed. Manifests over 256 KiB are not retained in
either 128-entry inspection cache. Valid larger manifests below 8 MiB are still
inspected, without a cached copy. Restore repeats header/manifest validation on
the reopened archive after producing its automatic safety backup, while retaining
byte verification during extraction and the existing rollback gates.

A five-round synthetic comparison against the pre-change `HEAD` implementation
used eight receipts totaling 16 MiB, a cold application cache and warm OS cache:

| Manifest-only inspection | Before | After |
| --- | --- | --- |
| Median elapsed time, final repeat | 67.68 ms | 1.45 ms |
| Median peak traced Python allocation, final repeat | 3,215,221 bytes | 93,887 bytes |

The earlier repeat measured 66.96 ms versus 2.78 ms. Timing varies with host
conditions; both runs showed about 97% less traced Python allocation. This is a
measurement of one metadata operation, not an app-wide speed or memory claim.
Traced allocation excludes native allocations and process RSS.
The disposable benchmark script was kept outside Git at
`%TEMP%/fab-backup-benchmark-20260906.py`; no real business backup was read.

The final focused regression file passed 21 tests. The standalone recovery
rehearsal also passed all 13 checks using synthetic evidence, including restored
bytes/checksums, SQLite integrity, audit and removal of its temporary workspace.
The complete Python suite then passed with 953 tests, one skipped test and 59
passing subtests. These counts overlap the focused runs and must not be added
together. No runtime source was edited while that full suite was running.

Remaining resource boundaries are explicit: ZIP central-directory parsing occurs
inside Python's standard library before member checks; deep checksum reads and
SQLite verification do not yet have a hard overall execution deadline or OS
memory sandbox. The JSON byte cap is not a bound on total Python object memory.
No target-host disaster recovery, provider acceptance or complete security scan
is implied by this change. Existing archives rejected by these checks remain
untouched and require investigation, not automatic deletion.

## Windows Lifecycle Verification - 2026-10-01

The combined Windows lifecycle suite passed 101 tests and four subtests in
269.23 seconds. It covered startup rollback, native job containment, normal
shutdown, retained process identities, runtime fingerprints, and launcher
validation. Disposable native-process cases ran under Windows PowerShell 5.1
and PowerShell 7; isolated extracted-script tests also exercised failure paths.
This is a focused suite, not a fresh full-application test run.
Six additional, non-overlapping extracted-script regressions then passed in
10.98 seconds, checking same-checkout identity carryover and rejection of
unknown or mismatched groups before termination.

Normal shutdown now reopens recorded native process groups, verifies membership
and original start times, terminates groups, and checks zero active members
before permitting lease cleanup. Regressions cover unresponsive discovery,
departed intermediate parents, descendants surviving an exited root while a
launcher handle remains, missing native groups, recycled ancestry, foreign
folder-prefix matches, and independent stop attempts after failures. Legacy,
partial, unreadable, and foreign runtime records retain recovery state rather
than silently authorizing cleanup. Startup carries over only identities from
the same checkout whose process IDs match the adopted services.

Scoped independent static review reported no further concrete unsafe-cleanup
findings after the corrections. It did not establish runtime acceptance. No
live bookkeeping service, provider record, receipt, Drive archive, tunnel,
restore, commit, or deployment was changed by these tests. Production Windows
host acceptance, hard multi-service handoff crash reconciliation, VM/ngrok
acceptance, provider readback, and the broader readiness requirements remain
open. Named groups belong to the launching Windows session; this is not a
cross-session Windows service manager or hostile-code sandbox.

## Shutdown Lease And Credential Hardening - 2026-10-01

The final combined ledger, HAI, worker, startup/shutdown, lifecycle-lock,
endpoint-security, launcher and fingerprint suite passed 154 tests and four
subtests in 208.68 seconds, with implementation files unchanged during the run.
After tightening disposable-worker teardown to use base Python directly, the
eight lease-cleanup tests passed again in 3.46 seconds. These eight overlap the
combined run and must not be added to its count. This is focused verification,
not a full-application suite or target-host deployment acceptance.

Failing tests reproduced an audit error leaving a force-released lease deleted,
and the actual embedded shutdown program leaving six HAI leases behind in a
506-command fixture. Cleanup now uses one SQLite transaction, literal scope
matching and 100-record keyset pages without a 500-command cutoff. Each release
and its existing redacted audit event commit together. Audit failure on a later
page or an invalid owner rolls back all prior pages. A 508-targeted-lease fixture
checks one database connection for the batch method, all individual audits,
preserved unrelated leases, and idempotent retry. This connection count excludes
ledger initialization and is not an app-wide performance claim.
The final synthetic embedded-program fixture handled 506 HAI commands plus two
worker leases in a 0.48-second test call. The earlier pre-fix fixture failed in
46.14 seconds with six HAI leases still present; these are diagnostic test
timings, not a controlled production benchmark or a global speedup guarantee.

Review also identified a missing-registration worker and a concurrent-start
window. The missing-registration case failed before the correction. Start and
stop now serialize through a checkout/session-scoped reentrant Windows mutex.
Cleanup holds the existing worker maintenance mutex through the committed batch
and both runtime-record removals. Real subprocess tests attempt worker startup
at the batch and removal boundaries, and copied complete launchers attempt work
while another process holds lifecycle ownership. All fixture records and servers
are disposable; the tests do not use live bookkeeping data.

Startup/shutdown identity probes now reject non-loopback hosts, URL credentials
and unsupported schemes before sending any token, and explicitly disable
redirects. Extracted-function tests exercise both scripts on both PowerShell
versions, including their different IPv6 representations. Real disposable HTTP
tests check that a direct JSON identity responds successfully while a redirect
target receives no request. The redirect setting follows Microsoft's
[documented MaximumRedirection semantics](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/invoke-restmethod?view=powershell-5.1#-maximumredirection).

Scoped static review found no further concrete issue under the documented
single-checkout, single-account/session, managed-launcher contract. It is not
an application-wide security assessment. The database transaction and metadata
removals cannot commit as one operation; interruption may leave records requiring
reconciliation. Arbitrarily launched APIs, other accounts/sessions, external
providers, production host/ngrok acceptance and hard-crash recovery are not
proved by these local checks.

## 2026-10-01 Packaging Follow-Up

Reproduced and fixed Windows failures in both production package start commands.
They now preload a small Node module that sets production mode before server
imports, without shell-specific environment assignment. Synthetic installations
with spaces in their paths exercise both actual package commands. The runtime
web image includes the preload module as well.

The Python image now uses the existing secret-file-aware, proxy-free API health
probe instead of reading only the inline token environment variable. Its outer
timeout is ten seconds, exceeding the probe's five-second request timeout.

Both Docker contexts now allow only required source paths. A real local Docker
scratch-image test reproduced inclusion of `.env.production` before the fix;
post-fix inspection of never-started disposable images checks that required
sources remain and synthetic environment files, credentials, runtime output,
databases, caches and unlisted private files are absent. No real credentials or
financial data enter these fixtures. Tests require the context-associated local
Docker driver; unavailable engines skip rather than count as proof.

Verification: seven packaging/probe regressions passed (including secret-file
input, disabled proxy handling and redacted probe errors); 329 web tests across 31 files,
web type checking and the production build passed. Production asset-budget
verification passed too. These checks do not establish complete application
image builds, VM deployment acceptance, live provider correctness, public TLS,
ngrok acceptance, disaster recovery or app-wide performance improvements.

## 2026-10-01 Operator Secret and Startup Follow-Up

The remote operator's separate secret reader accepted invalid UTF-8 (via
replacement characters), extra terminal newlines and trailing tabs. New tests
reproduced all three cases before the fix. Operator authentication now shares
the deployment descriptor-based reader, with its own 4096-byte limit retained.
Reads are bounded, regular-file checked, nonblocking where supported by Node,
strict UTF-8 and single-line. Empty optional environment values retain their
prior behavior. Each authentication check still rereads the mounted file so
rotation revokes existing sessions without a restart; no caching weakens this
boundary. Errors never include credential values or filesystem paths.

Successful-start smoke tests now launch both bundled production entrypoints in
disposable, secret-free directories on unused loopback ports. They verify the
runtime identity, served HTML and HTTP 403 for anonymous protected operations.
The HTML is a fixture, not a rendered dashboard or provider acceptance check.
The fixture directory must not be dot-prefixed: Express intentionally refuses
to serve hidden paths, which the previous negative-only fixture never tested.

Final web verification: 335 tests across 31 files, type checking and the
production build all passed. Production asset budgets passed. No live financial
records, provider credentials, tunnels or deployed services were used.

## 2026-10-01 Broad Backend and Shutdown Ordering Follow-Up

The fresh complete Python run finished with 1112 passed, one failed, one skipped
and 81 passed subtests in 591.73 seconds. The failure was an obsolete shutdown
wiring assertion expecting the old inline lease loop. It was updated to require
the audited batch helper after API stop, under worker maintenance ownership,
with no direct capped lease listing. Existing behavior tests still verify literal
HAI scope, per-lease audit reasons, full rollback, retained metadata and more
than 500 leases; moving code into a helper did not waive those requirements.

Strengthening the same ordering assertion then exposed a real gap: recorded
Job Objects could terminate API processes before the managed tunnel stopped,
although the later legacy process-tree path stopped the tunnel first. The new
assertion failed before correction. Tunnel shutdown now precedes both recorded
and discovered service termination paths. A new disposable PowerShell failure
fixture verifies that a tunnel-stop error prevents lease cleanup and preserves
runtime records. The focused shutdown/ngrok follow-up passed 14 tests after this
change. No real tunnel was opened or stopped.

The full Python snapshot predates the shutdown-order fix and is not a final
whole-suite green claim. Focused follow-ups are separate, overlapping runs.
The wider Windows shutdown/launcher/lifecycle/ngrok/credential/fingerprint and
lease regression run passed 88 tests and 22 subtests in 175.74 seconds. The
14-test follow-up includes the subsequently added tunnel-failure fixture.

## 2026-10-01 Secret Descriptors and Python Image Follow-Up

New regressions reproduced silent normalization of malformed Python secret files
and failure to validate the actually opened file. The shared reader retains its
Windows-compatible early path check, opens binary/nonblocking where supported,
checks the descriptor's type and size before reading, and closes it on failure.
Its maximum read stays 8193 bytes with an 8192-byte acceptance limit. Only one
optional LF or CRLF terminator is removed; extra whitespace, lone CR, additional
newlines and invalid UTF-8 are rejected with redacted errors. A simulated opened
pipe substitution verifies rejection before a read and descriptor closure.
This is not a hard OS-level deadline for Windows or network filesystem calls.

Runtime fingerprints now include web startup/build scripts. Both omitted scripts
failed the new invalidation checks before correction. Private runtime data and
credential files remain excluded from the existing fingerprint contract.
Focused verification passed 76 tests and 24 subtests covering descriptors,
fingerprints, deployment preflight, health probes and Windows/ngrok credentials.
This is not a new full Python suite result.

Built the actual Python production image twice on the explicitly selected local
Docker engine. Both builds used Python 3.13's same base manifest digest
`2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26`.
The final Dockerfile omits `libgl1`, avoiding its graphics-driver dependencies
while retaining headless OpenCV, Poppler and English/Dutch Tesseract. Docker's
reported `.Size` changed from 358430595 to 301122962 bytes: 57307633 bytes,
15.99 percent smaller. This metric is not total host disk use, RAM use, runtime
latency or a controlled build-time comparison. Python package name/version
inventory hashes matched between images:
`e2d2e756cccd7b2c4a9aac5dcec6ee8cf1f3d7eff7680a7ccdb4bded6b20e366`.
Requirements and the base tag remain unpinned, so reproducible future builds
and dependency vulnerability acceptance are still open.

The reusable `scripts/container_runtime_smoke.py` was piped into both images.
Each ran with no network, no published ports, no host data mounts, a read-only
root filesystem, 768 MiB memory, one CPU, 64 PIDs and a 64 MiB temporary filesystem.
Both passed non-root UID, actual entrypoint/dependency imports, OpenCV conversion,
native English/Dutch OCR of synthetic text, nonblank bounded native PDF rendering,
secret-file/FIFO checks, authenticated Flask API access against a disposable
SQLite ledger and HAI route-scope denial. These use Flask's test client, not a
deployed HTTP stack or a live HAI integration. All lab containers and unique image
tags were removed; Docker-managed build caches were not globally pruned.
No financial source files, provider accounts, live ledgers or external writes
were used. Complete Windows/VM/ngrok/provider acceptance remains open.

## 2026-10-01 Shared API Credentials and HTTP Denial Follow-Up

Reproduced non-ASCII Authorization headers and login form tokens producing HTTP
500 through Python's string-only constant-time comparison. These inputs now
fail authentication normally, with bounded ASCII checks before comparison.
Valid operator sessions, operator credentials and HAI route scoping retain their
existing behavior. A real disposable Waitress HTTP server verifies a Latin-1
malformed header is denied, the operator can read liveness, HAI can read its
manifest and HAI cannot read the operator health route. Its dispatcher and
listener are shut down and the test asserts no server thread remains alive.

Shared API configuration now rejects whitespace, non-ASCII and over-8192-character
tokens in every profile before ledger creation; local compatibility still allows
unset and short printable-ASCII tokens. Windows/VM strength requirements remain.
The web gateway's two backend-token settings now use the same character/size
limits, bounded 8192-byte file reads and absolute file paths. Production shared
API secrets require at least 12 distinct characters on both sides. Other existing
placeholder/repetition checks may remain stricter in the gateway; this is not
a claim that every secret type has identical validation. JWT and standalone
operator secrets retain their separate contracts. Optional file terminators
count toward the file's 8192-byte limit. Errors never echo values or paths.

Verification: the wider Python API/HAI/parent-session/deployment/credential run
passed 202 tests in 90.06 seconds. A subsequent overlapping 56-test run includes
the real Waitress check and positive local token-size boundaries of 1 and 8192
characters. Final web verification passed 341 tests across 31 files, type checking
and the production build with asset budgets. New regressions failed before the
fixes; a fixture was also corrected to accept the existing structured error
envelope and reset the module cache between credential cases. No live ledger,
provider action, tunnel, commit, publication or deployment occurred. This is
not a fresh full Python suite or complete target-host acceptance result.

## 2026-10-01 Dashboard Gateway Transport Follow-Up

Six new regressions reproduced automatic POST redirect replay, malformed 200
responses being accepted as empty success objects, and body-read aborts being
swallowed. A disposable loopback HTTP fixture reproduced the redirect replay
with synthetic data and credentials only. The gateway now forces manual redirect
handling even when a caller requests following redirects; all 3xx responses fail
without forwarding the mutation or returning the redirect location. Source
previews also refuse upstream redirects.

A shared streaming reader limits actual consumed response bytes before copying
an oversized chunk. JSON responses are limited to 8 MiB; source previews retain
their 25 MiB limit. Advertised oversized bodies are cancelled before reading,
and missing or misleading lengths cannot bypass the actual-byte limit. Buffer
capacity grows geometrically rather than retaining an unbounded chunk list.
These are per-response bounds, not a claim of constant total process memory.
Source preview SHA-256 verification remains mandatory before serving bytes.

Successful JSON must decode as strict UTF-8 and contain an object, not null,
an array, a scalar or malformed data. Gateway caller cancellation is preserved,
timeouts cover body consumption, and invalid timeouts fail before submission.
Header construction occurs before allocating timers. Submitted mutations
invalidate the dashboard snapshot even when response verification fails;
uncertain mutations are not automatically retried. Prior resource values remain
available only under the existing explicitly stale fallback contract.

Verification: all 366 web tests across 32 files passed, including a real local
HTTP body that never completes, real 307 refusal, cancellation, strict decoding,
uncertain-mutation cache invalidation, bounded chunked reads and source integrity.
Type checking and the production build with asset budgets passed. Disposable
HTTP fixtures were closed. No Python source changed in this follow-up, and no
fresh full Python run was performed. No provider writes, Drive archival, real
ledger access, ngrok tunnel, deployment, commit or publication occurred.
Complete target-host and live-provider acceptance remain open.

## 2026-10-01 Preview Cancellation and Dependency Follow-Up

Real disposable Express HTTP regressions reproduced downloads continuing after
the requesting browser disconnected and excess previews starting unbounded
backend reads. Source previews now abort their upstream request on browser
disconnect, avoid writing error responses to disconnected clients, and permit
at most four simultaneous previews per registered gateway instance. Excess
requests receive HTTP 429 with Retry-After: 1; there is no growing wait queue.
Slots are held until response finish/close, including after upstream completion,
and released after timeout, failure or disconnect. The delivery test deliberately
defers response completion; it is not a bandwidth or kernel-buffer benchmark.
Slot limits are per process, not a distributed or total-host memory guarantee.

Resource options must be positive safe integers, cannot raise the 25 MiB/four
preview ceilings, and enforce a maximum 120-second configured timeout. A timeout
is reported as 504 only when the route's timer expired, not for unrelated aborts.
Authentication, redirect refusal and mandatory checksum verification remain.
Both initial regressions failed before correction. The real HTTP fixtures use
synthetic bytes only and are closed after testing.

The approved dependency audit initially reported 19 advisories (seven high,
11 moderate and one low). Consulted maintainer advisories for
[Axios](https://github.com/axios/axios/security/advisories/GHSA-r4gj-5m52-g5wh),
[Vitest](https://github.com/vitest-dev/vitest/security/advisories/GHSA-82fw-gwwq-j7x9),
[ip-address](https://github.com/beaugunderson/ip-address/security/advisories/GHSA-j6r3-76f7-8jcv)
and [DOMPurify](https://github.com/cure53/DOMPurify/security/advisories/GHSA-p98j-92pf-mc4p).
Updated installed/locked versions to Axios 1.20.0, Vitest 4.1.11, ip-address
10.7.2 and DOMPurify 3.4.16 within existing major versions; raised the two direct
dependency floors and refreshed the selected transitive packages. Installation
scripts were disabled for this update. The subsequent audit at low severity
reported no known vulnerabilities. This is a point-in-time registry advisory
result, not an exploitability assessment or assurance that no vulnerabilities
exist. No upstream major-version migration was performed.

After those updates, all 378 web tests across 32 files, type checking and the
production build with asset budgets passed. Complete Windows/VM/ngrok/provider
acceptance remains open. No live records, source archival, tunnel, deployment,
commit or publication was used for this follow-up.

### Complete Python Run and Docker Probe Follow-Up

The complete Python command finished with 1138 passed, one failed, one skipped
and 83 passed subtests in 1181.40 seconds. The single failure was the Python
build-context fixture's Docker information probe exceeding its 15-second
deadline, before its build-context assertions ran. It is not evidence that the
context exclusion checks failed, nor a passing acceptance result for that case.

The fixture now resolves and verifies a local Docker context once, explicitly
pins every subsequent command (including cleanup) to it, and allows the
read-only engine information probe a bounded 120 seconds for a cold start.
The exclusion/source-preservation assertions and local-driver requirement are
unchanged. Engine-probe timeout still fails rather than being converted into a
skip. A unit check covers the explicit context argument. The focused follow-up
passed eight tests in 27.33 seconds, including both actual synthetic Docker
build contexts, the secret-aware health probe and production startup packaging.
This is not a fresh clean complete Python-suite result after the fixture change;
the complete rerun remains open. No application Python source changed in this
follow-up. Disposable Docker objects use unique fixture-owned names and cleanup.

## 2026-10-01 Production Dashboard API and Ledger Rehearsal

Added `scripts/run_stack_rehearsal.py`, a reusable real HTTP rehearsal using the
actual built production standalone dashboard, Waitress API and a fresh SQLite
ledger. All 23 assertions passed locally with Node 25.2.1 and Node 24.19.0 on
Windows. It proves anonymous denial, HAI manifest access/operator-route denial,
managed login and cross-origin rejection, live ledger counts and review visibility,
checksum-verified gateway bytes, tampered-source rejection, dashboard corrections
persisted in SQLite, refreshed review counts, signed parent-bound ledger handoff,
replay denial and logout revocation in both services. Only a synthetic text receipt
and disposable credentials are used. No worker or provider write is started.

The API fixture refuses existing ledger/source files and requires a fresh marked
rehearsal directory. Provider/application credentials and proxy settings are not
inherited. The API child uses the base Python executable with the current package
paths, avoiding the Windows virtual-environment redirector ownership problem.
Windows children are hidden, logs are bounded when reporting failure, credentials
are redacted and all child cleanup is attempted. Passing output is emitted only
after owned children have stopped and the temporary directory has been removed.
Separate tests cover environment isolation, preserving existing files on refusal
and actual disposable-child termination/reaping.

Initial harness attempts exposed a Windows absolute-path preload URL issue in the
new harness and an incorrect replay status expectation. The harness now uses a
file URI for the preload and asserts the existing managed replay denial contract
(401); application authentication was not weakened to accommodate the harness.
The review refresh assertion checks the actual review count before and after,
rather than defaulting a missing field to zero.

The prepared CI workflow runs this rehearsal after the Linux frontend build and
on Windows backend shard 1, avoiding four redundant Windows builds. Its YAML
parsed/round-tripped successfully through the installed Prettier parser. README
now documents the command and the current evidence record. Hosted CI and Node 22
acceptance have not been run here. HTTPS trusted-proxy headers are simulated;
this is not real TLS/ngrok acceptance, rendered-browser verification, Windows
launcher acceptance or a live provider posting/attachment/archive test.

Docker build-context verification now inspects only its named local builder,
instead of enumerating unrelated builders. This CLI does not support structured
`buildx inspect --format`, so a strict header parser verifies the exact context
name and Docker driver and rejects duplicates/foreign drivers. Eleven targeted
tests passed in 41.99 seconds, including the five parser cases, both actual Docker
build contexts and the three rehearsal guards. Fixture-owned cleanup was attempted;
the later scoped container inventory query stopped responding and was cancelled.
The image inventory query finished with exit 0 and no fixture tags reported.
Independent container cleanup-inventory verification remains open. No global
prune or shared-engine restart was performed.
Complete production acceptance remains open.

### Complete Rerun Result

The next complete Python run finished with 1139 passed, one failed, one skipped
and 83 passed subtests in 1343.79 seconds. The failure was the synthetic web
build-context Docker build exceeding its enforced 90-second deadline. This run
started before the targeted-builder refinement and new rehearsal/parser guard
tests were added. The later eleven-test focused run is separate evidence, not a
clean complete-suite replacement. No build deadline was increased and the failure
was not converted into a skip. Docker CLI termination alone does not establish
the daemon-side build outcome; container/engine acceptance remains unresolved.
No shared Docker restart, deployment, live financial mutation or publication
was performed. Full-suite and target-host acceptance remain open.

### Retained Source Descriptor Follow-Up

The source-preview API now inspects the actual opened descriptor rather than
trusting an earlier path-size check. A source disappearing after the path check
returns a redacted 409 instead of an uncaught 500. Non-regular descriptors are
rejected before reading; the opened regular-file size is checked against the
25 MiB ceiling. Unbuffered reads consume at most one byte beyond that ceiling
even if the file grows. Binary opening preserves Windows CRLF, control bytes
and non-ASCII bytes. Descriptors close on rejection and wrapper-creation failure.
Checksum validation before serving the complete bytes remains unchanged.

Six new regression tests cover disappearance, substitution with an actual pipe
descriptor, oversized opened metadata, wrapper failure, bounded growing-file
reads and byte-exact binary output. The substitution tests instrument descriptor
opening; they are not proof of a physical Windows named-pipe path race. The wider
API/credential/parent-session run passed 137 tests in 244.05 seconds. A separate
source/rehearsal-guard run passed nine tests in 6.43 seconds. The source payload
is still buffered before serving; neither a total-RSS reduction nor a hard
Windows/network filesystem open/read deadline is established by these changes.

Two real-stack attempts timed out at the review-correction gateway request;
the gateway's eight-second mutation deadline was not increased and mutations
were not replayed. The rehearsal now reports HTTP status and bounded redacted
RPC error detail, and its synthetic fixture can dump thread traces for a stalled
correction. Two subsequent fresh Node 24.19.0 runs and one Node 25.2.1 run each
passed all 23 real dashboard/API/SQLite assertions with owned children stopped
and temporary fixtures removed. The earlier timeout cause remains unproven;
later passes do not establish that it has been fixed. Source verification and
tamper rejection had passed before those attempts reached the correction step.

No web application source changed in this follow-up. No provider credentials,
live financial records, source archival, shared service restart, commit, push
or deployment was used. Full-suite, container, rendered-browser and real
Windows/ngrok/provider acceptance remain open.

### Atomic Review and Normalized Record Changes

Failure injection reproduced partially committed review corrections at three
stages: correction-history insertion, normalized line-item replacement and audit
recording. A successful synthetic approved category correction with learning
opened 17 database connections. Three more regression cases reproduced partial
normalized records when direct document/bank line-item creation or direct record
resolution audit recording failed. These were real local database-state failures,
not inferred from static code inspection.

`LocalOperationsLedger.write_transaction()` now reserves the SQLite writer with
`BEGIN IMMEDIATE` before decision reads, reuses one connection and commits once.
Nested changes use savepoints; an outer failure rolls back successful nested
changes too. Read-only snapshots cannot escalate to writes, and reads within a
write transaction share that transaction. Connections close and the context resets
on begin/commit errors or interrupts. No schema migration or credential change
was needed.

The review service, exact-vendor propagation and direct normalized record
creation/resolution use this boundary. It covers document changes, review and
correction history, learned rules, normalized records/line items and their success
audit entries. Other processes do not see partial uncommitted state. Concurrent
decisions for the same review cannot both close it. A propagated-batch failure
rolls back the primary decision as well. This does not make every FAB workflow
one transaction, does not wrap provider writes, and does not provide a distributed
transaction or a hard request deadline. API exception reporting intentionally
adds a separate redacted error event after financial rollback.

The same review fixture now opens one connection instead of 17. This is a measured
reduction in connection creation for that fixture, not a total-RSS, CPU or
throughput benchmark. The SQLite lock-wait and gateway deadlines were not relaxed;
large batches can hold a writer longer. A client timeout is still an uncertain
outcome and must be inspected rather than automatically replayed. The earlier
real-stack timeout cause remains unproven.

Verification on Windows:

- 19 new transaction tests passed in 6.61 seconds, covering injected failures,
  connection reuse, savepoints, concurrent decisions, read-only protection, begin/
  commit failure cleanup, independent readers, interruption, vendor batches,
  direct document/bank records and the API's redacted error event.
- The wider review/ledger/record/reconciliation/API/bank/export/worker run passed
  226 tests and 12 subtests in 149.14 seconds. It collected before the final two
  independent-reader/interrupt tests were added; those passed in the separate
  19-test run. An earlier 176-test pass predates the direct-record wrappers.
- A separate pipeline/worker/reporting/master-ledger/auth/session/preview run
  passed 63 tests in 28.19 seconds.
- All 23 real production-dashboard/Waitress/SQLite rehearsal checks passed after
  the final application changes, on Node 24.19.0 and Node 25.2.1. Only synthetic
  data and credentials were used; owned children stopped and temporary fixtures
  were removed.

No web application source changed. Full Python-suite, Docker/container, hosted CI,
rendered-browser, actual Windows launcher/ngrok and live provider acceptance are
still open. No provider record, source archival, deployment, shared restart,
commit, push or publication was performed.

### Reconciliation Consistency and Real Dashboard Bank Path

Local failure injection reproduced partially committed match resolutions after
bank-status or audit failure, and partly persisted reconciliation runs after
review creation failed. Further regressions reproduced document-linked reviews
being closed for a different match, `needs_review` closing its own gate, a linked
review missed below the first 50 rows, a stale normalized review-required flag
after approval and an existing older review falsely counted as newly created.

Reconciliation result persistence and match resolution now use the shared ledger
write transaction. Matching computation runs before this service starts its
writer transaction; result persistence and its success audit commit together.
Match resolution uses one database connection in the tested fixture, including
nested normalized-record updates. Document-linked review decisions apply only
to the recorded match ID, and malformed/non-object/boolean/fractional ID evidence
remains pending. `needs_review` preserves or reopens the document review gate.
The normalized document record is refreshed after review closure/reopening so
its review-required flag reflects the decision.

Review searches use bounded 100-row pages. Resolution pages all statuses, whose
creation-time/ID ordering is unchanged by closing a review, preventing offset
skips during mutation. Both 60 and 205 unrelated-review fixtures passed; lookup
does not collect every review into a growing in-memory list. The existing
one-open-review-per-document/reason policy was not removed or bypassed.

The real-stack rehearsal now imports a synthetic JSON bank statement through
the production dashboard RPC, runs reconciliation through its operator command,
approves the linked review and checks the actual SQLite match, document, bank
transaction, normalized record gate and refreshed dashboard review count. Its
synthetic receipt includes the correction's date and amount. An initial harness
attempt incorrectly expected a bank-ID field omitted by the gateway's response
projection; it now checks the documented import count and obtains the ID from
the owned fixture ledger. No gateway response was broadened to make it pass.

Final evidence:

- All 17 reconciliation regression checks passed in 13.49 seconds.
- The final wider reconciliation/review/ledger/normalized-record/bank/API/
  pipeline/worker/master-ledger/rehearsal-guard run passed 224 tests and three
  subtests in 222.13 seconds. This includes the final strict ID-shape guard and
  expanded pagination tests. The earlier 215-test pass predates those additions.
- All 31 actual dashboard/Waitress/SQLite rehearsal assertions passed with both
  Node 24.19.0 and Node 25.2.1 on Windows; temporary data was removed and owned
  children stopped. Trusted HTTPS headers remain simulated, not real TLS.
- Diff whitespace validation passed. No web application source changed.

This is not complete reconciliation or production acceptance. Missing-receipt-
only resolution/reopening without a linked document, evidence changes between
matching computation and persistence, and the multiple-bank-candidate review
policy need further audit. Atomic persistence does not establish snapshot
freshness or a hard request deadline. Full Python-suite, Docker, hosted CI,
rendered-browser, actual launcher/ngrok and provider/attachment/archive acceptance
remain open. No live account, source archival, shared restart, deployment,
commit, push or publication was used.

### Missing Receipts, Run-Time Freshness and Later-Run Preservation

Seven initial regression cases reproduced missing-receipt dispositions leaving
their review open, failure to reopen a new gate after disposition, and unsupported
`approved`/`reconciled` confirmation of a bank match with no linked document.
The API previously returned 200 for that confirmation. It now returns 400 and
leaves the financial state unchanged. Resolved/rejected/ignored dispositions
close only the matching missing-receipt gate. `needs_review` reopens exactly one
gate, retaining the older decision and audit history. A failure while reopening
rolls back the match, bank and related local changes.

Documentless review lookup selects only missing-receipt rows with a NULL document
ID, using bounded 100-row primary-key cursor pages rather than progressively
larger offsets. A 205-unrelated-review fixture verifies lookup beyond the first
page and preserves document-linked reviews even when their metadata repeats the
same match ID. No schema or source files were deleted or rewritten.

Five initial race/stale-input cases proved that amount/vendor/status changes to
selected documents, bank changes during computation, and already-stale stored
bank input could previously produce persisted results. The service now captures
document and stored-bank evidence in one read snapshot, computes without holding
its writer reservation, then rechecks evidence inside the writer transaction
before any result persistence. Canonical fact hashes are streamed and do not
depend on second-resolution timestamps. Light core-row queries avoid fetching
document histories for explicit candidate selection and revalidation. Stored
bank input must agree with its current identity/account/currency/financial facts;
malformed, conflicting and duplicated stored-row references are rejected.

Three later regressions reproduced confirmed documents becoming unmatched on
a subsequent empty-bank run, newer completed rows hiding an older open candidate
behind the batch limit, and a new default-pool candidate appearing during matching
without invalidating the results. Closed document reconciliation states are now
excluded in SQL before the limit, and excluded from explicit selection too.
Default-pool membership/order and fact hashes are rechecked before persistence;
explicit selection rechecks its selected rows. Candidate selection stops once
its requested valid-row limit is met. New facts written during computation remain
intact when the stale reconciliation is refused. These are local database-fact
guards, not a substitute for actual source-byte or provider attachment verification.

Evidence:

- The final broader run passed 247 tests and three subtests in 172.55 seconds.
  It covers reconciliation, reviews, ledger, normalized records, bank imports,
  API, pipeline, worker, master ledger and rehearsal guards after the final
  application changes. The earlier 244-test pass predates the closed-pool fixes.
- All 24 focused missing-receipt/freshness/resource-selection checks passed in
  27.58 seconds. The selection-resource assertion was added after the wider run
  collected; application source did not change afterward. An earlier 37-test
  pass covers the keyset refinement separately.
- The real dashboard/API/SQLite rehearsal now passes all 39 assertions on
  Windows with Node 24.19.0 and Node 25.2.1. Added checks cover missing-receipt
  import/disposition, no-document confirmation denial, reopening, actual HAI
  denial of financial disposition, operator closure and preservation on a later
  run. All data/credentials are synthetic, children stop and fixtures are removed.
- Diff whitespace validation passed. No web application source changed.

Approval-time staleness between candidate creation and later operator approval,
multiple-bank-candidate policy and bulk/long-running acceptance remain audit
items. This is not a whole-system freshness guarantee, a hard I/O/request deadline
or a total-memory/throughput benchmark. Complete Python-suite, Docker/container,
hosted CI, rendered-browser, real launcher/ngrok/provider/attachment/archive and
disaster-recovery acceptance remain open. No live provider record, archival,
shared restart, credential rotation, commit, push or deployment was performed.

### Reconciliation Lookup Indexes (2026-10-01)

Two derived SQLite indexes now cover missing-receipt review links and imported
bank-row reconciliation history. Query and index expressions share constants,
as required by [SQLite expression index documentation](https://www.sqlite.org/expridx.html).
Strict Python reference validation, transaction boundaries and provider approval
rules remain in place; SQL integer coercion is only a candidate prefilter.

JSON validity guards allow index creation over damaged legacy JSON without
rewriting or removing those records. Such rows do not become valid financial
evidence and still require investigation. Reopening installs missing indexes
idempotently using the existing optional-schema setup; these are rebuildable
lookup structures, not a new financial-row migration or baseline checksum.

Four focused tests pass, checking the actual public queries' execution plans,
absence of temporary sorting for scoped keyset lookups, unchanged rows across
index installation (including corrupt legacy fixtures), and database integrity.
For a synthetic 2,001-review backlog, the same lookup returned identical results
with 28,054 SQLite VM instructions without the index versus 53 with it. Both
49-check built dashboard/API/SQLite rehearsals pass on Node 24.19.0 and 25.2.1,
with synthetic data, simulated trusted-proxy headers and no provider writes.

Final regression verification passed 358 tests and three subtests, with one
skipped test, in 220.21 seconds across 22 selected modules. Coverage includes
reconciliation freshness/dispositions/supersession, review rollback, operations
API, normalized records, bank import, pipeline, worker, master ledger, backup,
recovery rehearsal and backup inspection bounds. This was not the full Python
suite. No web source changed, so web builds/type checks were not rerun; the two
real stack rehearsals used the existing built dashboard. Whitespace validation
passed (line-ending warnings only).

This is a database-instruction measurement, not elapsed-time, whole-app CPU/RAM,
or production throughput proof. Index storage, write maintenance, initial build
latency and locking on a large deployed ledger remain workload acceptance gates.
No real operator ledger was opened or changed for this verification, and no
shared service restart, commit, push, source archival or deployment occurred.

### Receipt Arrival and Auditable Supersession (2026-10-01)

Regression tests reproduced confirmed receipt matches leaving their old
missing-receipt exception/review open. Final local confirmation now resolves
open documentless missing-receipt matches for the same imported bank-row identity
and their exact linked reviews, within the existing confirmation transaction.
Original bank snapshots remain intact; `supersededBy` records the confirming
local match/document, basis and timestamp, with a separate audit event. This does
not rewrite an old exception as a confirmed financial match, delete history,
prove a provider attachment or authorize source archival.

Unconfirmed candidates, exceptions belonging to another imported account,
previously ignored/rejected human decisions and conflicting review references
remain untouched. Ad-hoc bank inputs have no independently verified stored-row
identity and are not automatically superseded. Malformed legacy links still
require manual review. Bounded primary-key pagination covers changing exception
statuses without OFFSET skips; 205 extra exceptions and an injected failure on
the second page prove complete processing and all-table rollback, respectively.

Two additional regressions reproduced contradictory review references being
closed and every unrelated missing-receipt row being materialized per lookup.
Review and bank reference parsing now share a strict positive, SQLite-range
integer/reference-consistency check. SQL filters the linked review ID before
the row limit, followed by strict application validation. A measured fixture
returns one matching row instead of 206 backlog rows. Native filtering preserves
tested integer-string forms (padding, leading plus and surrounding ASCII spaces);
invalid boolean/fractional/conflicting references are not trusted. These are
bounded materialization/correctness proofs, not whole-app resource benchmarks.

Evidence after final application changes:

- A fresh broader run passed 309 Python tests and three subtests in 368.19
  seconds. It covers supersession, dispositions/freshness, review/reconciliation
  transactions, API, bank import, ledger/normalized records, worker/pipeline,
  master-ledger and rehearsal guards after all application refinements. This is
  not the complete Python suite or target-host/provider acceptance.
- A 94-test focused run passed in 161.26 seconds, covering supersession,
  dispositions, approval freshness, review/reconciliation transactions and
  rehearsal guards. The new supersession file has 14 tests. Earlier 106-test
  and nine-test passes predate the final native-filter/reference refinements.
- The built dashboard/API/SQLite rehearsal passes all 49 checks on Node
  24.19.0 and 25.2.1. Six new checks cover imported bank data without a receipt,
  a retained synthetic receipt registered through the real ledger service,
  candidate creation leaving the exception open, hash-verified bytes through
  the gateway, dashboard approval retaining linked exception history, and the
  live dashboard queue becoming clear. This is not a rendered-browser, OCR/intake,
  provider or archive acceptance test. Managed-session/HAI/replay/logout checks
  remain included; owned fixture processes/data are removed before success.
- Superseded intermediate wider runs were stopped after application edits;
  none is claimed as a completed final-code suite. Diff whitespace checks pass.
  No web application source changed; no new web build/type/test claim is made.

Full-suite/container/hosted-CI, real Windows launcher/ngrok, rendered-browser,
intake/OCR/provider/attachment/archive and disaster-recovery acceptance remain
open. Larger-workload latency, legacy ambiguity and richer multi-candidate/
partial-payment selection also remain open. Earlier timeout diagnostics are
not resolved by these passes. No live provider record, source archive, shared
restart, credential rotation, commit, push or deployment was changed.

### Approval-Time Evidence and Review Rollback (2026-10-01)

Eleven new regressions first failed against the previous implementation: changed
document/bank facts could be confirmed, rejected reconciliation could still close
its review, and API callers received success. Five additional failures reproduced
review normalization hiding a duplicate status and malformed match links closing
reviews. Another regression proved two bank candidates could both confirm the
same document. These failures were repaired rather than accepted as readiness.

New match candidates carry document source/financial-fact and bank-input hashes.
Final approval rechecks these under the decision's existing writer transaction,
including current stored bank identity/account/currency/amount/date/vendor facts.
Review approval validates before edits and again after edits, so a workflow status
change cannot hide a duplicate and corrections cannot silently change the matched
evidence. Rejected nested decisions raise through the transaction boundary and
return a failure only after rollback, preserving corrections, learning, reviews,
normalized records and audit tables together. Invalid/conflicting review links
or links to another document are rejected before mutation.

Already-reconciled/ignored documents and finalized bank rows cannot be confirmed
against another candidate. Legacy matches missing hashes remain intact but must
be refreshed through a new matching run. Reviewers can save financial corrections
without confirming, then rematch. This does not implement split/partial-payment
matching or a complete multi-candidate selection UI. Hashes bind local stored
facts, not an actual re-read of retained bytes or downstream attachments.

Both JSON and legacy form routes now expose rejected decisions; the web gateway
preserves HTTP 409 as a tRPC conflict with a sanitized message and no retry.
The new gateway regression first failed because conflicts were generic errors.

Verification after the final application changes:

- 271 Python tests and three subtests passed in 197.44 seconds across the review,
  reconciliation, ledger, API, bank import, normalized records, worker, pipeline,
  master-ledger and rehearsal guard modules. Five extra snapshot/link assertions
  were added afterward without changing application code and verified separately.
- 28 focused approval-freshness checks passed. Exact database-table comparisons
  cover rejection and rollback; legacy refresh and unchanged review approval pass.
- All 379 web tests / 32 files passed with `vitest run --maxWorkers=2` in 27.16
  seconds. The earlier default-concurrency run had 378 passes and one startup
  identity timeout at its existing 20-second deadline. This bounded rerun does
  not prove the initial timeout's cause or unrestricted-concurrency reliability;
  no deadline or assertion was weakened.
- Type checking and the production build/bundle budgets passed. The refreshed
  built dashboard/API/SQLite rehearsal passes 41 assertions on Windows with
  Node 24.19.0 and Node 25.2.1, including actual stale-approval conflict delivery
  and unchanged open review/financial facts. All fixture data are synthetic,
  provider writes remain disabled, and owned children/fixtures are cleaned up.

Full Python-suite/Docker/hosted-CI acceptance, real Windows launcher/ngrok,
rendered-browser workflows, provider/attachment/archive verification, disaster
recovery and large-workload benchmarks remain open. Stale non-confirmation
disposition policy and richer multi-candidate/partial-payment matching remain
audit items. No live financial record, source archive, shared restart, credential
rotation, commit, push or deployment was performed.

### Exception Disposition, Ownership and Bank Batching (2026-10-01)

New regressions reproduced changed missing-receipt amount/currency being ignored
or resolved, old candidates downgrading another confirmed document match, an old
missing-receipt exception taking over a newer document candidate, completed bank
rows hiding older open work behind the batch limit, and matching records colliding
when two imported accounts share the same external transaction identifier. Test
setup mistakes were corrected before treating the candidate cases as reproduced;
the corrected cases then demonstrated the unsafe state transitions.

Exception closure now verifies current stored bank facts, its bound input hash
and bank decision ownership under the same local writer transaction. Every match
disposition checks for another finalized document match using a bounded indexed
existence query. Reopening the same ignored/confirmed decision is retained;
an old exception cannot overwrite a newer candidate. Non-confirmation document
corrections remain possible; these checks do not bind every document fact for
every non-final decision or implement split/partial-payment policy.

Open missing-receipt matching refreshes the retained match and its open review
bank snapshot together, preserving review IDs rather than silently showing old
financial facts. A competing transaction consuming the document in one batch
does not erase an earlier pending bank/document candidate. Scoped SQL matching
lookups apply stored bank-row identity and documentless filters before the limit;
imported accounts no longer share a match merely because their external IDs
coincide. Ad-hoc input has no independent stored-row freshness proof.

Bank candidate selection now uses the existing open-state SQL filter before the
limit, rather than materializing a completed batch and filtering afterward. A
fixture with 120 newer closed bank rows still returns its older open row at limit
one. This is a functional/resource-bound improvement, not a measured whole-app
RAM, CPU, throughput or hard-deadline claim. Existing indexes and tables suffice;
no schema migration or source deletion was needed.

Current verification:

- Final application code passed 101 tests in 111.05 seconds across dispositions,
  approval freshness, missing receipts, reconciliation transactions, bank import,
  local reconciliation/review and rehearsal guards. The new disposition file has
  19 tests, including exact all-table state comparisons and four JSON/form API
  rejection paths. An earlier 77-test pass predates the final review-snapshot
  refresh assertion and repair; it is not the final acceptance result.
- The real built dashboard/API/SQLite rehearsal passes all 43 checks on Node
  24.19.0 and 25.2.1. New checks exercise actual HTTP 409 conflict delivery for a
  stale missing-receipt disposition and preservation of its open gate and changed
  amount. HAI scope restrictions, normal approval, reopening and logout/replay
  guards still pass. Fixtures are synthetic; owned children and fixtures stop
  and are removed before success is reported.
- An earlier rehearsal timed out during a reconciliation command; bounded log
  tails contained no diagnostic stack. Added fixture-only stall tracing covers
  reconciliation too. Subsequent passes do not establish that timeout's cause or
  a production latency guarantee. Two superseded intermediate test runs were
  stopped after later edits; neither is reported as a full passing suite.
- Diff whitespace validation passed. No web application source changed in this
  iteration; prior web build/type/test results are not claimed as newly rerun.

The complete Python suite, broader final-code API/worker/report/recovery coverage,
Docker/hosted CI, rendered-browser, real launcher/ngrok/provider/attachment/archive,
disaster recovery and larger-workload acceptance remain open. Richer candidate
selection, legacy/ambiguous-reference handling and superseded missing-receipt
review cleanup still need audit. No live provider mutation, source archival,
shared restart, credential rotation, commit, push or deployment was performed.

### Completed Confirmation Retry And Review Repair (2026-10-01)

Identical-status retries of a completed imported-bank confirmation now verify the
retained document/bank approval hashes, canonical current financial facts, final
bank/document states, matched timestamp, original resolution status and exact
final owner links before acknowledging the existing decision. Unchanged retries
do not write ledger rows or replace notes/timestamps. Ad-hoc bank inputs, missing
hashes and ambiguous or stale evidence do not receive this completed-state path.

The existing dashboard review action can repair an open completed-candidate task
or an already-superseded missing-receipt task. Only strictly validated review
links and supersession pointers are eligible. A missing/changed owner or attempted
correction is rejected. Partial repairs roll back with the outer transaction;
successful changes are audited. Confirmation and supersession history remain
intact, human ignored/rejected tasks are not reopened, and normal reconciliation
review approval now retains its original reference metadata alongside correction
details. No new approval or learning is generated during orphan repair.

This is an operator-triggered repair, not a blanket legacy sweep or autonomous
approval. Legacy decisions lacking verifiable hashes/links remain for manual
investigation. Closed review requests retain their existing already-closed
response; the read-only retry contract applies to the confirmation endpoint.
Live Wave record/attachment verification and source archival gates are unchanged.

The final built dashboard/API/SQLite rehearsal passes all 51 checks on Node
24.19.0 and 25.2.1. Added checks exercise the actual dashboard gateway review
repair, queue refresh and operator confirmation retry while verifying unchanged
final match and supersession history. This uses retained synthetic files,
loopback services and simulated trusted-proxy headers, not live accounts or real
TLS. Owned children and fixtures are stopped/removed before success. No web
application source changed; frontend build/type/unit checks were not rerun.

Final application code passed 378 selected Python tests and three subtests, with
one skipped test, in 590.22 seconds across 23 modules. These include 20 new retry
and review-repair cases, concurrent retries, exact all-table rollback/no-op
comparisons, invalid owner/hash/financial evidence, correction refusal, normal
review provenance retention, JSON/form API paths, reconciliation freshness,
supersession, review transactions, pipeline/worker/master ledger and backup/
recovery checks. An earlier 94-test pass predates the final provenance fix and
is not the final acceptance result. This is not a full-suite run or a comparable
performance benchmark. Whitespace checks passed (line-ending warnings only).

Full suites, rendered UI, real Windows launcher/ngrok/container deployment,
provider record/attachment readback, live disaster recovery and resource/latency
workload acceptance remain open. No shared service restart, live financial
mutation, source archival, credential rotation, commit, push or deployment was
performed. The production goal remains active.

### Financial Numeric Boundaries (2026-10-01)

Regression tests reproduced a real freshness defect: Decimal bank amounts were
compared with binary floats, falsely rejecting unchanged ordinary values such
as 4.28, 27.81, 1234.56 and 0.1. Both sides now use the existing Decimal parser,
and neither invalid side can compare equal merely because both parse to None.
The choice follows [Python Decimal documentation](https://docs.python.org/3/library/decimal.html),
not a relaxed tolerance or removal of evidence checks.

Bank parsing, ledger numeric writes and receipt numeric validation now reject
booleans, NaN, signaling NaN, infinities and values that overflow float storage.
Valid plain scientific notation is parsed as a number rather than having its
exponent stripped. Mixed bank imports skip invalid rows and retain valid rows;
invalid explicit amounts/debit/credit values are not silently replaced by other
fields. Receipt numeric strings are checked without a TypeError, present invalid
VAT is blocked instead of treated as absent, and confidence values/configuration
cannot use NaN or out-of-range scores to bypass review thresholds. An explicit
zero/invalid document total cannot fall through to a different amount field.

The final focused run passed 103 tests across the new numeric boundary module,
core reconciliation, receipt/reconciliation validation and bank import. Its
61-test initial red run had 53 failures, before any application changes. The
existing 51-check real built-stack rehearsal now uses synthetic EUR 4.28 source,
review correction and bank evidence together and passes on Node 24.19.0 and
25.2.1, including stale-conflict, approval, readback, supersession, retry, HAI
scope and logout checks. Services/files are isolated and cleaned before success.

No historical financial rows were rewritten and no live providers/sources were
changed. This is not a fixed-point storage migration, a complete locale/currency
parser, exchange-rate reconciliation, legal/VAT certification, or proof of
whole-app resource/latency improvements. These broader requirements and actual
Windows/ngrok/provider/recovery acceptance remain open.
Ad-hoc reconciliation payloads and retained historical raw JSON still require
their own validation audit. Normalized numeric guards do not sanitize or remove
source evidence, and no historical cleanup was performed by these tests.

Final full Python suite: **1,386 passed, one skipped, 83 subtests passed** in
1,273.76 seconds (21:13), with no test-file exclusions. This supersedes the
older selected-suite-only and failed Docker-context snapshots for local Python
acceptance. Both actual Docker build-context checks passed on this run (Python
31.76 seconds, web 16.58 seconds); that does not prove container deployment or
resolve all historical engine stalls. The slowest test was isolated Windows
startup rejection without services at 50.99 seconds. None of these timings is
a whole-app performance benchmark or a cause attribution for earlier timeouts.

A final fresh backup/rehearsal-guard run passed 20 tests, with the same one skip,
in 11.65 seconds. The native missing-evidence-directory symlink check is skipped
because Windows reports WinError 1314 (required privilege unavailable). No
privilege, developer-mode or shared-host settings were changed to force it.
The native skipped case remains an acceptance gap, not a passing guard test.

Frontend build/type/unit checks were not rerun because no web source changed;
the actual built gateway/application was exercised by the two EUR 4.28
rehearsals. Hosted CI, rendered-browser, real launcher/ngrok/container deployment,
live provider/attachment/archive, disaster recovery and workload/resource
acceptance remain open. No shared services were restarted and no live financial
mutation, source archival, credential rotation, commit, push or deployment was
performed. The full production goal is not complete.

### Direct Reconciliation Request Guards (2026-10-01)

Direct service/JSON/form reconciliation now captures an independent, canonical
bank request before matching. It requires object rows with finite amounts and
parseable dates and rejects non-finite JSON anywhere in retained bank metadata,
non-string object keys, cycles, excessive depth/width and oversized encoded data.
Limits are 500 rows, 4 MiB encoded UTF-8 bank JSON, 32 nesting levels and 50,000
values. These bounds apply after HTTP JSON decoding; the inherited transport/
upload body limit is unchanged and is not claimed to be 4 MiB for every route.
Large workloads can still use bank import and existing bounded processing.
Requests are not silently truncated and source files are not removed.

Caller mutations cannot alter the captured request, and a matching-engine
mutation of its captured input is rejected under the writer before recording
results. Existing database document/bank freshness checks remain. Explicit
null/false transaction arrays no longer become empty batches. The endpoint also
checks its JSON-object contract rather than depending only on existing global
malformed-body guards. Initial regression evidence: 21 failures and two already
passing cases in the 23-case pre-change run; the first selected post-change run
passed 59 tests before four additional resource-bound cases were added.

The actual built dashboard/API/SQLite rehearsal passes all 53 checks on both
Node 24.19.0 and 25.2.1. Two new real HTTP rejection checks use explicit null and
raw NaN JSON and verify absence of financial matches/open reviews before the
normal dashboard matching/approval flow proceeds. The first rehearsal failed
because Requests refused to prepare the negative NaN fixture; an isolated
request-preparation reproduction confirmed this without any network call. Raw
malformed JSON was then sent deliberately to the owned synthetic API. The final
passes exercise server rejection, not only client-side refusal. Owned children
and fixtures are stopped/removed before success.

Historical raw JSON, ambiguous ad-hoc identities across accounts, complete
currency/date semantics and matching workload/latency profiling remain separate
audit work. These request bounds are not whole-app speed/RAM measurements.
Provider mutation, attachment readback and Drive archival authority are unchanged.

Final selected regression verification passed **455 tests and three subtests**
in 229.64 seconds across 22 modules, including 27 direct-input guard cases,
financial boundaries, reconciliation/review freshness and rollback, API, bank/
bookkeeping/master ledger, pipeline, worker, autonomy and workflow recovery.
The fresh rehearsal-guard module passed three tests in 1.54 seconds after the
negative-HTTP fixture correction. Whitespace checks passed (line-ending warnings
only). No web application source changed; web build/type/unit checks were not
rerun. The earlier 1,386-test full-suite pass describes the preceding numeric
boundary snapshot, not a new full-suite result after these guards.

Full-suite revalidation of the new code, inherited HTTP decoding/resource limits,
rendered UI, real Windows/ngrok/container deployment, hosted CI, live provider/
attachment/archive, disaster recovery and workload acceptance remain open. No
live financial writes, source archival, shared restart, credential rotation,
commit, push or deployment occurred. The production goal remains active.

### Direct Reconciliation Account Isolation (2026-10-01)

Eleven initial regressions reproduced direct-request account collisions:
identical bank references in different accounts overwrote retained missing-
receipt evidence, duplicates within one batch silently reused a record,
non-string/conflicting account labels were accepted, and newer other-account
records hid the correct older record. These cases now pass. Thirteen account
tests include the JSON API and refusal to overwrite conflicting legacy aliases.

Ad-hoc lookups now scope the external reference by the exact account string
before limiting results, then validate the selected stored aliases again.
Within-batch duplicates reject the entire request before financial writes.
Native imported-bank record identity is unchanged. Missing/null/empty account
scope remains unscoped and separate from named accounts; strings are not trimmed,
case-folded or inferred. Existing history is not relabelled or deleted.

A derived partial account index supports the actual lookup without a temporary
sort. Reopening an older synthetic ledger installs it idempotently without
changing any table rows, including deliberately malformed legacy JSON; SQLite
integrity checks pass. Eighteen focused account/index tests pass in 3.93 seconds.
The index adds storage, write maintenance and first-install work; this is not a
whole-app speed or memory measurement.

Fresh ledger/backup/correction/backup-inspection verification passed **76 tests,
with one skip**, in 57.73 seconds. The skip remains the native Windows directory-
symlink guard requiring unavailable privilege; no host settings were changed.
After the rehearsal extension, its three isolation/cleanup guard tests passed
in 5.92 seconds.

Final selected regression verification passed **473 tests and three subtests**
across 24 modules in 353.12 seconds: account/index/input guards, numeric
boundaries, reconciliation freshness/rollback/retries/dispositions, review/API,
bank/bookkeeping/master ledger, processing, worker, autonomy, workflow recovery
and rehearsal guards. No web application source changed, so web build/type/unit
checks were not rerun; both actual built-stack rehearsals exercise the retained
web build against this backend. Whitespace checks passed with line-ending
warnings only. This is selected-suite proof, not a new full-suite result.

Both Node 24.19.0 and 25.2.1 pass the expanded **56-check** real built dashboard/
Waitress/SQLite rehearsal. Three new operator-authenticated HTTP checks exercise
direct-account acceptance, independent same-reference account records with the
first snapshot unchanged, and duplicate rejection without financial/review
changes. These are owned synthetic loopback fixtures, not live bank/provider
proof. Owned children are stopped and temporary fixtures removed before success.

Historical unknown account ownership, anonymous-reference ambiguity, complete
currency/date semantics, full-suite revalidation, rendered UI, workload/resource
profiling and real Windows/ngrok/container/provider/attachment/archive/disaster-
recovery acceptance remain open. No web source changed, no provider writes or
source archival occurred, and no shared service restart, credential rotation,
commit, push or deployment was performed. The production goal remains active.

### Reconciliation Reference Alias Integrity (2026-10-01)

The initial 21-case regression run exposed **18 failures and three already
passing cases**. Direct requests accepted malformed explicit references and
conflicting aliases; imported-bank freshness preferred `transaction_id` while
recording preferred `id`. A synthetic legacy candidate carrying that mismatch
could be confirmed even with its inconsistent metadata retained by a hash.

Direct input now validates both account aliases and external-reference aliases
before ledger reads/matching. Supplied `id`/`transaction_id` values must be
non-empty strings or integers, excluding booleans; aliases must agree after
integer-to-string conversion. Strings retain exact spelling, with whitespace-
only values rejected. Explicit null/empty/float/container values no longer
silently generate a fallback reference. A numeric zero is an explicit reference.
Requests with neither alias retain the existing inferred reference behavior.

Imported-bank freshness, approvals, dispositions, completed confirmation retries
and reuse of retained history share identity validation. Invalid/conflicting
retained references fail closed rather than confirming or overwriting history.
This does not certify all historical identifier corruption or anonymous-reference
uniqueness. A first selected run passed 115 tests with one message-pattern
assertion failure; the stored-conflict diagnostic was aligned with the existing
contract. The subsequent focused account/reference/API suite passes all **36
tests** in 8.04 seconds, including 23 reference cases.

Both Node 24.19.0 and 25.2.1 pass **58 built application checks** using the actual
dashboard/Waitress/SQLite loopback fixture. Two additional authenticated HTTP
checks reject conflicting and boolean references and verify unchanged financial
matches and review records. Provider writes remain disabled; owned children and
temporary fixtures are stopped/removed before success. This is not live-provider,
browser-rendering, deployment or whole-app performance certification.

Final selected regression verification passed **496 tests and three subtests**
across 25 modules in 271.57 seconds: reference/account/index/input guards,
numeric boundaries, reconciliation/review freshness and atomic rollback,
confirmation retries/dispositions, API, bank/bookkeeping/master ledger,
processing, worker, autonomy, workflow recovery and rehearsal guards. Fresh
rehearsal isolation/cleanup tests passed three cases in 3.15 seconds. Whitespace
checks passed. This is selected-suite proof; the earlier 1,386-test full-suite
snapshot remains historical. Web build/type/unit checks were not rerun because
no web source changed in this phase; the actual retained build was exercised by
both synthetic application rehearsals.

The full production goal remains active. Full-suite revalidation, historical
corruption/unknown ownership, anonymous-reference ambiguity, raw HTTP decoding
bounds, currency/date semantics, workload profiling and live Windows/ngrok/
container/provider/attachment/archive/disaster-recovery acceptance remain open.
No web application source changed and no live provider writes, source archival,
shared restart, credential rotation, commit, push or deployment occurred.

### Prepared Matching Performance (2026-10-01)

Matching previously reparsed bank and document fields for each eligible pair.
A deterministic 100-bank/80-document no-match fixture observed **16,000 amount
parses**. Prepared per-run features reduce that to **180**; date/vendor parsing
is also bounded by row count. There is no cache retained between requests or
runs. Custom `_match_score` overrides keep their original two-argument contract,
and empty bank batches do not inspect financial document payloads.

The default matcher retains scoring, tolerances, signs, date/vendor precedence,
rounding, document order, first-in-order ties and duplicate document-ID behavior.
Identical non-empty normalized vendors use exact similarity 1.0 without
constructing a fuzzy matcher. Randomized complete-result comparisons cover four
seeds and three configurations, malformed/zero/localized amounts, invalid/missing
dates, vendor variations and duplicate IDs. Original input objects are unchanged,
and a subsequent run sees changed document values rather than stale features.

`scripts/benchmark_reconciliation.py` is a bounded, synthetic-only reproduction:
500 bank rows, 100 documents, one warmup and three timing samples for each path,
plus a separate `tracemalloc` allocation-peak run. The comparison subclass invokes
the unchanged unprepared scoring path. Complete result lists must match on every
warmup, timing and memory run. It uses no accounts, database, network or source
files. Saved evidence is `.ecc/benchmarks/reconciliation-2026-10-01.json`.

| Final Variant | Unprepared Median | Prepared Median | Repeat Unprepared / Prepared |
| --- | --- | --- | --- |
| Different amounts | 108.33 ms | 32.27 ms | 208.93 / 80.52 ms |
| Same amount, identical vendor ties | 168.57 ms | 20.93 ms | 387.58 / 48.53 ms |

The repeated results show about **2.6-3.4x** faster different-amount matching and
about **8x** faster identical-vendor matching on these fixtures. Shared-host wall
time varies substantially; no production latency SLA or whole-app speed target
is established. The preparation-only variant showed little dense-workload gain,
so the final variant also avoids identical-vendor fuzzy matcher allocations.
The pre-edit two identical paths had noisy timing differences; these are not
claimed as improvements.

Allocation peaks increased from 163,536 to 191,779 bytes for different amounts,
and 170,088 to 199,454 bytes for identical-vendor ties. These are traced Python
allocation peaks including result materialization, not process RSS. Preparation
trades about 28-29 KB of per-run feature storage for less repeated parsing/CPU;
it does not prove the requested whole-app memory reduction. The core pairwise
search is still quadratic, and mixed fuzzy-vendor/large/OCR/provider workloads
need separate profiling. Bounds, approval/readback and archival gates are unchanged.

Both Node versions pass the existing **58-check** actual built dashboard/API/
SQLite rehearsal with the prepared matcher. Owned synthetic children and fixtures
are cleaned up before success. No web application source changed, live financial
mutation, source archival, shared restart, credential rotation, commit, push or
deployment occurred. Full-suite revalidation, process-RAM/whole-app workload
profiling, rendered UI and live production acceptance remain open; the full
production goal remains active.

Final prepared-matcher selected regression verification passed **521 tests and
three subtests** across 28 modules in 456.73 seconds. Coverage includes 17 new
prepared-feature cases, base and validation matching, identity/index/input and
numeric guards, reconciliation/review freshness and rollback, API, bank/
bookkeeping/master ledger, processing, worker, autonomy, recovery and rehearsal
guards. Whitespace checks and the saved benchmark JSON validation pass. Earlier
110/111-test focused results preceded the final between-run freshness case; the
521-test run includes that case. No new full-suite pass is claimed, and web
build/type/unit checks were not rerun because this phase changed no web source.

### Retained Bank Reference Preservation (2026-10-01)

The first 14-case run reproduced **11 failures and three passing controls**:
stored null bank-record aliases could be treated as genuinely unlinked evidence,
and selected native history with null, boolean, float, fractional-string or
conflicting aliases could silently create replacement history. The JSON API
returned 200 instead of rejecting an ambiguous retained record.

Shared identity validation now requires every supplied persisted-bank alias to
parse consistently as a positive integer. Absent aliases still mean unlinked;
explicit null/invalid/conflicting aliases do not. Matching-history reuse,
freshness, approval, disposition and confirmation retry use this validation.
No historical table rows are relabelled or repaired automatically.

Two additional failing regressions reproduced disagreement between SQLite's
numeric query prefilter and strict Python parsing for underscore/mixed-Unicode
integer strings. A selected record whose strict bank identity differs from the
requested identity now stops the operation rather than acting as a missing
record and creating replacement history. This protects selected ambiguous rows;
it is not a complete discovery/migration of all unselected legacy corruption.
Consistent zero-padded, signed and whitespace-wrapped positive references retain
their existing behavior.

All **139 focused tests** pass in 47.98 seconds across retained-reference,
account/external-reference, freshness/approval/retry/disposition and indexed-
lookup modules, including 16 retained-reference cases. Those regression cases
compare every table row before/after rejection. Both Node versions pass **59
built dashboard/API/SQLite checks**. The new authenticated HTTP case seeds only
an owned synthetic retained null reference, sends valid unlinked input and
verifies 400 plus unchanged financial matches and review records. Owned children
and temporary fixtures are stopped/removed before success.

No provider writes, source archival, shared restart, credential rotation,
commit, push or deployment occurred. Web application source is unchanged in
this phase; the real retained web build was exercised by both rehearsals.
Unknown/unselected legacy ownership, full-suite revalidation, inherited raw HTTP
decoding limits, rendered UI, workload/process-RAM and live production acceptance
remain open. The full production goal remains active.

### Refreshed Full Local Verification (2026-10-01)

After the retained-reference fixes, the complete Python command
`.venv\Scripts\python.exe -m pytest -q --tb=short --durations=15 -p no:cacheprovider -p no:stepwise`
passed **1,483 tests and 83 subtests**, with **one skip**, in 933.05 seconds.
This supersedes the earlier per-phase full-suite revalidation gaps for the
current Python source snapshot; the 1,386-test numeric-stage snapshot remains
historical. Both real Docker build-context exclusion tests passed (Python 6.46
seconds; web 7.48 seconds), not merely mocked Compose/context checks. This is
not a real deployed-container acceptance test.

The only skip is `TestLocalBackupService::test_full_restore_rejects_dangling_link_target`.
A fresh single-case `-rs` run confirmed Windows WinError 1314: the process lacks
the directory-symlink creation privilege. That native case remains unverified;
no privilege, developer-mode or shared-host settings were changed to force it.

Fresh dashboard verification through the bundled pnpm wrapper passed **379 tests
across 32 files** with two test workers (43.43 seconds), the TypeScript no-emit
check, and the production build. Client production budgets passed: index HTML
2,031 bytes and largest JavaScript asset 480,764 bytes. Both server bundles were
rebuilt, including the standalone split-chunk bundle. No web source was edited
in this phase; these are fresh checks of the preserved current web source.

After that rebuild, both Node 24.19.0 and 25.2.1 again passed all **59 actual
application rehearsal checks**, including backend/SQLite persistence, rejection
of ambiguous retained references, source-byte verification, HAI scope denial,
operator approval freshness, handoff/replay/logout and owned-fixture cleanup.
These use synthetic loopback data and simulated trusted-proxy HTTPS headers,
not real TLS, browser rendering or downstream providers. All required test,
type-check, build and rehearsal processes were terminal before reporting success.

The full production objective is still unproven: unknown/unselected legacy
corruption, raw HTTP decoding limits, rendered UI, process-RAM/whole-app workload
profiling, hosted CI, real Windows/ngrok/container deployment, live provider
record/binary-attachment readback, Drive archival and disaster-recovery acceptance
remain separate gates. There were no live financial mutations, source archival,
shared-service restarts, credential rotations, commits, pushes or deployments.
Whitespace checks pass; the goal remains active.

## Predecode Reconciliation Request Boundary (2026-10-01)

Synthetic red tests reproduced oversized root-envelope acceptance and decoder
recursion errors: five failures and two passing controls before the fix.
Authentication and origin checks now precede a route-specific 5 MiB stream bound
for the API and form reconciliation POST routes. The bound leaves 1 MiB beyond
the existing 4 MiB canonical bank-transaction limit for the request envelope and
preserves a stricter global setting. JSON mutation decoding catches recursion
errors as HTTP 400; the reconciliation form retains its reviewable error redirect.
No partial transaction batches are accepted or silently truncated.

Both requirements files now require Flask >=3.1,<4 for the per-request limit
setter. The implementation uses Flask's guarded request stream, not raw WSGI
reads. See the official [Flask request-limit documentation](https://flask.palletsprojects.com/en/stable/api/#flask.Request.max_content_length).

Fresh verification: **178 focused Python tests passed in 97.95 seconds** across
request boundaries, operations API, HAI scope, parent sessions, bank imports,
service input bounds and local intake. Eleven new boundary cases cover known-
length rejection before any body read, terminated streams without a length,
no large-body JSON decoding on rejection, exact-limit acceptance, stricter
configured limits, auth/origin priority, deep API/form JSON and unchanged ledger
tables. A valid 4 MiB bank JSON file, whose base64 envelope exceeds 5 MiB,
still imports successfully through the real bank-import API.

A second run passed **172 tests in 56.91 seconds** across 12 reconciliation and
stack-rehearsal guard modules: transaction rollback, retained/reference/account
identity, lookup indexes, freshness, dispositions, confirmation retries,
approval freshness, service integration and missing-receipt handling. Total:
**350 passing tests across 19 modules**. Both processes exited successfully.

Both Node 24.19.0 and 25.2.1 passed **61 real-HTTP rehearsal checks**, including
two new malformed-body checks that preserve reconciliation and review records.
Both fixtures reported owned children stopped and cleaned their temporary state.
The web bundles from the prior successful build were reused; no web source changed.

The prior 1,483-test full-suite and 379-test web results remain historical for
this new Python change. These checks do not demonstrate Waitress/ngrok/proxy
buffering bounds, native browser rendering, production traffic/RAM, deployment,
live provider attachment readback or archival safety acceptance. Existing form
field limits remain unchanged. No live provider writes, source archival, shared
service restarts, credential changes, commits, pushes or deployment occurred.

## Exact Bounded Document Selection (2026-10-01)

A complete synthetic red run reproduced 24 failures: empty selectors expanded
into automatic discovery, booleans/fractions could select unrelated documents,
scalar/container selectors and oversized integers raised server errors,
duplicate references were reprocessed and missing-reference iterables consumed
unbounded lookups. The iterable regression uses a guarded prefix so a missing
bound fails instead of hanging the test runner.

The service now captures explicit selections before database reads or matching.
At most 500 submitted references are accepted, including duplicates. References
must be positive integers or integer strings in the SQLite signed-64-bit range;
booleans/fractions, strings as the collection itself, mappings and unordered
sets are rejected. Duplicate IDs are retained once in first-occurrence order.
An explicit empty collection stays empty through the write-phase freshness
check. Omitted/null selection still uses automatic candidate discovery. The
existing candidate-count limit remains independent. A follow-up red run exposed
two additional failures for missing selected documents (service and API). These
now reject the entire selection instead of silently producing missing-receipt
results. All selected references are checked, including those beyond the
candidate-count limit; tests cover both limit 1 and limit 100.

The initial selection snapshot passed **210 tests in 67.45 seconds** across 14 reconciliation
and stack-guard modules, including 27 selection cases plus the request-boundary
tests. Cases cover rejection before lookups/writes, exact 500-reference acceptance,
bounded iterable consumption, duplicate capture, automatic-mode compatibility,
caller mutation after capture and HTTP error/no-scope-expansion behavior. This
snapshot predates the follow-up missing-document rejection.

The final combined run, after the missing-document fix, passed **381 tests across
20 modules in 156.81 seconds**, including all 31 new document-selection cases.
It also rechecked request bounds, reconciliation/approval/disposition/retry
freshness, identity/index/transaction invariants, operations API, HAI scope,
parent operator sessions, bank imports, service input bounds and local intake.
The process exited successfully. This is focused regression evidence, not a new
full Python/web suite result or live-provider/deployment acceptance.
Both Node 24.19.0 and 25.2.1 passed **65 actual HTTP rehearsal checks**; four
new selection checks preserved financial/review records. Owned child processes
stopped and temporary fixture state was cleaned. No web source/build changed.

Full-suite, rendered-browser, live-provider and deployment acceptance remain
separate gates. No real account mutation, archival, shared-service restart,
credential change, commit, push or deployment was performed.

## Reconciliation Document Eligibility (2026-10-01)

Synthetic red tests reproduced 12 failures and six passing status controls:
explicit selections could bypass processing gates, and processed documents with
a duplicate-source link entered both automatic and explicit candidate pools.
The service now rejects nonterminal explicit selections whose processing status
is outside the existing reconcilable set or whose duplicate-source link is set.
The automatic SQL query excludes linked duplicates before ordering/limiting.
No document, source, duplicate linkage or existing financial history is removed.

The initial targeted regression run passed 96 tests in 46.73 seconds. A later
eligibility run passed all **26 cases in 8.66 seconds**, covering service/API
rejection with unchanged ledger tables, all six eligible processing statuses,
completed/ignored exclusions, gate rejection beyond limit 1/100 and automatic
discovery at limit one with a newer duplicate before a valid document.

The combined regression run passed **406 tests across 21 modules in 161.05
seconds**, covering selection/request bounds, reconciliation transactions,
identity/index/freshness/disposition/retry/approval guards, operations API,
HAI scope, parent sessions, imports, service bounds, local intake and rehearsal
guards. It collected the first 25 eligibility cases; the later 26-case run added
the candidate-displacement regression. Together they cover **407 distinct
passing tests**, not 432. Both processes exited successfully. The previous
full-suite snapshot predates this stage and was not rerun here.

Both Node 24.19.0 and 25.2.1 passed **67 real-HTTP rehearsal checks**, including
two new review/duplicate-gate checks with unchanged financial/review records.
Fixtures used isolated synthetic loopback data, stopped owned children and
cleaned temporary state. Existing web bundles were reused; web source/build
was not changed or rerun. Provider calls, archival, deployment, real TLS/browser
rendering and whole-application speed/RAM acceptance are not demonstrated.
The full production goal remains active; no commit/push or live mutation occurred.

## Dashboard Gateway Rejection Codes (2026-10-01)

The gateway previously preserved HTTP 409 as a conflict but collapsed other
structured backend rejections into generic errors. Red tests reproduced eight
failures with the conflict control passing. It now preserves the tRPC rejection
code for JSON-object HTTP 400/401/403/404/409/413/422/429/503 responses. Error
messages remain sanitized/bounded; response-body code fields cannot override
HTTP status. The change adds no retries or provider permissions and does not
change malformed JSON, unknown-status, timeout or cancellation handling.

Fresh web verification passed **390 tests across 32 files in 49.20 seconds**, the
TypeScript no-emit check and production build. Eleven new tests include all nine
status mappings, credential redaction/500-character bounds/body-code distrust,
and a real loopback HTTP 413 rejection with exactly one mutation submission.
The production budgets passed: index HTML 2,031 bytes and largest JavaScript
asset 480,764 bytes. Both server bundles were rebuilt. Rehearsal guard tests also
passed (three Python tests in 3.00 seconds); no Python application source changed.

After rebuilding, both Node 24.19.0 and 25.2.1 passed **68 actual HTTP rehearsal
checks**. A new malformed bank-file import passed through the real dashboard
mutation route and Python backend, returned HTTP 400 with tRPC `BAD_REQUEST`,
and preserved the imported bank rows and reconciliation records. Existing
successful imports, stale-approval conflicts, review persistence, source-byte
integrity, HAI denial, parent-session/replay/logout and cleanup checks also passed.
All test/type-check/build/rehearsal processes exited; owned fixtures reported
children stopped and removed their temporary state.

No rendered client component changed, and no browser-rendering check was run.
This is current gateway/transport evidence, not a fresh full Python suite,
live-provider, real TLS/ngrok, deployment, whole-app RAM/performance or disaster-
recovery acceptance. No shared service restart, credentials change, external
financial mutation, archival, commit, push or deployment occurred.

## Avoid Failed-Batch Request Amplification (2026-10-01)

Red tests reproduced six failures: HTTP 405 lacked a typed unsupported-method
code, and batch 401/403/429/503/transport failures caused the refresh to issue
30 backend requests instead of the initial three. The gateway now propagates
non-compatibility batch failures to resource error/stale states rather than
attempting 27 individual fallback reads. Structured JSON-object 404/405 errors
retain the bounded legacy fallback; successful incompatible batch schemas also
retain the prior fallback. Backup/cloud-status reads remain independent. The
mutation retry policy, ledger and provider permissions were not changed.

All five synthetic failure cases observed **3 requests instead of 30**, a 90%
request-count reduction for these specific refresh failures. A separate owned
loopback HTTP 503 server also observed exactly the batch/backup/cloud requests
and a disconnected result with null unavailable metrics. No client credential
headers were forwarded to this synthetic server. Neither result demonstrates
90% less whole-app CPU/RAM or a universal latency improvement.

Fresh full web verification passed **399 tests across 32 files in 23.34 seconds**,
including nine new cases: 405 classification, five failure-fanout cases, two
unsupported-endpoint controls and real HTTP 503 fanout prevention. Type checking
and production builds passed; index HTML remains 2,031 bytes and largest client
JavaScript 480,764 bytes. Both server bundles were rebuilt. The web suite still
checks cached stale-state handling, valid batched reads, bounded compatibility
concurrency, mutation invalidation, malformed data and credentials/redirect safety.

Both Node 24.19.0 and 25.2.1 passed all **68 actual HTTP rehearsal checks** using
the rebuilt bundles and existing Python source. Owned children stopped and
temporary fixture state was removed. No Python application or rendered client
component changed; full Python/browser-rendering/provider/deployment/RAM gates
remain unproven. No live financial mutation, archival, shared-service restart,
credentials change, commit, push or deployment was performed.

## Overlapping Dashboard Cache Generations (2026-10-01)

Three red tests reproduced two races: an older slow refresh could publish live
data and overwrite newer per-resource history after explicit refresh; a snapshot
loaded while a mutation was pending remained cached after that mutation finished,
including an unreadable response. The control-center builder now checks its
captured generation after read settlement and before any per-resource cache
writes, rejecting superseded reads with tRPC `CONFLICT`. Mutation requests
invalidate the snapshot both before submission and in final cleanup. No mutation
is automatically retried; retained last-valid data remains explicitly stale on
subsequent failed reads.

Fresh full web verification passed **403 tests across 32 files in 26.35 seconds**,
including four new cases: obsolete-reader/new-history preservation, successful
and unreadable mutation completion after an intervening cached read, and a real
owned HTTP mutation held pending while dashboard reads observe the old value.
The HTTP case then completes the mutation, verifies a fresh value and confirms
exactly one submission. Synthetic servers receive no credential headers.
Type checking and production builds passed; index HTML remains 2,031 bytes and
largest client JavaScript 480,764 bytes. Both server bundles were rebuilt.

Both Node 24.19.0 and 25.2.1 passed **68 actual HTTP rehearsal checks** after
rebuilding, including SQLite persistence, dashboard refresh after writes,
validation/stale-approval codes, source-byte integrity, HAI restrictions and
parent-session/replay/logout. All required processes exited and owned fixtures
stopped children and removed temporary state. No Python application or rendered
client component changed. Full Python, browser-rendering, provider, real TLS/
ngrok, deployment, disaster-recovery and whole-app resource/performance gates
remain separate and unproven. No live financial mutation, archival, shared-service
restart, credentials change, commit, push or deployment occurred.

## Strict HAI Integer And Replay-Key Contract (2026-10-01)

Red tests reproduced **23 failures and six passing controls**: limit/document-ID
booleans, fractions, whole-valued floats and numeric strings were coerced into
integers; infinity raised unhandled overflow errors; oversized document IDs were
accepted; non-string replay keys were converted into valid-looking identities.
A further three red cases exposed missing required attestation fields being
marked ready. Inputs now fail before executors, lease changes or command audit
writes. Limits retain their per-command ranges. Local document IDs accept native
integer values from 1 through 2^63-1, and the manifest declares that maximum.
Whole-valued floats are intentionally rejected rather than converted. Replay
keys must be strings; existing trimming/pattern and durable replay protections
remain unchanged. Attestation requires `documentId` and `evidence`, without
weakening exact downstream/binary-readback and archival gates.

The initial targeted run passed **41 tests in 8.93 seconds** before the required-
field follow-up. The new test module contains 36 service/API cases, including
valid minimum/maximum integer controls, manifest maximum consistency, replay
reuse, malformed plan/execute inputs and unchanged ledger-table snapshots.

The final focused regression run passed **193 tests across seven modules in
234.46 seconds**, including the 36 new contract cases, existing HAI execution/
idempotency/concurrency checks, operations API, parent sessions, credential
format, rehearsal guards and reconciliation request boundaries. The process
exited successfully. This does not replace a fresh full Python suite result.

Both Node 24.19.0 and 25.2.1 passed all **70 actual HTTP rehearsal checks**, adding
two malformed-HAI execute cases (boolean limit and boolean replay key). Existing
HAI scope denial, dashboard/SQLite persistence, source integrity, stale approval,
account/reference isolation and parent-session/replay/logout checks still passed.
The fixtures stopped their owned children and removed temporary state. Existing
web bundles were reused; no web source, type check or build changed in this stage.

These are local synthetic checks, not proof of live provider attachment readback,
source archival, hosted TLS/ngrok/container deployment, browser rendering,
whole-app resource/performance or disaster-recovery acceptance. No live account
mutation, archival, shared-service restart, credentials change, commit, push or
deployment occurred; the full production goal remains active.

## Bounded HAI Metadata And Audit Snapshots (2026-10-01)

The corrected red regression run reproduced **11 failures**: nested caller/
executor dictionaries could change the audited request, breaking legitimate
replay; non-finite, non-JSON, oversized, overly wide/deep and cyclic metadata
was admitted by planning. Command normalization now captures an independent
JSON snapshot, and execution receives a separate copy. Successful and failed
audit records retain the captured request rather than executor-mutated data.
The snapshot must contain finite JSON-compatible values, at most 1 MiB in
canonical UTF-8, depth 32 and 10,000 values. Tree checks occur before encoding
and again on the decoded owned snapshot; bytes are bounded during encoding.
Existing integer, required-field, authority and replay rules remain in place.

Authenticated HAI plan/execute HTTP envelopes now have a 2 MiB predecode limit,
preserving stricter global limits and authentication/origin precedence. Receipt
and bank-file upload routes retain their existing limits. Canonical metadata
size and HTTP wire size are separate constraints. These bounds do not prove
upstream buffering or whole-process RAM limits.

The initial focused run passed **59 tests in 18.87 seconds** before five further
boundary/HTTP controls were added. The final current-source run passed **209
tests across eight modules in 150.65 seconds**, including all 16 new snapshot
cases, strict integer contracts, existing HAI execution/idempotency/concurrency,
operations API, parent sessions, credential format, rehearsal guards and
reconciliation request boundaries. Tests cover success/failure executor
mutation, caller mutation, exact 1 MiB UTF-8 acceptance, invalid JSON metadata,
HTTP non-finite rejection and both oversized HTTP routes without body reads.
The process exited successfully; this is not a fresh full Python suite.

Both Node 24.19.0 and 25.2.1 passed all **71 actual HTTP rehearsal checks**, adding
authenticated oversized HAI plan rejection. Dashboard/SQLite persistence,
source integrity, approval freshness, account/reference isolation and parent
sessions/replay/logout still passed. Owned children stopped and temporary state
was removed. Existing web bundles were reused; no web source, type check or
build changed in this stage. The earlier 403-test web result is historical.

Local synthetic checks do not establish live provider attachment readback,
archival, browser rendering, hosted TLS/ngrok/container deployment, whole-app
resource/performance or disaster-recovery acceptance. No live account mutation,
archival, shared-service restart, credential change, commit, push or deployment
occurred. The full production goal remains active.

## HAI HTTP Command Audit Attribution (2026-10-01)

Red tests reproduced **10 failures with one passing unauthenticated control**:
body-supplied actor labels were recorded as the requested/completed/failed actor
and passed to executors, including labels impersonating human operators. The
HTTP command route now derives its actor from the request's verified credential
class: `fab_hai_api:hai` or `fab_hai_api:operator`. Token-free loopback access
uses `fab_hai_api:loopback`, which is not a verified personal identity. A scoped
HAI bearer token keeps its classification even with an operator session cookie.
Changing the body label on replay does not replace the original audit or cause
another executor call. Trusted in-process calls and other API routes retain
their existing attribution; this is not app-wide individual identity proof.

All **75 focused HAI tests passed in 14.68 seconds** after the fix. The wider
regression run passed **220 tests across nine modules in 106.79 seconds**, with
successful process exit. Its 11 new cases cover success and failure for HAI/
operator tokens, operator sessions, HAI-with-session precedence, token-free
loopback, replay with a changed label and unauthenticated rejection. Existing
snapshot/integer contracts, operations API, parent sessions, credential format,
rehearsal guards and reconciliation request-boundary tests remain green. This
does not replace a fresh full Python suite.

Both Node 24.19.0 and 25.2.1 passed all **73 actual HTTP rehearsal checks**,
including persisted server-attributed HAI audit records and replay preserving
the original audit event. The first Node 24 attempt correctly denied the new
command because the disposable fixture had no command allowlist. The fixture
now explicitly allows only notification refresh; production configuration was
not broadened. Both successful fixtures stopped owned children and removed
temporary state. Existing built web bundles were reused, with no web source,
type check or build changes in this stage.

Documentation and whitespace checks were updated/passed. No live account
mutation, archival, shared-service restart, credential change, commit, push or
deployment occurred. Provider attachment readback, browser rendering, hosted
TLS/ngrok/container deployment, whole-app performance/resource use and disaster
recovery remain separate acceptance gates. The full production goal stays active.

## Valid Wave Fields Before Readback Or Work-Order Execution (2026-10-01)

The first red run reported **20 failures and eight passing test results**:
16 direct field-comparison cases and four corrupted-ledger readback subcases.
Invalid expected/observed dates or amounts could both normalize to `None` and
compare equal; non-finite monetary values, blank normalized text, booleans and
containers could also appear to match. Synthetic records with invalid dates,
`NaN`/infinite amounts or blank vendors were incorrectly recorded as verified
despite otherwise exact attachment bytes. No live source or provider was used.

Comparison now requires valid canonical values on both sides. Monetary values
must be finite parseable numbers and cannot be booleans or containers; textual
fields must be strings with nonblank normalized content. Supported date formats,
cent precision and valid case/spacing normalization are unchanged. Invalid
expected values remain in the ledger for investigation rather than being fixed
or deleted automatically. Matching metadata is not evidence of actual stored
Wave bytes or permission to archive a Drive source.

Further red tests reproduced four work-order subcase failures and one missing-
document-VAT failure. Work-order readiness now checks all required expected
fields through the same canonical validation used by readback, holding otherwise-
ready invalid records at `needs_processing` and listing them in
`missingExpectedFields`. Existing source-file/review gates take precedence.
Work orders, recorded readback and later evidence checks share required-field
selection; VAT from either the document or normalized bookkeeping record must
be observed, including zero. A positive control confirms that correct record
VAT readback still succeeds when document VAT is absent.

The initial corrected service/executor run passed **65 tests and 14 subtests in
34.38 seconds**, before the authenticated-route and final signaling-Decimal
controls. The later focused run passed **26 tests in 3.02 seconds**, including
those controls. An initial API test incorrectly expected HAI to read the
operator-only archive-plan route. Its assertion was corrected to expect the
existing HAI HTTP 403 and use the operator token for that read; route authority
was not broadened. The first wider regression run therefore ended with that
one test assertion failure, 238 passing tests and 14 passing subtests, not an
all-green result.

The final current-source regression run passed **240 tests and 14 subtests across
eight modules in 204.93 seconds**, with successful process exit. It includes all
24 field-comparison cases, corrupted-ledger exact-byte readback rejection,
work-order invalid-field holds, missing-document-VAT rejection and its valid
record-VAT control, authenticated multipart-route rejection, receipt-executor
coordination, operations API routes and existing HAI integer/snapshot/actor/
execution/replay contracts. This is focused regression coverage, not a new full
Python suite result. Whitespace checks passed; documentation records the real
failed and successful verification attempts.

Both Node 24.19.0 and 25.2.1 passed the existing **73 actual HTTP rehearsal
checks** against the updated backend, with owned children stopped and temporary
state removed. These checks exercise the dashboard/backend/SQLite/auth/replay
paths, not live Wave readback or this new corrupted-field scenario. The latter
is covered by disposable service and Flask multipart-route tests. Existing web
bundles were reused; no web source, type check or build changed in this stage.

No live provider mutation, archival, credential change, shared-service restart,
commit, push or deployment occurred. Full Python/browser-rendering, live-provider,
hosted TLS/ngrok/container, disaster-recovery and whole-app resource/performance
acceptance remain separate gates. The full production goal stays active.

## Exact Source And Provider Byte-Size Gates (2026-10-01)

Red tests reported **36 failures and 26 passing results in 3.36 seconds**:
source sizes could coerce booleans/floats or use a fallback after zero/false,
conflicting declared sizes were ignored, infinity could raise an overflow, and
missing current provider size could pass archival. Source sizes now require
positive native integers or trimmed 1-19-digit ASCII strings within 2^63-1;
all declared intake/provider sizes must agree. Evidence integer parsing also
rejects floats, signed/underscore/non-ASCII strings and oversized values rather
than coercing them. Existing receipt file-size limits remain independent.

Unknown/invalid source size now blocks full attachment verification as well as
archival, tightening the earlier contract that allowed verification but held
archival. Upload work orders report `source_incompatible`, and the existing
dashboard blocked-count aggregation already includes that stage. Current Drive
size must be present, valid and exact before and after a move. Synthetic tests
prove no pre-move mutation on missing size, rollback with no archival marker
after missing post-move size, and rechecking prior verified evidence after
source metadata changes while preserving its audit and original bytes.

The initial focused run passed **122 tests and 20 subtests in 19.26 seconds**.
After route/rollback/prior-evidence controls, another focused run passed **56
tests in 3.99 seconds**. An initial wider run ended with **296 passing tests,
20 passing subtests and one failed test assertion in 187.57 seconds**: the new
test incorrectly expected HAI to access an individual work-order route. It now
expects the existing HTTP 403 and uses the allowed bulk route, which reports
the malformed source as blocked rather than returning a server error. The
authenticated binary-readback route also rejects it without recording verified
evidence. No route permission was broadened.

The final current-source regression run passed **298 tests and 20 subtests across
nine modules in 368.96 seconds**, with successful process exit. It includes all
53 size-contract cases, source/incomplete-size rejection, provider pre/post-move
guards, rollback without an archival marker, prior-evidence/audit preservation,
authenticated route rejection/blocked counts and the existing field, receipt-
executor, operations API and HAI contract suites. This is focused regression
coverage, not a fresh full Python suite. Documentation and whitespace checks
passed; unrelated dirty-worktree changes were preserved.

Both Node 24.19.0 and 25.2.1 passed all existing **73 actual HTTP rehearsal
checks** against this backend, with owned children stopped and temporary state
removed. Those checks cover dashboard/backend/SQLite/auth/replay, not the new
size scenarios or live providers; new size cases use disposable service/Flask
fixtures and a fake Drive archiver. Existing web bundles were reused, with no
web source, type check or build changes in this stage. No whole-app speed/RAM,
live Drive restore or production deployment claim follows from these checks.

No live account mutation, archival, shared-service restart, credential change,
commit, push or deployment occurred. Full-suite, rendered-browser, live-provider,
hosted TLS/ngrok/container, disaster-recovery and resource/performance acceptance
remain separate gates. The full production goal stays active.

## Delivery JSON And Line-Item Quality, 2026-10-03

The legacy-corruption regression was corrected to write an infinite amount
directly into its disposable SQLite fixture. The public ledger update helper
already rejects non-finite numbers; using it had not created the intended
legacy state. The corrected pre-fix test failed strict JSON encoding of the
authenticated bulk work-order response, proving a real handoff defect.

Delivery work orders now project invalid expected fields as null while retaining
the original ledger values and reporting required-field failures. A scoped,
independent line-item projection reports invalid finite-number representations
by path in `wave.invalidLineItemFields`, including non-finite nested metadata.
Quantity, unit price, amount, tax amount, tax rate and confidence are checked
when present. Optional absent numbers remain supported; valid precision and
text metadata are unchanged. No tax/range/arithmetic correctness is inferred
from this representation check, and other API serialization is not certified.

Both full and compact work orders block otherwise-ready malformed records at
`needs_processing`. Existing source/review gates retain precedence. The same
line-item diagnostics block attachment readback approval and archive planning,
including when prior verified evidence exists. Tests verify that no fake Drive
move occurs after corruption, prior audit records survive and source bytes are
unchanged. The projection does not repair or rewrite persisted financial data.

An initial nine-module regression run passed **303 tests and 32 subtests in
61.78 seconds**. Its only warning was inability to write the existing pytest
cache; no cache permission changes or deletions were made. Additional direct
tests cover booleans, malformed strings, NaN/infinity, nested metadata, optional
values, precise values and independent projection copies. The final ten-module
run passed **356 tests and 32 subtests in 47.62 seconds**, exit 0, with caching
disabled for this run and no warnings. This is focused regression coverage,
not a fresh full Python suite or a whole-app performance comparison.

Both Node 24.19.0 and 25.2.1 passed all existing **73 actual HTTP rehearsal
checks**, with owned children stopped. These checks used the existing web
bundles and synthetic ledger/backend fixtures; they do not cover the newly
corrupted line-item states, which are covered by the Python service/Flask tests.
No fresh web build, rendered-browser, live provider, whole-app RAM/speed or
deployment acceptance is claimed. No account mutation, real archival,
credential change, shared-service restart, commit, push or deployment occurred.
The full production objective remains incomplete.

## Pre-Parse Upload Request Bounds (2026-10-08)

Review found that the global 101 MiB API request ceiling also applied to small
authenticated JSON commands and routes whose decoded uploads are limited to
4-6 MiB. Requests could consume unnecessary memory and parser work before
rejection. Mutating API requests now default to a 2 MiB pre-parse body limit.
Larger valid contracts have explicit limits for local intake, bank imports,
Gmail/Drive OAuth credentials, Wave attachment readback, Drive relay, autonomy
bank-transaction input, reconciliation, and HAI. Upload budgets include bounded
base64 or multipart metadata overhead and retain decoded-byte checks. All route
budgets are clamped by a lower configured global ceiling.

Current-source verification before the default limit passed **103 tests** in
`tests/test_local_operations_api.py` and **87 tests** across bank import,
reconciliation request-boundary, HAI contract, and ngrok-script test modules.
After adding the default limit, four focused request-boundary tests passed.
They verify POST/PUT selection, bounded exceptions for the 100 MiB Drive relay
and autonomy inputs, global-limit clamping, and oversized JSON rejection with
HTTP 413 before decoding. The suite covers route-specific caps but is not a new
full Python suite, Windows-host acceptance, or live-provider/tunnel run. No
account, provider, source-file, tunnel, or deployment state was changed.

## Wave Report Result Boundaries (2026-10-09)

The report-result endpoint now has a dedicated 16 MiB source-data budget plus
bounded JSON metadata overhead, rather than inheriting the 2 MiB ordinary API
limit. Report parsing accepts up to 10,000 rows. The optional import-and-
reconcile path rejects inputs above the reconciler's existing 500-row batch
limit before importing any transactions, avoiding a partial import when
reconciliation would otherwise fail afterward. These limits also apply in the
Wave report service to parsed report text and row collections.

Current-source verification passed **121 tests** across the local operations
API and Wave control suites, and **87 tests** across bank import, reconciliation
request boundaries, HAI contracts and Windows tunnel scripts. Python
compilation and `git diff --check` passed. These are focused local regressions;
no live Wave report, large production export, provider write, deployment or
disaster-recovery behavior was exercised.

## Atomic Bank Transaction Imports (2026-10-09)

Bank transaction import previously committed its import record, individual
transactions, completion state and audit event through separate SQLite
connections. An unexpected failure could leave a partial batch and a stale
`running` import. The importer now wraps that complete local operation in the
ledger's existing write transaction; row-level validation skips remain
reported normally, while unexpected failures roll back all batch writes.
This also reuses one SQLite transaction/connection instead of committing each
row separately.

A failure-injection test forces the second row write to fail and verifies that
no transaction, import record or completion audit event remains. This verifies
local SQLite atomicity only; power-loss/filesystem fault testing and concurrent
production load are not claimed. The affected bank-import, local API and Wave
control suites passed **130 tests**; Python compilation and diff checks passed.
