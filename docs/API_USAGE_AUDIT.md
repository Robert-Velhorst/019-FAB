# API Usage Audit

## Local API

The Flask API is the source of operational truth. Major endpoint groups are:

| Group | Representative endpoints | Mutation policy |
| --- | --- | --- |
| Liveness/health/readiness | `/api/live`, `/api/health`, `/api/settings`, `/api/doctor` | `/api/live` is constant-time; deeper reports are read-only and secret-redacted. Health detail is prioritized and bounded while exact totals remain available. |
| Intake/documents | `/api/intake/upload`, `/api/intake/rescan`, `/api/documents/*` | Local evidence writes only. |
| Bank transactions | `/api/bank-transactions/import`, `/api/wave/report-results` | Each transaction import, import status and audit event commit atomically; exact identities are no-op duplicates, while changed facts under an existing identity are preserved and surfaced as `needs_review`. |
| Reviews/categories | `/api/review`, `/api/review/<id>/resolve`, `/api/categories/*` | Operator decisions are audited. |
| Autonomy/workflows | `/api/autonomy/plan`, `/api/autonomy/run`, `/api/workflows/*` | Lease, safety, recovery, and emergency-stop gated. |
| Emergency control | `/api/autonomy/emergency-stop` | Any operator/HAI may stop; only operator DELETE with exact phrase may resume. |
| Routing/exports | `/api/routing/*`, `/api/exports/*` | Preparation, approval, execution, and verification remain distinct states. |
| Wave | `/api/wave/*`, `/api/drive-wave/*` | Capability and business mapping gated; attachment verification required before archival. Work-order lists accept `view=summary` for queue rendering while the default and per-document endpoints retain complete executor/evidence payloads. |
| Reconciliation/reporting | `/api/reconciliation/*`, `/api/report-runs/*`, `/api/compliance/*`, `/api/master-ledger` | Local computation; provisional findings are labeled. `summaryOnly=true` returns the exact checksum and aggregate projection without serializing rows to the caller. |
| Recovery/support | `/api/backups`, `/api/backups/restore-plan`, `/api/backups/restore`, `/api/support-bundles` | Restore is local-maintenance-only, exact-confirmation-gated, and worker-lock protected; support output is sanitized. |
| HAI | `/api/hai/status`, `/api/hai/manifest`, `/api/hai/commands/execute` | Fixed command allowlist, normalized payloads, no external approval, emergency-stop clear, or restore. All commands are disabled during maintenance; redacted recovery status remains readable. |

## HAI command results

Executor results must be finite JSON objects within the limits advertised as
`resultLimits` in the HAI manifest: 1 MiB encoded JSON, depth 32, and 10,000
values. Optional `success` fields must be booleans; optional `status` fields
must be non-empty strings. Results without either field remain valid for local
services that return aggregate counts.

Malformed or oversized results produce a durable `invalid_executor_result`
failure rather than a completion event. Reusing the same request ID returns
the stored failure without invoking the executor again. An operation that could
have changed an external provider retains an `unknown` effect until verified.
Input validation is performed once per dispatch; policy and executor readiness
are still checked before execution, and the executor receives a separate copy
of the normalized payload.

## Web gateway

The Express/tRPC gateway calls fixed local paths from `web/server/fabLocalGateway.ts`. The browser supplies a typed command ID, never an arbitrary URL. The gateway:

- validates endpoint scheme/origin;
- adds the local API bearer token server-side;
- applies timeouts, an eight-read concurrency bound, and a bounded projection before returning data;
- caches only read resources for degraded/stale display;
- coalesces concurrent control-center reads, caches verified snapshots for 65 seconds, and invalidates snapshots before and after mutations, including failed readbacks;
- compresses responses larger than 1 KiB for browser clients;
- does not convert an API error into success;
- uses role-gated `fabOperatorProcedure` for financial operations.

Operator-facing links into the protected Flask ledger use `GET /api/fab/operator-session?next=...` instead of exposing a direct token-gated URL. The gateway authorizes a loopback operator or authenticated administrator, accepts only a bounded relative destination, and creates a 45-second HMAC ticket using the server-only API token. `/operator/session/bootstrap` independently verifies audience, version, signature, issue/expiry bounds, actor, nonce, and destination; consumes the nonce once; rotates the Flask session; writes redacted audit evidence; and redirects to the requested ledger page or read-only artifact. The bearer token is never placed in browser code or URLs. Non-loopback public API origins must be clean HTTPS origins.

## Worker telemetry client

`src/operations/operations_client.py` posts workflow/document telemetry to the
configured dashboard service. Requests do not follow redirects, so a 307/308
cannot forward the financial payload to a different destination. Redirects are
reported as failures, not successful writes. Responses are streamed in 64 KiB
chunks with a 1 MiB decoded-body limit, and closed on success and failure.
Connect/read timeouts default to five seconds for invalid or non-finite values
and are capped at 120 seconds. This is a per-connect/read timeout, not an overall
wall-clock deadline. Existing best-effort local-ledger persistence remains active
when the remote service fails. No automatic network retry is introduced.

