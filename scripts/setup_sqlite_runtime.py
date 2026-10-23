"""Provision only FAB's pinned native runtime; never open operator data."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fab_sqlite_runtime import platform_key


def write_atomic(path: Path, payload: bytes) -> None:
    """Publish a complete artifact inside the already checked runtime directory."""
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".sqlite-", delete=False) as temporary:
        temporary.write(payload)
        temporary.flush()
        os.fsync(temporary.fileno())
        staged = Path(temporary.name)
    try:
        if os.name != "nt":
            staged.chmod(0o644)
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def main() -> int:
    spec = json.loads((ROOT / "sqlite-runtime.json").read_text(encoding="utf-8"))
    key = platform_key()
    asset = spec[key]
    target = ROOT / ".fab-runtime" / "sqlite" / spec["version"] / key
    library = target / asset["library"]
    if not target.resolve().is_relative_to(ROOT.resolve()):
        raise RuntimeError("Native runtime target leaves the project")
    for path in [library, target / "integrity.json", target, target.parent, target.parent.parent, ROOT / ".fab-runtime"]:
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise RuntimeError("Linked runtime targets are not accepted")
    if library.exists():
        if key.startswith("windows") and hashlib.sha256(library.read_bytes()).hexdigest() != asset["binary_sha256"]:
            raise RuntimeError("Existing runtime is not the pinned binary; refusing to overwrite it")
    else:
        with urllib.request.urlopen(asset["url"], timeout=60) as response:
            if not response.url.startswith("https://www.sqlite.org/"):
                raise RuntimeError("Unexpected SQLite download origin")
            archive = response.read(8 * 1024 * 1024)
        if len(archive) >= 8 * 1024 * 1024 or hashlib.sha3_256(archive).hexdigest() != asset["archive_sha3_256"]:
            raise RuntimeError("SQLite archive integrity check failed")
        target.mkdir(parents=True, exist_ok=True)
        if key.startswith("windows"):
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                member = bundle.getinfo("sqlite3.dll")
                if member.file_size > 5 * 1024 * 1024:
                    raise RuntimeError("Unexpected native library size")
                binary = bundle.read(member)
            if hashlib.sha256(binary).hexdigest() != asset["binary_sha256"]:
                raise RuntimeError("SQLite binary integrity check failed")
            write_atomic(library, binary)
        else:
            with tempfile.TemporaryDirectory(prefix="fab-sqlite-build-") as directory:
                source = Path(directory) / "sqlite3.c"
                with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
                    member = bundle.getmember("sqlite-autoconf-3540100/sqlite3.c")
                    if not member.isfile() or member.size > 20 * 1024 * 1024:
                        raise RuntimeError("Unexpected SQLite source member")
                    source.write_bytes(bundle.extractfile(member).read())
                output = Path(directory) / asset["library"]
                flags = ["-O2", "-fPIC", "-shared", "-DSQLITE_THREADSAFE=1", "-DSQLITE_USE_URI=1",
                         "-DSQLITE_ENABLE_COLUMN_METADATA", "-DSQLITE_ENABLE_FTS5", "-DSQLITE_ENABLE_RTREE",
                         "-DSQLITE_ENABLE_DBSTAT_VTAB", "-DSQLITE_ENABLE_MATH_FUNCTIONS",
                         "-Wl,-soname,libsqlite3.so.0"]
                subprocess.run(["cc", *flags, str(source), "-lm", "-ldl", "-pthread", "-o", str(output)], check=True, timeout=300)
                binary = output.read_bytes()
                compiler = subprocess.check_output(["cc", "--version"], text=True).splitlines()[0]
            provenance = json.dumps({
                "version": spec["version"], "source_sha3_256": asset["archive_sha3_256"],
                "binary_sha256": hashlib.sha256(binary).hexdigest(),
                "compiler": compiler, "flags": flags,
            }, indent=2) + "\n"
            write_atomic(target / "integrity.json", provenance.encode("utf-8"))
            write_atomic(library, binary)
    # Fresh process: no replacing an already initialized sqlite3 binding.
    subprocess.run([sys.executable, "-c", "import json; from fab_sqlite_runtime import runtime_evidence; print(json.dumps(runtime_evidence()))"], cwd=ROOT, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
