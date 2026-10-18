# FAB | Financial Automation Bookkeeper

FAB is a local-first bookkeeping operations platform. It collects financial
documents, extracts and validates bookkeeping data, prepares reviewable records,
and coordinates approved work with supported accounting services. Its local
ledger keeps source evidence, decisions, provider operations, and reconciliation
history connected.

```text
You and your team <-> FAB (local operations ledger and review) <-> accounting providers
```

FAB is designed to reduce repetitive bookkeeping, not to hide uncertainty or
make irreversible financial decisions without control. Local intake, extraction,
matching, reporting, and other configured work can run automatically. Provider
changes remain capability- and approval-gated, and archival requires positive
readback evidence.

## Status and Scope

This repository contains a runnable Windows-oriented application, Docker Compose
deployment profiles, a React operator dashboard, a Python bookkeeping engine,
automated tests, and a source-package builder. It is an actively developed
system, not a certified accounting product or a fully unattended replacement
for professional oversight.

Check the Actions page for the current main-branch verification result. A successful run covers only its recorded Linux and Windows backend tests, frontend checks/build, and synthetic dashboard/API/ledger rehearsals; it does not prove a user's installation, credentials, provider
access, backups, deployment, tax treatment, or live bookkeeping workflow.
See [production verification](docs/production-verification.md) and the
[goal-completion matrix](docs/GOAL_COMPLETION_MATRIX.md) for detailed evidence
and remaining acceptance work.

| Area | What FAB supports | Important boundary |
| --- | --- | --- |
| Documents | Local upload/folder intake, Gmail and Drive connectors, OCR, field extraction, validation, duplicate/revision handling | Provider access requires the operator's own OAuth setup and configuration. |
| Review and ledger | Exception queue, corrections, category suggestions, audit history, local operational ledger | Ambiguous or invalid evidence remains for review; local records are not automatically official filings. |
| Wave | Account/category discovery and mapping, supported draft/export flows, approval tracking, and supervised receipt attachment work where needed | Wave API coverage is incomplete for some receipt operations; verify the configured executor and actual record/attachment readback. |
| Banking and reports | Bank-file import, reconciliation, local reports, provisional VAT/retention evidence, scheduled local work | No direct PSD2 bank feed or tax return submission is implemented. |
| MijnGeldzaken | Checksum-bound artifacts for supervised handling | FAB does not store DigiD credentials or claim direct account mutation. |
| HAI | Bounded status, resource discovery, and governed local commands | HAI cannot approve provider exports, clear the emergency stop, restore backups, alter access controls, or bypass review gates. |
| Recovery | Ledger/evidence backups, integrity checks, restore rehearsal and maintenance mode | Off-host recovery, host encryption, and restore acceptance must be verified in the target environment. |

Direct SVB submissions, official tax filing, complete multi-tenant permissions,
and automatic bank feeds are not represented as implemented. Drive source
archival is permitted only after the configured path proves the exact downstream
Wave record and stored attachment; a visible attachment icon alone is not proof.

### Preview release

