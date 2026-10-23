# FAB stable dependency refresh - 23 October 2026

This draft preserves the existing prepared-draft, approval, ownership and
non-submitting financial boundaries. It includes security draft #147 and
updates 91 direct frontend declarations to their audited public releases.
Python inputs are exact and all four hash locks were upgraded with uv 0.13.5
for Python 3.15.1. Node 26.12.1, npm 12.2.1 and pnpm 12.15.0 are explicit.

The Windows launcher selects the exact Python patch, can provision it through
current uv, and checks Node/pnpm versions. Active installation guides agree.
Dashboard images move from Debian Bookworm to pinned Alpine 3.24.3 and remove
unused runtime npm/Yarn. Hosted image qualification remains required.

## Compatibility changes

- Cookie 2 renamed parse to parseCookie. Remove the obsolete local ambient
  declaration which masked the missing runtime export; use upstream types.
- Preserve Wouter route collection after its source-path/hook changes.
- Preserve percentage sizes and direction styling on resizable panels 4.
- Use the current calendar month-grid class and TypeScript-relative imports.
- Stripe 23 uses its typed 2026-09-30.endive API shape, the card-only allowed
  method filter and the existing EUR product currency. Setup mode and usage
  billing metadata remain unchanged. Mocked request verification only; no
  provider account, webhook endpoint, payment or live acceptance was changed.

## Completed local evidence

- Frozen strict peer installation, fresh TypeScript 7.0.2 check and all builds.
- Frontend 491 tests pass; seven MySQL integration cases require a disposable
  server and were skipped locally. Four new cases cover compatibility paths.
- npm audit: zero findings across 630 package records.
- Four Python locks regenerate reproducibly; hash-required installation and
  Python advisory audit pass with zero known findings.

## Native SQLite upgrade

Owned CPython 3.15.1 bundles SQLite 3.53.1. FAB now provisions and verifies the
separate official SQLite 3.54.1 library before importing service modules.
The Windows launcher, CI profiles and API container use this guard. The API
image targets pinned Python 3.15.1 / Debian Trixie; a separate build stage
compiles the checksum-verified SQLite source. Read the
[provisioning and recovery instructions](sqlite-runtime.md).

Windows qualification completed 115 tests and 23 subtests, with two optional
environment cases skipped. These cover native identity, tamper/missing-library
rejection, actual bundled-engine synthetic ledger readback with identical
schema/rows, WAL, foreign keys, rollback, backup integrity, routing/export
approval boundaries and recovery rehearsal. No operator ledger was opened or
upgraded. Linux/image qualification remains pending.

## Unfinished qualification

Current-head hosted suites, installed-image/native
inventory and browser behavior remain required. This is not a claim that all
runtime, transitive or embedded compiler components are current, nor a
deployment or authorization to submit financial operations.
