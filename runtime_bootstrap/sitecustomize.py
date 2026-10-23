"""Child-process bootstrap selected explicitly by FAB's runtime entrypoints."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from fab_sqlite_runtime import require_stable_sqlite
    require_stable_sqlite()
except Exception as error:
    # Report controlled failure categories, never arbitrary exception text,
    # environment values, database paths or credentials.
    reason = type(error).__name__
    allowed = {
        "SQLite was initialized before FAB's verified bootstrap",
        "Linked SQLite runtime paths are not accepted",
        "Verified SQLite runtime missing; run scripts/setup_sqlite_runtime.py",
        "SQLite build provenance does not match the pinned release",
        "SQLite runtime integrity check failed",
        "Loaded SQLite version or thread-safety policy is incorrect",
    }
    if str(error) in allowed:
        reason = str(error)
    elif isinstance(error, OSError):
        reason += " (errno=" + str(error.errno) + ", winerror=" + str(getattr(error, "winerror", None)) + ")"
    os.write(2, ("FAB verified SQLite bootstrap failed: " + reason + ". Run scripts/setup_sqlite_runtime.py.\n").encode("utf-8"))
    os._exit(78)
