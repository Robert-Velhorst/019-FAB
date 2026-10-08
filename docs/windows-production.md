# Windows Production Profile

This profile is for one Windows account, one business, one local API, and one
worker. It is not a public or multi-tenant deployment. The existing local profile
remains the default; selecting Windows production does not migrate any data.

## Storage and Credentials

Before switching an existing installation, stop it with its previous configuration
and preserve a verified source-complete recovery package. Select the correct
external ledger deliberately. A new path means a new empty ledger, not a migration.
Do not copy a running SQLite database as a migration shortcut. Keep source evidence
available and use the existing maintenance restore workflow for an approved move.

Configure these values in the environment used to start FAB:

| Setting | Windows profile requirement |
| --- | --- |
| `FAB_DEPLOYMENT_PROFILE` | `windows` |
| `FAB_LOCAL_LEDGER_PATH` | Absolute file path outside the code checkout |
| `FAB_LOCAL_BACKUP_DIR` | Absolute directory outside the code checkout |
| `FAB_LOCAL_API_HOST` | Loopback only; the launcher uses `127.0.0.1` after validation |
| `FAB_LOCAL_API_PORT` | API port, default `5001`; no automatic fallback |
| `PORT` | Dashboard port, default `3000`; distinct from the API port |
| `FAB_LOCAL_API_TOKEN` | Random secret: at least 32 characters and 12 distinct characters; no placeholders |
| `FAB_LOCAL_API_TOKEN_FILE` | Alternative to the token value: absolute readable UTF-8 regular file, at most 8192 bytes, printable ASCII token with at most one optional LF/CRLF terminator; no extra whitespace |

Never set both token input forms. ConfigLoader resolves the file; startup captures
the token privately and scopes the same resolved value to preflight, API, worker,
and dashboard. File-variable aliases are removed only inside that scope to prevent
conflicting inputs, then the caller's environment is restored. Credentials are not
placed in process arguments, runtime JSON, URLs, or launcher logs.

Gmail, Drive, and Google Photos user-token files are encrypted with Windows
current-user DPAPI. Existing plaintext JSON token files are migrated atomically
when first loaded; they are not migrated merely by starting the dashboard. A
token protected by one Windows user cannot be loaded under another user. OAuth
client-credential files are separate and still require restrictive filesystem
ACLs. This local-user protection does not replace host encryption or backups.

If no API token is supplied, the launcher uses the existing encrypted local secret
store to provision/reuse one. A supplied weak production token is rejected, not
silently replaced. HAI uses a separate token; its file option is
`FAB_HAI_API_TOKEN_FILE`. The dashboard also requires its signing secret, which the
launcher provisions through the existing secret store when absent. Supply
`JWT_SECRET` or `JWT_SECRET_FILE`, never both, to use an operator-managed signing
secret instead. The file is resolved before startup; its alias is cleared only in
the dashboard's credential scope and restored afterward. Weak supplied Windows
signing secrets are blocked before any service is spawned.

Restrict ledger, evidence, backup, and credential directories to the owning account
and approved administrators. Verify host encryption and recovery-key custody before
setting `FAB_STORAGE_ENCRYPTION_CONFIRMED=true`. That setting is an operator
attestation, not proof of BitLocker or any other host control. Missing attestation
is reported as attention/degraded rather than an automatic encryption claim.

## Start and Stop

Example session configuration below uses illustrative external locations. Replace
them with the intended locations before starting; these commands do not relocate
an existing ledger.

```powershell
$env:FAB_DEPLOYMENT_PROFILE = 'windows'
$env:FAB_LOCAL_LEDGER_PATH = 'C:\FAB-Data\ledger\fab_operations.sqlite3'
$env:FAB_LOCAL_BACKUP_DIR = 'D:\FAB-Recovery\packages'
$env:FAB_LOCAL_API_HOST = '127.0.0.1'
$env:FAB_LOCAL_API_PORT = '5001'
$env:PORT = '3000'
.\Start-FAB.ps1 -NoBrowser
```

`Start-FAB.cmd -DeploymentProfile windows` is equivalent when the other settings
are already configured. `-Development` is rejected in the Windows profile.
Existing local mode retains its development option and port-search behavior.

Startup runs `src.run_deployment_preflight` with the effective runtime credentials
and the checkout's actual instance root before starting any FAB service. It blocks
invalid paths, weak credentials, invalid binding, and other production blockers.
It checks both service ports before spawning children. A port conflict must be
resolved explicitly; production never silently selects the next port.

A newly launched worker starts only after the API and dashboard identity endpoints
respond successfully. Failures during service creation, dashboard build, readiness
checks or runtime registration trigger cleanup of services created by that launch.
Already running services adopted by the launcher are not stopped by this rollback.
Cleanup attempts continue if one service cannot be stopped; the original startup
error is retained. This does not undo work already performed by an existing worker
or by a newly started worker before a later registration failure.

