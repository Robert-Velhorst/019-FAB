# Deployment and Operations Errors

Run `python -m src.run_deployment_preflight` for configuration checks without
opening the ledger. `python -m src.run_fab_doctor --json` adds operational and
provider diagnostics. A failing check does not authorise deleting evidence.

| Code or condition | Meaning and next action |
| --- | --- |
| `deployment_profile` | Choose `local`, `windows`, or `vm`; unknown names fail startup. |
| `deployment_api_secret` | Replace missing/placeholder configuration with a random token of at least 32 characters. Managed session validation requires this even in the `local` profile. |
| `deployment_parent_session` | Set `FAB_OPERATOR_SESSION_VALIDATION_URL` to the actual Node listener's `/api/fab/operator-session/status` URL. VM Compose supplies its internal URL. Do not bypass this check to restore anonymous access. |
| `deployment_hai_secret` | HAI needs a separate random credential; never reuse the operator API token. |
| `deployment_bind` | Windows requires loopback; VM permits internal container binding. Check the explicit profile. |
| `deployment_port` | Use one integer port from 1 through 65535. Production does not silently change ports. |
| `deployment_https` | Set the HTTPS operator dashboard URL for the VM deployment. |
| `deployment_ledger_https` | Set the ledger HTTPS origin so handoff sessions use Secure cookies. |
| `deployment_ledger_path`, `deployment_backup_path` | Use absolute locations without parent traversal. |
| `deployment_ledger_location`, `deployment_backup_location` | Windows production data must live outside the source checkout. Migrate through verified backup/restore. |
| `deployment_ledger_writable`, `deployment_backup_writable` | Check permissions for the service account and its mounted volume. |
| `deployment_backup_directory` | The configured backup target is a file; choose a directory. |
| `deployment_ledger_disk`, `deployment_backup_disk` | Less than 256 MiB is free. Restore sufficient capacity without deleting unverified documents. |
| `deployment_backups_disabled`, `deployment_backup_evidence` | Enable scheduled backups with complete source evidence. |
| `deployment_ledger_disabled` | Enable the authoritative operations ledger; scheduled recovery requires it. |
| `deployment_storage_encryption` | Check host disk/volume encryption and record the operator attestation. This is a readiness warning. |
| Secret-file error | Use either a value or an absolute readable `_FILE`, never both; files must contain a bounded UTF-8 secret. |
| ngrok cannot load existing API/HAI credentials | Use the same configuration/environment as the running FAB instance. Both scripts read configured secrets/files first and otherwise reuse existing encrypted credentials; they never provision replacements. Resolve missing, weak or identical credentials through the normal startup configuration before retrying. |
| Remote ledger link opens localhost | Configure `FAB_LOCAL_API_PUBLIC_URL` with the owned HTTPS ledger origin before starting the dashboard. The internal API stays on loopback. A public address alone does not start a tunnel or expose the dashboard. |
| `EADDRINUSE` | The selected web port is occupied. Check ownership, then choose an explicit free port or stop the owned service. |
| Windows startup/build/registration failed | The launcher attempts to stop only services created by the failed launch, preserves existing runtime metadata and reports the original failure. Correct the reported cause before retrying. Retained metadata alone does not prove a process is alive. |
| Could not stop a newly started service | Cleanup continues for the other services. Inspect this checkout with `Stop-FAB.cmd`; do not delete worker locks or stop unrelated processes. Resolve ownership and the original startup error before restarting. |
| Could not verify complete cleanup of a newly started process tree | Legacy discovery, creation-time verification or termination failed. Inspect owned processes before restarting. Uncontained legacy descendants may escape discovery after an intermediate exits. New service roots instead use native Job Object cleanup. |
| Failed to create/configure/contain/resume a service job | Native launch failed closed. Check executable availability, writable log paths and host policy permitting Add-Type and Windows Job Objects; do not bypass containment. No uncontained fallback is used. |
| Could not verify complete service job cleanup | Termination was requested but active job membership did not reach zero within five seconds. Inspect the service and host before retrying; this is not successful cleanup. |
| Containment code changed | A different helper version is loaded in the current PowerShell. Use a fresh PowerShell process instead of reusing the stale native type. |
| Worker exited/did not confirm runtime ownership | Check the local worker error log and the checkout's runtime ownership. Startup requires a live matching registration before announcing readiness; another process's registration is not adopted. Resolve missing dependencies, invalid configuration or worker-lock conflicts without deleting locks to force overlap. |
| Sign-in rejected | Verify the operator secret and exact HTTPS origin; forwarding headers must come from an explicitly trusted proxy. |
| Linked ledger returns `Unauthorized` after dashboard access | Sign in again after logout, expiry, web restart or key rotation. If it persists, check the configured private validation URL, matching API secrets and clocks on both services. Unavailable, slow, redirected or malformed authority responses deny access; do not disable authentication. |
| OAuth login is not configured | Optional full-server SaaS login requires server-side portal/app/API settings and an exact `OAUTH_REDIRECT_URI`; see `web/.env.example`. Standalone/managed login does not use this flow. |
| Invalid or expired login | Restart sign-in from FAB. An OAuth challenge is tied to the browser, expires in ten minutes and cannot be replayed; a server restart also invalidates it. |
| `invalid_grant` | Reauthorise the affected Google connector. Repeated processing retries cannot repair withdrawn consent. |
| Export needs supervision | Resolve the recorded ambiguous provider action by readback before retrying. |
| Review or normalized-record request failed/timed out | Review decisions and normalized-record creation/resolution use local transactions, so an exception inside those changes rolls them back; the API may record a separate redacted error event. A client timeout can still occur after a commit or while the server continues. Refresh and inspect the document, review and correction history before trying again; do not automatically replay the mutation. |
| Reconciliation review remains pending after a match decision | A document-linked review closes only when its recorded match ID agrees with the decision. Missing, malformed or different match IDs remain pending. `needs_review` is not approval and preserves or reopens the review gate. Inspect the specific match and source evidence; do not bulk-close unrelated reviews. |
| A bank match cannot be confirmed without a linked document | Missing-receipt-only records may be resolved, rejected or ignored as an operator disposition, but cannot be marked `approved`/`reconciled` without document evidence. This does not authorize source archival or provider posting. Preserve the original evidence and repair the linkage through the supported review workflow. |
| Reconciliation evidence changed or is duplicated | The bank request is stale, its stored-row references are malformed/conflicting/duplicated, a selected document/bank fact changed, or the default candidate pool changed before persistence. No result changes are committed from that run. Refresh the source state and inspect the new facts before starting a fresh run; do not replay financial mutations blindly. |
| Reconciliation evidence changed or is incomplete before approval | Final confirmation returns `stale_evidence` / HTTP 409 if source/financial document facts or stored bank facts changed, the candidate lacks approval hashes, references are invalid, or another decision owns the final document/bank. An identical-status retry of its own completed imported-bank match succeeds only when retained approval hashes, current facts and exact final owner links still agree; it never overwrites the original confirmation. Review edits and learning roll back on a rejected nested decision. Inspect changed facts and refresh candidates before confirming. Legacy candidates are preserved. This is not source-byte or provider attachment verification. |
| Completed reconciliation has an orphaned open review task | The existing review action can repair an exactly linked completed candidate or a missing-receipt task with a verified `supersededBy` pointer. Repairs preserve confirmed financial history and original snapshots, are atomic and audited, and refuse corrections. Missing hashes, conflicting references, changed evidence or an unverified final owner remain blocked; investigate these rather than clearing the task blindly. Closed reviews are not reopened by a retry, and no blanket legacy cleanup is performed. |
| Reconciliation evidence changed or another match owns the completed decision | Exception disposition returns HTTP 409 when current bank facts disagree with the reviewed snapshot or a different match owns the bank/document decision. Refresh matching for changed open missing-receipt evidence; the same review is updated rather than discarded. Reopening your own ignored/confirmed decision remains supported, but an older exception cannot take over a newer candidate. No source archival is authorized by a local disposition. |
| Missing-receipt exception after a receipt arrives | An unconfirmed candidate intentionally leaves the exception open. Confirmation against the same imported bank row resolves open missing-receipt exceptions and records their confirming match/document link without deleting original snapshots. Other accounts, closed human decisions and malformed/conflicting review links remain untouched. Ad-hoc bank data has no independent stored-row identity proof and is not auto-superseded. Local confirmation does not authorize source archival. |
| `blocked_processing`, `blocked_duplicate`, `blocked_by_review` during export approval/execution | The current source is failed, duplicate or marked for review; an older draft does not override this. The export is removed from automatic execution and requires attention. Preserve the source and draft, resolve the source issue, compare the draft against the corrected evidence and regenerate it if necessary, then explicitly approve again. |
| Attachment/archive blocked | Verify the actual provider attachment and checksum. Keep the Drive source until every archive gate passes. |
| Backup incomplete | Restore access to every referenced source file before treating the package as complete. |
| Backup manifest exceeds size/depth limits or contains invalid/non-finite JSON | Preserve the package. Inspection allows at most an 8 MiB manifest and 32 container levels. Inspect the generating system and recover through a controlled migration; do not delete the archive or turn off validation. |
| Backup exceeds maximum uncompressed size / oversized source-evidence file | Archive metadata declares more than the supported 20 GiB total or 250 MiB per evidence file. This is rejected before member decompression. Preserve the original package and arrange a supported recovery path. |
| Backup manifest changed after inspection | The archive changed between inspection and restore preparation. No ledger replacement is authorized from that result. Preserve the package and automatic safety backup, establish a stable verified source, and repeat the review. |
| Worker already running | Inspect the owned worker before starting another. Do not remove leases to force overlap. |
| Direct reconciliation input rejected | Supply a JSON object with a transaction list; explicit null/false is invalid rather than an empty batch. Every row needs a finite amount and parseable date. The captured bank payload permits at most 500 rows, 4 MiB encoded UTF-8 JSON, 32 levels and 50,000 values. Invalid or oversized batches are rejected atomically, not truncated. Use bank import and bounded batches for larger workloads, preserving original files. |
| Invalid numeric amount, VAT or confidence | Bank import skips invalid rows and reports the skip count; valid rows continue. Receipt totals and present malformed VAT block validation rather than passing as absent. Confidence values and configured thresholds must be finite and in range. Preserve source evidence and correct or review the original value; do not substitute an alternate field to bypass the gate. Ledger numeric writes do not retain non-finite/boolean values as financial numbers. |
| Bank account aliases conflict or evidence is duplicated within the account | Direct reconciliation accepts string account identifiers using `account_identifier` or `accountIdentifier`; supplied aliases must agree. The same account/reference cannot appear twice in one batch, even with different amounts. Supply an unambiguous bounded batch or use bank import for persistent row identities. Different accounts retain separate records. Missing account scope remains separate from named accounts; FAB does not infer or rewrite legacy ownership. Conflicting stored aliases require review of the original evidence, not deletion. |
| Bank transaction reference aliases conflict or an explicit reference is malformed | Direct reconciliation requires each supplied `id`/`transaction_id` to be a non-empty string or integer, excluding booleans. Aliases must agree after integer-to-string conversion; exact string references are not trimmed. Null, empty/whitespace-only strings, floats and container values are rejected before matching rather than falling back to an inferred ID. Conflicting retained references cannot be approved merely because their hash matches. Preserve original evidence and investigate the source mapping before retrying; these guards do not provide automatic legacy repair. Anonymous requests with neither alias retain the existing inferred-reference behavior and its ambiguity limits. |
| Stored bank reference evidence is ambiguous or disagrees with the selected bank record | Any supplied persisted bank-record alias must parse to the same positive integer; null is not equivalent to an absent alias. A selected historical record with malformed/conflicting references is not overwritten or silently replaced. A numeric SQL prefilter is not proof of identity: disagreement with strict validation also stops the operation. Inspect the original source and linkage, preserving the ledger and receipts; automatic legacy repair is not provided. Consistent zero-padded, signed and whitespace-wrapped positive references remain supported. |
| PDF rendering timed out / Document OCR deadline exceeded | The document remains available for review; partial OCR is not accepted. Inspect the source and Poppler installation. The Tesseract budget is at most 300 seconds per document, with bounded individual native calls. |

