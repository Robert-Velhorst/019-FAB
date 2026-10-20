# Operator Runbook

## Optional Remote Telemetry Identity Safety

Standalone bookkeeping keeps SQLite authoritative; the optional web operations
mirror is disabled by default. Schema v3 adds destination-scoped remote identity
links; schema v4 retains quarantine markers for both sides of a collision,
including a rejected local record that has no link. Existing ledgers receive a
verified pre-upgrade snapshot through the
normal schema-upgrade flow. Preserve that snapshot and the compatible release
before upgrading a real installation.

Local IDs are never treated as remote IDs merely because their numbers match.
Missing links defer remote updates and related-record writes, while local work
continues and the audit trail records the deferral. Conflicting links are
quarantined and require investigation; FAB does not automatically reassign them.
Do not remove quarantine markers or treat a new remote ID as recovery evidence.
After a collision, compare both source identities and the actual remote records
before planning a repair. Quarantine does not block unrelated healthy records.
Historical rows have no inferred links. Document re-registration can establish
a link through the remote service's source-identity deduplication, but workflow
and other legacy records require an explicit verified reconciliation plan.

If the remote database is replaced or reset at the same address, change the
non-secret `[operations] identity_scope` generation label before re-enabling
mirroring. This prevents old links being used for a different database; FAB
does not detect such a replacement automatically. Keep mirroring disabled
until remote identity/readback and recovery have been verified. An unknown
create outcome must be reconciled before replaying a non-idempotent operation.
There is no automatic historical-backfill or exactly-once delivery guarantee.

Developers can run `python scripts/rehearse_operations_identity.py --run` after
building the web server. It requires an already-cached `mysql:8.4` image and
creates a uniquely named, loopback-only, memory-limited disposable container,
temporary SQLite ledger, fresh synthetic credentials, and a temporary full web
server. It checks real HTTP updates and MySQL review/routing references across
deliberately colliding local/remote IDs and a client restart. It does not use
configured accounts, provider data, persistent Docker volumes, or a live ledger.

## Start and inspect

```powershell
.\Start-FAB.ps1 -NoBrowser
.\.venv\Scripts\python.exe -m src.run_fab_doctor
```

Open the dashboard URL printed by the launcher, normally `http://127.0.0.1:3000/admin/operations`. The local API normally uses `http://127.0.0.1:5001`.

The local API uses Waitress and an SQLite WAL ledger. `GET /api/live` is the fast authenticated process check; `GET /api/health` performs the deeper bookkeeping assessment. Use the dashboard Retry action if a cold backup-integrity scan causes one resource to be temporarily stale.

## Daily operation

1. Check source and provider readiness.
2. Sync configured read-only sources or upload documents.
3. Run eligible local work.
4. Resolve the exception and review queues.
5. Inspect routing/export drafts before approving external changes.
6. Run reconciliation and review discrepancies.
7. Generate due reports and compliance findings.
8. Confirm a current, source-complete recovery package exists.

## Emergency stop

Use **Stop automation** when provider behavior, duplicate risk, unexpected data, or a security concern needs investigation. An active step may finish, but no next step starts. HAI can stop FAB but cannot resume it.

To resume, wait until the active cycle lease is released, inspect audit/workflow evidence, enter `RESUME FAB AUTONOMY`, state why resumption is safe, and review the new autonomy plan.

## Provider activation

- Google: install the owner-approved desktop OAuth client, complete consent, and confirm the configured Gmail/Drive sources report ready.
- Wave: save the correct business ID and token, validate identity, choose the anchor/default category mappings, and pair the supervised receipt executor where attachment API coverage is unavailable.
- MijnGeldzaken: generate and inspect the supervised master-ledger export. Record completion manually; do not claim direct account mutation.

## Receipt archival rule

Never archive a Drive source merely because a Wave record exists. FAB must verify the target business, expected bookkeeping fields, the actual downstream attachment, and its evidence digest. Any ambiguity keeps the source in place.

## Diagnostics and recovery

```powershell
.\.venv\Scripts\python.exe -m src.run_fab_doctor --json
.\.venv\Scripts\python.exe -m src.run_fab_doctor --support-bundle
.\Stop-FAB.ps1
.\Start-FAB-Maintenance.cmd
```

Production deployment is targeted to Hetzner. Keep this Windows profile loopback-only; use the Hetzner deployment instructions for public HTTPS access.

Create a verified recovery package from the dashboard. Restore only after starting `Start-FAB-Maintenance.cmd`; the worker, normal mutations, HAI execution are then locked. Inspect the package first. Ledger-only recovery requires `RESTORE FAB LOCAL LEDGER`; a source-complete version 2 package can also recover exact source bytes with `RESTORE FAB LEDGER AND SOURCE EVIDENCE`. FAB creates a pre-restore package, refuses source overwrite or checksum drift, verifies rewritten paths and bytes, and rolls the ledger back after a failed final check. When recovery is complete, run `Stop-FAB.cmd` and `Start-FAB.cmd`, then verify standard liveness and worker ownership.

### Schema upgrades and rollback

FAB records ordered, checksum-bound SQLite schema migrations. Before an existing ledger is upgraded, startup creates a private SQLite snapshot and JSON manifest in the ledger's sibling `schema_backups` directory. The snapshot must pass SQLite integrity verification, and its SHA-256 must match the manifest, before FAB changes the live schema. `/api/health`, the doctor report, and support diagnostics expose the current and supported schema versions without exposing ledger contents.

For rollback, stop FAB first. Preserve the failed live database and its WAL/SHM sidecars as incident evidence; do not overwrite or delete them. Verify the selected pre-upgrade snapshot against its JSON manifest and run `PRAGMA integrity_check` on a copy. Restore that verified copy atomically to the configured ledger path, then start the prior FAB release that supports the snapshot's source schema. Starting the newer release will intentionally upgrade it again. Keep both copies until the prior release starts, health is green, and document/audit counts have been reconciled.

## Verification

```powershell
python -m pytest -q -p no:cacheprovider -p no:stepwise
pnpm.cmd --dir web check
pnpm.cmd --dir web test
pnpm.cmd --dir web build
```