New API, dashboard and worker roots are created suspended inside a uniquely named
Windows Job Object using atomic job-list assignment, then resumed. Rollback terminates
that group and checks that its active-process count reaches zero, including normal
descendants whose intermediate parent already exited. Startup fails rather than
falling back to an uncontained launch. Standard input is closed through NUL;
only the stdout/stderr log handles and NUL are inherited from the launcher.

After readiness checks, the root receives a non-inheritable, zero-access job handle
to retain the group when the launching PowerShell exits. Root exit then closes the
last handle and terminates remaining group members. There is no additional resident
supervisor. A failure while committing jobs or publishing metadata still invokes
group rollback, including PowerShell cancellation. A hard launcher crash during
this multi-service handoff is not an atomic transaction and still requires runtime
reconciliation.

This is lifecycle management, not a hostile-code sandbox; externally brokered
process creation such as WMI is outside normal child inheritance. See Microsoft's
[Job Objects documentation](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).
Atomic [create-in-job assignment](https://devblogs.microsoft.com/oldnewthing/20230209-00/?p=107812)
removes the separate create/assign crash window; it requires Windows 10 or newer,
consistent with FAB's Windows 11 target.
The helper is compiled locally on first launch and supports Windows PowerShell 5.1
and PowerShell 7; hosts prohibiting Add-Type/native calls will fail startup. Helper
source changes invalidate the runtime fingerprint; an already-loaded changed helper
requires a fresh PowerShell process.

Runtime metadata records each new root's process ID, exact start-time ticks (as a
string), and random Windows job name. `Stop-FAB.ps1` reopens that group with query
and termination rights, verifies membership when the original root is present,
terminates it, and verifies zero active members. This does not depend on a
responsive dashboard or API. If the root is already gone, the recorded group can
still be stopped; a missing named group is treated as gone only when its original
root is also gone. Windows applies its object access controls to these operations.
Start and stop must use the same Windows account and session; these local-session
job identities are not a cross-session Windows service-management interface.

Adopted services are not retroactively contained. Legacy cleanup retains original
process objects and checks creation times, but discovery can miss a descendant
with a departed intermediate. Therefore legacy or incomplete cleanup cannot
authorize clearing runtime metadata or ledger leases. An unresponsive candidate,
an ownership mismatch, a process-query failure, or a lease-cleanup failure also
retains those records and reports incomplete shutdown. Copied foreign-checkout
records, unknown service entries, and mismatched process IDs cannot authorize
group termination. Do not delete recovery records merely
to suppress the warning: verify the old services and descendants have stopped
before reconciling their state. Acceptance on the intended production Windows
host remains a separate requirement; disposable-process tests do not establish
live provider or deployment readiness.

Start and stop share a checkout-scoped Windows lifecycle mutex. A competing
launcher fails before lifecycle work rather than interleaving with it. The same
PowerShell thread may reenter the mutex when startup invokes shutdown for
reconciliation. Shutdown additionally takes the worker's existing maintenance
mutex through database cleanup and removal of old runtime records. An active
worker blocks this cleanup even if its registration file is missing. A new worker
cannot start while those records are being removed.

After verified shutdown, the two local worker leases and all literal
`hai_command:` leases are released in one SQLite transaction, in pages of 100,
with a separate audit event for every release. There is no 500-command cutoff.
Failed audit insertion or an invalid owner rolls back the entire batch. This
transaction and filesystem record removal are not one atomic operation: an
interruption or filesystem error after the commit can leave old runtime files
requiring reconciliation. Do not run unmanaged APIs or another checkout against
the ledger during shutdown; launcher serialization does not control arbitrary
processes or cross-session starts.

