"""Video ingestion jobs: metadata probing and poster frames."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app.core.errors import MediaError, NotFoundError
from app.core.logging import get_logger
from app.models import Job, Project, ProjectStatus, Video, VideoStatus
from app.services.job_service import JobService
from app.services.media import ffmpeg
from app.services.pipeline.workspace import workspace_for
from app.services.storage import get_storage

log = get_logger(__name__)


def run_probe(db: Session, job: Job) -> dict:
    """Read real stream metadata and store a thumbnail."""
    video_id = job.payload.get("video_id") or job.video_id
    video = db.get(Video, video_id) if video_id else None
    if video is None:
        raise NotFoundError("Video not found for probe job.", code="video_not_found")

    video.status = VideoStatus.PROBING.value
    db.commit()
    JobService.report_progress(db, job, stage="probe", progress=5, message="Reading video metadata", force=True)

    storage = get_storage()
    with workspace_for(job.id) as workdir:
        local = storage.materialize(video.storage_key, workdir)
        JobService.check_cancelled(db, job)

        probe = ffmpeg.probe_media(local)
        if probe["duration"] <= 0:
            raise MediaError("This file reports no duration and cannot be processed.", code="invalid_media")

        video.duration_seconds = float(probe["duration"])
        video.width = int(probe["width"])
        video.height = int(probe["height"])
        video.fps = float(probe["fps"] or 30.0)
        video.video_codec = probe["video_codec"]
        video.audio_codec = probe["audio_codec"]
        video.audio_channels = int(probe["audio_channels"])
        video.audio_sample_rate = int(probe["audio_sample_rate"])
        video.has_audio = bool(probe["has_audio"])
        video.rotation = int(probe["rotation"])
        video.probe = {k: v for k, v in probe.items() if k != "streams"}
        video.status = VideoStatus.READY.value
        video.error_message = ""
        db.commit()

        JobService.report_progress(db, job, stage="thumbnail", progress=70, message="Generating preview frame")

        poster_at = min(max(0.5, video.duration_seconds * 0.08), max(0.5, video.duration_seconds - 0.5))
        thumb_local = workdir / "poster.jpg"
        try:
            ffmpeg.extract_thumbnail(local, thumb_local, at=poster_at, width=720, job_id=job.id)
            thumb_key = f"projects/{video.project_id}/videos/{video.id}/poster.jpg"
            storage.put_file(thumb_key, thumb_local, "image/jpeg")
            video.thumbnail_key = thumb_key
            db.commit()
        except MediaError as exc:
            log.warning("probe.thumbnail_failed", video_id=video.id, error=str(exc))

    project = db.get(Project, video.project_id)
    if project and project.status in (ProjectStatus.DRAFT.value, ProjectStatus.UPLOADING.value):
        project.status = ProjectStatus.READY.value
        db.commit()

    JobService.report_progress(db, job, stage="probe", progress=95, message="Metadata ready")
    return {
        "video_id": video.id,
        "duration": video.duration_seconds,
        "width": video.width,
        "height": video.height,
        "fps": video.fps,
        "has_audio": video.has_audio,
    }