### Explicit Document Selection

`documentIds: []` now means no documents, not automatic discovery. Omitted/null
selection retains discovery. Explicit selections accept at most 500 submitted
references, counting duplicates, and each reference must be a positive SQLite-
range integer or integer string; booleans, fractions and unordered/container
selectors are rejected with HTTP 400 before document lookups. Duplicate IDs are
captured once in first-occurrence order. A missing selected document rejects the
entire run, including references after the candidate-count limit; refresh the
selection rather than substituting missing-receipt results. This does not repair
legacy evidence. Preserve source files and inspect candidates before confirmation.

### Document Eligibility

Explicit selection cannot bypass the reconciliation processing-status gate.
Imported, review-blocked, failed, duplicate and unknown-status documents, or
documents linked to a duplicate source, reject the run with HTTP 400 before any
match/review writes. This includes selected references beyond the candidate
limit. Resolve the underlying processing/review state, preserving the original
evidence. Automatic discovery filters linked duplicates before its candidate
limit; completed/ignored documents remain excluded, not reopened by matching.

### Request Body Limits And Decoding

Reconciliation POST requests exceeding 5 MiB (or a stricter configured global
limit) return HTTP 413 before JSON/form decoding. Preserve the source files and
use bounded batches or the bank-file import route, not blind retries. The
existing 4 MiB canonical transaction-payload limit still applies separately.
Malformed or excessively nested JSON mutation bodies return HTTP 400 without
financial writes; deep reconciliation form JSON returns the existing error
redirect. Authentication/origin rejection still takes precedence. Bank-file
uploads retain their separate allowance; upstream server/proxy buffering and
existing form-field limits are not changed by this application request bound.

