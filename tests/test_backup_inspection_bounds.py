import hashlib
import json
import struct
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.operations import local_backup as backups
from src.operations.local_ledger import LocalOperationsLedger


@pytest.fixture
def package(tmp_path):
    source = tmp_path / "receipt.pdf"
    source.write_bytes(b"synthetic receipt evidence" * 100)
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    ledger.register_document({"source": "scanner", "sourceDocumentId": "test-receipt",
                              "originalFilename": source.name, "storagePath": str(source),
                              "contentSha256": hashlib.sha256(source.read_bytes()).hexdigest()})
    service = backups.LocalBackupService(ledger, {"fab_local_backup_dir": str(tmp_path / "backups"),
                                                  "fab_maintenance_mode": True})
    result = service.create_backup(require_complete_source_evidence=True)
    path = result["backupPath"]
    backups._INSPECTION_CACHE.pop(path, None)
    backups._MANIFEST_INSPECTION_CACHE.pop(path, None)
    yield service, path, source
    backups._INSPECTION_CACHE.pop(path, None)
    backups._MANIFEST_INSPECTION_CACHE.pop(path, None)


def replace_member(path, name, data):
    with zipfile.ZipFile(path) as archive:
        members = {info.filename: archive.read(info) for info in archive.infolist()}
    members[name] = data
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for member, content in members.items():
            archive.writestr(member, content)


def record_opens(opened):
    original = zipfile.ZipFile.open

    def tracked(archive, name, *args, **kwargs):
        opened.append(name.filename if isinstance(name, zipfile.ZipInfo) else name)
        return original(archive, name, *args, **kwargs)

    return patch.object(zipfile.ZipFile, "open", tracked)


@pytest.mark.parametrize("second_attempt_fails", [False, True])
def test_repeated_backup_timestamp_preserves_previous_package(tmp_path, second_attempt_fails):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    service = backups.LocalBackupService(ledger, {"fab_local_backup_dir": str(tmp_path / "backups")})
    with patch.object(backups, "_timestamp", return_value="same-clock-tick"):
        first = service.create_backup(note="original recovery point")
        with open(first["backupPath"], "rb") as retained:
            original = retained.read()
        if second_attempt_fails:
            with patch.object(service, "_snapshot_ledger", side_effect=OSError("synthetic snapshot failure")):
                with pytest.raises(OSError, match="snapshot failure"):
                    service.create_backup(note="failed replacement")
        else:
            second = service.create_backup(note="new recovery point")
            assert first["backupPath"] != second["backupPath"]
        with open(first["backupPath"], "rb") as retained:
            assert retained.read() == original
    assert not list((tmp_path / "backups").glob("*.tmp"))


def test_publication_collision_cannot_replace_or_remove_an_existing_backup(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    service = backups.LocalBackupService(ledger, {"fab_local_backup_dir": str(tmp_path / "backups")})
    with (patch.object(backups, "_timestamp", return_value="same-clock-tick"),
          patch.object(backups.uuid, "uuid4") as identifier):
        identifier.return_value.hex = "same-identifier"
        first = service.create_backup(note="preserved")
        with open(first["backupPath"], "rb") as retained:
            original = retained.read()
        with pytest.raises(FileExistsError):
            service.create_backup(note="must not overwrite")
        with open(first["backupPath"], "rb") as retained:
            assert retained.read() == original
    assert not list((tmp_path / "backups").glob("*.tmp"))
    assert len([event for event in ledger.list_audit_events() if event["action"] == "local_backup.created"]) == 1


def test_source_capture_enforces_limit_while_streaming(tmp_path):
    source = tmp_path / "growing-receipt.pdf"
    destination = tmp_path / "captured.bin"
    source.write_bytes(b"expanded beyond the pre-copy size check")
    with patch.object(backups, "MAX_BACKUP_EVIDENCE_FILE_BYTES", 8):
        with pytest.raises(ValueError, match="source.*size|source.*large"):
            backups._copy_with_sha256(str(source), str(destination))
    assert destination.stat().st_size <= 8


@pytest.mark.parametrize("setting,limit", [
    ("MAX_BACKUP_ARCHIVE_FILES", 2),
    ("MAX_BACKUP_MANIFEST_BYTES", 16),
    ("MAX_BACKUP_UNCOMPRESSED_BYTES", 64),
])
def test_creation_rejects_known_budget_overrun_before_compression(package, setting, limit):
    service, path, _ = package
    with (patch.object(backups, setting, limit),
          patch.object(backups.zipfile, "ZipFile") as compression):
        with pytest.raises(ValueError):
            service.create_backup(require_complete_source_evidence=True)
        compression.assert_not_called()
    assert len(list(Path(service.backup_dir).glob("*.zip"))) == 1
    assert service.inspect_backup(path)["status"] == "valid"


def force_zip64_end_record(path):
    with open(path, "rb") as handle:
        contents = bytearray(handle.read())
    end_offset = contents.rfind(b"PK\x05\x06")
    fields = struct.unpack_from("<4s4H2LH", contents, end_offset)
    _, _, _, entries_on_disk, entries_total, directory_size, directory_offset, _ = fields
    zip64_offset = end_offset
    zip64_end = struct.pack(
        "<4sQ2H2I4Q",
        b"PK\x06\x06",
        44,
        45,
        45,
        0,
        0,
        entries_on_disk,
        entries_total,
        directory_size,
        directory_offset,
    )
    locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, zip64_offset, 1)
    contents[end_offset:end_offset] = zip64_end + locator
    end_offset += len(zip64_end) + len(locator)
    struct.pack_into(
        "<4H2L",
        contents,
        end_offset + 4,
        0,
        0,
        0xFFFF,
        0xFFFF,
        0xFFFFFFFF,
        0xFFFFFFFF,
    )
    with open(path, "wb") as handle:
        handle.write(contents)


