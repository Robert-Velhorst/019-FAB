"""Compile or verify FAB's hash-locked Python dependency sets."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


UV_VERSION = "0.11.26"
LOCKS = (
    ("requirements.txt", "requirements.lock"),
    ("requirements-local.txt", "requirements-local.lock"),
    ("requirements-test.in", "requirements-test.lock"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify", action="store_true", help="fail if committed locks are stale")
    mode.add_argument("--upgrade", action="store_true", help="resolve the newest compatible versions")
    args = parser.parse_args()

    uv = shutil.which("uv")
    if not uv:
        parser.error("uv 0.11.26 is required; install the pinned version before compiling locks")
    version = subprocess.run([uv, "--version"], capture_output=True, text=True, check=True).stdout.strip()
    if not version.startswith(f"uv {UV_VERSION} "):
        parser.error(f"expected uv {UV_VERSION}, found {version}")

    root = Path(__file__).resolve().parents[1]
    mismatches = []
    with tempfile.TemporaryDirectory(prefix="fab-python-locks-") as temporary:
        temp_root = Path(temporary)
        for input_name, output_name in LOCKS:
            output = temp_root / output_name if args.verify else root / output_name
            command = [
                uv,
                "pip",
                "compile",
                input_name,
                "--universal",
                "--python-version",
                "3.13",
                "--generate-hashes",
                "--no-header",
                "--output-file",
                str(output),
            ]
            if args.upgrade:
                command.append("--upgrade")
            result = subprocess.run(command, cwd=root, capture_output=True, text=True)
            if result.returncode:
                sys.stderr.write(result.stderr)
                return result.returncode
            if args.verify and output.read_bytes() != (root / output_name).read_bytes():
                mismatches.append(output_name)

    if mismatches:
        print("Stale Python lockfiles: " + ", ".join(mismatches), file=sys.stderr)
        return 1
    if args.verify:
        print("Python dependency locks match their inputs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