### Dashboard Gateway Rejections

Valid JSON-object backend errors preserve their classification through the
dashboard: HTTP 400/401/403/404/405/409/413/422/429/503 map respectively to
`BAD_REQUEST`, `UNAUTHORIZED`, `FORBIDDEN`, `NOT_FOUND`, `METHOD_NOT_SUPPORTED`, `CONFLICT`,
`PAYLOAD_TOO_LARGE`, `UNPROCESSABLE_CONTENT`, `TOO_MANY_REQUESTS` and
`SERVICE_UNAVAILABLE`. Fix the input/access/configuration problem or refresh
stale evidence, rather than blindly replaying a financial action. Messages
remain redacted and capped at 500 characters. Body-supplied error codes do not
override HTTP status. Malformed/non-object replies, unknown status handling,
timeouts and cancellation retain their previous fail-closed behavior; the
gateway does not automatically retry mutations.

Failed control-center batch reads do not trigger individual-resource fallback
unless a structured 404/405 explicitly indicates an unsupported batch endpoint.
Authentication, rate-limit, service and transport failures remain visible as
unavailable/error or stale evidence, not live zeroes. Two independent backup and
cloud-status reads still run. Successful incompatible batch schemas retain the
existing bounded fallback. Resolve the connection/access problem or wait for
the normal polling/manual refresh; do not blindly replay financial mutations.

