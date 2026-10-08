from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable


_SOURCE_DIRECTORIES = (
    "src",
    "web/client",
    "web/server",
    "web/shared",
    "web/scripts",
)
_SOURCE_FILES = (
    "src/main.py",
    "Start-FAB.ps1",
    "Stop-FAB.ps1",
    "scripts/Windows-Profile.ps1",
    "scripts/Windows-Job.ps1",
    "scripts/Windows-Job.cs",
    "scripts/Windows-Process.ps1",
    "requirements.txt",
    "requirements-local.txt",
    "config/config_template.ini",
    "web/.env.example",
    "web/package.json",
    "web/pnpm-lock.yaml",
    "web/pnpm-workspace.yaml",
    "web/tsconfig.json",
    "web/vite.config.ts",
)
_CONFIGURATION_FILES = (
    "config/config.ini",
    "web/.env",
)
_SOURCE_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".mjs",
    ".py",
    ".ts",
    ".tsx",
}


def runtime_fingerprint(root: str | Path) -> str:
    """Return a secret-safe fingerprint of code and local configuration state."""

    root_path = Path(root).resolve()
    digest = hashlib.sha256(b"fab-runtime-fingerprint-v1\0")
    for path in _source_paths(root_path):
        relative = path.relative_to(root_path).as_posix()
        digest.update(b"source\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")

    # Configuration content can contain secrets. Metadata is enough to make a
    # normal edit restart the services without copying secret-derived hashes to
    # the runtime manifest.
    for relative in _CONFIGURATION_FILES:
        path = root_path / relative
        digest.update(b"config-metadata\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if path.is_file():
            stat = path.stat()
            digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode("ascii"))
        else:
            digest.update(b"missing")
        digest.update(b"\0")
    return digest.hexdigest()


def _source_paths(root: Path) -> Iterable[Path]:
    paths: dict[str, Path] = {}
    for relative in _SOURCE_FILES:
        path = root / relative
        if path.is_file():
            paths[path.relative_to(root).as_posix()] = path
    for relative in _SOURCE_DIRECTORIES:
        directory = root / relative
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if path.is_file() and path.suffix.lower() in _SOURCE_SUFFIXES:
                paths[path.relative_to(root).as_posix()] = path
    for relative in sorted(paths):
        yield paths[relative]


def main() -> int:
    print(runtime_fingerprint(Path.cwd()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
