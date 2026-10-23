"""Child-process bootstrap selected explicitly by FAB's runtime entrypoints."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from fab_sqlite_runtime import require_stable_sqlite
    require_stable_sqlite()
except Exception:
    os.write(2, b"FAB verified SQLite bootstrap failed; run scripts/setup_sqlite_runtime.py.\n")
    os._exit(78)
