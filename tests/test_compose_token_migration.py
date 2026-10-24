import pytest

from src.security.compose_token_migration import TokenMigrationError, migrate_tokens


def test_token_migration_copies_and_verifies_files_idempotently(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "persistent"
    source.mkdir()
    (source / "drive.json").write_text('{"refresh_token":"synthetic"}', encoding="utf-8")

    first = migrate_tokens(source, destination)
    second = migrate_tokens(source, destination)

    assert first == second
    assert (destination / "drive.json").read_text(encoding="utf-8") == '{"refresh_token":"synthetic"}'


def test_token_migration_refuses_conflicting_destination_without_overwrite(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "persistent"
    source.mkdir()
    destination.mkdir()
    (source / "drive.json").write_text("source-token", encoding="utf-8")
    (destination / "drive.json").write_text("different-token", encoding="utf-8")

    with pytest.raises(TokenMigrationError, match="Refusing to overwrite conflicting token"):
        migrate_tokens(source, destination)

    assert (destination / "drive.json").read_text(encoding="utf-8") == "different-token"


def test_token_migration_rejects_source_symlinks(tmp_path):
    source = tmp_path / "legacy"
    source.mkdir()
    external = tmp_path / "outside.json"
    external.write_text("synthetic", encoding="utf-8")
    try:
        (source / "linked.json").symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks are unavailable for this test account")

    with pytest.raises(TokenMigrationError, match="symlinks"):
        migrate_tokens(source, tmp_path / "persistent")


def test_token_migration_rejects_oversized_source_file(tmp_path, monkeypatch):
    import src.security.compose_token_migration as migration

    source = tmp_path / "legacy"
    source.mkdir()
    (source / "drive.json").write_bytes(b"x" * 32)
    monkeypatch.setattr(migration, "MAX_TOKEN_FILE_BYTES", 16)

    with pytest.raises(TokenMigrationError, match="exceeds"):
        migrate_tokens(source, tmp_path / "persistent")
