# FAB Production Readiness Design

**Date:** 2026-08-30
**Status:** Approved design; implementation is in progress and partially verified (see production verification)
**Scope:** Single-user Windows deployment and single-business Docker/VM deployment

## Goal

Make FAB safe and operable as the financial-document system of record for one
business. It must run predictably on a Windows workstation and on one managed
Docker/VM host, protect financial data and credentials, recover from ordinary
failures, and make unsafe or incomplete provider operations visibly blocked.

Production readiness in this design means a deployment can be installed,
started, monitored, backed up, recovered, upgraded, and stopped without
silently losing or duplicating accounting data. It does not mean that an
unverified third-party provider, human accountant, tax authority, or browser
session is automatically safe to mutate.

## Product Boundary

FAB remains the source of truth for its local operational ledger. Google Drive,
Gmail, Wave, and MijnGeldzaken are providers with explicit capability state,
not interchangeable databases.

```text
Operator / approved HAI command
            |
            v
    FAB operator dashboard and local API
            |
            v
   SQLite operational ledger + source evidence + audit history
            |
            v
   supervised or verified provider execution and readback
```

The worker may process, classify, validate, reconcile, prepare drafts, retry
read-only work, create reports, and create recovery packages autonomously.
It must not bypass review, approval, credential, attachment, duplicate,
emergency-stop, or archive-evidence gates.

## Deployment Targets

### A. Windows workstation

- One named business, one active local ledger, and one owned FAB worker.
- `Start-FAB.cmd` and `Start-FAB.ps1` remain the supported operator entrypoints.
- The local API and dashboard bind to loopback by default.
- Windows DPAPI protects local secret-store encryption material for the owning
  Windows account.
- Scheduled work uses the application worker and, when configured, an owned
  Windows Task Scheduler task; duplicate workers must be rejected by a durable
  lease.
- Local financial data, logs, recovery packages, and credentials stay outside
  tracked source paths.

### B. Docker/VM

- One managed host, one business, one API container, one worker container, and
  one dashboard container.
- Containers run as non-root, use named durable data/output volumes, and expose
  no public application port by default.
- A separately managed TLS reverse proxy may publish the dashboard. FAB will
  trust forwarding headers only from explicitly configured proxy addresses.
- Production services use the configured port exactly. Automatic port fallback
  is development-only because a silent port change breaks reverse-proxy and
  health-check contracts.
- SQLite is supported only for this single-host, single-business topology. A
  horizontally scaled or multi-host deployment requires a later PostgreSQL
  design and is outside this release.

## Explicit Non-Goals

- Multi-tenant SaaS isolation, organization billing, and public self-service
  signup.
- Automatic use of passwords, DigiD, browser cookies, or private browser state.
- Direct PSD2, SVB, tax-authority, or accountant actions.
- Treating a prepared provider draft as an executed provider record.
- Automatically archiving a Drive source before the Wave record, attachment,
  checksum, key financial fields, and review gates have been read back and
  verified.
- Claiming live provider support merely because local tests or simulated
  responses succeed.

## Security and Access Design

### Network exposure

1. The default host remains `127.0.0.1`.
2. Non-loopback API exposure requires an explicit, high-entropy local API token
   and a production deployment profile.
3. Dashboard access from a non-loopback origin requires HTTPS and authenticated
   operator access.
4. The Node service uses an explicit proxy-trust policy. It does not blindly
   trust the first client-supplied forwarding header.
5. Webhook routes have an independent rate limit and preserve the verified raw
   request body required by their provider.

### Secrets and session handling

1. Real secrets are absent from Git, release archives, logs, health responses,
   diagnostic bundles, and browser code.
2. Windows deployments use the encrypted local secret store. Docker/VM
   deployments receive secrets through environment injection or a mounted
   read-only secret file outside the repository.
3. Production startup refuses known placeholder values and missing required
   secrets. Development remains explicitly marked as development.
4. Rotation is an operator workflow: replace the service secret, invalidate
   server sessions, revalidate provider capability, and retain only redacted
   audit evidence of the event.
5. Browser-to-ledger navigation continues to use short-lived, single-use,
   server-signed handoff tickets. The API bearer token is never placed in a URL
   or client-side bundle.