Identity probes accept only HTTP/HTTPS loopback addresses or `localhost`, reject
URL credentials, and never follow redirects. PowerShell 5.1's expanded IPv6
representation is normalized before checking the host. Redirect blocking uses
the documented [MaximumRedirection control](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/invoke-restmethod?view=powershell-5.1#-maximumredirection).

Before announcing readiness, the launcher waits for a live worker registration
belonging to this checkout and the expected process lineage. It rejects exits,
missing/foreign registrations and late success results. The acceptance budget is
30 seconds; individual Windows process-query timeouts are capped at two seconds
and reduced to the remaining whole-second budget. This is not a hard OS wall-clock
deadline: blocking filesystem/OS calls and scheduling can still delay failure
reporting. See Microsoft's [CIM timeout semantics](https://learn.microsoft.com/en-us/powershell/module/cimcmdlets/get-ciminstance?view=powershell-7.6#-operationtimeoutsec).

Runtime metadata is published through a same-directory temporary file and atomic
replacement, so a failed write does not truncate the previous record. Rollback does
not delete the prior runtime/worker metadata. Such a retained record may be stale;
process and endpoint ownership checks, not the file's presence, determine whether
FAB is running. Readiness here checks responding endpoints and worker registration,
not provider acceptance or continuing worker progress.

The launcher records the profile and a non-secret storage-configuration hash in its
runtime metadata. Changing production storage/profile requires an explicit stop
with the previous configuration. A discovered process without compatible launcher
metadata is not adopted into the production profile.

Runtime ownership remains anchored to this checkout: `data/fab-runtime.json`,
`data/fab-worker-runtime.json`, and the existing checkout-scoped worker mutex/lock.
External ledger paths do not move that coordination root. The API and worker still
receive the same `FAB_INSTANCE_ROOT`; live maintenance restore uses that root to
exclude the worker. Never point a second checkout at the same ledger concurrently.

Other existing output/log/credential locations are not migrated by this change.
Review their configuration, access controls, encryption, and backup policy before
operational acceptance. The launcher is interactive startup support, not a newly
installed Windows service or scheduled task.

### Managed HTTPS Access

Ordinary loopback-only Windows access is unchanged. If enabling managed HTTPS
sign-in (including an owned ngrok/proxy endpoint), also set
`FAB_OPERATOR_SESSION_VALIDATION_URL` in the Python/API environment to the actual
dashboard listener, for example
`http://127.0.0.1:3000/api/fab/operator-session/status`. Use the configured `PORT`,
not an assumed fallback port. The INI equivalent is
`[operations] operator_session_validation_url`. The URL is not a credential and
must not include user information, query parameters or a fragment.

Managed configuration requires a strong shared API token even under the `local`
profile; preflight blocks missing credentials or a missing/invalid validation URL
before creating the ledger. Configure the separate operator access secret, JWT
secret, exact HTTPS public origin and trusted proxy peers on Node. The validation
URL alone does not enable managed sign-in on Node. The launcher does not provision
an ngrok tunnel, public domain or TLS configuration automatically.

Set `FAB_LOCAL_API_PUBLIC_URL` to the HTTPS ledger origin reachable by the remote
browser. The launcher preserves this configured address while keeping
`FAB_LOCAL_API_URL` pointed at the actual loopback API. Without a public address,
the browser ledger address defaults to that loopback URL. Neither setting opens
a port or provisions a tunnel; the managed ngrok scripts expose only the API,
not the React dashboard. See [ngrok setup](local_windows_ngrok_setup.md) for the
existing-credential and endpoint ownership requirements.

Dashboard logout, expiry and restart revoke subsequent linked ledger requests.
If the dashboard authority cannot be reached, linked browser access is denied;
restore connectivity and sign in again. Independent bearer/HAI access does not
use that browser session. Explicit master-token ledger logins are independent
and require their own logout. Already running actions are not cancelled by logout.

To stop the owned runtime, use `.\Stop-FAB.cmd` from this checkout with the same
configuration. It resolves either the configured API token/file or the encrypted
stored credential without creating a replacement secret.

## Disposable Recovery Rehearsal

Run from the repository using its existing environment:

```powershell
.\.venv\Scripts\python.exe -m src.run_recovery_rehearsal
```

The CLI accepts no ledger, backup, destination, or configuration path. It never
loads the operator configuration. Each run creates a fresh temporary workspace
and a small synthetic document, then uses `LocalOperationsLedger` and the existing
`LocalBackupService` to:

1. Create and deeply inspect a source-complete recovery package.
2. Verify archived ledger byte count and SHA-256 against the manifest.
3. Change the disposable record and remove its original source bytes.
4. Plan and perform full ledger-and-source restore in maintenance mode, with the
   exact confirmation phrase and the synthetic workspace's worker ownership lock.
5. Verify the automatic pre-restore safety backup, restored record, source bytes,
   checksum, confined restored path, SQLite integrity, and restore audit event.
6. Remove the disposable workspace before reporting success.

The CLI returns a small JSON evidence object with status, format, checksums, source
coverage counts, and boolean checks. It contains no real ledger data, credentials,
or temporary paths. Exit `0` means all checks passed; exit `1` reports bounded
failure evidence. Unsupported arguments are rejected. No network/provider action
is performed and no real service is stopped or restarted.

This proves the current code's synthetic source-complete recovery path. It does
not prove that a real business backup is complete, an off-device backup is
accessible, Windows disk encryption is enabled, or an operator can meet a recovery
time objective. Those remain separate acceptance checks. Provider, accounting,
privacy, and legal acceptance remain separate as well.

## Focused Verification

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_windows_launcher.py tests/test_recovery_rehearsal.py tests/test_local_backup.py -q -p no:cacheprovider
```

Launcher tests execute only isolated configuration/preflight and credential-scope
blocks with synthetic settings, never the full launcher. The `no:cacheprovider`
option avoids shared pytest-cache writes during concurrent verification.
