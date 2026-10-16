import hashlib
import unittest
from unittest.mock import patch

from src.document_fetchers.drive_archiver import DRIVE_HASH_CHUNK_BYTES, DriveArchiveClient


class FakeRequest:
    def __init__(self, result=None, action=None):
        self.result = result
        self.action = action

    def execute(self):
        if self.action:
            self.action()
        return dict(self.result or {})


class FakeDriveFiles:
    def __init__(self, parents):
        self.file = {
            "id": "file-1",
            "name": "invoice.pdf",
            "mimeType": "application/pdf",
            "parents": list(parents),
            "size": "17",
            "md5Checksum": "provider-md5",
            "trashed": False,
        }
        self.updates = []

    def get(self, **kwargs):
        return FakeRequest(self.file)

    def get_media(self, **kwargs):
        return FakeRequest()

    def update(self, **kwargs):
        self.updates.append(dict(kwargs))

        def apply_update():
            parents = set(self.file["parents"])
            parents.add(kwargs["addParents"])
            parents.discard(kwargs["removeParents"])
            self.file["parents"] = sorted(parents)

        return FakeRequest(self.file, apply_update)


class FakeDriveService:
    def __init__(self, parents):
        self.resource = FakeDriveFiles(parents)

    def files(self):
        return self.resource


class FakeMediaIoBaseDownload:
    payload = b""
    chunk_sizes = []

    def __init__(self, sink, _request, chunksize):
        self.sink = sink
        self.chunksize = chunksize
        self.offset = 0
        type(self).chunk_sizes.append(chunksize)

    def next_chunk(self):
        chunk = type(self).payload[self.offset : self.offset + self.chunksize]
        self.sink.write(chunk)
        self.offset += len(chunk)
        return None, self.offset >= len(type(self).payload)


class TestDriveArchiveClient(unittest.TestCase):
    def test_download_sha256_hashes_incremental_chunks_with_a_bounded_chunk_size(self):
        payload = b"FAB receipt evidence" * 100_000
        FakeMediaIoBaseDownload.payload = payload
        FakeMediaIoBaseDownload.chunk_sizes = []
        client = DriveArchiveClient(service=FakeDriveService(["source-folder"]))

        with patch(
            "src.document_fetchers.drive_archiver.MediaIoBaseDownload",
            FakeMediaIoBaseDownload,
        ):
            digest = client.download_sha256("file-1")

        self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
        self.assertEqual(FakeMediaIoBaseDownload.chunk_sizes, [DRIVE_HASH_CHUNK_BYTES])

    def test_download_sha256_rejects_content_above_the_source_document_limit(self):
        FakeMediaIoBaseDownload.payload = b"123456"
        FakeMediaIoBaseDownload.chunk_sizes = []
        client = DriveArchiveClient(service=FakeDriveService(["source-folder"]))

        with (
            patch(
                "src.document_fetchers.drive_archiver.MediaIoBaseDownload",
                FakeMediaIoBaseDownload,
            ),
            patch("src.document_fetchers.drive_archiver.MAX_SOURCE_DOCUMENT_BYTES", 5),
        ):
            with self.assertRaisesRegex(ValueError, "drive_archive_document_exceeds_limit"):
                client.download_sha256("file-1")

    def test_restore_moves_same_file_back_to_intake(self):
        service = FakeDriveService(["archive-folder"])
        client = DriveArchiveClient(service=service)

        result = client.restore_file("file-1", "source-folder", "archive-folder")

        self.assertEqual(result["status"], "restored")
        self.assertEqual(service.resource.file["parents"], ["source-folder"])
        self.assertEqual(service.resource.updates[0]["fileId"], "file-1")

    def test_restore_is_idempotent_after_success(self):
        service = FakeDriveService(["source-folder"])
        client = DriveArchiveClient(service=service)

        result = client.restore_file("file-1", "source-folder", "archive-folder")

        self.assertEqual(result["status"], "already_restored")
        self.assertEqual(service.resource.updates, [])

    def test_restore_fails_closed_when_file_is_in_neither_folder(self):
        service = FakeDriveService(["other-folder"])
        client = DriveArchiveClient(service=service)

        with self.assertRaisesRegex(RuntimeError, "cannot find"):
            client.restore_file("file-1", "source-folder", "archive-folder")

    def test_move_refuses_identical_intake_and_archive_folders(self):
        service = FakeDriveService(["same-folder"])
        client = DriveArchiveClient(service=service)

        with self.assertRaisesRegex(RuntimeError, "must be different"):
            client.move_file("file-1", "same-folder", "same-folder")

        self.assertEqual(service.resource.updates, [])


if __name__ == "__main__":
    unittest.main()
