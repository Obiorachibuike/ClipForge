"""Job lifecycle: create -> queue -> run -> progress -> terminal state.

All state changes are persisted (so a browser refresh can resynchronise) and
mirrored as WebSocket events (so the UI updates live).
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError, CancelledError_, NotFoundError, user_message
from app.core.logging import get_logger
from app.models import Job, JobStatus, utcnow
from app.services import events as ev
from app.services.queue import get_queue
from app.services.runtime import registry

log = get_logger(__name__)

PROGRESS_MIN_INTERVAL = 0.6  # seconds between persisted progress writes
PROGRESS_MIN_DELTA = 0.5  # percentage points


def _now() -> datetime:
    return datetime.now(UTC)


class JobService:
    """Stateless helper namespace over the `jobs` table + queue."""

    # ------------------------------------------------------------- create ---
    @staticmethod
    def create(
        db: Session,
        *,
        type_: str,
        payload: dict[str, Any] | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
        video_id: str | None = None,
        clip_id: str | None = None,
        priority: int = 100,
        max_attempts: int | None = None,
        enqueue: bool = True,
    ) -> Job:
        job = Job(
            type=type_,
            status=JobStatus.QUEUED.value,
            stage="queued",
            progress=0.0,
            message="Queued",
            payload=payload or {},
            user_id=user_id,
            project_id=project_id,
            video_id=video_id,
            clip_id=clip_id,
            priority=priority,
            max_attempts=max_attempts or settings.worker_max_attempts,
            queued_at=_now(),
            run_after=_now(),
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        if enqueue:
            get_queue().enqueue(job.id, priority=priority)
        ev.get_event_bus().emit(
            ev.EVENT_JOB_CREATED,
            user_id=user_id,
            project_id=project_id,
            job_id=job.id,
            payload=JobService.serialize(job),
        )
        log.info("job.created", job_id=job.id, type=job.type, project_id=project_id)
        return job

    @staticmethod
    def serialize(job: Job) -> dict[str, Any]:
        return {
            "id": job.id,
            "type": job.type,
            "status": job.status,
            "stage": job.stage,
            "progress": round(float(job.progress or 0.0), 2),
            "message": job.message or "",
            "project_id": job.project_id,
            "video_id": job.video_id,
            "clip_id": job.clip_id,
            "user_id": job.user_id,
            "attempts": job.attempts,
            "max_attempts": job.max_attempts,
            "error_code": job.error_code or "",
            "error_message": job.error_message or "",
            "result": job.result or {},
            "steps": job.steps or [],
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "queued_at": job.queued_at.isoformat() if job.queued_at else None,
            "cancel_requested": bool(job.cancel_requested),
        }

    # ----------------------------------------------------------- lifecycle ---
    @staticmethod
    def mark_started(db: Session, job: Job, worker_id: str) -> Job:
        job.status = JobStatus.RUNNING.value
        job.stage = "starting"
        job.progress = max(1.0, float(job.progress or 0))
        job.message = "Starting"
        job.worker_id = worker_id
        job.started_at = _now()
        job.attempts = int(job.attempts or 0) + 1
        db.commit()
        registry.token(job.id)
        ev.get_event_bus().emit(
            ev.EVENT_JOB_STARTED,
            user_id=job.user_id,
            project_id=job.project_id,
            job_id=job.id,
            payload=JobService.serialize(job),
        )
        return job

    @staticmethod
    def report_progress(
        db: Session,
        job: Job,
        *,
        stage: str | None = None,
        progress: float | None = None,
        message: str | None = None,
        force: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Persist + broadcast progress. Called from worker threads."""
        previous = float(job.progress or 0.0)
        last_write = getattr(job, "_last_progress_write", 0.0)
        stage_changed = stage is not None and stage != job.stage
        new_progress = previous if progress is None else max(previous, min(100.0, float(progress)))
        delta = abs(new_progress - previous)

        if stage:
            job.stage = stage
        if message is not None:
            job.message = message[:255]
        if force or stage_changed or delta >= PROGRESS_MIN_DELTA or time.time() - last_write >= 2.0:
            job.progress = new_progress
            db.commit()
            job._last_progress_write = time.time()  # type: ignore[attr-defined]
        else:
            job.progress = new_progress

        payload = {
            "job_id": job.id,
            "type": job.type,
            "stage": job.stage,
            "progress": round(new_progress, 2),
            "message": job.message,
            "project_id": job.project_id,
        }
        if extra:
            payload.update(extra)
        ev.get_event_bus().emit(
            ev.EVENT_JOB_PROGRESS,
            user_id=job.user_id,
            project_id=job.project_id,
            job_id=job.id,
            payload=payload,
        )
        if job.type == "clip.render":
            ev.get_event_bus().emit(
                ev.EVENT_RENDER_PROGRESS,
                user_id=job.user_id,
                project_id=job.project_id,
                job_id=job.id,
                payload=payload,
            )
        registry.token(job.id).raise_if_cancelled()

    @staticmethod
    def mark_succeeded(db: Session, job: Job, result: dict[str, Any] | None = None) -> Job:
        job.status = JobStatus.SUCCEEDED.value
        job.progress = 100.0
        job.stage = "completed"
        job.message = "Completed"
        job.result = result or {}
        job.finished_at = _now()
        job.duration_seconds = _duration(job)
        db.commit()
        ev.get_event_bus().emit(
            ev.EVENT_JOB_COMPLETED,
            user_id=job.user_id,
            project_id=job.project_id,
            job_id=job.id,
            payload=JobService.serialize(job),
        )
        log.info("job.completed", job_id=job.id, type=job.type, seconds=round(job.duration_seconds, 1))
        registry.clear(job.id)
        return job

    @staticmethod
    def mark_failed(db: Session, job: Job, exc: BaseException) -> Job:
        app_error = exc if isinstance(exc, AppError) else None
        job.status = JobStatus.FAILED.value
        job.stage = "failed"
        job.error_code = app_error.code if app_error else "internal_error"
        job.error_message = user_message(exc)
        job.error_detail = f"{type(exc).__name__}: {exc}"[:8000]
        job.finished_at = _now()
        job.duration_seconds = _duration(job)
        db.commit()
        ev.get_event_bus().emit(
            ev.EVENT_JOB_FAILED,
            user_id=job.user_id,
            project_id=job.project_id,
            job_id=job.id,
            payload=JobService.serialize(job),
        )
        log.error("job.failed", job_id=job.id, type=job.type, code=job.error_code, error=str(exc)[:500])
        registry.clear(job.id)
        return job

    @staticmethod
    def mark_cancelled(db: Session, job: Job, message: str = "Cancelled by user") -> Job:
        job.status = JobStatus.CANCELED.value
        job.stage = "cancelled"
        job.message = message
        job.finished_at = _now()
        job.duration_seconds = _duration(job)
        db.commit()
        ev.get_event_bus().emit(
            ev.EVENT_JOB_CANCELLED,
            user_id=job.user_id,
            project_id=job.project_id,
            job_id=job.id,
            payload=JobService.serialize(job),
        )
        log.info("job.cancelled", job_id=job.id, type=job.type)
        registry.clear(job.id)
        return job

    @staticmethod
    def schedule_retry(db: Session, job: Job, exc: BaseException, delay: float | None = None) -> bool:
        """Requeue when attempts remain. Returns True when a retry was queued."""
        if int(job.attempts or 0) >= int(job.max_attempts or 1):
            return False
        backoff = delay if delay is not None else settings.worker_backoff_seconds * (2 ** max(0, job.attempts - 1))
        job.status = JobStatus.RETRYING.value
        job.stage = "retrying"
        job.message = f"Retrying in {int(backoff)}s"
        job.error_code = exc.code if isinstance(exc, AppError) else "internal_error"
        job.error_message = user_message(exc)
        job.run_after = _now() + timedelta(seconds=backoff)
        db.commit()
        get_queue().enqueue(job.id, priority=job.priority, delay_seconds=backoff)
        ev.get_event_bus().emit(
            ev.EVENT_JOB_RETRYING,
            user_id=job.user_id,
            project_id=job.project_id,
            job_id=job.id,
            payload={**JobService.serialize(job), "retry_in_seconds": backoff},
        )
        log.warning("job.retry_scheduled", job_id=job.id, attempt=job.attempts, in_seconds=backoff)
        return True

    # -------------------------------------------------------------- cancel ---
    @staticmethod
    def request_cancel(db: Session, job: Job) -> Job:
        job.cancel_requested = True
        db.commit()
        get_queue().cancel(job.id)
        registry.request_cancel(job.id)
        if job.status in (JobStatus.QUEUED.value, JobStatus.RETRYING.value):
            # Not running yet: retire it immediately.
            JobService.mark_cancelled(db, job)
        else:
            ev.get_event_bus().emit(
                ev.EVENT_JOB_PROGRESS,
                user_id=job.user_id,
                project_id=job.project_id,
                job_id=job.id,
                payload={**JobService.serialize(job), "message": "Cancelling…"},
            )
        return job

    @staticmethod
    def cancel_requested(db: Session, job_id: str) -> bool:
        job = db.get(Job, job_id)
        if job is None:
            return False
        return bool(job.cancel_requested) or registry.is_cancelled(job_id) or get_queue().is_cancelled(job_id)

    @staticmethod
    def check_cancelled(db: Session, job: Job) -> None:
        """Raise when cancellation was requested for this job."""
        if registry.is_cancelled(job.id):
            raise CancelledError_()
        if job.cancel_requested:
            raise CancelledError_()
        if get_queue().is_cancelled(job.id):
            raise CancelledError_()

    # ------------------------------------------------------------- queries ---
    @staticmethod
    def get(db: Session, job_id: str, user_id: str | None = None) -> Job:
        job = db.get(Job, job_id)
        if job is None:
            raise NotFoundError("Job not found.", code="job_not_found")
        if user_id is not None and job.user_id not in (None, user_id):
            raise NotFoundError("Job not found.", code="job_not_found")
        return job

    @staticmethod
    def active_for_user(db: Session, user_id: str, project_id: str | None = None) -> list[Job]:
        stmt = select(Job).where(
            Job.user_id == user_id,
            Job.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value, JobStatus.RETRYING.value]),
        )
        if project_id:
            stmt = stmt.where(Job.project_id == project_id)
        return list(db.execute(stmt.order_by(Job.created_at.desc())).scalars())

    @staticmethod
    def recent_for_project(db: Session, project_id: str, limit: int = 30) -> list[Job]:
        stmt = select(Job).where(Job.project_id == project_id).order_by(Job.created_at.desc()).limit(limit)
        return list(db.execute(stmt).scalars())

    @staticmethod
    def stats(db: Session) -> dict[str, Any]:
        rows = db.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)).all()
        counts = {status: int(count) for status, count in rows}
        queue = get_queue()
        return {
            "by_status": counts,
            "queue_depth": queue.depth(),
            "active": len(queue.active_jobs()),
            "queue_backend": queue.backend_name(),
        }

    @staticmethod
    def recover_stale(db: Session, older_than_minutes: int | None = None) -> int:
        """Requeue or fail jobs left running by a crashed worker."""
        threshold = _now() - timedelta(minutes=older_than_minutes or settings.worker_job_timeout_minutes)
        stmt = select(Job).where(
            Job.status.in_([JobStatus.RUNNING.value, JobStatus.RETRYING.value]),
            or_(Job.started_at.is_(None), Job.started_at < threshold),
        )
        requeued = 0
        for job in db.execute(stmt).scalars():
            if job.status == JobStatus.RETRYING.value:
                continue
            if int(job.attempts or 0) < int(job.max_attempts or 1):
                job.status = JobStatus.QUEUED.value
                job.stage = "queued"
                job.message = "Recovered after worker restart"
                db.commit()
                get_queue().enqueue(job.id, priority=job.priority)
                requeued += 1
                log.warning("job.recovered", job_id=job.id, type=job.type)
            else:
                JobService.mark_failed(db, job, AppError("Processing stopped unexpectedly.", code="worker_lost"))
        return requeued


def _duration(job: Job) -> float:
    if job.started_at and job.finished_at:
        return max(0.0, (job.finished_at - job.started_at).total_seconds())
    return 0.0