### Superseded Dashboard Read

`FAB dashboard changed while loading; refresh to read current data` is a
dashboard read conflict, not a rejected bookkeeping decision. A mutation or
explicit refresh invalidated the request's cache generation before it finished.
The superseded response is not stored as live data or used to overwrite newer
resource history. Refresh the dashboard; do not replay a financial mutation.
Snapshots read while a mutation was pending are also invalidated when that
request finishes, even if its response is unreadable. Existing last-valid data
can still appear as visibly stale after a failed refresh; stale is not current
evidence or permission to submit/confirm a provider action.

### Invalid HAI Command Contract

HAI plan/execute routes return HTTP 400 before command execution, lease changes
or command audit writes for malformed limit/document-ID inputs. Values must be
integers, not booleans, floats (even 1.0) or numeric strings. Existing per-command
limit ranges still apply. Document IDs must be 1 through 2^63-1 and replay keys
must be strings matching the existing 1-128-character ASCII pattern. Supply both
`documentId` and `evidence` for attachment attestation. A structurally valid
envelope is not proof that the document exists, its attachment matches, or any
source is safe to archive. Valid request-ID replay protections and authority
restrictions remain unchanged; do not retry malformed commands using guessed
document IDs or weakened evidence.

### Invalid Or Oversized HAI Metadata

`payload must be bounded, finite JSON metadata` returns an invalid plan or HTTP
400 before command audit writes or executor calls. Supply JSON-compatible
values only: no non-finite numbers, byte strings, tuples or cyclic structures.
Command metadata is capped at 1 MiB of canonical UTF-8 JSON, depth 32 and 10,000
values. Its captured snapshot remains separate from caller and executor copies,
so mutations cannot change the audited request or replay identity.

