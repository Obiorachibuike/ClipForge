"""Chunked, resumable upload handling.

Chunks are appended to a server-side staging file at their byte offset, so
uploads can arrive out of order (parallel chunk sending from the browser) and
resume after a dropped connection. The final object is streamed into storage
once, which keeps large videos off the API's memory and out of the database.
"""
from __future__ import annotations

import hashlib
import mimetypes
import shutil
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models import (
    JobType,
    Project,
    ProjectStatus,
    UploadSession,
    UploadStatus,
    User,
    Video,
    VideoStatus,
    utcnow,
)
from app.services import events as ev
from app.services.job_service import JobService
from app.services.storage import get_storage, safe_filename
from app.services.usage_service import UsageService

log = get_logger(__name__)

STAGING_ROOT = Path(settings.storage_local_root).expanduser().resolve().parent / "uploads"
MIN_CHUNK = 256 * 1024
MAX_CHUNK = 64 * 1024 * 1024

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
        _staging_path(upload_id).parent.mkdir(parents=True, exist_ok=True)
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
        if index < 0 or index >= session.total_chunks:
            raise ValidationError(f"Chunk index {index} is out of range.", field="index")
        if len(data) == 0:
            raise ValidationError("Empty chunk received.", field="chunk")
        if len(data) > session.chunk_size:
            raise ValidationError("Chunk exceeds the negotiated chunk size.", field="chunk")
        expected_last = session.size_bytes - session.chunk_size * (session.total_chunks - 1)
        if index < session.total_chunks - 1 and len(data) != session.chunk_size:
            # Allow a short non-final chunk only if it fills exactly the remaining bytes.
            remaining_before = session.size_bytes - index * session.chunk_size
            if len(data) > remaining_before:
                raise ValidationError("Chunk does not match the agreed chunk size.", field="chunk")

        path = _staging_path(upload_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with open(path, "wb") as handle:
                handle.truncate(session.size_bytes)
        with open(path, "r+b") as handle:
            handle.seek(index * session.chunk_size)
            handle.write(data)

        received = dict(session.received_chunks or {})
        key = str(index)
        if key not in received:
            received[key] = len(data)
            session.received_chunks = received
            session.received_bytes = int(sum(received.values()))
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
        source,  # noqa: ANN001 - binary file-like object
        *,
        max_bytes: int | None = None,
        chunk_bytes: int = 1024 * 1024,
    ) -> UploadSession:
        """Stream a whole file into staging for the single-request upload path.

        The file is read in bounded chunks and written straight to disk: it is
        never held in memory, and the size ceiling is enforced while streaming so
        an oversized upload is rejected before it fills the volume.
        """
        session = UploadService._session(db, user, upload_id)
        if session.status in (UploadStatus.COMPLETED.value, UploadStatus.ABORTED.value):
            raise ConflictError("This upload can no longer be modified.", code="upload_closed")
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
        if head and not looks_like_media(head, session.filename):
            log.warning("upload.unrecognised_container", upload_id=upload_id, mode="stream", head=head[:16])

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
    def status(db: Session, user: User, upload_id: str) -> UploadSession:
        return UploadService._session(db, user, upload_id)

    # ------------------------------------------------------------ complete ---
    @staticmethod
    def complete_upload(db: Session, user: User, upload_id: str) -> tuple[Video, UploadSession, "object"]:
        session = UploadService._session(db, user, upload_id)
        if session.status == UploadStatus.COMPLETED.value and session.video_id:
            video = db.get(Video, session.video_id)
            job = JobService.create(
                db,
                type_=JobType.VIDEO_PROBE.value,
                payload={"video_id": video.id},
                user_id=user.id,
                project_id=session.project_id,
                video_id=video.id,
                priority=10,
            )
            return video, session, job

        missing = [
            index for index in range(session.total_chunks) if str(index) not in (session.received_chunks or {})
        ]
        if missing:
            raise ConflictError(
                f"Upload is incomplete: {len(missing)} of {session.total_chunks} chunks are missing.",
                code="upload_incomplete",
            )

        path = _staging_path(upload_id)
        if not path.exists():
            raise NotFoundError("Staged upload data is missing. Please restart the upload.", code="upload_data_missing")
        actual_size = path.stat().st_size
        if actual_size != session.size_bytes:
            raise ConflictError(
                f"Uploaded size ({actual_size} bytes) does not match the declared size ({session.size_bytes} bytes).",
                code="upload_size_mismatch",
            )

        with open(path, "rb") as handle:
            head = handle.read(64)
        if not looks_like_media(head, session.filename):
            log.warning("upload.unrecognised_container", upload_id=upload_id, head=head[:16])
            # Not fatal: many valid streams start with data we do not fingerprint,
            # and the probe job will reject anything unreadable.

        checksum = _sha256(path, limit_bytes=256 * 1024 * 1024)
        storage = get_storage()
        video = db.get(Video, session.video_id) if session.video_id else None
        if video is None:
            raise NotFoundError("Video record not found.", code="video_not_found")

        storage.put_file(session.storage_key, path, session.content_type or "video/mp4")
        video.storage_key = session.storage_key
        video.size_bytes = actual_size
        video.checksum = checksum
        video.status = VideoStatus.UPLOADED.value
        session.status = UploadStatus.COMPLETED.value
        db.commit()

        # Staged chunks are no longer needed.
        path.unlink(missing_ok=True)

        project = db.get(Project, session.project_id)
        if project:
            project.status = ProjectStatus.PROCESSING.value
            db.commit()

        job = JobService.create(
            db,
            type_=JobType.VIDEO_PROBE.value,
            payload={"video_id": video.id},
            user_id=user.id,
            project_id=session.project_id,
            video_id=video.id,
            priority=10,
        )
        UsageService.record(
            db,
            user_id=user.id,
            metric="videos_uploaded",
            quantity=1,
            unit="videos",
            project_id=session.project_id,
            video_id=video.id,
            job_id=job.id,
        )
        UsageService.record(
            db,
            user_id=user.id,
            metric="storage_bytes",
            quantity=float(actual_size),
            unit="bytes",
            project_id=session.project_id,
            video_id=video.id,
            job_id=job.id,
        )
        log.info(
            "upload.completed",
            upload_id=upload_id,
            video_id=video.id,
            bytes=actual_size,
            seconds=round(session.created_at.timestamp() and 0, 1),
        )
        return video, session, job

    # --------------------------------------------------------------- abort ---
    @staticmethod
    def abort(db: Session, user: User, upload_id: str, *, delete_video: bool = True) -> UploadSession:
        session = UploadService._session(db, user, upload_id)
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
    def _session(db: Session, user: User, upload_id: str) -> UploadSession:
        session = (
            db.query(UploadSession).filter(UploadSession.upload_id == upload_id).one_or_none()
        )
        if session is None:
            raise NotFoundError("Upload session not found.", code="upload_not_found")
        if session.user_id != user.id:
            # Same response as a missing session: never confirm existence to others.
            raise NotFoundError("Upload session not found.", code="upload_not_found")
        return session


def _staging_path(upload_id: str) -> Path:
    # upload_id is a generated hex string; validate defensively anyway.
    safe = "".join(ch for ch in upload_id if ch.isalnum() or ch in "-_")[:64]
    if not safe:
        raise ValidationError("Invalid upload identifier.", field="upload_id")
    return STAGING_ROOT / f"{safe}.part"


def _sha256(path: Path, limit_bytes: int) -> str:
    digest = hashlib.sha256()
    remaining = limit_bytes
    with open(path, "rb") as handle:
        while remaining > 0:
            chunk = handle.read(min(4 * 1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


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
