"""In-process runtime registry: cancellation tokens and child processes.

Cancellation must be able to reach a running FFmpeg process, which is only
possible inside the process that spawned it. The queue carries the durable
cancel flag; this registry carries the immediate one.
"""
from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, field

from app.core.errors import CancelledError_
from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass
class CancelToken:
    job_id: str
    event: threading.Event = field(default_factory=threading.Event)
    requested_at: float | None = None

    def request(self) -> None:
        self.requested_at = time.time()
        self.event.set()

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise CancelledError_()


class RuntimeRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, CancelToken] = {}
        self._processes: dict[str, list[subprocess.Popen]] = {}

    # ------------------------------------------------------------- tokens ---
    def token(self, job_id: str) -> CancelToken:
        with self._lock:
            token = self._tokens.get(job_id)
            if token is None:
                token = CancelToken(job_id)
                self._tokens[job_id] = token
            return token

    def request_cancel(self, job_id: str) -> bool:
        token = self.token(job_id)
        was_new = not token.cancelled
        token.request()
        terminated = self.terminate_processes(job_id)
        if was_new:
            log.info("runtime.cancel_requested", job_id=job_id, processes=terminated)
        return was_new

    def clear(self, job_id: str) -> None:
        with self._lock:
            self._tokens.pop(job_id, None)
            self._processes.pop(job_id, None)

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            token = self._tokens.get(job_id)
            return bool(token and token.cancelled)

    # ---------------------------------------------------------- processes ---
    def register_process(self, job_id: str, proc: subprocess.Popen) -> None:
        with self._lock:
            self._processes.setdefault(job_id, []).append(proc)

    def unregister_process(self, job_id: str, proc: subprocess.Popen) -> None:
        with self._lock:
            procs = self._processes.get(job_id)
            if procs and proc in procs:
                procs.remove(proc)

    def terminate_processes(self, job_id: str) -> int:
        with self._lock:
            procs = list(self._processes.get(job_id, []))
        killed = 0
        for proc in procs:
            if proc.poll() is None:
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    killed += 1
                except Exception as exc:  # pragma: no cover
                    log.warning("runtime.terminate_failed", job_id=job_id, error=str(exc))
        return killed


registry = RuntimeRegistry()
