# API Usage Audit

## Local API

The Flask API is the source of operational truth. Major endpoint groups are:

| Group | Representative endpoints | Mutation policy |
| --- | --- | --- |
| Liveness/health/readiness | `/api/live`, `/api/health`, `/api/settings`, `/api/doctor` | `/api/live` is constant-time; deeper reports are read-only and secret-redacted. Health detail is prioritized and bounded while exact totals remain available. |
| Intake/documents | `/api/intake/upload`, `/api/intake/rescan`, `/api/documents/*` | Local evidence writes only. |
| Reviews/categories | `/api/review`, `/api/review/<id>/resolve`, `/api/categories/*` | Operator decisions are audited. |
| Autonomy/workflows | `/api/autonomy/plan`, `/api/autonomy/run`, `/api/workflows/*` | Lease, safety, recovery, and emergency-stop gated. |
| Emergency control | `/api/autonomy/emergency-stop` | Any operator/HAI may stop; only operator DELETE with exact phrase may resume. |
| Routing/exports | `/api/routing/*`, `/api/exports/*` | Preparation, approval, execution, and verification remain distinct states. |
| Wave | `/api/wave/*`, `/api/drive-wave/*` | Capability and business mapping gated; attachment verification required before archival. Work-order lists accept `view=summary` for queue rendering while the default and per-document endpoints retain complete executor/evidence payloads. |
| Reconciliation/reporting | `/api/reconciliation/*`, `/api/report-runs/*`, `/api/compliance/*`, `/api/master-ledger` | Local computation; provisional findings are labeled. `summaryOnly=true` returns the exact checksum and aggregate projection without serializing rows to the caller. |
| Recovery/support | `/api/backups`, `/api/backups/restore-plan`, `/api/backups/restore`, `/api/support-bundles` | Restore is local-maintenance-only, exact-confirmation-gated, and worker-lock protected; support output is sanitized. |
| HAI | `/api/hai/status`, `/api/hai/manifest`, `/api/hai/commands/execute` | Fixed command allowlist, normalized payloads, no external approval, emergency-stop clear, or restore. All commands are disabled during maintenance; redacted recovery status remains readable. |
| Managed cloud access | `/api/cloud/status` | Read-only, secret-safe status. Reports active only when project runtime identity and the exact owned ngrok tunnel remain verifiable; maintenance always reports disabled. |

## Web gateway

The Express/tRPC gateway calls fixed local paths from `web/server/fabLocalGateway.ts`. The browser supplies a typed command ID, never an arbitrary URL. The gateway:

- validates endpoint scheme/origin;
- adds the local API bearer token server-side;
- applies timeouts, a four-read concurrency bound, and a bounded projection before returning data;
- caches only read resources for degraded/stale display;
- coalesces identical control-center reads for two seconds and invalidates that snapshot after every successful mutation;
- compresses responses larger than 1 KiB for browser/ngrok clients;
- does not convert an API error into success;
- uses role-gated `fabOperatorProcedure` for financial operations.

Operator-facing links into the protected Flask ledger use `GET /api/fab/operator-session?next=...` instead of exposing a direct token-gated URL. The gateway authorizes a loopback operator or authenticated administrator, accepts only a bounded relative destination, and creates a 45-second HMAC ticket using the server-only API token. `/operator/session/bootstrap` independently verifies audience, version, signature, issue/expiry bounds, actor, nonce, and destination; consumes the nonce once; rotates the Flask session; writes redacted audit evidence; and redirects to the requested ledger page or read-only artifact. The bearer token is never placed in browser code or URLs. Non-loopback public API origins must be clean HTTPS origins.

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
reconciliation, and HAI. Upload budgets include bounded encoding/metadata
overhead and retain the existing decoded-byte checks. Every route limit is
reduced by a smaller configured global request limit, so the 101 MiB global
ceiling is no longer the default memory budget for ordinary commands.

## Error contract

Every JSON error below `/api/` has the same transport envelope: `success=false`, `status`, `errorCode`, `message`, and `requestId`. Route-specific fields such as `error`, validation details, or provider state are preserved. FAB accepts a caller-provided `X-Request-ID` only when it is a bounded safe identifier; otherwise it creates one and always returns the effective value in the response header and body. Unexpected exceptions return a generic message and create a sanitized correlated ledger audit event plus local log entry without exposing financial or provider details.

`GET /api/health` computes the complete issue set before deriving status, severity counts, metrics, type counts, and next actions. It returns the highest-priority issue-detail window only: 50 by default and 1-500 through `issueLimit`. `issueCount`, `issueTypeCounts`, `issuesReturned`, and `issuesTruncated` distinguish complete aggregate evidence from the bounded detail array. Identical read-only projections are coalesced in an eight-entry, two-second server cache; `X-FAB-Health-Cache` reports `hit`, `miss`, or `disabled`, while browser/proxy caching remains `no-store`. Internal autonomy, notifications, close readiness, exception materialization, and every external-execution gate continue to read the ledger directly and never use this cache.
