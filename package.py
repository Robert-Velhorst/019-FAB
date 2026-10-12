"""Build and verify source release archives for supported FAB runtimes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import re
import subprocess
import uuid
import zipfile


class ReleasePackageError(RuntimeError):
    """Raised when a release archive cannot be built or verified safely."""


class PackageBuilder:
    """Create tracked-only, checksum-bound Windows and Compose source bundles."""

    ROOT = "FAB"
    MANIFEST_PATH = f"{ROOT}/RELEASE-MANIFEST.json"
    MAX_FILE_BYTES = 64 * 1024 * 1024
    MAX_TOTAL_BYTES = 512 * 1024 * 1024
    REQUIRED = {
        "windows": {
            "Start-FAB-Maintenance.cmd",
            "Start-FAB.ps1",
            "Stop-FAB.ps1",
            "config/config_template.ini",
            "requirements.txt",
            "requirements-local.lock",
            "src/main.py",
            "web/package.json",
            "web/pnpm-lock.yaml",
        },
        "compose": {
            "Dockerfile",
            "docker-compose.maintenance.yml",
            "docker-compose.yml",
            "config/config_template.ini",
            "requirements.txt",
            "requirements.lock",
            "src/main.py",
            "web/Dockerfile",
            "web/package.json",
            "web/pnpm-lock.yaml",
        },
    }
    FORBIDDEN_PARTS = {
        ".git",
        ".venv",
        "backups",
        "build",
        "credentials",
        "data",
        "dist",
        "downloads",
        "logs",
        "node_modules",
        "output",
        "tmp",
        "tokens",
        "venv",
    }
    FORBIDDEN_FILES = {
        ".encryption_key",
        ".env",
        "cloud_functions.py",
        "config/config.ini",
        "main.py",
        "src/cloud_functions.py",
        "src/mobile_capture/mobile_document_capture.py",
    }
    FORBIDDEN_SUFFIXES = {".key", ".kdbx", ".p12", ".pem", ".pfx"}
    DEVELOPMENT_PREFIXES = {".github", "tests"}

    def __init__(self, base_dir: str | os.PathLike[str] = ".", dist_dir: str | os.PathLike[str] | None = None):
        self.base_dir = Path(base_dir).resolve()
        self.dist_dir = Path(dist_dir).resolve() if dist_dir else self.base_dir / "dist"

    def build_local_package(
        self,
        output_name: str = "fab-windows",
        *,
        generated_at: datetime | None = None,
    ) -> str:
        """Build the supported Windows 11 standalone source archive."""
        return self._build("windows", output_name, generated_at=generated_at)

    def build_cloud_package(
        self,
        output_name: str = "fab-compose",
        *,
        generated_at: datetime | None = None,
    ) -> str:
        """Build the supported Docker Compose cloud source archive."""
        return self._build("compose", output_name, generated_at=generated_at)

    def verify_package(self, archive_path: str | os.PathLike[str]) -> dict:
        """Verify archive topology, manifest coverage, and every file checksum."""
        archive = Path(archive_path).resolve()
        if not archive.is_file():
            raise ReleasePackageError(f"Release archive does not exist: {archive}")
        if archive.stat().st_size > self.MAX_TOTAL_BYTES:
            raise ReleasePackageError("Release archive exceeds the compressed size limit.")

        archive_sha256 = _sha256_file(archive)
        sidecar = archive.with_suffix(f"{archive.suffix}.sha256")
        if sidecar.exists():
            if sidecar.stat().st_size > 4096:
                raise ReleasePackageError("Release SHA-256 sidecar exceeds the size limit.")
            expected_archive_hash = sidecar.read_text(encoding="ascii").split(maxsplit=1)[0].strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected_archive_hash):
                raise ReleasePackageError("Release SHA-256 sidecar is malformed.")
            if expected_archive_hash != archive_sha256:
                raise ReleasePackageError("Release archive does not match its SHA-256 sidecar.")

        try:
            release_context = zipfile.ZipFile(archive, "r")
        except zipfile.BadZipFile as exc:
            raise ReleasePackageError("Release archive is not a valid ZIP file.") from exc
        with release_context as release:
            names = release.namelist()
            if len(names) != len(set(names)):
                raise ReleasePackageError("Release archive contains duplicate members.")
            for name in names:
                _validate_archive_name(name, root=self.ROOT)
            try:
                manifest_info = release.getinfo(self.MANIFEST_PATH)
                if manifest_info.file_size > 4 * 1024 * 1024:
                    raise ReleasePackageError("Release manifest exceeds the size limit.")
                _reject_link_member(manifest_info)
                manifest = json.loads(release.read(manifest_info))
            except KeyError as exc:
                raise ReleasePackageError("Release manifest is missing.") from exc
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReleasePackageError("Release manifest is not valid JSON.") from exc

            records = manifest.get("files")
            if not isinstance(records, list) or not records or len(records) > 20_000:
                raise ReleasePackageError("Release manifest has no file inventory.")
            if manifest.get("schemaVersion") != 1 or manifest.get("target") not in self.REQUIRED:
                raise ReleasePackageError("Release manifest contract is unsupported.")
            if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("sourceCommit") or "").lower()):
                raise ReleasePackageError("Release manifest source revision is invalid.")
            expected_members = {self.MANIFEST_PATH}
            verified_bytes = 0
            seen_paths = set()
            for record in records:
                if not isinstance(record, dict):
                    raise ReleasePackageError("Release manifest contains an invalid file record.")
                relative_path = str(record.get("path") or "")
                if relative_path in seen_paths:
                    raise ReleasePackageError("Release manifest contains duplicate file records.")
                seen_paths.add(relative_path)
                member = f"{self.ROOT}/{relative_path}"
                _validate_archive_name(member, root=self.ROOT)
                expected_members.add(member)
                try:
                    declared_size = int(record.get("size"))
                except (TypeError, ValueError) as exc:
                    raise ReleasePackageError(f"Invalid size for release member {relative_path}.") from exc
                if declared_size < 0 or declared_size > self.MAX_FILE_BYTES:
                    raise ReleasePackageError(f"Release member exceeds the size limit: {relative_path}")
                if verified_bytes + declared_size > self.MAX_TOTAL_BYTES:
                    raise ReleasePackageError("Release members exceed the total size limit.")
                declared_hash = str(record.get("sha256") or "").lower()
                if not re.fullmatch(r"[0-9a-f]{64}", declared_hash):
                    raise ReleasePackageError(f"Invalid checksum for release member {relative_path}.")
                try:
                    member_info = release.getinfo(member)
                except KeyError as exc:
                    raise ReleasePackageError(f"Release archive member is missing: {member}") from exc
                _reject_link_member(member_info)
                if member_info.is_dir() or member_info.file_size != declared_size:
                    raise ReleasePackageError(f"Release member size does not match its manifest: {relative_path}")
                actual_hash, actual_size = _sha256_zip_member(release, member)
                if actual_size != declared_size or actual_hash != declared_hash:
                    raise ReleasePackageError(f"Release member verification failed: {relative_path}")
                verified_bytes += actual_size

            if set(names) != expected_members:
                raise ReleasePackageError("Release archive and manifest inventories differ.")
            try:
                manifest_file_count = int(manifest.get("fileCount"))
                manifest_total_bytes = int(manifest.get("totalBytes"))
            except (TypeError, ValueError) as exc:
                raise ReleasePackageError("Release manifest totals are invalid.") from exc
            if manifest_file_count != len(records):
                raise ReleasePackageError("Release manifest file count is incorrect.")
            if manifest_total_bytes != verified_bytes:
                raise ReleasePackageError("Release manifest byte count is incorrect.")

        return {
            "archivePath": str(archive),
            "archiveSha256": archive_sha256,
            "target": manifest.get("target"),
            "sourceCommit": manifest.get("sourceCommit"),
            "fileCount": len(records),
            "totalBytes": verified_bytes,
            "status": "verified",
        }

    def _build(self, target: str, output_name: str, *, generated_at: datetime | None) -> str:
        if target not in self.REQUIRED:
            raise ReleasePackageError(f"Unsupported release target: {target}")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", output_name):
            raise ReleasePackageError("Release output name contains unsafe characters.")
        if not self.base_dir.is_dir():
            raise ReleasePackageError(f"Release source directory does not exist: {self.base_dir}")

        source_commit = self._source_commit()
        self._assert_clean_source()
        tracked = self._tracked_files()
        sources = self._select_sources(tracked)
        missing = sorted(self.REQUIRED[target] - {item[0] for item in sources})
        if missing:
            raise ReleasePackageError(f"Release target {target} is missing required files: {', '.join(missing)}")

        generated = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        stamp = generated.strftime("%Y%m%dT%H%M%S%fZ")
        self.dist_dir.mkdir(parents=True, exist_ok=True)
        final_path = self.dist_dir / f"{output_name}-{stamp}.zip"
        temp_path = self.dist_dir / f".{final_path.name}.{uuid.uuid4().hex}.tmp"
        sidecar = final_path.with_suffix(f"{final_path.suffix}.sha256")
        sidecar_temp = sidecar.with_name(f".{sidecar.name}.{uuid.uuid4().hex}.tmp")
        if final_path.exists() or sidecar.exists():
            raise ReleasePackageError(f"Release output already exists: {final_path.name}")

        records = []
        total_bytes = 0
        for relative_path, source_path in sources:
            size = source_path.stat().st_size
            if size > self.MAX_FILE_BYTES:
                raise ReleasePackageError(f"Release source exceeds the per-file limit: {relative_path}")
            total_bytes += size
            if total_bytes > self.MAX_TOTAL_BYTES:
                raise ReleasePackageError("Release sources exceed the total size limit.")
            records.append({
                "path": relative_path,
                "size": size,
                "sha256": _sha256_file(source_path),
            })

        manifest = {
            "schemaVersion": 1,
            "product": "FAB",
            "target": target,
            "sourceCommit": source_commit,
            "generatedAt": generated.isoformat().replace("+00:00", "Z"),
            "fileCount": len(records),
            "totalBytes": total_bytes,
            "runtimeContract": (
                "Windows 11 standalone via Start-FAB.cmd"
                if target == "windows"
                else "Docker Compose behind authenticated TLS ingress"
            ),
            "files": records,
        }

        try:
            with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as release:
                for relative_path, source_path in sources:
                    _write_file_member(release, source_path, f"{self.ROOT}/{relative_path}")
                manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
                _write_bytes_member(release, manifest_bytes, self.MANIFEST_PATH)
            os.replace(temp_path, final_path)
            archive_hash = _sha256_file(final_path)
            sidecar_temp.write_text(f"{archive_hash}  {final_path.name}\n", encoding="ascii")
            os.replace(sidecar_temp, sidecar)
            self._assert_clean_source()
            self.verify_package(final_path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            sidecar_temp.unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise
        return str(final_path)

    def _tracked_files(self) -> list[str]:
        result = subprocess.run(
            ["git", "-C", str(self.base_dir), "ls-files", "--cached", "-z"],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise ReleasePackageError("Release packaging requires a Git source checkout.")
        return sorted(item.decode("utf-8") for item in result.stdout.split(b"\0") if item)

    def _source_commit(self) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.base_dir), "rev-parse", "HEAD"],
            capture_output=True,
            check=False,
            text=True,
        )
        commit = result.stdout.strip().lower()
        if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ReleasePackageError("Release packaging requires a committed Git revision.")
        return commit

    def _assert_clean_source(self) -> None:
        result = subprocess.run(
            ["git", "-C", str(self.base_dir), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            check=False,
            text=True,
        )
        if result.returncode != 0:
            raise ReleasePackageError("Could not verify the release source status.")
        if result.stdout.strip():
            raise ReleasePackageError("Release packaging refuses modified tracked files; commit and verify them first.")

    def _select_sources(self, tracked: list[str]) -> list[tuple[str, Path]]:
        selected = []
        for value in tracked:
            relative_path = _normalize_relative_path(value)
            lower_path = relative_path.lower()
            parts = PurePosixPath(lower_path).parts
            if lower_path in self.FORBIDDEN_FILES or any(part in self.FORBIDDEN_PARTS for part in parts):
                raise ReleasePackageError(f"Forbidden runtime or secret path is tracked: {relative_path}")
            if PurePosixPath(lower_path).suffix in self.FORBIDDEN_SUFFIXES:
                raise ReleasePackageError(f"Forbidden credential-like file is tracked: {relative_path}")
            if parts and parts[0] in self.DEVELOPMENT_PREFIXES:
                continue
            source_path = (self.base_dir / Path(*PurePosixPath(relative_path).parts)).resolve()
            try:
                source_path.relative_to(self.base_dir)
            except ValueError as exc:
                raise ReleasePackageError(f"Release source escapes the checkout: {relative_path}") from exc
            if source_path.is_symlink() or not source_path.is_file():
                raise ReleasePackageError(f"Release source is missing or not a regular file: {relative_path}")
            selected.append((relative_path, source_path))
        if not selected:
            raise ReleasePackageError("Release source inventory is empty.")
        return selected


def _normalize_relative_path(value: str) -> str:
    if not value or "\\" in value:
        raise ReleasePackageError(f"Invalid tracked release path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleasePackageError(f"Invalid tracked release path: {value!r}")
    return path.as_posix()


def _validate_archive_name(value: str, *, root: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or path.parts[0] != root or ".." in path.parts:
        raise ReleasePackageError(f"Unsafe release archive member: {value!r}")


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    return info


def _reject_link_member(info: zipfile.ZipInfo) -> None:
    unix_file_type = (info.external_attr >> 16) & 0o170000
    if unix_file_type == 0o120000:
        raise ReleasePackageError(f"Release archive contains a symbolic link: {info.filename}")


def _write_file_member(release: zipfile.ZipFile, source: Path, member: str) -> None:
    with source.open("rb") as input_file, release.open(_zip_info(member), "w") as output_file:
        while chunk := input_file.read(1024 * 1024):
            output_file.write(chunk)


def _write_bytes_member(release: zipfile.ZipFile, data: bytes, member: str) -> None:
    release.writestr(_zip_info(member), data)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_zip_member(release: zipfile.ZipFile, member: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with release.open(member, "r") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
    except KeyError as exc:
        raise ReleasePackageError(f"Release archive member is missing: {member}") from exc
    return digest.hexdigest(), size


def main() -> int:
    parser = argparse.ArgumentParser(description="Build verified FAB source release archives.")
    parser.add_argument("--target", choices=("windows", "compose", "all"), default="all")
    parser.add_argument("--output-dir", default="dist")
    args = parser.parse_args()

    builder = PackageBuilder(base_dir=Path(__file__).parent, dist_dir=args.output_dir)
    archives = []
    if args.target in {"windows", "all"}:
        archives.append(builder.build_local_package())
    if args.target in {"compose", "all"}:
        archives.append(builder.build_cloud_package())
    print(json.dumps([builder.verify_package(path) for path in archives], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
