"""Safely migrate legacy Compose OAuth tokens into the persistent token volume."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile


MAX_TOKEN_FILES = 100
MAX_TOKEN_FILE_BYTES = 16 * 1024 * 1024
MAX_TOKEN_TOTAL_BYTES = 32 * 1024 * 1024
CHUNK_BYTES = 64 * 1024


class TokenMigrationError(ValueError):
    """Raised when migration inputs or existing destination state are unsafe."""


def _digest(path: Path) -> tuple[str, int]:
    size = 0
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            size += len(chunk)
            if size > MAX_TOKEN_FILE_BYTES:
                raise TokenMigrationError(f"Token file exceeds {MAX_TOKEN_FILE_BYTES} bytes")
            digest.update(chunk)
    return digest.hexdigest(), size


def _manifest(source: Path) -> list[dict[str, object]]:
    source = Path(source)
    if source.is_symlink():
        raise TokenMigrationError("Token source must be a real directory")
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise TokenMigrationError("Token source must be a real directory")

    files: list[dict[str, object]] = []
    total_bytes = 0
    def fail_walk(error: OSError) -> None:
        raise TokenMigrationError("Could not completely inspect the token source") from error

    for current, directories, names in os.walk(source, followlinks=False, onerror=fail_walk):
        current_path = Path(current)
        for name in list(directories):
            path = current_path / name
            if path.is_symlink():
                raise TokenMigrationError("Token source may not contain symlinks")
        for name in names:
            path = current_path / name
            if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
                raise TokenMigrationError("Token source must contain regular files only")
            relative = path.relative_to(source)
            if any(part in {".", ".."} for part in relative.parts):
                raise TokenMigrationError("Token source contains an unsafe path")
            digest, size = _digest(path)
            total_bytes += size
            files.append({"path": relative.as_posix(), "sha256": digest, "bytes": size})
            if len(files) > MAX_TOKEN_FILES or total_bytes > MAX_TOKEN_TOTAL_BYTES:
                raise TokenMigrationError("Token source exceeds migration limits")
    return sorted(files, key=lambda entry: str(entry["path"]))


def migrate_tokens(source: str | Path, destination: str | Path) -> list[dict[str, object]]:
    source_path = Path(source)
    destination_path = Path(destination)
    manifest = _manifest(source_path)
    source_path = source_path.resolve(strict=True)
    destination_path.mkdir(parents=True, exist_ok=True)
    if destination_path.is_symlink():
        raise TokenMigrationError("Token destination must be a real directory")

    for entry in manifest:
        relative = Path(str(entry["path"]))
        target = destination_path / relative
        parent = destination_path
        for component in relative.parts[:-1]:
            parent = parent / component
            if parent.is_symlink():
                raise TokenMigrationError("Destination token path contains a symlink directory")
        if target.is_symlink():
            raise TokenMigrationError("Destination contains a symlink")
        if target.exists():
            if not target.is_file():
                raise TokenMigrationError("Destination token path is not a regular file")
            existing_digest, existing_size = _digest(target)
            if existing_digest != entry["sha256"] or existing_size != entry["bytes"]:
                raise TokenMigrationError(f"Refusing to overwrite conflicting token: {relative.as_posix()}")
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".fab-token-", delete=False) as output:
                temporary_path = Path(output.name)
                with (source_path / relative).open("rb") as input_file:
                    while chunk := input_file.read(CHUNK_BYTES):
                        output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.chmod(temporary_path, 0o600)
            except OSError:
                pass
            try:
                if os.name == "nt":
                    os.rename(temporary_path, target)
                    temporary_path = None
                else:
                    os.link(temporary_path, target)
                    temporary_path.unlink()
            except FileExistsError as error:
                raise TokenMigrationError(f"Refusing to overwrite concurrent token: {relative.as_posix()}") from error
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    # Verify the completed destination against source evidence before reporting success.
    for entry in manifest:
        digest, size = _digest(destination_path / str(entry["path"]))
        if digest != entry["sha256"] or size != entry["bytes"]:
            raise TokenMigrationError(f"Migrated token verification failed: {entry['path']}")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="Read-only legacy token directory")
    parser.add_argument("--destination", default="/app/tokens", help="Persistent token volume mount")
    args = parser.parse_args()
    print(json.dumps({"files": migrate_tokens(args.source, args.destination)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
