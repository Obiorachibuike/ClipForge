"""Job inspection and control.

The frontend resynchronises from these endpoints after a WebSocket reconnect or
a page refresh, so job state is never only in the socket.
"""
from __future__ import annotations

from fastapi import APIRouter, Query, status
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession, PageParams, owned_job
from app.core.errors import ConflictError
from app.core.logging import get_logger
from app.models import Job, JobStatus
from app.schemas.common import OkResponse, Page
from app.schemas.media import JobOut
from app.services.job_service import JobService

log = get_logger(__name__)
router = APIRouter(tags=["Jobs"])


@router.get("/jobs", response_model=Page[JobOut])
def list_jobs(
    db: DbSession,
    user: CurrentUser,
    params: PageParams,
    status_filter: str | None = Query(default=None, alias="status"),
    project_id: str | None = Query(default=None),
    active_only: bool = Query(default=False),
) -> Page[JobOut]:
    stmt = select(Job).where(Job.user_id == user.id)
    count_stmt = select(func.count(Job.id)).where(Job.user_id == user.id)
    if status_filter:
        stmt = stmt.where(Job.status == status_filter)
        count_stmt = count_stmt.where(Job.status == status_filter)
    if project_id:
        stmt = stmt.where(Job.project_id == project_id)
        count_stmt = count_stmt.where(Job.project_id == project_id)
    if active_only:
        active = [JobStatus.QUEUED.value, JobStatus.RUNNING.value, JobStatus.RETRYING.value]
        stmt = stmt.where(Job.status.in_(active))
        count_stmt = count_stmt.where(Job.status.in_(active))
    total = int(db.execute(count_stmt).scalar_one())
    rows = list(db.execute(stmt.order_by(Job.created_at.desc()).limit(params.limit).offset(params.offset)).scalars())
    return Page.build([JobOut.model_validate(row) for row in rows], total, params.limit, params.offset)


@router.get("/jobs/active", response_model=list[JobOut])
def active_jobs(db: DbSession, user: CurrentUser, project_id: str | None = Query(default=None)) -> list[JobOut]:
    """Everything in flight for this user - used to restore UI state on load."""
    jobs = JobService.active_for_user(db, user.id, project_id)
    return [JobOut.model_validate(job) for job in jobs]


@router.get("/jobs/{job_id}", response_model=JobOut)
def read_job(job_id: str, db: DbSession, user: CurrentUser) -> JobOut:
    job = owned_job(db, user, job_id)
    return JobOut.model_validate(job)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: str, db: DbSession, user: CurrentUser) -> JobOut:
    job = owned_job(db, user, job_id)
    if job.status not in (JobStatus.QUEUED.value, JobStatus.RUNNING.value, JobStatus.RETRYING.value):
        raise ConflictError("This job has already finished.", code="job_finished")
    JobService.request_cancel(db, job)
    db.refresh(job)
    return JobOut.model_validate(job)


@router.post("/jobs/{job_id}/retry", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def retry_job(job_id: str, db: DbSession, user: CurrentUser) -> JobOut:
    job = owned_job(db, user, job_id)
    if job.status not in (JobStatus.FAILED.value, JobStatus.CANCELED.value):
        raise ConflictError("Only failed or cancelled jobs can be retried.", code="job_not_retryable")
    from app.services.queue import get_queue

    job.status = JobStatus.QUEUED.value
    job.stage = "queued"
    job.message = "Retrying"
    job.progress = 0.0
    job.error_code = ""
    job.error_message = ""
    job.error_detail = ""
    job.cancel_requested = False
    job.attempts = 0
    job.worker_id = ""
    job.finished_at = None
    db.commit()
    get_queue().clear_cancel(job.id)
    get_queue().enqueue(job.id, priority=job.priority)
    log.info("job.manual_retry", job_id=job.id, type=job.type)
    return JobOut.model_validate(job)


@router.delete("/jobs/{job_id}", response_model=OkResponse)
def delete_job(job_id: str, db: DbSession, user: CurrentUser) -> OkResponse:
    job = owned_job(db, user, job_id)
    if job.status in (JobStatus.QUEUED.value, JobStatus.RUNNING.value, JobStatus.RETRYING.value):
        raise ConflictError("Cancel the job before deleting it.", code="job_active")
    db.delete(job)
    db.commit()
    return OkResponse(message="Job removed from history.")
