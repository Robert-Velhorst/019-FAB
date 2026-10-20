# Acceptance Tests

The final verification report records actual results. This file defines the release acceptance contract.

| ID | Test | Expected result |
| --- | --- | --- |
| A01 | Start with `Start-FAB.ps1 -NoBrowser` | API, worker, and dashboard bind to loopback and publish runtime identity. |
| A02 | Open `/admin/operations` | Live resource states appear; unavailable services are labeled and no fake data substitutes for them. |
| A03 | Upload a supported synthetic receipt | Source hash and provenance are recorded; processing creates extracted evidence or a reviewable failure. |
| A04 | Process low-confidence or incomplete evidence | Record enters review and cannot be posted externally. |
| A05 | Resolve a review | Corrections are audited and downstream work remains a draft until approved. |
| A06 | Prepare and approve an export | Approval is separate from execution and uses idempotent operation identity. |
| A07 | Engage emergency stop | Autonomy plan becomes blocked and no new workflow step starts. |
| A08 | Attempt HAI resume | Resume is unavailable to HAI; only the operator confirmation endpoint can clear the stop. |
| A09 | Create support bundle | ZIP contains only manifest, doctor, audit index, and README; known secrets and source identifiers are absent. |
| A10 | Create recovery package with missing source evidence | Package is not reported as complete. |
| A11 | Run reconciliation and reports | Discrepancies and provisional compliance state remain visible. |
| A12 | Attempt Drive archive without Wave attachment verification | Archive is blocked and the exact missing evidence is shown. |
| A13 | Verify desktop and mobile dashboard geometry | No horizontal page overflow, inaccessible controls, or clipped emergency/support actions. |
| A14 | Run Python and web verification suites | Tests, type check, and production build pass. |
| A15 | Inspect Git status and tracked files | No credentials, tokens, uploaded files, runtime databases, logs, or generated support ZIPs are tracked. |
| A16 | Request bounded health detail with more open issues than `issueLimit` | Returned details are prioritized and truncated, while status, exact issue/severity/type counts, metrics, next actions, and internal safety decisions still reflect every issue. |
| A17 | Request the same health projection concurrently | One request computes the read-only snapshot and near-simultaneous requests reuse it for at most the configured short TTL; responses remain `no-store`, projection keys stay isolated, and internal safety paths remain uncached. |
| A18 | Build and serve the production dashboard | The build rejects developer instrumentation and oversized bundles; the live shell is no-cache, hashed assets are immutable, CSP is restrictive, HTTP does not emit HSTS, trusted HTTPS does, and compressed responses use gzip. |
| A19 | Start the isolated Compose stack | API/web become healthy, worker remains running, all processes are non-root, local tRPC returns a complete control-center payload, and unauthenticated or invalid operations-bridge requests fail closed. |
| A20 | Enter maintenance and rehearse full recovery on an isolated copy | Worker is absent and cannot acquire ownership, normal/HAI mutations are locked, a source-complete package restores exact bytes and rewritten paths without overwrite, and a forced post-restore failure returns the ledger to its pre-restore state. |

## Live provider acceptance

These are blocked until real authorization is available:

- Google Drive: consent, approved folder listing, read-only import, source hash verification, and non-destructive archive dry run.
- Wave: business identity, chart mapping, synthetic record creation, receipt upload, attachment readback/hash evidence, and rollback guidance.
- MijnGeldzaken: operator review of the generated master-ledger artifact; no direct mutation is claimed.
