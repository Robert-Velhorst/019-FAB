from __future__ import annotations

import threading

from src.operations.local_ledger import LocalOperationsLedger


class RuntimeLeaseHeartbeat:
    """Keep a bounded runtime lease alive while its owner performs slow work."""

    def __init__(
        self,
        ledger: LocalOperationsLedger,
        lease_name: str,
        owner_token: str,
        ttl_seconds: float,
        *,
        thread_name: str = "fab-runtime-lease-heartbeat",
        lost_error: str = "runtime_lease_lost",
        heartbeat_error: str = "runtime_lease_heartbeat_failed",
    ):
        self.ledger = ledger
        self.lease_name = lease_name
        self.owner_token = owner_token
        self.ttl_seconds = max(float(ttl_seconds), 1.0)
        self.interval_seconds = max(0.01, min(self.ttl_seconds / 3, 30.0))
        self.lost_error = lost_error
        self.heartbeat_error = heartbeat_error
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=thread_name,
            daemon=True,
        )

    def __enter__(self) -> RuntimeLeaseHeartbeat:
        self._thread.start()
        return self

    def __exit__(self, *_exc_info) -> None:
        self._stop.set()
        self._thread.join()

    def _renew(self) -> bool:
        renewed = self.ledger.heartbeat_runtime_lease(
            self.lease_name,
            self.owner_token,
            ttl_seconds=self.ttl_seconds,
        )
        if not renewed:
            self._lost.set()
        return renewed

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                if not self._renew():
                    return
            except Exception:
                self._lost.set()
                return

    def assert_owned(self) -> None:
        if self._lost.is_set():
            raise RuntimeError(self.lost_error)
        try:
            if self._renew():
                return
        except Exception as exc:
            raise RuntimeError(self.heartbeat_error) from exc
        raise RuntimeError(self.lost_error)
