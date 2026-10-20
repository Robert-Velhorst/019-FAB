import hashlib
import os
import stat
from types import SimpleNamespace

import pytest

from src.operations import local_api
from src.operations.local_ledger import LocalOperationsLedger


@pytest.fixture
def retained_source(tmp_path):
    data = b"synthetic retained receipt\n"
    source = tmp_path / "receipt.txt"
    source.write_bytes(data)
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    document_id = ledger.register_document({
        "source": "manual", "sourceDocumentId": "descriptor-fixture", "originalFilename": source.name,
        "mimeType": "text/plain", "storagePath": str(source),
        "contentSha256": hashlib.sha256(data).hexdigest(),
    })
    app = local_api.create_app({"fab_local_ledger_path": str(tmp_path / "ledger.sqlite3")})
    return app.test_client(), f"/api/documents/{document_id}/source", source, data


def test_source_disappearing_after_path_check_fails_closed_not_500(retained_source, monkeypatch):
    client, route, source, data = retained_source
    original = os.path.isfile

    def check_then_remove(path):
        result = original(path)
        if os.fspath(path) == str(source) and result:
            source.unlink()
        return result

    monkeypatch.setattr(local_api.os.path, "isfile", check_then_remove)
    response = client.get(route)
    assert response.status_code == 409
    assert data not in response.data
    assert str(source).encode() not in response.data


def test_opened_pipe_is_rejected_without_reading_and_closed(retained_source, monkeypatch):
    client, route, source, data = retained_source
    read_fd, write_fd = os.pipe()
    opened = []
    original = os.open

    def substituted_open(path, flags, *args, **kwargs):
        if os.fspath(path) == str(source):
            descriptor = os.dup(read_fd)
            opened.append(descriptor)
            return descriptor
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(local_api.os, "open", substituted_open)
    try:
        response = client.get(route)
        assert response.status_code == 409
        assert data not in response.data
        assert len(opened) == 1
        with pytest.raises(OSError):
            os.fstat(opened[0])
    finally:
        for descriptor in opened:
            try:
                os.close(descriptor)
            except OSError:
                pass
        os.close(read_fd)
        os.close(write_fd)


