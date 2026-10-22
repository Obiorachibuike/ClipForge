"""Stale-job recovery: crash recovery must not duplicate live work."""
from __future__ import annotations

from datetime import timedelta

from app.models import Job, JobStatus, JobType, utcnow
from app.services import queue as queue_module
from app.services.job_service import JobService


def _running_job(db, *, started_hours_ago: float, type_=JobType.VIDEO_PROBE.value) -> Job:
    job = JobService.create(db, type_=type_, payload={"video_id": "v"}, enqueue=False)
    job.status = JobStatus.RUNNING.value
    job.stage = "transcription"
    job.attempts = 1
    job.started_at = utcnow() - timedelta(hours=started_hours_ago)
    db.commit()
    return job


def _memory_queue(monkeypatch) -> queue_module.InMemoryJobQueue:
    memory = queue_module.InMemoryJobQueue()
    monkeypatch.setattr(queue_module, "_queue", memory)
    return memory


def test_stale_job_is_requeued_but_a_fresh_worker_heartbeat_is_respected(db, monkeypatch):
    memory = _memory_queue(monkeypatch)
    abandoned = _running_job(db, started_hours_ago=3)
    alive = _running_job(db, started_hours_ago=3)

    # A live worker holds a fresh claim (and keeps touching it via heartbeats).
    memory.enqueue(alive.id)
    assert memory.claim("live-worker", timeout=0.5) == alive.id

    JobService.recover_stale(db, older_than_minutes=60)

    assert memory.is_queued(abandoned.id)
    assert not memory.is_queued(alive.id), "a job with a live heartbeat must not be duplicated"
    db.refresh(abandoned)
    db.refresh(alive)
    assert abandoned.status == JobStatus.QUEUED.value
    assert alive.status == JobStatus.RUNNING.value


def test_jobs_owned_by_this_worker_are_never_recovered_by_its_own_pass(db, monkeypatch):
    memory = _memory_queue(monkeypatch)
    job = _running_job(db, started_hours_ago=3)

    JobService.recover_stale(db, older_than_minutes=60, active_ids={job.id})

    assert not memory.is_queued(job.id)


def test_a_running_job_out_of_retries_is_failed_not_stranded(db, monkeypatch):
    _memory_queue(monkeypatch)
    job = _running_job(db, started_hours_ago=3)
    job.max_attempts = 1
    db.commit()

    JobService.recover_stale(db, older_than_minutes=60)

    db.refresh(job)
    assert job.status == JobStatus.FAILED.value
    assert job.error_code == "worker_lost"


def test_worker_reconciles_at_the_configured_interval_not_on_every_loop(db, monkeypatch):
    from app.core.config import settings
    from app.workers.worker import Worker

    memory = _memory_queue(monkeypatch)
    monkeypatch.setattr(settings, "worker_reconcile_seconds", 60.0)
    calls: list[int] = []
    monkeypatch.setattr(JobService, "recover_stale", staticmethod(lambda *a, **k: calls.append(1) or 0))
    monkeypatch.setattr(JobService, "recover_unqueued", staticmethod(lambda *a, **k: calls.append(2) or 0))

    worker = Worker(concurrency=1, name="reconcile-test")
    assert worker.queue is memory

    worker._reconcile(force=True)
    assert len(calls) == 2  # stale + unqueued, once

    worker._reconcile()  # too soon
    assert len(calls) == 2

    worker._last_reconcile = 0.0  # interval elapsed
    worker._reconcile()
    assert len(calls) == 4


def test_periodic_reconciliation_actually_requeues_an_abandoned_job(db, monkeypatch):
    from app.core.config import settings
    from app.workers.worker import Worker

    memory = _memory_queue(monkeypatch)
    monkeypatch.setattr(settings, "worker_reconcile_seconds", 0.0)
    job = _running_job(db, started_hours_ago=5)

    worker = Worker(concurrency=1, name="reconcile-test")
    worker._reconcile()  # interval 0 disables the background pass
    assert not memory.is_queued(job.id)

    worker._reconcile(force=True)
    assert memory.is_queued(job.id)
