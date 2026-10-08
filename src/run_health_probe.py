"""Small container probes with no ledger writes or credential output."""

import json
import os
import sys
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler

from src.security.deployment_secrets import read_secret


def probe_api():
    token = read_secret("FAB_LOCAL_API_TOKEN")
    request = Request("http://127.0.0.1:5001/api/live", headers={"Authorization": "Bearer " + token})
    with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
        return response.status == 200


def probe_worker():
    # Runs in the worker container's PID namespace; a stale file cannot prove liveness.
    root = Path(os.environ.get("FAB_INSTANCE_ROOT", "/app"))
    data = json.loads((root / "data" / "fab-worker-runtime.json").read_text(encoding="utf-8"))
    pid = int(data["pid"])
    if pid <= 0 or data.get("service") != "fab-autonomous-worker":
        return False
    command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    return b"src.run_worker" in command


def main():
    try:
        healthy = probe_worker() if sys.argv[1:] == ["worker"] else probe_api()
        return 0 if healthy else 1
    except Exception:
        print("FAB health probe failed; inspect authenticated diagnostics.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