def test_manifest_only_never_decompresses_ledger_or_receipts(package):
    service, path, _ = package
    opened = []
    with record_opens(opened):
        result = service.list_backups(deep_verify=False)
    assert result["backups"][0]["status"] == "manifest_valid"
    assert result["schedule"]["integrityVerification"] == "manifest_only"
    assert opened == [backups.BACKUP_MANIFEST_NAME]
    assert path not in backups._INSPECTION_CACHE


def test_metadata_success_cannot_authorize_corrupt_evidence_restore(package):
    service, path, source = package
    with zipfile.ZipFile(path) as archive:
        evidence = next(name for name in archive.namelist() if name.startswith(backups.SOURCE_EVIDENCE_PREFIX))
        original = archive.read(evidence)
    replace_member(path, evidence, b"X" + original[1:])
    assert service.inspect_backup_manifest(path)["status"] == "manifest_valid"
    before = source.read_bytes()
    for action in (service.inspect_backup, service.plan_restore):
        with pytest.raises(ValueError, match="checksum"):
            action(path)
    with pytest.raises(ValueError, match="checksum"):
        service.restore_backup(path, backups.FULL_RESTORE_CONFIRMATION_PHRASE, restore_mode=backups.RESTORE_MODE_FULL)
    assert source.read_bytes() == before
    assert not any(event["action"] == "local_backup.restored" for event in service.ledger.list_audit_events())


@pytest.mark.parametrize("method", ["inspect_backup_manifest", "inspect_backup"])
@pytest.mark.parametrize("limit", ["manifest", "total"])
def test_archive_limits_are_checked_before_member_decompression(package, method, limit):
    service, path, _ = package
    setting = "MAX_BACKUP_MANIFEST_BYTES" if limit == "manifest" else "MAX_BACKUP_UNCOMPRESSED_BYTES"
    opened = []
    with patch.object(backups, setting, 64, create=True), record_opens(opened):
        with pytest.raises(ValueError, match="size|oversized"):
            getattr(service, method)(path)
    assert opened == []


@pytest.mark.parametrize("method", ["inspect_backup_manifest", "inspect_backup"])
@pytest.mark.parametrize(
    "setting,limit",
    [
        ("MAX_BACKUP_ARCHIVE_FILES", 2),
        ("MAX_BACKUP_CENTRAL_DIRECTORY_BYTES", 1),
    ],
)
def test_directory_budgets_are_enforced_before_zipfile_parsing(
    package,
    method,
    setting,
    limit,
):
    service, path, _ = package
    with (
        patch.object(backups, setting, limit),
        patch.object(backups.zipfile, "ZipFile") as parser,
    ):
        with pytest.raises(ValueError, match="too many files|directory exceeds"):
            getattr(service, method)(path)
    parser.assert_not_called()


@pytest.mark.parametrize(
    "method,expected_status",
    [("inspect_backup_manifest", "manifest_valid"), ("inspect_backup", "valid")],
)
def test_zip64_backup_end_record_passes_bounded_preflight(package, method, expected_status):
    service, path, _ = package
    force_zip64_end_record(path)

    result = getattr(service, method)(path)

    assert result["status"] == expected_status


@pytest.mark.parametrize("method", ["inspect_backup_manifest", "inspect_backup"])
@pytest.mark.parametrize("note", ["[" * 100 + "0" + "]" * 100, "NaN", "Infinity", "1e400", "-1e400"],
                         ids=["deep", "nan", "infinity", "positive-overflow", "negative-overflow"])