Nonempty success responses must be JSON objects containing only finite numbers;
an optional `success` flag must be boolean and cannot be false. Returned IDs
must be positive signed 64-bit integers (or their ASCII decimal strings), not
booleans or coerced floating-point values. Malformed responses are failures,
allowing the existing local-ledger fallback without reusing an invalid ID.
When the local ledger is configured, it is persisted first and its IDs remain
authoritative. Successful remote creations establish destination-scoped links
in the schema-v3 `remote_identity_links` table. Update URLs and document,
workflow, routing, duplicate-document, and audit entity references are translated
through verified links, never through numeric equality. Audit detail references
are explicitly retained as scoped local evidence rather than remote IDs.

Missing links defer remote writes while preserving local records. Conflicting
links are quarantined and cannot trigger further creates or updates; failures
and deferrals are recorded in the local audit trail. Non-document upserts with
an existing verified link are not submitted as additional remote creates.
Document registration can refresh its idempotent source identity without
discarding an intake checksum omitted by telemetry.

Destination scope includes the configured URL, endpoint paths, and optional
`operations.identity_scope` generation label. When replacing/resetting a remote
database at the same address, operators must change that label before enabling
mirroring; the client cannot automatically detect an unrelated database behind
an unchanged URL. Historical unmapped rows, unknown write outcomes, unsupported
remote resources, and non-idempotent create replay still require readback or
an explicit reconciliation plan. This is not a historical backfill or an
exactly-once delivery protocol.

## Provider APIs

- Google connectors use owner OAuth and scoped read operations against configured sources.
- Wave GraphQL coverage is capability-based. Receipt attachment work can require a supervised executor when the API cannot provide the necessary action/readback.
- MijnGeldzaken is an artifact export with supervised completion tracking.
- Direct PSD2 and SVB mutation APIs are absent and are not advertised as live.

## Request body limits

The API applies a 2 MiB default request-stream limit to mutating API requests,
after authentication and before JSON or multipart parsing. Larger legitimate
contracts have explicit limits: document intake, bank imports, OAuth credentials,
Wave attachment readback, Drive relay, autonomy bank-transaction input,
Wave report-result capture, reconciliation, and HAI. Wave report results allow
up to 16 MiB of source data and 10,000 rows; optional reconciliation remains
limited to its existing 500-row batch size and rejects oversized combined
imports before writing transactions. Upload budgets include bounded encoding/metadata
overhead and retain the existing decoded-byte checks. Every route limit is
reduced by a smaller configured global request limit, so the 101 MiB global
ceiling is no longer the default memory budget for ordinary commands.

## Error contract

Every JSON error below `/api/` has the same transport envelope: `success=false`, `status`, `errorCode`, `message`, and `requestId`. Route-specific fields such as `error`, validation details, or provider state are preserved. FAB accepts a caller-provided `X-Request-ID` only when it is a bounded safe identifier; otherwise it creates one and always returns the effective value in the response header and body. Unexpected exceptions return a generic message and create a sanitized correlated ledger audit event plus local log entry without exposing financial or provider details.

`GET /api/health` computes the complete issue set before deriving status, severity counts, metrics, type counts, and next actions. It returns the highest-priority issue-detail window only: 50 by default and 1-500 through `issueLimit`. `issueCount`, `issueTypeCounts`, `issuesReturned`, and `issuesTruncated` distinguish complete aggregate evidence from the bounded detail array. Identical read-only projections are coalesced in an eight-entry, two-second server cache; `X-FAB-Health-Cache` reports `hit`, `miss`, or `disabled`, while browser/proxy caching remains `no-store`. Internal autonomy, notifications, close readiness, exception materialization, and every external-execution gate continue to read the ledger directly and never use this cache.

## Complete bank dashboard aggregates

`GET /api/bank-transactions?view=summary` computes counts and gross unreconciled
amounts by currency over all matching bank transactions, not a limited detail
page. It applies the same account/status/reconciliation filters as the detail
route and excludes approved, reconciled, and ignored transactions from exposure.
Amounts are absolute values (incoming and outgoing do not cancel), rounded to
two decimal places for this dashboard metric. The summary omits transaction
details and returns only the five most recent statement imports.

Both the batched control-center resource and individual gateway read use this
summary. Missing amounts or non-finite aggregate totals mark it incomplete;
the gateway then displays the amount as unavailable. It also rejects missing,
incomplete, malformed, or unsupported-currency aggregates instead of reconstructing
an apparently complete amount from a legacy server's latest 250 detail rows.
The default detail endpoint remains available with its existing bounded list.

## Verified source previews

The advanced document review page and `/api/documents/<id>/source` use the same
bounded, descriptor-based source reader. Both require the recorded SHA-256 to
match the retained file before exposing any of its bytes. Missing hashes,
changed or unavailable files, non-regular descriptors, and files over the
25 MiB preview limit fail closed with safe diagnostics.

Text review excerpts verify the whole file, including content beyond the
displayed excerpt, while retaining only a bounded UTF-8 prefix. Download
responses serve the already-verified chunks with an explicit content length;
they do not reopen the path or join the full file into a second buffer. The
dashboard proxy still independently checks the hash and allowed content type
before serving a source preview to the browser. Neither path repairs or
rehashes changed evidence automatically.
