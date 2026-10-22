"""Production queue configuration must fail closed instead of using process memory."""
from __future__ import annotations

import pytest
from app.services import queue


def test_in_memory_queue_does_not_add_the_same_job_twice():
    job_queue = queue.InMemoryJobQueue()
    job_queue.enqueue("job-1", priority=0)
    job_queue.enqueue("job-1", priority=1)

    assert job_queue.depth() == 1
    assert job_queue.is_queued("job-1")
    assert job_queue.claim("worker-1", timeout=0.05) == "job-1"
    assert not job_queue.is_queued("job-1")
    assert job_queue.is_active("job-1")


def test_worker_startup_reconciles_persisted_jobs_missing_from_queue(db, monkeypatch):
    from app.models import JobType
    from app.services.job_service import JobService

    memory_queue = queue.InMemoryJobQueue()
    monkeypatch.setattr(queue, "_queue", memory_queue)
    job = JobService.create(
        db,
        type_=JobType.VIDEO_PROBE.value,
        payload={"video_id": "missing-video"},
        enqueue=False,
    )

    assert not memory_queue.is_queued(job.id)
    assert JobService.recover_unqueued(db) >= 1
    assert memory_queue.is_queued(job.id)
    assert JobService.recover_unqueued(db) == 0


def test_explicit_redis_does_not_silently_fall_back_to_memory(monkeypatch):
    queue.set_queue(None)
    monkeypatch.setattr(queue.settings, "queue_backend", "redis")
    monkeypatch.setattr(queue, "get_redis", lambda: None)

    with pytest.raises(queue.QueueUnavailable):
        queue.get_queue()

    assert queue._queue is None


def test_production_auto_queue_requires_redis(monkeypatch):
    queue.set_queue(None)
    monkeypatch.setattr(queue.settings, "environment", "production")
    monkeypatch.setattr(queue.settings, "queue_backend", "auto")
    monkeypatch.setattr(queue, "get_redis", lambda: None)

    with pytest.raises(queue.QueueUnavailable):
        queue.get_queue()

    assert queue._queue is None
