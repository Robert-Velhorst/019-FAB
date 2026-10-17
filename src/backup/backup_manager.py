from typing import Any, Dict, List
import os
import datetime
import re
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from pathlib import Path
from werkzeug.utils import safe_join

MAX_BACKUP_SOURCES = 100
MAX_RESTORE_MEMBERS = 20_000
MAX_RESTORE_BYTES = 512 * 1024 * 1024
WINDOWS_RESERVED_NAME = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.IGNORECASE)


def _canonical_path(value: Any) -> str:
    path = os.fspath(value)
    if not isinstance(path, str):
        raise TypeError("Filesystem paths must be text paths.")
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def _path_is_within(path: str, root: str) -> bool:
    try:
        return os.path.normcase(os.path.commonpath((path, root))) == os.path.normcase(root)
    except (TypeError, ValueError):
        return False


def _raise_walk_error(error: OSError) -> None:
    raise error


class BackupManager:
    """Manages backup and restoration of FAB local data/configuration."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config or {}
        self.backup_base_dir = _canonical_path(self.config.get("backup_base_dir", "backups"))
        self.restore_base_dir = _canonical_path(
            self.config.get("backup_restore_dir") or os.path.join(self.backup_base_dir, "restore")
        )
        os.makedirs(self.backup_base_dir, exist_ok=True)

    def default_backup_paths(self) -> List[str]:
        paths = [
            self.config.get("database_path", "data/fab.sqlite3"),
            self.config.get("manual_review_queue_file", "data/manual_review_queue.json"),
            self.config.get("local_input_dir", "data/sort_out"),
            self.config.get("bank_import_dir", "data/bank_exports"),
            self.config.get("mijngeldzaken_export_dir", "data/exports/mijngeldzaken"),
        ]
        configured_paths = self.config.get("backup_paths", [])
        if isinstance(configured_paths, (list, tuple)):
            paths.extend(configured_paths)
        return [
            _canonical_path(path)
            for path in paths
            if isinstance(path, (str, os.PathLike)) and path and os.path.exists(path)
        ]

    def _snapshot_sqlite_database(self, database_path: str) -> str:
        snapshot_dir = tempfile.mkdtemp(prefix=".fab-sqlite-", dir=self.backup_base_dir)
        snapshot_path = os.path.join(snapshot_dir, os.path.basename(database_path))
        source_uri = f"{Path(database_path).as_uri()}?mode=ro"
        snapshot_uri = f"{Path(snapshot_path).as_uri()}?mode=ro"
        source = target = None
        try:
            source = sqlite3.connect(source_uri, uri=True, timeout=30)
            target = sqlite3.connect(snapshot_path, timeout=30)
            source.backup(target, pages=256, sleep=0.05)
            target.close()
            target = None
            source.close()
            source = None

            verification = sqlite3.connect(snapshot_uri, uri=True, timeout=30)
            try:
                integrity = verification.execute("PRAGMA integrity_check").fetchone()
            finally:
                verification.close()
            if integrity != ("ok",):
                raise ValueError("SQLite backup snapshot failed its integrity check.")
            return snapshot_path
        except Exception:
            if target is not None:
                target.close()
            if source is not None:
                source.close()
            shutil.rmtree(snapshot_dir, ignore_errors=True)
            raise

    def perform_backup(self, paths_to_backup: List[str] = None, backup_config: Dict[str, Any] = None) -> Dict[str, Any]:
        if backup_config is not None and not isinstance(backup_config, dict):
            return {"status": "failure", "message": "Invalid backup configuration."}
        backup_config = backup_config or {}
        if paths_to_backup is None:
            paths_to_backup = self.default_backup_paths()
        if not isinstance(paths_to_backup, list) or len(paths_to_backup) > MAX_BACKUP_SOURCES:
            return {"status": "failure", "message": "Invalid or excessive backup source list."}
        allowed_paths = self.default_backup_paths()
        resolved_paths = []
        for path in paths_to_backup:
            if not isinstance(path, (str, os.PathLike)) or not os.fspath(path).strip():
                return {"status": "failure", "message": "Backup sources must be configured filesystem paths."}
            candidate = _canonical_path(path)
            resolved = None
            for root in allowed_paths:
                if os.path.normcase(candidate) == os.path.normcase(root):
                    resolved = root
                    break
                if os.path.isdir(root) and _path_is_within(candidate, root):
                    relative = os.path.relpath(candidate, root).replace(os.path.sep, "/")
                    resolved = safe_join(root, relative)
                    break
            if resolved is None or _path_is_within(resolved, self.backup_base_dir):
                return {"status": "failure", "message": "A requested backup source is not configured."}
            if not os.path.exists(resolved) or not (os.path.isfile(resolved) or os.path.isdir(resolved)):
                return {"status": "failure", "message": "A requested backup source does not exist."}
            resolved_paths.append(resolved)
        paths_to_backup = list(dict.fromkeys(resolved_paths))
        if not paths_to_backup:
            return {"status": "failure", "message": "No configured backup sources are available."}
        backup_type = backup_config.get("type", "zip")
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_path = os.path.join(
            self.backup_base_dir,
            f"fab_backup_{timestamp}_{uuid.uuid4().hex[:12]}.zip",
        )

        if backup_type != "zip":
            return {"status": "failure", "message": f"Unsupported backup type: {backup_type}"}

        partial_path = os.path.join(self.backup_base_dir, f".fab-backup-{uuid.uuid4().hex}.partial")
        temporary_snapshots = []
        database_path = _canonical_path(self.config.get("database_path", "data/fab.sqlite3"))
        try:
            written_names = set()
            total_source_bytes = 0
            with zipfile.ZipFile(partial_path, "x", zipfile.ZIP_DEFLATED) as zipf:
                def write_source_file(file_path: str, arcname: str) -> None:
                    nonlocal total_source_bytes
                    size_before = os.stat(file_path, follow_symlinks=False).st_size
                    if size_before < 0 or total_source_bytes + size_before > MAX_RESTORE_BYTES:
                        raise ValueError("Backup source data exceeds the restore size limit.")
                    zipf.write(file_path, arcname)
                    actual_size = zipf.filelist[-1].file_size
                    if actual_size != size_before or total_source_bytes + actual_size > MAX_RESTORE_BYTES:
                        raise ValueError("Backup source changed or exceeded the restore size limit.")
                    total_source_bytes += actual_size

                for path in paths_to_backup:
                    if not os.path.exists(path):
                        raise FileNotFoundError(f"Backup source disappeared: {path}")
                    if os.path.isfile(path):
                        arcname = os.path.basename(path)
                        normalized_arcname = os.path.normcase(arcname.replace("/", os.path.sep)).casefold()
                        if normalized_arcname in written_names:
                            raise ValueError("Backup sources contain duplicate archive names.")
                        written_names.add(normalized_arcname)
                        if path == database_path:
                            snapshot_path = self._snapshot_sqlite_database(path)
                            temporary_snapshots.append(snapshot_path)
                            write_source_file(snapshot_path, arcname)
                        else:
                            write_source_file(path, arcname)
                    elif os.path.isdir(path):
                        root_name = os.path.basename(os.path.normpath(path))
                        for root, directories, files in os.walk(
                            path, followlinks=False, onerror=_raise_walk_error
                        ):
                            retained_directories = []
                            for name in directories:
                                directory_path = os.path.join(root, name)
                                if os.path.islink(directory_path):
                                    raise ValueError("Backup source contains a symbolic link.")
                                if not _path_is_within(
                                    _canonical_path(directory_path), self.backup_base_dir
                                ):
                                    retained_directories.append(name)
                            directories[:] = retained_directories
                            for file in files:
                                file_path = os.path.join(root, file)
                                if os.path.islink(file_path):
                                    raise ValueError("Backup source contains a symbolic link.")
                                if not os.path.isfile(file_path):
                                    raise ValueError("Backup source contains a non-regular file.")
                                arcname = os.path.join(
                                    root_name,
                                    os.path.relpath(file_path, start=path),
                                )
                                normalized_arcname = os.path.normcase(arcname).casefold()
                                if normalized_arcname in written_names:
                                    raise ValueError("Backup sources contain duplicate archive names.")
                                written_names.add(normalized_arcname)
                                write_source_file(file_path, arcname)
            if os.path.getsize(partial_path) > MAX_RESTORE_BYTES:
                raise ValueError("Backup archive exceeds the restore size limit.")
            for snapshot_path in temporary_snapshots:
                shutil.rmtree(os.path.dirname(snapshot_path))
            temporary_snapshots.clear()
            os.replace(partial_path, backup_path)
            return {"status": "success", "path": backup_path, "included_paths": paths_to_backup}
        except Exception as exc:
            try:
                os.remove(partial_path)
            except OSError:
                pass
            return {"status": "failure", "message": str(exc)}
        finally:
            for snapshot_path in temporary_snapshots:
                shutil.rmtree(os.path.dirname(snapshot_path), ignore_errors=True)

    def list_backups(self) -> List[Dict[str, Any]]:
        backups = []
        if not os.path.exists(self.backup_base_dir):
            return backups
        for filename in sorted(os.listdir(self.backup_base_dir), reverse=True):
            if not filename.endswith(".zip"):
                continue
            path = os.path.join(self.backup_base_dir, filename)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            backups.append(
                {
                    "filename": filename,
                    "path": path,
                    "size_bytes": os.path.getsize(path),
                    "modified_at": datetime.datetime.fromtimestamp(os.path.getmtime(path)).isoformat(),
                }
            )
        return backups

    def restore_backup(self, backup_file_path: str, restore_dir: str, allow_overwrite: bool = False) -> Dict[str, Any]:
        if not isinstance(allow_overwrite, bool):
            return {"status": "failure", "message": "allow_overwrite must be a boolean."}
        try:
            backup_value = os.fspath(backup_file_path)
            if not isinstance(backup_value, str) or not backup_value.strip():
                return {"status": "failure", "message": "Backup archive path must be a regular file path."}
            backup_candidate = os.path.normcase(
                os.path.abspath(os.path.expanduser(backup_value))
            )
            backup_path = next(
                (
                    backup["path"]
                    for backup in self.list_backups()
                    if backup_candidate == os.path.normcase(backup["path"])
                    or backup_value == backup["filename"]
                ),
                None,
            )
            if backup_path is None:
                return {"status": "failure", "message": "The backup archive must be a listed file in the configured backup directory."}
            restore_value = os.fspath(restore_dir)
            if not isinstance(restore_value, str) or not restore_value.strip():
                return {"status": "failure", "message": "Restore destination must be a non-empty path."}
            restore_candidate = _canonical_path(
                restore_value
                if os.path.isabs(restore_value)
                else os.path.join(self.restore_base_dir, restore_value)
            )
            relative_restore = os.path.relpath(restore_candidate, self.restore_base_dir)
            relative_restore = relative_restore.replace(os.path.sep, "/")
            restore_parts = relative_restore.split("/")
            if (
                not _path_is_within(restore_candidate, self.restore_base_dir)
                or not restore_parts
                or any(
                    not part
                    or part in {".", ".."}
                    or ":" in part
                    or "\x00" in part
                    or part.endswith((" ", "."))
                    or WINDOWS_RESERVED_NAME.fullmatch(part)
                    for part in restore_parts
                )
            ):
                return {"status": "failure", "message": "Restore destination must be a safe folder inside the configured restore directory."}
            restore_root = safe_join(self.restore_base_dir, relative_restore)
            if restore_root is None or not _path_is_within(_canonical_path(restore_root), self.restore_base_dir):
                return {"status": "failure", "message": "Restore destination must be inside the configured restore directory."}
            if os.path.islink(restore_root):
                return {"status": "failure", "message": "Restore destination cannot be a symbolic link."}
        except (TypeError, ValueError, OSError):
            return {"status": "failure", "message": "Invalid backup or restore path."}
        if not _path_is_within(backup_path, self.backup_base_dir) or backup_path == self.backup_base_dir:
            return {"status": "failure", "message": "The backup archive must be inside the configured backup directory."}
        if not _path_is_within(restore_root, self.restore_base_dir) or restore_root == self.restore_base_dir:
            return {"status": "failure", "message": "The restore destination must be inside the configured restore directory."}
        if not os.path.isfile(backup_path):
            return {"status": "failure", "message": "The configured backup archive was not found."}
        if os.path.getsize(backup_path) > MAX_RESTORE_BYTES:
            return {"status": "failure", "message": "Backup archive exceeds the restore size limit."}
        if not backup_path.lower().endswith(".zip"):
            return {"status": "failure", "message": "Unsupported backup file format. Only .zip is supported."}

        if os.path.lexists(restore_root) and not allow_overwrite:
            return {"status": "failure", "message": "Restore destination already exists."}

        staging_root = None
        try:
            os.makedirs(self.restore_base_dir, exist_ok=True)
            os.makedirs(os.path.dirname(restore_root), exist_ok=True)
            staging_root = tempfile.mkdtemp(prefix=".fab-restore-stage-", dir=self.restore_base_dir)
            with zipfile.ZipFile(backup_path, "r") as zipf:
                members = zipf.infolist()
                if len(members) > MAX_RESTORE_MEMBERS:
                    return {"status": "failure", "message": "Backup archive contains too many files."}
                declared_size = 0
                entries = {}
                for info in members:
                    member = info.filename
                    normalized = member.replace("\\", "/")
                    parts = [part for part in normalized.split("/") if part not in {"", "."}]
                    if (
                        not parts
                        or normalized.startswith("/")
                        or any(
                            part == ".."
                            or ":" in part
                            or "\x00" in part
                            or part.endswith((" ", "."))
                            or WINDOWS_RESERVED_NAME.fullmatch(part)
                            for part in parts
                        )
                    ):
                        return {"status": "failure", "message": "Unsafe path in backup archive."}
                    if stat.S_ISLNK(info.external_attr >> 16):
                        return {"status": "failure", "message": "Symbolic links are not allowed in backups."}
                    relative_path = "/".join(parts)
                    key = os.path.normcase(relative_path.replace("/", os.path.sep)).casefold()
                    if key in entries:
                        return {"status": "failure", "message": "Backup archive contains duplicate paths."}
                    entries[key] = info.is_dir()
                    declared_size += info.file_size
                    if declared_size > MAX_RESTORE_BYTES:
                        return {"status": "failure", "message": "Backup archive exceeds the restore size limit."}
                    target_path = _canonical_path(os.path.join(restore_root, *parts))
                    if not _path_is_within(target_path, restore_root):
                        return {"status": "failure", "message": "Unsafe path in backup archive."}
                for key, is_directory in entries.items():
                    if is_directory:
                        continue
                    prefix = key + os.path.sep
                    if any(other.startswith(prefix) for other in entries):
                        return {"status": "failure", "message": "Backup archive has an invalid file/directory layout."}

                actual_size = 0
                for info in members:
                    parts = [part for part in info.filename.replace("\\", "/").split("/") if part not in {"", "."}]
                    staged_target = os.path.join(staging_root, *parts)
                    if info.is_dir():
                        os.makedirs(staged_target, exist_ok=True)
                        continue
                    os.makedirs(os.path.dirname(staged_target), exist_ok=True)
                    with zipf.open(info, "r") as source, open(staged_target, "xb") as target:
                        while True:
                            chunk = source.read(64 * 1024)
                            if not chunk:
                                break
                            actual_size += len(chunk)
                            if actual_size > MAX_RESTORE_BYTES:
                                raise ValueError("Backup archive exceeds the restore size limit.")
                            target.write(chunk)

            previous_root = None
            if os.path.lexists(restore_root):
                if not os.path.isdir(restore_root) or os.path.islink(restore_root):
                    return {"status": "failure", "message": "Restore destination must be a real directory."}
                previous_root = os.path.join(self.restore_base_dir, f".fab-restore-old-{uuid.uuid4().hex}")
                os.replace(restore_root, previous_root)
            try:
                os.replace(staging_root, restore_root)
                staging_root = None
            except OSError:
                if previous_root and os.path.exists(previous_root):
                    os.replace(previous_root, restore_root)
                raise
            if previous_root:
                shutil.rmtree(previous_root, ignore_errors=True)
            return {"status": "success", "path": restore_root}
        except Exception as exc:
            return {"status": "failure", "message": str(exc)}
        finally:
            if staging_root:
                shutil.rmtree(staging_root, ignore_errors=True)
