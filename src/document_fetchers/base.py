from abc import ABC, abstractmethod
import hashlib
import os
import re
import tempfile
from typing import List, Dict, Any

class BaseFetcher(ABC):
    """Abstract base class for all document fetchers."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.last_error = None
        self.last_run = {
            "status": "idle",
            "fetched": 0,
            "skipped": 0,
            "pages": 0,
        }

    @abstractmethod
    def fetch_documents(self) -> List[Dict[str, Any]]:
        """Fetches documents from the source.

        Returns:
            A list of dictionaries, where each dictionary represents a document
            and contains its metadata and local path.
        """
        pass

    @abstractmethod
    def _authenticate(self):
        """Handles authentication with the document source."""
        pass

    def _save_document(self, content: bytes, filename: str) -> str:
        """Saves the document content to a local file.

        Args:
            content: The binary content of the document.
            filename: The desired filename for the document.

        Returns:
            The local path where the document was saved.
        """
        directory = self._download_directory("source")
        return self._store_content(directory, filename, filename, content)

    def _download_directory(self, section: str, *config_keys: str) -> str:
        section_config = self.config.get(section)
        section_config = section_config if isinstance(section_config, dict) else {}
        candidates = [self.config.get(key) for key in config_keys]
        candidates.extend((section_config.get("download_dir"), self.config.get("attachments_save_dir")))
        for candidate in candidates:
            value = os.path.expandvars(os.path.expanduser(str(candidate or "").strip()))
            if value:
                return os.path.normpath(value)
        root = os.path.join("data", "source_downloads")
        return os.path.join(root, section) if section != "source" else root

    def _start_run(self) -> None:
        self.last_error = None
        self.last_run = {"status": "running", "fetched": 0, "skipped": 0, "pages": 0}

    def _finish_run(self, fetched: int, skipped: int = 0, pages: int = 0) -> None:
        self.last_run = {
            "status": "completed",
            "fetched": int(fetched),
            "skipped": int(skipped),
            "pages": int(pages),
        }

    def _fail_run(self, error: Exception, fetched: int = 0, skipped: int = 0, pages: int = 0) -> None:
        self.last_error = error
        self.last_run = {
            "status": "partial" if fetched else "failed",
            "fetched": int(fetched),
            "skipped": int(skipped),
            "pages": int(pages),
            "errorType": type(error).__name__,
        }

    def _download_path(self, directory: str, filename: str, identity: Any) -> str:
        safe_name = os.path.basename(str(filename or "document"))
        safe_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", safe_name).strip(" .") or "document"
        if len(safe_name) > 180:
            stem, extension = os.path.splitext(safe_name)
            extension = extension[:20]
            safe_name = f"{stem[:max(1, 180 - len(extension))]}{extension}"
        prefix = hashlib.sha256(str(identity or safe_name).encode("utf-8")).hexdigest()[:12]
        return os.path.join(directory, f"{prefix}-{safe_name}")

    def _content_download_path(
        self,
        directory: str,
        filename: str,
        identity: Any,
        content: bytes,
    ) -> str:
        content_hash = hashlib.sha256(content).hexdigest()
        return self._download_path(directory, filename, f"{identity}:{content_hash}")

    def _store_content(
        self,
        directory: str,
        filename: str,
        identity: Any,
        content: bytes,
    ) -> str:
        """Persist immutable connector evidence through an atomic verified write."""
        if not isinstance(content, bytes):
            raise TypeError("Connector evidence content must be bytes.")

        destination_dir = os.path.abspath(directory)
        os.makedirs(destination_dir, exist_ok=True)
        local_path = self._content_download_path(
            destination_dir,
            filename,
            identity,
            content,
        )
        expected_sha256 = hashlib.sha256(content).hexdigest()
        expected_size = len(content)
        if os.path.lexists(local_path):
            self._verify_stored_content(local_path, expected_sha256, expected_size)
            return local_path

        descriptor, staged_path = tempfile.mkstemp(
            prefix=".fab-evidence-",
            suffix=".tmp",
            dir=destination_dir,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            self._verify_stored_content(staged_path, expected_sha256, expected_size)

            if os.path.lexists(local_path):
                self._verify_stored_content(local_path, expected_sha256, expected_size)
                return local_path
            os.replace(staged_path, local_path)
            staged_path = ""
            self._verify_stored_content(local_path, expected_sha256, expected_size)
            return local_path
        finally:
            if staged_path and os.path.exists(staged_path):
                os.remove(staged_path)

    @staticmethod
    def _verify_stored_content(
        path: str,
        expected_sha256: str,
        expected_size: int,
    ) -> None:
        if os.path.islink(path) or not os.path.isfile(path):
            raise OSError("Connector evidence destination is not a regular file.")
        if os.path.getsize(path) != expected_size:
            raise OSError("Retained connector evidence size does not match the source.")

        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            raise OSError("Retained connector evidence checksum does not match the source.")

    def _request_timeout(self, default: float = 30.0) -> float:
        for key in (
            "source_request_timeout_seconds",
            "document_fetch_timeout_seconds",
            "request_timeout_seconds",
        ):
            value = self.config.get(key)
            if value not in (None, ""):
                try:
                    parsed = float(value)
                except (TypeError, ValueError):
                    continue
                return max(1.0, min(parsed, 300.0))
        return default

    def _interactive_auth_enabled(self, source: str) -> bool:
        value = self.config.get(
            f"{source}_interactive_auth",
            self.config.get("source_interactive_auth", False),
        )
        if isinstance(value, str):
            return value.strip().lower() not in {"", "0", "false", "no", "off"}
        return bool(value)


