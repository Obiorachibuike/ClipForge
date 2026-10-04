"""Project endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Query, status
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession, PageParams, owned_project
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models import (
    AnalysisKind,
    Clip,
    ClipCandidate,
    JobStatus,
    JobType,
    Project,
    ProjectStatus,
    RenderJob,
    Transcript,
    TranscriptStatus,
    User,
    Video,
    VideoAnalysis,
    VideoStatus,
    utcnow,
)
from app.schemas.common import OkResponse, Page
from app.schemas.media import (
    JobOut,
    ProjectCreate,
    ProjectOut,
    ProjectSummary,
    ProjectUpdate,
    TranscriptOut,
    VideoOut,
)
from app.services import events as ev
from app.services.job_service import JobService
from app.services.storage import get_storage

log = get_logger(__name__)
router = APIRouter(prefix="/projects", tags=["Projects"])


@router.get("", response_model=Page[ProjectSummary])
def list_projects(
    db: DbSession,
    user: CurrentUser,
    params: PageParams,
    status_filter: str | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None, max_length=120),
    include_archived: bool = Query(default=False),
) -> Page[ProjectSummary]:
    stmt = select(Project).where(Project.user_id == user.id)
    count_stmt = select(func.count(Project.id)).where(Project.user_id == user.id)
    if status_filter:
        stmt = stmt.where(Project.status == status_filter)
        count_stmt = count_stmt.where(Project.status == status_filter)
    if not include_archived:
        stmt = stmt.where(Project.archived_at.is_(None))
        count_stmt = count_stmt.where(Project.archived_at.is_(None))
    if search:
        pattern = f"%{search.lower()}%"
        stmt = stmt.where(func.lower(Project.name).like(pattern))
        count_stmt = count_stmt.where(func.lower(Project.name).like(pattern))

    total = int(db.execute(count_stmt).scalar_one())
    rows = list(
        db.execute(stmt.order_by(Project.updated_at.desc()).limit(params.limit).offset(params.offset)).scalars()
    )
    items = [_summary(db, project) for project in rows]
    return Page.build(items, total, params.limit, params.offset)


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreate, db: DbSession, user: CurrentUser) -> ProjectOut:
    project = Project(
        user_id=user.id,
        name=payload.name,
        description=payload.description,
        target_aspect_ratio=payload.target_aspect_ratio,
        privacy_mode=payload.privacy_mode,
        settings=payload.settings,
        status=ProjectStatus.DRAFT.value,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    log.info("project.created", project_id=project.id, user_id=user.id)
    return ProjectOut.model_validate(project)


@router.get("/{project_id}", response_model=ProjectSummary)
def read_project(project_id: str, db: DbSession, user: CurrentUser) -> ProjectSummary:
    project = owned_project(db, user, project_id)
    return _summary(db, project)


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(project_id: str, payload: ProjectUpdate, db: DbSession, user: CurrentUser) -> ProjectOut:
    project = owned_project(db, user, project_id)
    data = payload.model_dump(exclude_unset=True)
    if "status" in data and data["status"] not in {s.value for s in ProjectStatus}:
        raise ValidationError("Unknown project status.", field="status")
    for key, value in data.items():
        setattr(project, key, value)
    db.commit()
    db.refresh(project)
    return ProjectOut.model_validate(project)


@router.delete("/{project_id}", response_model=OkResponse)
def delete_project(project_id: str, db: DbSession, user: CurrentUser) -> OkResponse:
    project = owned_project(db, user, project_id)
    storage = get_storage()
    try:
        storage.delete_prefix(f"projects/{project.id}")
    except Exception as exc:  # storage hiccups must not block deletion
        log.warning("project.storage_cleanup_failed", project_id=project.id, error=str(exc))
    db.delete(project)
    db.commit()
    log.info("project.deleted", project_id=project_id)
    return OkResponse(message="Project deleted.")


@router.post("/{project_id}/archive", response_model=ProjectOut)
def archive_project(project_id: str, db: DbSession, user: CurrentUser) -> ProjectOut:
    project = owned_project(db, user, project_id)
    project.archived_at = utcnow()
    project.status = ProjectStatus.ARCHIVED.value
    db.commit()
    return ProjectOut.model_validate(project)


@router.post("/{project_id}/restore", response_model=ProjectOut)
def restore_project(project_id: str, db: DbSession, user: CurrentUser) -> ProjectOut:
    project = owned_project(db, user, project_id)
    project.archived_at = None
    project.status = ProjectStatus.READY.value
    db.commit()
    return ProjectOut.model_validate(project)


@router.post("/{project_id}/discover", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def discover_clips(project_id: str, db: DbSession, user: CurrentUser, body: dict | None = None) -> JobOut:
    """Queue the clip discovery pipeline for this project."""
    project = owned_project(db, user, project_id)
    video = db.execute(
        select(Video).where(Video.project_id == project.id).order_by(Video.created_at.desc())
    ).scalars().first()
    if video is None:
        raise NotFoundError("Upload a video before discovering clips.", code="video_required")
    if video.status not in (VideoStatus.READY.value,):
        raise ConflictError("The video is still being processed.", code="video_not_ready")
    transcript = db.execute(
        select(Transcript)
        .where(Transcript.video_id == video.id, Transcript.status == TranscriptStatus.COMPLETED.value)
        .order_by(Transcript.created_at.desc())
    ).scalars().first()
    if transcript is None:
        raise ConflictError("Transcribe the video first.", code="transcript_required")

    active_jobs = JobService.active_for_user(db, user.id, project.id)
    if any(job.type == JobType.PROJECT_DISCOVER_CLIPS.value for job in active_jobs):
        raise ConflictError("Clip discovery is already running for this project.", code="discovery_running")

    payload = {"project_id": project.id, "video_id": video.id, **(body or {})}
    project.status = ProjectStatus.PROCESSING.value
    db.commit()
    job = JobService.create(
        db,
        type_=JobType.PROJECT_DISCOVER_CLIPS.value,
        payload=payload,
        user_id=user.id,
        project_id=project.id,
        video_id=video.id,
        priority=30,
    )
    return JobOut.model_validate(job)


@router.get("/{project_id}/jobs", response_model=list[JobOut])
def project_jobs(project_id: str, db: DbSession, user: CurrentUser, limit: int = 30) -> list[JobOut]:
    owned_project(db, user, project_id)
    jobs = JobService.recent_for_project(db, project_id, limit=max(1, min(100, limit)))
    return [JobOut.model_validate(job) for job in jobs]


@router.get("/{project_id}/transcript", response_model=TranscriptOut | None)
def project_transcript(project_id: str, db: DbSession, user: CurrentUser) -> TranscriptOut | None:
    project = owned_project(db, user, project_id)
    video = db.execute(
        select(Video).where(Video.project_id == project.id).order_by(Video.created_at.desc())
    ).scalars().first()
    if video is None:
        return None
    transcript = db.execute(
        select(Transcript).where(Transcript.video_id == video.id).order_by(Transcript.created_at.desc())
    ).scalars().first()
    return TranscriptOut.model_validate(transcript) if transcript else None


@router.get("/{project_id}/framing")
def project_framing(project_id: str, db: DbSession, user: CurrentUser) -> dict:
    """Framing plan for the project's latest video (used by the preview layer)."""
    project = owned_project(db, user, project_id)
    video = db.execute(
        select(Video).where(Video.project_id == project.id).order_by(Video.created_at.desc())
    ).scalars().first()
    if video is None:
        return {"available": False, "reason": "no_video"}
    row = db.execute(
        select(VideoAnalysis)
        .where(VideoAnalysis.video_id == video.id, VideoAnalysis.kind == AnalysisKind.FRAMING.value)
        .order_by(VideoAnalysis.created_at.desc())
    ).scalars().first()
    if row is None:
        return {"available": False, "reason": "not_analyzed", "video_id": video.id}
    return {
        "available": row.status == "completed",
        "status": row.status,
        "video_id": video.id,
        "detector": row.detector,
        "error": row.error_message,
        **(row.data or {}),
    }


