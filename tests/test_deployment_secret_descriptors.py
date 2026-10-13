import os
from pathlib import Path

import pytest

from src.security import deployment_secrets


@pytest.mark.parametrize("suffix", [b"\n\n", b"\t", b" ", b"\r", b"\xff"])
def test_secret_loader_rejects_malformed_bytes_without_normalizing(tmp_path, suffix):
    source = tmp_path / "private-secret-path"
    source.write_bytes(b"synthetic-token" + suffix)
    with pytest.raises(ValueError) as error:
        deployment_secrets.read_secret("FAB_LOCAL_API_TOKEN", {"FAB_LOCAL_API_TOKEN_FILE": str(source)})
    assert "synthetic-token" not in str(error.value)
    assert str(source) not in str(error.value)


@pytest.mark.parametrize("suffix", [b"", b"\n", b"\r\n"])
def test_secret_loader_preserves_valid_token_bytes(tmp_path, suffix):
    source = tmp_path / "token"
    source.write_bytes(b"synthetic-token" + suffix)
    assert deployment_secrets.read_secret(
        "FAB_LOCAL_API_TOKEN", {"FAB_LOCAL_API_TOKEN_FILE": str(source)}
    ) == "synthetic-token"


def test_secret_loader_checks_open_descriptor_before_read_and_closes_it(tmp_path, monkeypatch):
    source = tmp_path / "token"
    source.write_bytes(b"synthetic-token")
    reader, writer = os.pipe()
    opened = []

    class UncheckedHandle:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            return b"synthetic-token"

    def swapped_open(path, flags):
        assert flags == os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        if getattr(os, "O_NONBLOCK", 0):
            assert flags & os.O_NONBLOCK
        descriptor = os.dup(reader)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: UncheckedHandle())
    monkeypatch.setattr(deployment_secrets.os, "open", swapped_open)
    try:
        with pytest.raises(ValueError):
            deployment_secrets.read_secret("FAB_LOCAL_API_TOKEN", {"FAB_LOCAL_API_TOKEN_FILE": str(source)})
        assert len(opened) == 1
        with pytest.raises(OSError):
            os.fstat(opened[0])
    finally:
        os.close(reader)
        os.close(writer)
