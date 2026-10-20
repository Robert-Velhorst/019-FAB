# Approved Posting Execution

FAB now separates posting into three stages:

1. Dry-run creation.
2. Human approval or rejection.
3. Execution of approved posting attempts.

## Safety Defaults

Approved posting execution is disabled unless the local config explicitly enables it.

The retired checkpoint controller can no longer submit or prepare postings.
Supported external execution is owned by the operations-ledger export path and
remains approval, backup, idempotency, and provider-readback gated.

Add these keys to the `[operations]` section in `config/config.ini` when the operator is ready:

```ini
fab_autonomy_execute_approved_exports = false
worker_process_approved_postings = true
worker_process_legacy_postings = false
```

Meaning:

- `fab_autonomy_execute_approved_exports = false` means approved operations-ledger export attempts are not executed automatically.
- `worker_process_approved_postings = true` means the worker checks the authoritative `export_attempts` queue, but execution still respects `fab_autonomy_execute_approved_exports`.
- `worker_process_legacy_postings = false` prevents the worker from polling deprecated `posting_attempts` in parallel.

To enable real execution after careful testing:

```ini
fab_autonomy_execute_approved_exports = true
```

## Manual Runner

Approved posting execution can be run manually with:

```powershell
python src\run_approved_postings.py
```

This runner prepares ready routes and processes approved attempts from
`LocalOperationsLedger`. It respects the same config safety flag and creates a
pre-execution ledger backup. It only falls back to legacy `posting_attempts`
when the operations ledger is not configured.

## MijnGeldzaken supervised submission

MijnGeldzaken does not use the Wave API execution path. Once an approved
operations-ledger export attempt reaches execution, FAB writes a durable CSV package to
`mijngeldzaken_export_dir`, records its SHA-256 checksum, changes the attempt to
`supervision_required`, and opens a review item. No stored MijnGeldzaken
username, password, or DigiD credential is used.

After the operator imports the package in a user-owned MijnGeldzaken session,
record completion through the token-protected dashboard API:

```text
POST /api/export-attempts/{export_attempt_id}/result
```

The attempt must already be `supervision_required`, and the JSON body must
include `confirmation` with the exact value `RECORD FAB EXPORT RESULT`, a
`status` (`executed`, `submitted`, or `failed`), and
`result.artifactSha256` exactly matching the SHA-256 of the persisted artifact.
FAB stores only a fixed operator-attestation source label and that checksum as
evidence; an optional `externalId` is limited to 200 printable characters.
The dashboard supplies the persisted artifact checksum with its supervised
result form. FAB then updates the master ledger, resolves the supervision
review item for a successful submission, and writes an audit event. This
endpoint records an operator attestation; it does not independently verify
the external import, log in to MijnGeldzaken, or bypass DigiD. Wave and other
provider results cannot be recorded through this manual endpoint.

The older `/posting-attempts/{attempt_id}/complete-supervised` endpoint remains
available only for compatibility with legacy databases. New operations use the
export-attempt endpoint above.

## Worker Execution

After each workflow cycle, the worker prepares export attempts from ready
routes and checks approved operations-ledger exports. Execution remains disabled
until `fab_autonomy_execute_approved_exports` is enabled. A backup is created
before any approved batch is processed; a failed backup blocks the entire batch.

## Verified Wave API execution

The authoritative export service now attaches `WaveappsApiExecutor` to the
existing Wave action planner. An approved export is only marked `executed` when
the configured Wave handler returns a successful `moneyTransactionCreate`
result with a Wave transaction ID. FAB passes the durable export operation ID
into Wave's `externalId` field so retries use the same idempotency identity.

Current verified API coverage is `transaction_add` for expenses. Modeled Wave
actions such as bill, invoice, bank-feed, user-management, and destructive
operations are not falsely reported as queued: they change to
`attention_required`, remain `externalSubmission=not_executed`, and create a
review item until a verified executor is implemented.

Every route must identify `waveapps_business` or `waveapps_personal`. A generic
`waveapps` target is blocked for review unless `[waveapps] default_target` was
deliberately configured. This prevents a personal expense from silently being
posted into the business ledger, or vice versa.

For local Windows operation, use **Connections > Wave - Noodzakelijk Online**
in the operator dashboard. The setup flow stores the token in FAB's encrypted
local secret store, validates the business with a read-only chart-of-accounts
request, and accepts only account IDs returned by that validation. The
encryption key is protected with Windows DPAPI for the current user. Tokens are
never returned in setup status or written to the ledger and audit log.

Environment variables are still available for managed deployments and take
precedence over the local store:

```powershell
$env:FAB_WAVEAPPS_BUSINESS_ACCESS_TOKEN = "..."
$env:FAB_WAVEAPPS_BUSINESS_ID = "..."
$env:FAB_WAVEAPPS_PERSONAL_ACCESS_TOKEN = "..."
$env:FAB_WAVEAPPS_PERSONAL_ID = "..."
```

Anchor and category account IDs are non-secret routing configuration and must
still be verified through account discovery. `GET /api/wave/setup`,
`PUT /api/wave/setup`, and `POST /api/wave/setup/validate` back the loopback
dashboard workflow without returning the access token. Missing credentials, ambiguous
targets, missing account mappings, and unsupported actions create review work;
they never count as external submission.

## Audit Trail

Each execution attempt is written to the audit log:

- execution started
- posted
- quota-deferred with next retry time
- attention required with linked review item
- posting failed
- manual review item created when posting fails

## Current Limitation

Wave expense execution uses the `moneyTransactionCreate` API contract already
covered by FAB's handler tests.
Before FAB can submit, each Wave target needs a verified anchor account ID and
a category-to-account-ID mapping. FAB refuses to send a request when any of
those are absent and sends the item to review instead. The configuration
template shows the required fields under `[waveapps_business]` and
`[waveapps_personal]`; keep OAuth tokens out of `config.ini`.

Use the dashboard's **Validate Wave** action or `POST /api/wave/accounts/discover`
to read the current chart of accounts. FAB records the read as an audited Wave
operation snapshot and shows whether each configured anchor/category ID still
exists before an approved export is dispatched.

Use **Sync Wave records** or `POST /api/wave/entities/sync` to refresh the
read-only customer, product/service, and invoice mirror. Future invoice and
customer mutations must resolve their external IDs from this durable mirror;
records marked `missing_downstream` must be reviewed instead of reused.

MijnGeldzaken execution remains supervised. FAB prepares the artifact and
review state locally; the user-owned session performs the external import.