Authenticated HAI plan/execute HTTP requests above the 2 MiB envelope limit
return HTTP 413 before decoding; a stricter configured global limit takes
precedence. Wire size and canonical metadata size are different limits. Receipt
and bank upload allowances are unchanged. This application bound does not prove
upstream proxy/server memory limits. Structural acceptance does not verify
provider evidence or grant approval to archive a source document.

### HAI Command Audit Attribution

The HTTP command executor ignores any body-supplied `actor`. Requested and
completed/failed events, and the executor itself, receive `fab_hai_api:hai` for
a scoped HAI credential or `fab_hai_api:operator` for an operator credential/
session. Token-free loopback mode records `fab_hai_api:loopback`, not a verified
person. A valid HAI bearer credential retains its machine classification even
when an operator session cookie is also present. Changing a label on replay
does not replace the original audit event or cause another execution.

These are credential-class labels, not individual identity verification.
Existing authorization/allowlist, durable replay, provider and archival gates
remain unchanged. Trusted in-process actors and other API routes retain their
existing behavior. An actor label alone never authenticates an HTTP request.

### Invalid Wave Field Evidence

`wave_field_mismatch:<field>` also covers unusable expected or observed values,
not just two different valid values. Invalid dates, non-finite/unparseable
amounts, booleans or containers in monetary/text fields and blank normalized
text cannot establish a match, even when both sides are equally invalid.
Existing cent precision, supported date formats and text case/spacing rules
remain in place for valid values. VAT present in either the source document or
normalized bookkeeping record requires matching tax readback, including zero.

Work orders list required missing or invalid expected values in
`wave.missingExpectedFields` and hold otherwise-ready records at
`needs_processing` rather than queuing transaction creation or attachment
upload. Existing source-file/review gates still take precedence. Correct the source/
bookkeeping record through the normal review path, then obtain fresh readback.
Do not change evidence merely to make a match appear true. Matching metadata
still does not replace exact attachment bytes, source identity, unique finished
transaction, fresh observation, business, approval or archive gates.

### Invalid Receipt Byte Size

`source_size_missing` covers missing, malformed or conflicting recorded source
sizes. `wave_receipt_size_missing_or_invalid` holds upload work orders at
`source_incompatible` when the local source is available but its size metadata
is unusable. Byte counts must be positive native integers or trimmed strings
of 1-19 ASCII digits, at most 2^63-1. Booleans, floats (including 17.0), signs,
underscores, non-ASCII digits and non-finite values are rejected. If intake and
provider sizes are both declared, they must agree; zero/false cannot trigger
a fallback to a different declared size. Existing Wave file-size limits apply.

Correct or re-establish source metadata through the normal retained-evidence
workflow rather than guessing a size. Full readback verification requires a
valid recorded source size even if the downloaded attachment hash matches.
Current provider size must also be present, valid and exact before archival;
a failed post-move size check invokes the existing rollback path and does not
mark the source archived. Prior audit records are preserved and rechecked,
not treated as permission to bypass fresh size failures. Local rollback tests
are not proof that a live Drive restore will succeed.

### Invalid Line-Item Fields

`bookkeeping_line_item_invalid:lineItems[index].field` blocks Wave attachment
verification and Drive archival when a normalized line item has an invalid
declared numeric value. Quantity, unit price, amount, tax amount, tax rate and
confidence must parse as finite numbers when present; booleans are not numbers.
Non-finite numbers nested in line-item metadata are also reported by path.
This validates number representation, not tax correctness, ranges or arithmetic.

Detailed work orders expose `wave.invalidLineItemFields`. Invalid values are
projected as `null` in the delivery response, never guessed or saved over the
original ledger values. Invalid required expected fields are likewise projected
as `null` and reported through `wave.missingExpectedFields`. Both compact and
detailed queues hold otherwise-ready records at `needs_processing`. Source and
review gates retain precedence. Correct the original record through normal
review and obtain fresh readback; prior verification cannot bypass new failures.
Optional absent numbers and valid precision are preserved. This is scoped to
delivery work orders, not a claim that every API response is JSON-hardened.

Retain request/workflow IDs when investigating failures. Share only the sanitized
support bundle; never attach credentials or financial evidence to a public issue.
