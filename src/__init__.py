"""FAB services require the verified native database engine before opening data."""
from fab_sqlite_runtime import require_stable_sqlite

require_stable_sqlite()
