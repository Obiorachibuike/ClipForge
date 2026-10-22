"""Chunked, resumable uploads backed by the configured object-storage backend.

Each validated chunk is stored as its own object so separate serverless API
invocations never depend on a shared temporary filesystem. The queued probe job
assembles those objects on the processing worker, writes the source object to
shared storage, then continues through the existing media pipeline.
"""
from __future__ import annotations

import mimetypes
import shutil
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

from app.core.config import settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models import (
    Job,
    JobStatus,
    JobType,
    Project,
    ProjectStatus,
    UploadSession,
    UploadStatus,
    UsageRecord,
    User,
    Video,
    VideoStatus,
    as_utc,
    utcnow,
)
from app.services import events as ev
from app.services.job_service import JobService
from app.services.queue import get_queue
from app.services.storage import RangeSpec, get_storage, safe_filename
from app.services.usage_service import UsageService
from sqlalchemy import select
from sqlalchemy.orm import Session

log = get_logger(__name__)

STAGING_ROOT = Path(tempfile.gettempdir()) / "clipforge" / "uploads"
MIN_CHUNK = 256 * 1024
MAX_CHUNK = 4 * 1024 * 1024  # below Vercel's per-function request-body ceiling

# Magic-byte signatures for the containers we accept. Extensions alone are not
# trusted: a file must also look like the media type it claims to be.
CONTAINER_SIGNATURES: list[tuple[str, tuple[bytes, ...]]] = [
    ("mp4/mov/m4v", (b"ftyp",)),
    ("matroska/webm", (b"\x1a\x45\xdf\xa3",)),
    ("avi", (b"RIFF",)),
    ("mpegts", (b"\x47",)),
    ("mp3", (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")),
    ("wav", (b"RIFF",)),
    ("flac", (b"fLaC",)),
    ("ogg", (b"OggS",)),
]


def validate_upload(filename: str, content_type: str, size_bytes: int) -> str:
    """Validate extension, declared MIME type and size. Returns the clean name."""
    name = safe_filename(filename, fallback="upload.mp4")
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension not in settings.allowed_extensions:
        raise ValidationError(
            f"Unsupported file type '.{extension or 'unknown'}'. Allowed: "
            + ", ".join(sorted(settings.allowed_extensions)),
            field="filename",
        )
    if content_type:
        normalized = content_type.split(";")[0].strip().lower()
        if normalized and not any(normalized.startswith(prefix) for prefix in settings.allowed_mime_prefixes):
            raise ValidationError(
                f"Unsupported content type '{normalized}'. Upload a video or audio file.", field="content_type"
            )
    if size_bytes < settings.media_min_upload_bytes:
        raise ValidationError("That file is too small to be a video.", field="size_bytes")
    if size_bytes > settings.media_max_upload_bytes:
        raise ValidationError(
            f"Files must be smaller than {settings.media_max_upload_bytes / (1024**3):.1f} GB on this deployment.",
            field="size_bytes",
        )
    return name


# Headers that are conclusively *not* media. Unknown layouts still fall back to
# the extension (see `looks_like_media`), but a PDF or an HTML error page
# renamed to .mp4 must not be accepted as a video.
NON_MEDIA_SIGNATURES: tuple[bytes, ...] = (
    b"%PDF",
    b"PK\x03\x04",  # zip/docx
    b"\x89PNG",
    b"\xff\xd8\xff",  # jpeg
    b"GIF87a",
    b"GIF89a",
    b"<?xml",
    b"<!DOCTYPE",
    b"<html",
    b"<!doctype",
)


def definitely_not_media(first_bytes: bytes) -> bool:
    """True only when the header rules media out.

    Deliberately conservative: unknown headers return False so unusual but valid
    containers are not rejected on a guess.
    """
    if not first_bytes:
        return False
    head = first_bytes[:64].lstrip()
    lowered = head.lower()
    if any(lowered.startswith(signature.lower()) for signature in NON_MEDIA_SIGNATURES):
        return True
    if lowered.startswith((b"{\"", b"{\n", b"[\n")) or lowered.startswith(b"[{"):
        return True
    # Plain text (e.g. an SRT renamed to .mp4): printable ASCII with no binary
    # NUL bytes and no media signature is not a container.
    sample = head[:48]
    if sample and all(32 <= byte < 127 or byte in (9, 10, 13) for byte in sample):
        return not looks_like_media(sample, "")
    return False


def looks_like_media(first_bytes: bytes, filename: str) -> bool:
    """Check container magic bytes. Unknown layouts are accepted based on the
    extension only when the header is inconclusive (e.g. fragmented streams)."""
    if not first_bytes:
        return False
    head = first_bytes[:64]
    for _label, signatures in CONTAINER_SIGNATURES:
        for signature in signatures:
            if signature in head[:32] or head.startswith(signature):
                return True
    return False


class UploadService:
    # -------------------------------------------------------------- create ---
    @staticmethod
    def init_upload(
        db: Session,
        user: User,
        *,
        filename: str,
        size_bytes: int,
        content_type: str = "",
        project_id: str | None = None,
        project_name: str | None = None,
        chunk_size: int | None = None,
    ) -> UploadSession:
        name = validate_upload(filename, content_type, size_bytes)
        UsageService.check_upload(db, user, size_bytes)

        project = None
        if project_id:
            project = db.get(Project, project_id)
            if project is None or project.user_id != user.id:
                raise NotFoundError("Project not found.", code="project_not_found")
        if project is None:
            project = Project(
                user_id=user.id,
                name=(project_name or Path(name).stem or "Untitled project")[:160],
                status=ProjectStatus.UPLOADING.value,
                target_aspect_ratio=user.default_aspect_ratio or "9:16",
                privacy_mode=user.privacy_mode or "cloud",
            )
            db.add(project)
            db.flush()

        chunk = int(chunk_size or settings.upload_chunk_size)
        chunk = max(MIN_CHUNK, min(MAX_CHUNK, chunk))
        total_chunks = max(1, (size_bytes + chunk - 1) // chunk)
        upload_id = uuid.uuid4().hex
        video = Video(
            project_id=project.id,
            user_id=user.id,
            original_filename=name,
            storage_key=f"projects/{project.id}/videos/pending/{upload_id}/{name}",
            content_type=content_type or mimetypes.guess_type(name)[0] or "application/octet-stream",
            size_bytes=size_bytes,
            status=VideoStatus.UPLOADING.value,
        )
        db.add(video)
        db.flush()

        session = UploadSession(
            upload_id=upload_id,
            user_id=user.id,
            project_id=project.id,
            filename=name,
            content_type=video.content_type,
            size_bytes=size_bytes,
            chunk_size=chunk,
            total_chunks=total_chunks,
            received_chunks={},
            received_bytes=0,
            status=UploadStatus.INITIATED.value,
            storage_key=f"projects/{project.id}/videos/{video.id}/source/{name}",
            parts_prefix=f"projects/{project.id}/videos/{video.id}/parts",
            video_id=video.id,
            expires_at=utcnow() + timedelta(minutes=settings.upload_session_ttl_minutes),
        )
        db.add(session)
        project.status = ProjectStatus.UPLOADING.value
        db.commit()
        db.refresh(session)
        log.info(
            "upload.initialised",
            upload_id=upload_id,
            size=size_bytes,
            chunks=total_chunks,
            project_id=project.id,
            user_id=user.id,
        )
        return session

    # -------------------------------------------------------------- chunks ---
    @staticmethod
    def receive_chunk(db: Session, user: User, upload_id: str, index: int, data: bytes) -> UploadSession:
        session = UploadService._session(db, user, upload_id)
        if session.status in (UploadStatus.COMPLETED.value, UploadStatus.ABORTED.value):
            raise ConflictError("This upload can no longer be modified.", code="upload_closed")
        _require_not_expired(session)
        if index < 0 or index >= session.total_chunks:
            raise ValidationError(f"Chunk index {index} is out of range.", field="index")
        if len(data) == 0:
            raise ValidationError("Empty chunk received.", field="chunk")

        expected_size = min(session.chunk_size, session.size_bytes - index * session.chunk_size)
        if len(data) != expected_size:
            raise ValidationError(
                f"Chunk {index} must contain exactly {expected_size} bytes.", field="chunk"
            )
        key = str(index)
        received = dict(session.received_chunks or {})
        if key in received and int(received[key]) != len(data):
            raise ConflictError("A retry changed the size of an already received chunk.", code="upload_chunk_conflict")

        # Persist the chunk under a stable, generated object key. This is safe
        # across Vercel invocations and can be read by a worker on another host.
        get_storage().put_bytes(upload_part_key(session, index), data, "application/octet-stream")
        received[key] = len(data)
        session.received_chunks = received
        session.received_bytes = int(sum(int(size) for size in received.values()))
        session.status = UploadStatus.IN_PROGRESS.value
        db.commit()

        progress = (session.received_bytes / session.size_bytes) * 100 if session.size_bytes else 0.0
        ev.get_event_bus().emit(
            ev.EVENT_UPLOAD_PROGRESS,
            user_id=user.id,
            project_id=session.project_id,
            payload={
                "upload_id": upload_id,
                "video_id": session.video_id,
                "progress": round(progress, 2),
                "received_bytes": session.received_bytes,
                "size_bytes": session.size_bytes,
                "received_chunks": len(received),
                "total_chunks": session.total_chunks,
            },
        )
        return session

    # --------------------------------------------------- single-request upload ---
    @staticmethod
    def stage_stream(
        db: Session,
        user: User,
        upload_id: str,
        source,
        *,
        max_bytes: int | None = None,
        chunk_bytes: int = 1024 * 1024,
    ) -> UploadSession:
        """Stream the one-request convenience upload to a temporary file, then
        persist it as the first storage-backed chunk. The video itself is still
        assembled by the worker after the request returns.
        """
        session = UploadService._session(db, user, upload_id)
        if session.status in (UploadStatus.COMPLETED.value, UploadStatus.ABORTED.value):
            raise ConflictError("This upload can no longer be modified.", code="upload_closed")
        _require_not_expired(session)
        ceiling = min(max_bytes or settings.media_max_upload_bytes, settings.media_max_upload_bytes)
        path = _staging_path(upload_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        written = 0
        head = b""
        with open(path, "wb") as handle:
            while True:
                data = source.read(chunk_bytes)
                if not data:
                    break
                if not head:
                    head = data[:64]
                written += len(data)
                if written > ceiling:
                    handle.close()
                    path.unlink(missing_ok=True)
                    raise ValidationError(
                        f"Files larger than {ceiling / (1024**3):.1f} GB must use the chunked upload.",
                        field="file",
                    )
                handle.write(data)

        if written < settings.media_min_upload_bytes:
            path.unlink(missing_ok=True)
            raise ValidationError("That file is too small to be a video.", field="file")
        if head and definitely_not_media(head):
            path.unlink(missing_ok=True)
            raise ValidationError(
                "That file does not look like a video. Upload an MP4, MOV, WebM or similar file.",
                field="file",
            )
        if head and not looks_like_media(head, session.filename):
            log.warning("upload.unrecognised_container", upload_id=upload_id, mode="stream", head=head[:16])

        # This path is intended for smaller one-request uploads. The file is
        # stored as one object part; the worker will materialize the full source.
        try:
            UsageService.check_upload(db, user, written)
            get_storage().put_file(upload_part_key(session, 0), path, session.content_type)
        finally:
            path.unlink(missing_ok=True)

        # A streamed upload is one complete part as far as bookkeeping is concerned.
        session.size_bytes = written
        session.chunk_size = max(written, 1)
        session.total_chunks = 1
        session.received_chunks = {"0": written}
        session.received_bytes = written
        session.status = UploadStatus.IN_PROGRESS.value
        db.commit()
        ev.get_event_bus().emit(
            ev.EVENT_UPLOAD_PROGRESS,
            user_id=user.id,
            project_id=session.project_id,
            payload={
                "upload_id": upload_id,
                "video_id": session.video_id,
                "progress": 100.0,
                "received_bytes": written,
                "size_bytes": written,
                "received_chunks": 1,
                "total_chunks": 1,
            },
        )
        return session

    @staticmethod
    def expected_chunk_bytes(db: Session, user: User, upload_id: str, index: int) -> int:
        """Exact byte count a chunk must contain.

        Exposed so the HTTP layer can refuse an oversized body *before* reading
        it into memory, rather than buffering the request and validating after.
        """
        session = UploadService._session(db, user, upload_id)
        if index < 0 or index >= session.total_chunks:
            raise ValidationError(f"Chunk index {index} is out of range.", field="index")
        return min(session.chunk_size, session.size_bytes - index * session.chunk_size)

    @staticmethod
    def status(db: Session, user: User, upload_id: str) -> UploadSession:
        return UploadService._session(db, user, upload_id)

    # ------------------------------------------------------------ complete ---
    @staticmethod
    def complete_upload(db: Session, user: User, upload_id: str) -> tuple[Video, UploadSession, object]:
        session = UploadService._session(db, user, upload_id, for_update=True)
        video = db.get(Video, session.video_id) if session.video_id else None
        if video is None:
            raise NotFoundError("Video record not found.", code="video_not_found")

        if session.status == UploadStatus.COMPLETED.value:
            # Completion is idempotent even after the probe reaches a terminal
            # state. A row lock prevents concurrent retries from creating two
            # assembly jobs for the same upload.
            probe_jobs = list(
                db.execute(
                    select(Job)
                    .where(Job.video_id == video.id, Job.type == JobType.VIDEO_PROBE.value)
                    .order_by(Job.created_at)
                ).scalars()
            )
            # New probe jobs carry their upload id. Fall back to the oldest
            # legacy probe row for uploads completed before that field existed.
            existing = next(
                (item for item in probe_jobs if (item.payload or {}).get("upload_id") == upload_id),
                None,
            )
            if existing is None:
                existing = next(
                    (item for item in probe_jobs if not (item.payload or {}).get("upload_id")),
                    None,
                )
            if existing is not None:
                _record_upload_usage(db, user, session, video, existing)
                _ensure_job_enqueued(existing)
                return video, session, existing
            job = JobService.create(
                db,
                type_=JobType.VIDEO_PROBE.value,
                payload={"video_id": video.id, "upload_id": upload_id},
                user_id=user.id,
                project_id=session.project_id,
                video_id=video.id,
                priority=10,
                enqueue=False,
            )
            session = UploadService._session(db, user, upload_id, for_update=True)
            _record_upload_usage(db, user, session, video, job)
            _ensure_job_enqueued(job)
            return video, session, job

        # A completed upload stays idempotently retrievable for its probe job,
        # but an unfinished session past its TTL may not be completed.
        _require_not_expired(session)
        received = dict(session.received_chunks or {})
        missing = [index for index in range(session.total_chunks) if str(index) not in received]
        if missing:
            raise ConflictError(
                f"Upload is incomplete: {len(missing)} of {session.total_chunks} chunks are missing.",
                code="upload_incomplete",
            )

        storage = get_storage()
        actual_size = 0
        for index in range(session.total_chunks):
            expected_size = min(session.chunk_size, session.size_bytes - index * session.chunk_size)
            if int(received[str(index)]) != expected_size:
                raise ConflictError(
                    f"Chunk {index} has an unexpected size; please retry that chunk.",
                    code="upload_size_mismatch",
                )
            try:
                part = storage.stat(upload_part_key(session, index))
            except Exception as exc:
                raise NotFoundError(
                    f"Stored upload chunk {index} is missing. Please retry the upload.",
                    code="upload_data_missing",
                ) from exc
            if part.size != expected_size:
                raise ConflictError(
                    f"Stored upload chunk {index} is incomplete; please retry that chunk.",
                    code="upload_size_mismatch",
                )
            actual_size += part.size

        if actual_size != session.size_bytes or int(session.received_bytes) != actual_size:
            raise ConflictError(
                f"Uploaded size ({actual_size} bytes) does not match the declared size ({session.size_bytes} bytes).",
                code="upload_size_mismatch",
            )

        # The API has persisted every chunk in shared storage. It intentionally
        # does not join large files in the serverless request: the worker will
        # assemble and probe the source after it claims this job.
        try:
            head = storage.read_bytes(upload_part_key(session, 0), byte_range=RangeSpec(0, min(63, actual_size - 1)))
        except Exception:
            head = b""
        if head and definitely_not_media(head):
            raise ValidationError(
                "That file does not look like a video. Upload an MP4, MOV, WebM or similar file.",
                field="file",
            )
        if head and not looks_like_media(head, session.filename):
            log.warning("upload.unrecognised_container", upload_id=upload_id, head=head[:16])

        video.storage_key = session.storage_key
        video.size_bytes = actual_size
        video.checksum = ""  # calculated by the worker while it assembles the source
        video.status = VideoStatus.UPLOADED.value
        session.status = UploadStatus.COMPLETED.value
        project = db.get(Project, session.project_id)
        if project:
            project.status = ProjectStatus.PROCESSING.value

        # JobService commits this row with the completed upload session while
        # its row lock is still held. Enqueue afterwards so duplicate completion
        # requests cannot race into separate probe/assembly jobs.
        job = JobService.create(
            db,
            type_=JobType.VIDEO_PROBE.value,
            payload={"video_id": video.id, "upload_id": upload_id},
            user_id=user.id,
            project_id=session.project_id,
            video_id=video.id,
            priority=10,
            enqueue=False,
        )
        session = UploadService._session(db, user, upload_id, for_update=True)
        _record_upload_usage(db, user, session, video, job)
        _ensure_job_enqueued(job)
        log.info("upload.completed", upload_id=upload_id, video_id=video.id, bytes=actual_size)
        return video, session, job

    # --------------------------------------------------------------- abort ---
    @staticmethod
    def abort(db: Session, user: User, upload_id: str, *, delete_video: bool = True) -> UploadSession:
        session = UploadService._session(db, user, upload_id)
        if session.status == UploadStatus.COMPLETED.value:
            raise ConflictError("A completed upload is already queued for processing.", code="upload_closed")
        session.status = UploadStatus.ABORTED.value
        _staging_path(upload_id).unlink(missing_ok=True)
        storage = get_storage()
        try:
            storage.delete_prefix(session.parts_prefix)
        except Exception:  # pragma: no cover - best effort cleanup
            pass
        if delete_video and session.video_id:
            video = db.get(Video, session.video_id)
            if video and video.status in (VideoStatus.PENDING.value, VideoStatus.UPLOADING.value):
                db.delete(video)
        db.commit()
        log.info("upload.aborted", upload_id=upload_id)
        return session

    # ------------------------------------------------------------- helpers ---
    @staticmethod
    def _session(db: Session, user: User, upload_id: str, *, for_update: bool = False) -> UploadSession:
        query = db.query(UploadSession).filter(UploadSession.upload_id == upload_id)
        if for_update:
            query = query.with_for_update()
        session = query.one_or_none()
        if session is None:
            raise NotFoundError("Upload session not found.", code="upload_not_found")
        if session.user_id != user.id:
            # Same response as a missing session: never confirm existence to others.
            raise NotFoundError("Upload session not found.", code="upload_not_found")
        return session


def _record_upload_usage(db: Session, user: User, session: UploadSession, video: Video, job: Job) -> None:
    """Write upload counters once, including recovery after a partial request failure."""
    metrics = (
        ("videos_uploaded", 1.0, "videos"),
        ("storage_bytes", float(session.size_bytes), "bytes"),
    )
    added: list[tuple[str, str]] = []
    for metric, quantity, unit in metrics:
        exists = db.execute(
            select(UsageRecord.id).where(UsageRecord.job_id == job.id, UsageRecord.metric == metric)
        ).scalar_one_or_none()
        if exists is not None:
            continue
        UsageService.record(
            db,
            user_id=user.id,
            metric=metric,
            quantity=quantity,
            unit=unit,
            project_id=session.project_id,
            video_id=video.id,
            job_id=job.id,
            commit=False,
        )
        added.append((metric, unit))
    if not added:
        return
    db.commit()
    totals = UsageService.totals(db, user.id)
    for metric, unit in added:
        ev.get_event_bus().emit(
            ev.EVENT_USAGE_UPDATED,
            user_id=user.id,
            project_id=session.project_id,
            payload={"metric": metric, "total": totals.get(metric, 0.0), "unit": unit},
        )


def _ensure_job_enqueued(job: Job) -> None:
    """Repair a committed queued row if enqueue previously failed after commit."""
    if job.status not in (JobStatus.QUEUED.value, JobStatus.RETRYING.value):
        return
    queue = get_queue()
    if queue.is_queued(job.id) or queue.is_active(job.id):
        return
    delay = max(0.0, (as_utc(job.run_after) - utcnow()).total_seconds()) if job.run_after else 0.0
    queue.enqueue(job.id, priority=job.priority, delay_seconds=delay)


def _require_not_expired(session: UploadSession) -> None:
    """Reject writes to an upload session past its TTL."""
    if session.expires_at is not None and as_utc(session.expires_at) < utcnow():
        raise ConflictError(
            "This upload session has expired. Start the upload again.",
            code="upload_expired",
        )


def upload_part_key(session: UploadSession, index: int) -> str:
    """Stable storage object key for one validated upload chunk."""
    if index < 0 or index >= session.total_chunks:
        raise ValidationError("Invalid upload chunk index.", field="index")
    return f"{session.parts_prefix}/{index:08d}.part"


def _staging_path(upload_id: str) -> Path:
    # Used only for the single-request convenience path, never for resumable
    # chunks or as a dependency shared between the API and processing worker.
    safe = "".join(ch for ch in upload_id if ch.isalnum() or ch in "-_")[:64]
    if not safe:
        raise ValidationError("Invalid upload identifier.", field="upload_id")
    return STAGING_ROOT / f"{safe}.part"


def staging_usage_bytes() -> int:
    if not STAGING_ROOT.exists():
        return 0
    return sum(path.stat().st_size for path in STAGING_ROOT.glob("*.part") if path.is_file())


def purge_stale_staging(hours: int = 48) -> int:
    """Remove abandoned upload staging files (called by the worker janitor)."""
    if not STAGING_ROOT.exists():
        return 0
    cutoff = utcnow().timestamp() - hours * 3600
    removed = 0
    for path in STAGING_ROOT.glob("*.part"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def free_staging_space() -> None:  # pragma: no cover - ops helper
    shutil.rmtree(STAGING_ROOT, ignore_errors=True)
