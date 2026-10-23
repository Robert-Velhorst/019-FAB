# Verified SQLite runtime

FAB pins SQLite 3.54.1 separately from CPython 3.15.1. The supported native
profiles are Windows x86_64 and Linux x86_64. The release manifest is
`sqlite-runtime.json`; official archives and their SHA3-256 checksums come
from [SQLite downloads](https://www.sqlite.org/download.html).

`Start-FAB.cmd` provisions the official Windows DLL into this checkout's
ignored `.fab-runtime` directory before starting services. It verifies the
archive, the extracted DLL's SHA-256, the actual SQL engine version and thread
safety. It does not replace Python's installation or open a ledger during
provisioning. First provision requires HTTPS access to sqlite.org.

For developer or manually managed installations, provision explicitly:

```powershell
.\.venv\Scripts\python.exe -I scripts/setup_sqlite_runtime.py
$env:PYTHONPATH = "$PWD\runtime_bootstrap;$PWD"
.\.venv\Scripts\python.exe -c "from fab_sqlite_runtime import runtime_evidence; print(runtime_evidence())"
```

On Linux, a C compiler and libc development headers are required during
provisioning. Use the checkout's Python interpreter:

```sh
.venv/bin/python -I scripts/setup_sqlite_runtime.py
export PYTHONPATH="$PWD/runtime_bootstrap:$PWD"
.venv/bin/python -c 'from fab_sqlite_runtime import runtime_evidence; print(runtime_evidence())'
```

The Linux installer compiles only the checksum-verified official amalgamation,
records the source and binary hashes, and verifies the loaded engine in a fresh
process. A Docker build stage compiles it; the runtime image has no compiler.
Application code and the native runtime remain owned by root in the container;
the unprivileged application user owns only its writable data/configuration
directories. Do not bind-mount writable content over the application or native
runtime directory.

The early `sitecustomize` profile is necessary for tests or tools that import
`sqlite3` before application modules. Direct `python -m src...` entrypoints also
enforce the verified loader before importing service code. Missing, mismatched,
linked or already initialized unverified libraries stop startup. There is no
silent fallback to the interpreter's bundled engine. Use a fresh process after
changing the manifest; do not hot-swap a library in a running service.

Source release archives include the manifest, loader, installer and bootstrap;
native artifacts are provisioned separately and must never be committed.

Before an operator rollout, stop writers, retain a verified complete backup,
and rehearse recovery with copies in the target environment. Synthetic tests
exercise bundled-engine fixture readback, schema/row preservation, transactions,
WAL, foreign keys and backup integrity with the pinned engine. They do not
authorize or establish an operator database migration or live provider access.
