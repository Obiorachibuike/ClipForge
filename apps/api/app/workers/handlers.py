"""Job type -> handler registry.

Adding a pipeline stage means registering a function here; the worker loop never
changes.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import JobError
from app.core.logging import get_logger
from app.models import Job, JobType

log = get_logger(__name__)

Handler = Callable[[Session, Job], dict[str, Any]]


def _transcribe(db: Session, job: Job) -> dict[str, Any]:
    from app.services.pipeline.transcribe import run

    return run(db, job)


def _import_url(db: Session, job: Job) -> dict[str, Any]:
    from app.services.remote_video import run_import

    return run_import(db, job)


def _probe(db: Session, job: Job) -> dict[str, Any]:
    from app.services.pipeline.video_jobs import run_probe

    return run_probe(db, job)


def _framing(db: Session, job: Job) -> dict[str, Any]:
    from app.services.pipeline.framing_job import run

    return run(db, job)


def _discover(db: Session, job: Job) -> dict[str, Any]:
    from app.services.pipeline.discover_job import run

    return run(db, job)


def _render(db: Session, job: Job) -> dict[str, Any]:
    from app.services.pipeline.render_job import run

    return run(db, job)


HANDLERS: dict[str, Handler] = {
    JobType.VIDEO_IMPORT_URL.value: _import_url,
    JobType.VIDEO_PROBE.value: _probe,
    JobType.VIDEO_TRANSCRIBE.value: _transcribe,
    JobType.VIDEO_ANALYZE_FRAMING.value: _framing,
    JobType.PROJECT_DISCOVER_CLIPS.value: _discover,
    JobType.CLIP_RENDER.value: _render,
}


def handler_for(job_type: str) -> Handler:
    handler = HANDLERS.get(job_type)
    if handler is None:
        raise JobError(f"No handler is registered for job type '{job_type}'.", code="unknown_job_type", retryable=False)
    return handler
