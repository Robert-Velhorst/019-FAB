"""Load FAB's verified, project-local SQLite before any ledger is opened.

No network, downloads, database files, environment-selected library paths or
global interpreter modifications occur here. Provisioning is a separate step.
"""
from __future__ import annotations

import ctypes
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import sysconfig

ROOT = Path(__file__).resolve().parent
_handle = None
_verified = False
_identity = None


def platform_key() -> str:
    if sys.maxsize <= 2**32:
        raise RuntimeError("FAB's verified SQLite runtime supports x86_64 Windows/Linux")
    # Windows platform.machine() depends on inherited processor environment
    # variables. Select the DLL by the interpreter's compiled ABI instead.
    if os.name == "nt" and sysconfig.get_platform().lower() == "win-amd64":
        return "windows-x86_64"
    if sys.platform == "linux" and os.uname().machine.lower() == "x86_64":
        return "linux-x86_64"
    raise RuntimeError("FAB's verified SQLite runtime supports Windows/Linux")


def require_stable_sqlite():
    global _handle, _verified, _identity
    if _verified:
        return sys.modules["sqlite3"]
    if "sqlite3" in sys.modules or "_sqlite3" in sys.modules:
        raise RuntimeError("SQLite was initialized before FAB's verified bootstrap")
    spec = json.loads((ROOT / "sqlite-runtime.json").read_text(encoding="utf-8"))
    key = platform_key()
    target = ROOT / ".fab-runtime" / "sqlite" / spec["version"] / key
    library = target / spec[key]["library"]
    for path in [library, target / "integrity.json", target, target.parent, target.parent.parent, ROOT / ".fab-runtime"]:
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise RuntimeError("Linked SQLite runtime paths are not accepted")
    if not library.is_file():
        raise RuntimeError("Verified SQLite runtime missing; run scripts/setup_sqlite_runtime.py")
    digest = hashlib.sha256(library.read_bytes()).hexdigest()
    if key.startswith("windows"):
        expected = spec[key]["binary_sha256"]
    else:
        provenance = json.loads((target / "integrity.json").read_text(encoding="utf-8"))
        if provenance.get("source_sha3_256") != spec[key]["archive_sha3_256"] or provenance.get("version") != spec["version"]:
            raise RuntimeError("SQLite build provenance does not match the pinned release")
        expected = provenance.get("binary_sha256")
    if not expected or digest != expected:
        raise RuntimeError("SQLite runtime integrity check failed")
    if os.name == "nt":
        _handle = ctypes.WinDLL(str(library))
    else:
        _handle = ctypes.CDLL(str(library), mode=ctypes.RTLD_GLOBAL)
    sqlite = importlib.import_module("sqlite3")
    with sqlite.connect(":memory:") as connection:
        actual = connection.execute("SELECT sqlite_version()").fetchone()[0]
        source_id = connection.execute("SELECT sqlite_source_id()").fetchone()[0]
        options = [row[0] for row in connection.execute("PRAGMA compile_options")]
    connection.close()
    if actual != spec["version"] or "THREADSAFE=1" not in options:
        raise RuntimeError("Loaded SQLite version or thread-safety policy is incorrect")
    _identity = {"version": actual, "source_id": source_id, "binary_sha256": digest,
                 "platform": key, "compile_options": options}
    _verified = True
    return sqlite


def runtime_evidence() -> dict:
    require_stable_sqlite()
    return dict(_identity)
