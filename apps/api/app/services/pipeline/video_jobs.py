"""Video ingestion jobs: assemble storage-backed uploads, probe media and make a poster."""
from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import MediaError, NotFoundError, user_message
from app.core.config import settings
from app.core.logging import get_logger
from app.models import Job, Project, ProjectStatus, UploadSession, Video, VideoStatus
from app.services.job_service import JobService
from app.services.media import ffmpeg
from app.services.pipeline.workspace import workspace_for
from app.services.processor import sidecar
from app.services.storage import get_storage, safe_filename
from app.services.upload_service import upload_part_key

log = get_logger(__name__)
CHECKSUM_SAMPLE_BYTES = 256 * 1024 * 1024


def assemble_upload_parts(storage, session: UploadSession, destination: Path, progress_cb=None) -> str:
    """Assemble ordered object-storage chunks on the worker's local workspace.

    This deliberately runs in the worker, not the API function. The API only
    validates/stores individual chunks; the worker owns any full-size local file.
    The checksum matches the existing sampled SHA-256 policy (first 256 MiB).
    """
    if session.total_chunks < 1 or session.size_bytes < 1:
        raise MediaError("The upload has no stored media chunks.", code="upload_data_missing")
    received = dict(session.received_chunks or {})
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    hash_remaining = min(CHECKSUM_SAMPLE_BYTES, int(session.size_bytes))
    total_written = 0

    with destination.open("wb") as output:
        for index in range(session.total_chunks):
            expected_size = min(session.chunk_size, session.size_bytes - index * session.chunk_size)
            if expected_size <= 0 or str(index) not in received:
                raise MediaError("An uploaded chunk is missing.", code="upload_data_missing")
            if int(received[str(index)]) != expected_size:
                raise MediaError("An uploaded chunk has an invalid size.", code="upload_size_mismatch")

            key = upload_part_key(session, index)
            try:
                part = storage.stat(key)
            except Exception as exc:
                raise MediaError("An uploaded chunk is missing from object storage.", code="upload_data_missing") from exc
            if part.size != expected_size:
                raise MediaError("An uploaded chunk is incomplete in object storage.", code="upload_size_mismatch")

            part_written = 0
            for data in storage.open_stream(key):
                part_written += len(data)
                total_written += len(data)
                if part_written > expected_size or total_written > session.size_bytes:
                    raise MediaError("Uploaded data exceeds its declared size.", code="upload_size_mismatch")
                output.write(data)
                if hash_remaining:
                    sample = data[:hash_remaining]
                    digest.update(sample)
                    hash_remaining -= len(sample)
            if part_written != expected_size:
                raise MediaError("An uploaded chunk ended before its declared size.", code="upload_data_missing")
            if progress_cb:
                progress_cb((index + 1) / session.total_chunks * 100.0)

    if total_written != session.size_bytes:
        destination.unlink(missing_ok=True)
        raise MediaError("The assembled upload size does not match its declaration.", code="upload_size_mismatch")
    return digest.hexdigest()


def _sampled_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    remaining = min(CHECKSUM_SAMPLE_BYTES, path.stat().st_size)
    with Path(path).open("rb") as handle:
        while remaining:
            chunk = handle.read(min(4 * 1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def _upload_session(db: Session, upload_id: str, video_id: str) -> UploadSession | None:
    return db.execute(
        select(UploadSession).where(
            UploadSession.upload_id == upload_id,
            UploadSession.video_id == video_id,
        )
    ).scalars().one_or_none()


def run_probe(db: Session, job: Job) -> dict:
    """Read real stream metadata and store a thumbnail."""
    video_id = job.payload.get("video_id") or job.video_id
    video = db.get(Video, video_id) if video_id else None
    if video is None:
        raise NotFoundError("Video not found for probe job.", code="video_not_found")

    video.status = VideoStatus.PROBING.value
    db.commit()
    try:
        return _probe_media(db, job, video)
    except BaseException as exc:
        # A failed probe must not leave the video permanently "probing": the UI
        # and every downstream guard depend on this terminal state.
        _mark_video_failed(db, video, exc)
        raise


def _mark_video_failed(db: Session, video: Video, exc: BaseException) -> None:
    try:
        db.rollback()
        video.status = VideoStatus.FAILED.value
        video.error_message = user_message(exc)[:1000]
        db.commit()
    except Exception:  # pragma: no cover - the original error is what matters
        db.rollback()
        log.exception("probe.failure_state_not_saved", video_id=video.id)


def _probe_media(db: Session, job: Job, video: Video) -> dict:
    JobService.report_progress(db, job, stage="prepare", progress=2, message="Preparing source media", force=True)

    storage = get_storage()
    session = _upload_session(db, str(job.payload.get("upload_id") or ""), video.id) if job.payload.get("upload_id") else None
    with workspace_for(job.id) as workdir:
        if storage.exists(video.storage_key):
            # This also makes a retry safe if a worker crashed after the object
            # upload but before it committed the checksum/status to PostgreSQL.
            local = storage.materialize(video.storage_key, workdir)
            if not video.checksum:
                video.checksum = _sampled_sha256(local)
                video.size_bytes = local.stat().st_size
                db.commit()
            if session is not None:
                _delete_upload_parts(storage, session)
        elif session is not None:
            JobService.report_progress(
                db, job, stage="assemble", progress=2, message="Assembling uploaded chunks", force=True
            )
            local = workdir / safe_filename(session.filename, fallback="source.media")

            def assembly_progress(pct: float) -> None:
                JobService.report_progress(
                    db,
                    job,
                    stage="assemble",
                    progress=2 + max(0.0, min(100.0, pct)) * 0.025,
                    message="Assembling uploaded chunks",
                )

            checksum = assemble_upload_parts(storage, session, local, progress_cb=assembly_progress)
            storage.put_file(video.storage_key, local, session.content_type or video.content_type or "video/mp4")
            video.size_bytes = local.stat().st_size
            video.checksum = checksum
            db.commit()
            _delete_upload_parts(storage, session)
        else:
            # Legacy/imported videos already have a complete storage object.
            local = storage.materialize(video.storage_key, workdir)

        JobService.report_progress(db, job, stage="probe", progress=5, message="Reading video metadata", force=True)
        JobService.check_cancelled(db, job)

        # Prefer the Rust sidecar when it is configured and reachable; it returns
        # the same normalized shape (validated in services/processor.py), so this
        # is a pure speed-up and never a functional dependency.
        probe = sidecar.probe_media(local) or ffmpeg.probe_media(local)
        if probe["duration"] <= 0:
            raise MediaError("This file reports no duration and cannot be processed.", code="invalid_media")
        max_duration = int(settings.media_max_duration_seconds or 0)
        if max_duration and float(probe["duration"]) > max_duration:
            hours = max_duration / 3600
            raise MediaError(
                f"That video is {float(probe['duration']) / 3600:.1f} hours long; this deployment accepts "
                f"at most {hours:.1f} hours per source.",
                code="video_too_long",
            )

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


def _delete_upload_parts(storage, session: UploadSession) -> None:
    try:
        storage.delete_prefix(session.parts_prefix)
    except Exception as exc:  # pragma: no cover - storage lifecycle cleans leftovers
        log.warning("upload.parts_cleanup_failed", upload_id=session.upload_id, error=str(exc)[:200])
