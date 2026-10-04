"""Run a worker inside the API process.

Enabled with WORKER_INLINE=true (the default in development and for single-box
deployments). Production deployments set WORKER_INLINE=false and scale `worker`
containers independently - the code path is identical.
"""
from __future__ import annotations

import threading

from app.core.logging import get_logger
from app.workers.worker import Worker

log = get_logger(__name__)

_worker: Worker | None = None
_thread: threading.Thread | None = None


def start_inline_worker(concurrency: int | None = None) -> Worker:
    global _worker, _thread
    if _worker is not None:
        return _worker
    _worker = Worker(concurrency=concurrency, name="inline-worker")
    _thread = threading.Thread(target=_worker.start, name="clipforge-inline-worker", daemon=True)
    _thread.start()
    log.info("worker.inline_started", concurrency=_worker.concurrency, queue=_worker.queue.backend_name())
    return _worker


def stop_inline_worker() -> None:
    global _worker, _thread
    if _worker is not None:
        _worker.shutdown()
    _worker = None
    _thread = None


def inline_worker_stats() -> dict | None:
    return _worker.stats() if _worker is not None else None