The first development preview, [`v0.1.0-preview.1`](https://github.com/Robert-Velhorst/019-FAB/releases/tag/v0.1.0-preview.1), provides checksum-verified Windows and Docker Compose source bundles. Each archive contains a `RELEASE-MANIFEST.json` with per-file hashes and has a matching `.sha256` sidecar. These are source packages, not signed installers or prebuilt container images. Verify the checksum before extracting and read the security policy before connecting live financial sources.

## Operator Quick Start (Windows 11)

Requirements: Git; Node.js 22.12 or newer; pnpm 11.20.0 or newer; Python
3.13 (the launcher may provision it using the Windows Python launcher or uv);
Tesseract with Dutch and English language data; and Poppler for PDF OCR.
Node.js and pnpm build and run the operator dashboard; the launcher installs
dashboard packages from the frozen lockfile and Python dependencies in
project-local environments.

Requirements: Windows 11, Git, Python 3.13 (the launcher may provision it using
the Windows Python launcher or `uv`), Tesseract with Dutch and English language
data, and Poppler for PDF OCR. The launcher installs project dependencies in a
local virtual environment.

```powershell
git clone https://github.com/Robert-Velhorst/019-FAB.git
Set-Location 019-FAB
.\Start-FAB.cmd
```

Complete the dashboard's activation and connection steps. Configure only the
sources and providers you intend to use; owner consent and provider credentials
are not included in the repository. Put receipts and invoices in a configured
intake folder, connect an authorized source, or use the dashboard upload. Review
exceptions and evidence before approving a downstream action.

To stop services started by this checkout, run `.\Stop-FAB.cmd`. For restore
and maintenance work, use `.\Start-FAB-Maintenance.cmd` and follow the
[operator runbook](docs/OPERATOR_RUNBOOK.md). Avoid sharing a live ledger across
multiple checkouts or Windows sessions unless worker exclusion has been proven.

### Docker Compose

The standard Compose profile starts the Python API, recurring worker, and
production dashboard. Set strong unique secrets outside source control before
starting:

The release images are private. To pull them, authenticate with a GitHub personal access token (classic) that has only `read:packages`; enter it at Docker's password prompt, never as a command-line argument:

    docker login ghcr.io -u Robert-Velhorst

Then set `FAB_IMAGE_TAG` to a published release tag and run:

    $env:FAB_IMAGE_TAG = "v0.1.0-preview.1"
    docker compose pull
    docker compose up -d --no-build

The VM profile uses the same tags. Confirm you have package read access before pulling. The images contain no credentials or financial data; verify the tag and release notes before deployment.

Published ports bind to loopback by default. State is in persistent Compose volumes and configured mounts. Remote deployment additionally needs reviewed TLS/reverse-proxy configuration, managed secrets, access controls, monitoring, backup/restore drills, and provider acceptance. See the VM deployment and deployment guidance. A successful Compose config check does not certify a host deployment.

Published ports bind to loopback by default and persistent state uses Compose
volumes plus configured mounts. A remote deployment also needs a reviewed TLS
and reverse-proxy setup, managed secrets, access control, monitored backups,
restore drills, and provider acceptance. See [VM deployment](docs/vm-production.md)
and [deployment guidance](docs/deployment_guide.md). A successful Compose
configuration check is not proof that the services are production-ready on a
particular host.

### Review and approval model

The normal document path is:

```text
intake -> preserve and hash source -> OCR/extract -> validate -> categorize
-> deduplicate/group -> review exceptions -> local ledger -> prepare draft
-> explicit approval -> supported execution -> provider readback
-> reconcile/report -> backup or verified archival
```

Preparation is not execution. An approved action is not complete until its
result is verified. Timeouts or missing provider responses can be ambiguous;
check the operation history and provider before retrying. Never archive source
evidence merely because an export was queued or a record appears to exist.

## For Developers

### Architecture

- `src/`: Python 3.13 document intake, OCR/extraction, categorization,
  operations API, SQLite ledger, reconciliation, provider adapters, worker,
  audit, backup, and recovery services.
- `web/client/`: React operator dashboard.
- `web/server/`: Express/tRPC gateway and dashboard server. The browser does not
  receive the backend API token; the gateway validates and forwards bounded
  requests.
- `web/shared/`: shared types and provider surface definitions.
- `config/`: safe configuration template; real settings belong in ignored local
  files or environment variables.
- `deploy/`, Dockerfiles, Compose files, and PowerShell launchers: local and VM
  runtime profiles.
- `tests/`: Python and web regression/safety suites.
- `docs/`: operator, developer, deployment, security, workflow, and acceptance
  references.

Operational state is stored locally by default, including a SQLite database,
source evidence, and logs. These runtime paths are intentionally excluded from
Git and release packages. Do not put real financial documents, ledger databases,
provider tokens, OAuth files, encryption keys, support bundles, or backups in a
commit, issue, or public artifact.

### Development prerequisites

- Python 3.13 and PowerShell on Windows.
- Node.js 22 and pnpm 11.20.0 for the dashboard.
- Tesseract (`eng` and `nld`) and Poppler for document OCR tests/workflows.
- Docker Desktop/Engine only for Compose work.

Create a Python environment and install the hash-locked test dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --disable-pip-version-check --require-hashes -r requirements-test.lock
pnpm.cmd --dir web install --frozen-lockfile
```

Create local configuration from the template before running services:

```powershell
Copy-Item config\config_template.ini config\config.ini
```

Set service secrets through environment variables or the local encrypted setup;
never add them to `.ini`, `.env`, or source files that are tracked by Git.
`python -m src.operations.local_api` starts the local API. For the supported
integrated Windows runtime, prefer `Start-FAB.cmd` so API, worker, and dashboard
lifecycle checks remain coordinated.

Run the test and build gates:

```powershell
python -m pytest -q -p no:cacheprovider -p no:stepwise
pnpm.cmd --dir web check
pnpm.cmd --dir web test
pnpm.cmd --dir web build
python scripts/run_stack_rehearsal.py
```

The stack rehearsal uses synthetic data and a temporary ledger on loopback. It
does not access configured accounts or prove public TLS, a live provider
submission, attachment archival, or a production deployment. CI runs Linux and
Windows backend checks plus frontend type, test, audit, dependency-peer and
build checks. Read the verification records before describing a result as
current; separate test runs must not be combined into a fictitious total.

### Configuration and integrations

The current development preview and its verified source bundles are available in the [FAB v0.1.0 Preview 1 release](https://github.com/Robert-Velhorst/019-FAB/releases/tag/v0.1.0-preview.1). The attached Windows and Compose ZIP files are source distributions, not signed installers or prebuilt container images.

From a clean, committed checkout, build both source distributions:

Google Gmail/Drive sources require owner-authorized OAuth and explicit folder
configuration. Wave requires a valid user-owned token, business selection, and
verified account/category mappings; some receipt upload/readback paths require
the supervised executor. The scanner-mailbox behavior formerly handled by
`025-Scan-to-folder-automation` is integrated as guarded direct FAB intake, not
as a guarantee of live Gmail authorization. MijnGeldzaken remains supervised.
See the [Drive-to-Wave delivery contract](docs/drive_wave_delivery.md) and
[scanner migration notes](docs/scanner_mailbox_migration.md).

### Packaging

The workflow publishes separate GitHub Container Registry images for the Python API/worker and React dashboard:

    ghcr.io/robert-velhorst/019-fab-api:<release-tag>
    ghcr.io/robert-velhorst/019-fab-dashboard:<release-tag>

Both images target `linux/amd64`. The existing preview is `v0.1.0-preview.1`; its immutable source release is published from the commit named on the GitHub release page. The packages are already private and linked to this repository. GitHub Actions has package `Write` access on these two packages only. Keep them private: because this repository is public, Actions workflows triggered by forks may be able to use package access. Review workflow triggers and fork permissions before adding workflows that consume these images.

After this workflow is merged, run **Publish FAB container packages** manually from `main`, select `v0.1.0-preview.1`, and confirm the private-package check. This publishes the existing release commit under the release tag to the correct package names. Future `v*` release tags publish automatically. The workflow refuses non-private or missing packages and publishes the mutable `edge` alias only after both release images succeed. Use a token with only `read:packages` to pull; authenticate with `docker login ghcr.io -u Robert-Velhorst` and enter the token at Docker's password prompt. Never make either package public.

The workflow attaches SBOM and provenance attestations, but images are not cryptographically signed. Verify the source and tag; use checksum-manifested ZIPs when you need source bundles. Immutable release tags are published first; the mutable `edge` aliases are promoted only after both component images succeed. Release runs are serialized to prevent interleaved promotions.

## Security and Privacy

```powershell
python package.py --target all
```

The package builder uses tracked source files only and fails closed on dirty
tracked files, forbidden runtime/secret paths, unsafe links, missing required
files, or size limits. Each ZIP contains a `RELEASE-MANIFEST.json` with per-file
SHA-256 hashes; a `.zip.sha256` sidecar covers the archive itself. The builder
verifies each archive and every member before it reports success. Compare the
sidecar checksum after downloading. These are source distributions, not signed
installers or prebuilt images.

## Security and Data Handling

FAB handles sensitive financial evidence. The intended defaults include
loopback-only local services, authentication for non-loopback API access,
server-only API credentials, bounded/sanitized diagnostics, local audit events,
idempotency, worker leases, an emergency stop, approval gates, and explicit
maintenance mode for restore operations. Windows OAuth tokens are protected by
current-user DPAPI where supported; deployment still owns filesystem, host,
backup, and secret-management controls.

Operator responsibilities:

1. Keep `config/config.ini`, `.encryption_key`, `credentials/`, `tokens/`,
   `data/`, `downloads/`, `logs/`, `output/`, backups, and real source files out
   of Git and public support requests.
2. Use MFA on provider accounts and grant only the access FAB needs.
3. Rotate/revoke credentials after suspected disclosure; push protection does
   not replace incident response or remove a previously exposed secret from
   Git history.
4. Verify backup integrity and complete a restore rehearsal on the actual
   target host. A configured encryption flag is not proof of host encryption.
5. Have a qualified bookkeeper/accountant review tax and accounting treatment.

See repository [security policy](SECURITY.md), [security controls](docs/SECURITY.md),
and [security approach](docs/security_approach.md). FAB is not independently
penetration-tested or certified as compliant with Dutch tax, privacy, or
financial regulations. Security controls and passing tests are not a compliance
certificate.

## Documentation

- [Operator guide](docs/user_guide.md) and [daily runbook](docs/OPERATOR_RUNBOOK.md)
- [Implemented/partial/blocked capability matrix](docs/GOAL_COMPLETION_MATRIX.md)
- [Critical workflow invariants](docs/CRITICAL_PATH.md)
- [Acceptance tests](docs/ACCEPTANCE_TESTS.md) and
  [production verification](docs/production-verification.md)
- [Technical reference](docs/technical_reference.md),
  [API audit](docs/API_USAGE_AUDIT.md), and [UI action audit](docs/UI_ACTION_AUDIT.md)
- [Windows deployment](docs/windows-production.md),
  [VM deployment](docs/vm-production.md), and [error catalogue](docs/ERROR_CATALOG.md)
- [Backup and recovery](docs/OPERATOR_RUNBOOK.md),
  [Drive-to-Wave delivery](docs/drive_wave_delivery.md), and
  [scanner migration](docs/scanner_mailbox_migration.md)

## Contributing and License

Use a feature branch and pull request for code changes. Keep changes scoped,
add regression tests for behavior changes, preserve the fail-closed provider
gates, and report local tests separately from CI, live-provider, and deployment
acceptance. Do not stage broad directory globs in a checkout containing
runtime data or credentials.

The web package declares MIT, but the repository has no root `LICENSE` file;
therefore this repository's overall license is not yet stated. Do not assume
that the web package's license declaration covers every repository file.

For diagnostics, `python -m src.run_fab_doctor --support-bundle` can create a
sanitized support archive. Review the archive before sharing it; sanitization
is not a reason to include private documents or credentials.