# ------------------------------------------------------------------ helpers ---
def _summary(db: DbSession, project: Project) -> ProjectSummary:
    video = db.execute(
        select(Video).where(Video.project_id == project.id).order_by(Video.created_at.desc())
    ).scalars().first()
    counts = {
        "video_count": int(db.execute(select(func.count(Video.id)).where(Video.project_id == project.id)).scalar_one()),
        "clip_count": int(db.execute(select(func.count(Clip.id)).where(Clip.project_id == project.id)).scalar_one()),
        "candidate_count": int(
            db.execute(select(func.count(ClipCandidate.id)).where(ClipCandidate.project_id == project.id)).scalar_one()
        ),
        "render_count": int(
            db.execute(select(func.count(RenderJob.id)).where(RenderJob.project_id == project.id)).scalar_one()
        ),
        "total_duration_seconds": float(
            db.execute(
                select(func.coalesce(func.sum(Video.duration_seconds), 0.0)).where(Video.project_id == project.id)
            ).scalar_one()
        ),
    }
    active_jobs = [
        JobOut.model_validate(job)
        for job in JobService.active_for_user(db, project.user_id, project.id)
    ]
    progress = 0.0
    stage = ""
    if active_jobs:
        job = active_jobs[0]
        progress = float(job.progress or 0.0)
        stage = job.stage or ""
    elif project.status == ProjectStatus.READY.value:
        progress = 100.0
        stage = "ready"
    return ProjectSummary(
        **ProjectOut.model_validate(project).model_dump(),
        **counts,
        latest_video=VideoOut.model_validate(video) if video else None,
        active_jobs=active_jobs,
        progress=progress,
        stage=stage,
    )