def test_pathological_json_is_rejected_as_invalid_backup(package, method, note):
    service, path, _ = package
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read(backups.BACKUP_MANIFEST_NAME))
    manifest.pop("note", None)
    content = json.dumps(manifest)[:-1] + ',"note":' + note + "}"
    replace_member(path, backups.BACKUP_MANIFEST_NAME, content.encode())
    with pytest.raises(ValueError, match="manifest"):
        getattr(service, method)(path)


def test_exponent_overflow_does_not_break_backup_listing(package):
    service, path, _ = package
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read(backups.BACKUP_MANIFEST_NAME))
    manifest.pop("ledgerBytes")
    content = json.dumps(manifest)[:-1] + ',"ledgerBytes":1e400}'
    replace_member(path, backups.BACKUP_MANIFEST_NAME, content.encode())
    result = service.list_backups(deep_verify=False)
    assert result["backups"][0]["status"] == "invalid"
    assert "manifest" in result["backups"][0]["error"]


@pytest.mark.parametrize("method,cache_name", [("inspect_backup_manifest", "_MANIFEST_INSPECTION_CACHE"),
                                                ("inspect_backup", "_INSPECTION_CACHE")])
def test_large_manifests_are_validated_without_being_retained_in_cache(package, method, cache_name):
    service, path, _ = package
    with patch.object(backups, "MAX_CACHED_MANIFEST_BYTES", 64, create=True):
        result = getattr(service, method)(path)
    assert result["success"]
    assert path not in getattr(backups, cache_name)


def test_restore_rechecks_archive_limits_after_creating_safety_backup(package):
    service, path, source = package
    original_create = service.create_backup
    with zipfile.ZipFile(path) as archive:
        evidence = next(name for name in archive.namelist() if name.startswith(backups.SOURCE_EVIDENCE_PREFIX))
    opened = []
    tracking = record_opens(opened)

    def changed_after_inspection(*args, **kwargs):
        result = original_create(*args, **kwargs)
        replace_member(path, evidence, b"X" * 5_000)
        tracking.start()
        return result

    before = source.read_bytes()
    try:
        with patch.object(service, "create_backup", changed_after_inspection), patch.object(backups, "MAX_BACKUP_EVIDENCE_FILE_BYTES", 4_096):
            with pytest.raises(ValueError, match="oversized"):
                service.restore_backup(path, backups.FULL_RESTORE_CONFIRMATION_PHRASE, restore_mode=backups.RESTORE_MODE_FULL)
    finally:
        tracking.stop()
    assert opened == []
    assert source.read_bytes() == before
    assert not any(event["action"] == "local_backup.restored" for event in service.ledger.list_audit_events())


def test_backup_api_keeps_lightweight_status_separate_from_restore_authority(package):
    from src.operations.local_api import create_app

    service, path, source = package
    token = "synthetic-api-token"
    client = create_app({"fab_local_ledger_path": service.ledger_path,
                         "fab_local_backup_dir": service.backup_dir,
                         "fab_local_api_token": token, "fab_maintenance_mode": True}).test_client()
    headers = {"Authorization": f"Bearer {token}"}
    with zipfile.ZipFile(path) as archive:
        evidence = next(name for name in archive.namelist() if name.startswith(backups.SOURCE_EVIDENCE_PREFIX))
        original = archive.read(evidence)
    replace_member(path, evidence, b"X" + original[1:])
    opened = []
    with record_opens(opened):
        listing = client.get("/api/backups?verify=false", headers=headers)
    assert listing.status_code == 200
    assert listing.json["backups"][0]["status"] == "manifest_valid"
    assert listing.json["verificationMode"] == "manifest_only"
    assert opened == [backups.BACKUP_MANIFEST_NAME]
    for endpoint in ("/api/backups/inspect", "/api/backups/restore-plan"):
        response = client.get(endpoint, query_string={"backupPath": path}, headers=headers)
        assert response.status_code == 400
        assert response.json["status"] == "invalid"
    response = client.post("/api/backups/restore", headers=headers, json={
        "backupPath": path, "restoreMode": backups.RESTORE_MODE_FULL,
        "confirmation": backups.FULL_RESTORE_CONFIRMATION_PHRASE,
    })
    assert response.status_code == 400
    assert response.json["status"] == "failed"
    assert response.json["success"] is False
    assert source.read_bytes() == original
    assert not any(event["action"] == "local_backup.restored" for event in service.ledger.list_audit_events())
