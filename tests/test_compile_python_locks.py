from pathlib import Path

from scripts.compile_python_locks import seed_verification_lock


def test_verification_lock_is_seeded_from_committed_lock(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    temporary_root = tmp_path / "verify"
    root.mkdir()
    temporary_root.mkdir()
    committed_lock = root / "requirements.lock"
    committed_lock.write_text("package==1.2.3\n", encoding="utf-8")

    verification_lock = seed_verification_lock(root, temporary_root, committed_lock.name)

    assert verification_lock != committed_lock
    assert verification_lock.read_bytes() == committed_lock.read_bytes()