def test_opened_size_cannot_bypass_the_limit_and_descriptor_closes(retained_source, monkeypatch):
    client, route, source, _data = retained_source
    original_open = os.open
    original_stat = os.fstat
    opened = []

    def record_open(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        if os.fspath(path) == str(source):
            opened.append(descriptor)
        return descriptor

    def enlarged_stat(descriptor):
        if descriptor in opened:
            return SimpleNamespace(st_mode=stat.S_IFREG, st_size=local_api.LOCAL_SOURCE_PREVIEW_MAX_BYTES + 1)
        return original_stat(descriptor)

    monkeypatch.setattr(local_api.os, "open", record_open)
    monkeypatch.setattr(local_api.os, "fstat", enlarged_stat)
    response = client.get(route)
    assert response.status_code == 413
    assert len(opened) == 1
    with pytest.raises(OSError):
        original_stat(opened[0])


def test_source_descriptor_closes_when_wrapper_creation_fails(retained_source, monkeypatch):
    client, route, source, _data = retained_source
    opened = []

    def failed_wrapper(descriptor, *args, **kwargs):
        opened.append(descriptor)
        raise OSError("Synthetic private diagnostic " + str(source))

    monkeypatch.setattr(local_api.os, "fdopen", failed_wrapper)
    response = client.get(route)
    assert response.status_code == 409
    assert str(source).encode() not in response.data
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_growing_source_reads_only_one_byte_past_limit(retained_source, monkeypatch):
    client, route, _source, _data = retained_source
    original_wrapper = os.fdopen
    requested_sizes = []

    class ObservedFile:
        def __init__(self, descriptor, *args, **kwargs):
            self.handle = original_wrapper(descriptor, *args, **kwargs)
            assert kwargs["buffering"] == 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def read(self, size):
            requested_sizes.append(size)
            return self.handle.read(size)

    monkeypatch.setattr(local_api, "LOCAL_SOURCE_PREVIEW_MAX_BYTES", 8)
    monkeypatch.setattr(local_api.os, "fdopen", ObservedFile)
    monkeypatch.setattr(local_api.os, "fstat", lambda _fd: SimpleNamespace(st_mode=stat.S_IFREG, st_size=1))
    response = client.get(route)
    assert response.status_code == 413
    assert requested_sizes == [9]


def test_binary_source_bytes_survive_windows_descriptor_opening(tmp_path):
    data = b"%PDF-synthetic\r\nbinary\x1a\xff\r\n"
    source = tmp_path / "receipt.pdf"
    source.write_bytes(data)
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    document_id = ledger.register_document({
        "source": "manual", "sourceDocumentId": "binary-descriptor-fixture",
        "originalFilename": source.name, "mimeType": "application/pdf", "storagePath": str(source),
        "contentSha256": hashlib.sha256(data).hexdigest(),
    })
    app = local_api.create_app({"fab_local_ledger_path": str(tmp_path / "ledger.sqlite3")})
    response = app.test_client().get(f"/api/documents/{document_id}/source")
    assert response.status_code == 200
    assert response.data == data
    assert response.headers["X-FAB-Source-SHA256"] == hashlib.sha256(data).hexdigest()


def test_detail_preview_rejects_unhashed_or_changed_text(tmp_path):
    source = tmp_path / "receipt.txt"
    original = b"original financial evidence"
    source.write_bytes(original)
    document = {"storage_path": str(source), "mime_type": "text/plain"}
    assert local_api._document_source_preview(document)["text"] is None
    document["content_sha256"] = hashlib.sha256(original).hexdigest()
    source.write_bytes(b"altered financial evidence")
    preview = local_api._document_source_preview(document)
    assert preview["text"] is None
    assert "integrity check" in preview["status"]


def test_rendered_detail_page_never_exposes_changed_source_bytes(retained_source):
    client, route, source, data = retained_source
    detail_route = "/documents/" + route.split("/")[-2]
    original = client.get(detail_route)
    assert original.status_code == 200
    assert data.strip() in original.data
    source.write_bytes(b"TAMPERED-PRIVATE-SOURCE-BYTES")
    changed = client.get(detail_route)
    assert changed.status_code == 200
    assert b"TAMPERED-PRIVATE-SOURCE-BYTES" not in changed.data
    assert b"failed its integrity check" in changed.data


def test_detail_preview_hashes_beyond_the_excerpt_and_preserves_unicode(tmp_path):
    source = tmp_path / "receipt.txt"
    original = ("\u20ac" * 40 + "TAIL").encode("utf-8")
    source.write_bytes(original)
    document = {
        "storage_path": str(source), "mime_type": "text/plain",
        "content_sha256": hashlib.sha256(original).hexdigest(),
    }
    preview = local_api._document_source_preview(document, character_limit=20)
    assert preview["text"] == "\u20ac" * 20 + "\n... truncated ..."
    assert "verified" in preview["status"].lower()
    source.write_bytes(original[:-1] + b"X")
    assert local_api._document_source_preview(document, character_limit=20)["text"] is None


def test_api_returns_verified_chunks_without_joining_the_entire_file(tmp_path):
    data = b"x" * (1024 * 1024) + b"end"
    source = tmp_path / "large.txt"
    source.write_bytes(data)
    path = str(tmp_path / "ledger.sqlite3")
    ledger = LocalOperationsLedger(path)
    document_id = ledger.register_document({
        "source": "manual", "sourceDocumentId": "chunked-preview", "storagePath": str(source),
        "mimeType": "text/plain", "contentSha256": hashlib.sha256(data).hexdigest(),
    })
    app = local_api.create_app({"fab_local_ledger_path": path})
    response = app.test_client().get(f"/api/documents/{document_id}/source", buffered=False)
    try:
        assert response.status_code == 200
        assert response.is_streamed
        assert response.content_length == len(data)
        chunks = list(response.response)
        assert [len(chunk) for chunk in chunks] == [1024 * 1024, 3]
        assert b"".join(chunks) == data
    finally:
        response.close()
