"""Background worker.

Pulls jobs from the queue, executes them in a thread pool (media work releases
the GIL inside FFmpeg/NumPy, so threads are the right tool here), reports
progress over the event bus, honours cancellation, retries with backoff and
heartbeats so operators can see which worker owns which job.
"""
from __future__ import annotations

import os
import signal
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_session_factory, init_engine_if_needed
from app.core.errors import AppError, CancelledError_
from app.core.logging import configure_logging, get_logger
from app.models import Job, JobStatus
from app.services.events import get_event_bus
from app.services.job_service import JobService
from app.services.queue import get_queue
from app.services.runtime import registry
from app.workers.handlers import handler_for

log = get_logger(__name__)


class Worker:
    def __init__(self, *, concurrency: int | None = None, name: str | None = None) -> None:
        self.concurrency = max(1, int(concurrency or settings.worker_concurrency))
        self.worker_id = name or f"{socket.gethostname()}-{os.getpid()}"
        self._stop = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="clipforge-job")
        self._running: set[str] = set()
        self._running_lock = threading.Lock()
        self._heartbeat_thread: threading.Thread | None = None
        self.queue = get_queue()

    # ------------------------------------------------------------- control ---
    def start(self) -> None:
        configure_logging()
        log.info("worker.starting", worker_id=self.worker_id, concurrency=self.concurrency, queue=self.queue.backend_name())
        self._recover()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, name="worker-heartbeat", daemon=True)
        self._heartbeat_thread.start()
        try:
            self.run_forever()
        finally:
            self.shutdown()

    def install_signal_handlers(self) -> None:
        def _handler(signum, _frame):  # noqa: ANN001
            log.info("worker.signal", signal=signum)
            self._stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except ValueError:  # pragma: no cover - non-main thread
                pass

    def shutdown(self) -> None:
        log.info("worker.stopping", worker_id=self.worker_id)
        self._stop.set()
        self._executor.shutdown(wait=True, cancel_futures=False)
        get_event_bus().shutdown()

    # ---------------------------------------------------------------- loop ---
    def run_forever(self) -> None:
        while not self._stop.is_set():
            with self._running_lock:
                capacity = self.concurrency - len(self._running)
            if capacity <= 0:
                time.sleep(0.1)
                continue
            job_id = self.queue.claim(self.worker_id, timeout=min(1.0, settings.worker_poll_interval + 0.2))
            if job_id is None:
                continue
            with self._running_lock:
                self._running.add(job_id)
            self._executor.submit(self._run_job_safely, job_id)

    def run_once(self, timeout: float = 1.0) -> bool:
        """Execute a single claimed job synchronously (used by tests)."""
        job_id = self.queue.claim(self.worker_id, timeout=timeout)
        if job_id is None:
            return False
        self._run_job_safely(job_id)
        return True

    # ----------------------------------------------------------- execution ---
    def _run_job_safely(self, job_id: str) -> None:
        session_factory = get_session_factory()
        db: Session = session_factory()
        try:
            job = db.get(Job, job_id)
            if job is None:
                log.warning("worker.job_missing", job_id=job_id)
                self.queue.ack(job_id)
                return
            self._execute(db, job)
        except Exception as exc:  # pragma: no cover - last-resort safety net
            log.exception("worker.unhandled", job_id=job_id, error=str(exc))
        finally:
            db.close()
            with self._running_lock:
                self._running.discard(job_id)
            self.queue.ack(job_id)
            registry.clear(job_id)

    def _execute(self, db: Session, job: Job) -> None:
        started = time.time()
        if job.status == JobStatus.CANCELED.value or self.queue.is_cancelled(job.id):
            JobService.mark_cancelled(db, job, "Cancelled before starting")
            return

        try:
            handler = handler_for(job.type)
        except AppError as exc:
            JobService.mark_failed(db, job, exc)
            return

        JobService.mark_started(db, job, self.worker_id)
        self.queue.touch(job.id, self.worker_id)
        log.info("worker.job_started", job_id=job.id, type=job.type, attempt=job.attempts)

        try:
            result = handler(db, job) or {}
            if job.cancel_requested or self.queue.is_cancelled(job.id):
                JobService.mark_cancelled(db, job, "Cancelled")
                return
            JobService.mark_succeeded(db, job, result=result)
            log.info("worker.job_finished", job_id=job.id, type=job.type, seconds=round(time.time() - started, 1))
        except CancelledError_:
            JobService.mark_cancelled(db, job, "Cancelled by user")
        except AppError as exc:
            if exc.retryable and JobService.schedule_retry(db, job, exc):
                return
            JobService.mark_failed(db, job, exc)
        except Exception as exc:  # noqa: BLE001 - convert to a safe failure
            log.exception("worker.job_crashed", job_id=job.id, type=job.type)
            if JobService.schedule_retry(db, job, AppError("Processing failed.", code="internal_error")):
                return
            JobService.mark_failed(db, job, AppError("Processing failed. Our team has the details.", code="internal_error"))

    # ------------------------------------------------------------ recovery ---
    def _recover(self) -> None:
        """Requeue jobs interrupted by a previous crash/restart."""
        db = get_session_factory()()
        try:
            recovered = JobService.recover_stale(db)
            if recovered:
                log.info("worker.recovered_jobs", count=recovered)
        except Exception as exc:  # pragma: no cover
            log.warning("worker.recovery_failed", error=str(exc))
        finally:
            db.close()

    # ------------------------------------------------------------ heartbeat ---
    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                with self._running_lock:
                    active = list(self._running)
                for job_id in active:
                    self.queue.touch(job_id, self.worker_id)
            except Exception:  # pragma: no cover
                pass
            self._stop.wait(settings.heartbeat_seconds)

    # ---------------------------------------------------------------- info ---
    def stats(self) -> dict[str, Any]:
        with self._running_lock:
            running = sorted(self._running)
        return {
            "worker_id": self.worker_id,
            "concurrency": self.concurrency,
            "running": running,
            "queue_depth": self.queue.depth(),
            "backend": self.queue.backend_name(),
        }


def run_worker(concurrency: int | None = None) -> None:
    init_engine_if_needed()
    worker = Worker(concurrency=concurrency)
    worker.install_signal_handlers()
    worker.start()


def main() -> None:  # pragma: no cover - process entry point
    import argparse

    parser = argparse.ArgumentParser(description="ClipForge background worker")
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--name", type=str, default=None)
    args = parser.parse_args()
    init_engine_if_needed()
    worker = Worker(concurrency=args.concurrency, name=args.name)
    worker.install_signal_handlers()
    worker.start()


if __name__ == "__main__":  # pragma: no cover
    main()