### Roles

The target is a single business rather than a multi-tenant system. It supports
an authenticated administrator and operator boundary, with these enforced
rules:

- Operators may inspect, process, prepare, and resolve authorised review work.
- Administrators control secrets, provider activation, access configuration,
  emergency-stop reset, restore, and release settings.
- HAI stays limited to its governed command catalog; it cannot execute exports,
  restores, provider credential changes, or access-control changes.

## Data, Evidence, and Recovery Design

### Ledger and source evidence

- SQLite WAL remains the local ledger mode, with one durable worker lease and
  bounded database locking.
- All provider-facing operations use stable idempotency keys and persist their
  state before an external call.
- A provider operation can be marked executed only after supported provider
  readback proves the expected record and, where required, the original
  attachment.
- Every archive candidate includes source identity, content checksum, provider
  reference, attachment verification state, review state, and archive reason.
- Move-only archival is the only normal Drive archival action. Failed archive
  attempts retain the source and create an actionable exception.

### Backups and restores

- Scheduled recovery packages remain checksum-bound and source-evidence aware.
- Backup health distinguishes missing, stale, invalid, ledger-only, and
  source-complete packages.
- Production readiness requires a configured backup root outside the tracked
  checkout and a documented encrypted-at-rest control: BitLocker on Windows or
  encrypted VM/managed volume storage. FAB records the configured path but
  never claims it can prove host-level disk encryption.
- Restore remains maintenance-mode only, requires an exact confirmation phrase,
  a stopped worker, package integrity checks, and a post-restore SQLite
  integrity check.
- A release cannot be called operationally ready without a fresh, disposable
  recovery rehearsal from a source-complete package.

## Runtime Reliability Design

### Startup and shutdown

1. Centralise a production preflight that validates hosts, ports, tokens,
   storage roots, permissions where observable, database schema, backup
   schedule, required service configuration, and deployment profile.
2. Give every process a clear readiness state: `blocked`, `degraded`, or
   `ready`. Liveness only proves the process responds; readiness proves it can
   safely take its configured role.
3. On shutdown, stop accepting new work, allow bounded in-flight requests to
   finish, release leases only after state is durable, and leave resumable work
   in a recoverable status.
4. A production bind conflict fails startup with the owned process identity and
   next action. It never silently selects a different public port.

### Worker behavior

- The worker uses its existing lease and idempotency boundaries for source
  ingestion, autonomous processing, provider entity synchronization, reports,
  notifications, backup scheduling, and recovery.
- Retryable read-only and draft work uses bounded exponential backoff.
- External posting is excluded from automatic recovery unless a pre-existing
  export approval, the required capability, and the configured execution mode
  all allow it.
- A durable emergency stop is checked before every runnable step.
- Repeated provider or configuration failures become actionable exceptions,
  not infinite retry loops.

## Operations and Observability Design

### Health contract

- `/api/live` remains cheap and constant-time.
- `/api/health`, `/api/doctor`, and the dashboard report specific readiness
  blockers without returning secrets, document bytes, or unbounded provider
  errors.
- Every API response and durable operational error has a safe request ID.
- Structured log records include timestamp, component, severity, request or
  workflow ID, and safe action outcome. Sensitive fields are redacted before
  formatting or persistence.
- The support bundle remains intentionally diagnostic-only; it must never
  contain financial files, OCR text, ledger rows, source filenames, credentials,
  tokens, or raw provider payloads.

### Operator experience

- The dashboard shows one authoritative readiness summary, the first blocker,
  overdue backups, failed recovery work, provider state, and open review work.
- Errors map to a central operator-facing catalog containing the condition,
  safety implication, and next action. Provider diagnostic details stay
  redacted.
- Accessible controls retain keyboard support, focus management, usable narrow
  layouts, and no horizontal overflow.

## Deployment Artifacts

The implementation will extend the current repository rather than introduce a
parallel stack. Expected responsibility boundaries are:

| Area | Primary paths |
| --- | --- |
| Production profile and preflight | `src/operations/local_readiness.py`, `src/operations/local_runtime.py`, `src/operations/local_api.py`, `config/config_template.ini` |
| Worker lifecycle | `src/worker/runtime.py`, `src/worker/scheduler.py`, `src/run_worker.py` |
| Data recovery and backup readiness | `src/operations/local_backup.py`, `src/operations/local_health.py`, `src/operations/local_support_bundle.py` |
| Secret and token lifecycle | `src/security/local_secret_store.py`, `src/security/google_oauth_store.py`, relevant local setup services |
| Windows launch and diagnosis | `Start-FAB.ps1`, `Stop-FAB.ps1`, `src/run_fab_doctor.py`, Windows launcher tests |
| Docker/VM profile | `Dockerfile`, `docker-compose.yml`, a dedicated production Compose override, deployment documentation |
| Web gateway security and lifecycle | `web/server/_core/env.ts`, `web/server/_core/index.ts`, `web/server/_core/security.ts`, `web/server/fabLocalGateway.ts` |
| Operator usability | `web/client/src/components/fab/`, `web/client/src/pages/admin/Operations.tsx` |
| Release evidence | `.github/workflows/ci.yml`, `docs/ACCEPTANCE_TESTS.md`, `docs/OPERATOR_RUNBOOK.md`, `docs/deployment_guide.md` |

## Implementation Sequence

1. Establish a fresh baseline: repository hygiene, full test/build evidence,
   configuration audit, and a standard repository security scan.
2. Add an explicit deployment profile and fail-closed preflight, then cover it
   with Python tests and Windows/Docker startup tests.
3. Harden Node proxy trust, production port behavior, environment validation,
   graceful shutdown, and server-side security tests.
4. Add production Compose controls: resource limits, a read-only secret-file
   option, deterministic health/restart behavior, and reverse-proxy deployment
   guidance without bundling unowned TLS certificates.
5. Expand backup/readiness diagnostics and create the operator error catalog.
6. Add realistic failure and recovery tests, browser accessibility/responsive
   checks, Compose verification, and release gate automation.
7. Run the complete local verification matrix, record exact results, and list
   any provider, infrastructure, accountant, privacy, or legal owner gates
   separately from repository readiness.

## Acceptance Criteria

### Shared

- A clean checkout has no credentials, tokens, local financial data, runtime
  databases, logs, generated releases, or support bundles tracked by Git.
- Python tests, frontend type check, frontend tests, production build, and
  dependency audit pass from a fresh dependency installation.
- A repository security scan is completed and any validated finding is fixed,
  explicitly accepted, or reported as a blocker.
- The production profile rejects weak/default secrets, wildcard binding without
  explicit approval, path traversal, bad backup roots, and inconsistent service
  URLs.
- Existing provider gates remain fail-closed.

### Windows

- `Start-FAB.ps1` starts only this checkout's API, worker, and production
  dashboard; `Stop-FAB.ps1` stops only those owned processes.
- Readiness, health, dashboard, and HAI endpoints agree on runtime identity.
- A duplicate worker is rejected, scheduled backup status is visible, and an
  interrupted safe workflow is recoverable without duplicate provider work.
- A disposable source-complete recovery package is created, inspected, and
  restored through the maintenance path.

### Docker/VM

- The production Compose profile builds reproducibly and runs API, worker, and
  web services as non-root users with durable volumes.
- Public application ports are not published by default; an explicitly enabled
  TLS reverse proxy is the only supported remote exposure path.
- Health checks use authenticated internal endpoints, services restart after a
  controlled interruption, and the configured port remains stable.
- Containers have explicit resource limits appropriate for a single-business
  host and report a visible degraded state before disk, backup, or worker
  safety conditions become unsafe.

## External Acceptance Gates

The following are intentionally outside a source-code claim of completion and
will be reported separately if still pending:

- user-owned Google Gmail and Drive consent remains current;
- Wave token, business ID, account mapping, and receipt-session capability are
  current;
- a synthetic provider-safe document completes Wave record and attachment
  readback before Drive archival is enabled;
- a dedicated HTTPS endpoint and reverse proxy are provisioned for the VM;
- a business owner/accountant accepts accounting policies and final output;
- a privacy/legal owner approves retention, backup location, and operational
  data processing policies.
