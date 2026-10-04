"""Video upload, media streaming and video-level processing endpoints.

Streaming note: source videos are served through this API with HTTP Range
support and short-lived signed URLs, so browser players never need direct
storage access or long-lived public links.
"""
from __future__ import annotations

import asyncio
import re
from typing import Annotated

from fastapi import APIRouter, Body, Depends, File, Header, Query, Request, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api.deps import (
    CurrentUser,
    DbSession,
    PageParams,
    owned_project,
    owned_video,
    rate_limit,
    upload_rate_limit,
)
from app.core.config import settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models import (
    JobType,
    Project,
    ProjectStatus,
    Transcript,
    TranscriptStatus,
    UploadSession,
    UploadStatus,
    Video,
    VideoStatus,
)
from app.schemas.common import OkResponse, Page
from app.schemas.media import (
    JobOut,
    NarrationScriptOut,
    SignedUrlOut,
    UploadCompleteResponse,
    UploadInitRequest,
    UploadInitResponse,
    UploadStatusResponse,
    VideoUpdate,
    VideoOut,
)
from app.services.job_service import JobService
from app.services.storage import RangeSpec, get_storage, safe_filename, validate_key
from app.services.upload_service import UploadService
from app.services.usage_service import UsageService

log = get_logger(__name__)
router = APIRouter(tags=["Videos"])

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
STREAM_CHUNK = 512 * 1024


# ------------------------------------------------------------------ listing ---
@router.get("/projects/{project_id}/videos", response_model=Page[VideoOut])
def list_videos(project_id: str, db: DbSession, user: CurrentUser, params: PageParams) -> Page[VideoOut]:
    owned_project(db, user, project_id)
    rows = list(
        db.execute(
            select(Video)
            .where(Video.project_id == project_id)
            .order_by(Video.created_at.desc())
            .limit(params.limit)
            .offset(params.offset)
        ).scalars()
    )
    from sqlalchemy import func

    total = int(db.execute(select(func.count(Video.id)).where(Video.project_id == project_id)).scalar_one())
    return Page.build([VideoOut.model_validate(row) for row in rows], total, params.limit, params.offset)


@router.get("/videos/{video_id}", response_model=VideoOut)
def read_video(video_id: str, db: DbSession, user: CurrentUser) -> VideoOut:
    video = owned_video(db, user, video_id)
    return VideoOut.model_validate(video)


@router.get("/videos/{video_id}/narration-script", response_model=NarrationScriptOut)
def read_narration_script(video_id: str, db: DbSession, user: CurrentUser) -> NarrationScriptOut:
    """Return the stored narration script so the editor can display and revise it."""
    video = owned_video(db, user, video_id)
    script = video.narration_script or ""
    return NarrationScriptOut(
        video_id=video.id,
        narration_script=script,
        has_narration_script=bool(script.strip()),
        character_count=len(script),
        word_count=len(script.split()),
    )


@router.patch("/videos/{video_id}", response_model=VideoOut)
def update_video(video_id: str, payload: VideoUpdate, db: DbSession, user: CurrentUser) -> VideoOut:
    """Update editable video metadata.

    `narration_script` is the offline path to word-level timings: with a script
    present, transcription aligns it to the audio rather than needing ASR weights.
    A script change invalidates any existing transcript, so the next run is a
    fresh alignment rather than a mix of old and new timings.
    """
    video = owned_video(db, user, video_id)
    changed = False

    if payload.original_filename is not None:
        name = payload.original_filename.strip()
        if not name:
            raise ValidationError("A filename is required.", field="original_filename")
        video.original_filename = name[:400]
        changed = True

    if payload.narration_script is not None:
        script = payload.narration_script.strip()
        if script != (video.narration_script or ""):
            video.narration_script = script
            changed = True
            if script:
                # Timings from the previous script would be wrong; clear the stale
                # transcript so the UI cannot present them as current.
                for transcript in db.execute(
                    select(Transcript).where(Transcript.video_id == video.id)
                ).scalars():
                    if transcript.status == TranscriptStatus.COMPLETED.value:
                        transcript.status = TranscriptStatus.SUPERSEDED.value
                        transcript.error_message = "Superseded: the narration script changed."

    if changed:
        db.commit()
        db.refresh(video)
    return VideoOut.model_validate(video)


@router.delete("/videos/{video_id}", response_model=OkResponse)
def delete_video(video_id: str, db: DbSession, user: CurrentUser) -> OkResponse:
    video = owned_video(db, user, video_id)
    storage = get_storage()
    prefix = f"projects/{video.project_id}/videos/{video.id}"
    try:
        storage.delete_prefix(prefix)
    except Exception as exc:
        log.warning("video.cleanup_failed", video_id=video.id, error=str(exc))
    db.delete(video)
    db.commit()
    return OkResponse(message="Video deleted.")


# ------------------------------------------------------------------ uploads ---
@router.post(
    "/uploads/init",
    response_model=UploadInitResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(upload_rate_limit)],
)
def init_upload(payload: UploadInitRequest, request: Request, db: DbSession, user: CurrentUser) -> UploadInitResponse:
    """Start a chunked upload. Creates the project when none is supplied."""
    session = UploadService.init_upload(
        db,
        user,
        filename=payload.filename,
        size_bytes=payload.size_bytes,
        content_type=payload.content_type,
        project_id=payload.project_id,
        project_name=payload.project_name,
        chunk_size=payload.chunk_size,
    )
    return UploadInitResponse(
        upload_id=session.upload_id,
        project_id=session.project_id,
        video_id=session.video_id or "",
        chunk_size=session.chunk_size,
        total_chunks=session.total_chunks,
        received_chunks=sorted(int(k) for k in (session.received_chunks or {})),
        expires_at=session.expires_at,
        storage_backend=get_storage().name,
    )


@router.put(
    "/uploads/{upload_id}/chunk/{index}",
    response_model=UploadStatusResponse,
    dependencies=[Depends(upload_rate_limit)],
)
async def upload_chunk(
    upload_id: str,
    index: int,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> UploadStatusResponse:
    """Receive one chunk. Chunks may arrive in any order; retries are safe."""
    body = await request.body()
    if not body:
        raise ValidationError("Empty chunk.", field="chunk")
    session = UploadService.receive_chunk(db, user, upload_id, index, body)
    return _status_payload(session)


@router.post("/uploads/{upload_id}/chunk/{index}", response_model=UploadStatusResponse, include_in_schema=False)
async def upload_chunk_post(
    upload_id: str, index: int, request: Request, db: DbSession, user: CurrentUser
) -> UploadStatusResponse:
    return await upload_chunk(upload_id, index, request, db, user)


@router.get("/uploads/{upload_id}", response_model=UploadStatusResponse)
def upload_status(upload_id: str, db: DbSession, user: CurrentUser) -> UploadStatusResponse:
    return _status_payload(UploadService.status(db, user, upload_id))


@router.post("/uploads/{upload_id}/complete", response_model=UploadCompleteResponse)
def complete_upload(upload_id: str, db: DbSession, user: CurrentUser) -> UploadCompleteResponse:
    video, session, job = UploadService.complete_upload(db, user, upload_id)
    return UploadCompleteResponse(
        video=VideoOut.model_validate(video),
        job=JobOut.model_validate(job),
    )


@router.delete("/uploads/{upload_id}", response_model=OkResponse)
def abort_upload(upload_id: str, db: DbSession, user: CurrentUser) -> OkResponse:
    UploadService.abort(db, user, upload_id)
    return OkResponse(message="Upload cancelled.")


# ------------------------------------------------------- simple (small) upload ---
@router.post("/projects/{project_id}/videos", response_model=UploadCompleteResponse, dependencies=[Depends(upload_rate_limit)])
async def upload_small_file(
    project_id: str,
    db: DbSession,
    user: CurrentUser,
    file: Annotated[UploadFile, File(description="Video or audio file, up to the single-request limit")],
) -> UploadCompleteResponse:
    """Convenience path for smaller files posted in one request.

    Chunked/resumable uploads are the primary route; this streams the body to
    staging in bounded chunks (never buffering the file in memory or in React
    state) and then feeds the exact same completion logic.
    """
    project = owned_project(db, user, project_id)
    declared = int(getattr(file, "size", 0) or 0)
    session = UploadService.init_upload(
        db,
        user,
        filename=file.filename or "upload.mp4",
        size_bytes=max(declared, settings.media_min_upload_bytes),
        content_type=file.content_type or "",
        project_id=project.id,
    )
    # Reading the SpooledTemporaryFile is blocking I/O, so it runs off the loop.
    await asyncio.to_thread(UploadService.stage_stream, db, user, session.upload_id, file.file)
    video, session, job = UploadService.complete_upload(db, user, session.upload_id)
    return UploadCompleteResponse(
        video=VideoOut.model_validate(video),
        job=JobOut.model_validate(job),
    )


# --------------------------------------------------------------- processing ---
@router.post("/videos/{video_id}/transcribe", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def transcribe_video(video_id: str, db: DbSession, user: CurrentUser, language: str | None = Body(default=None, embed=True)) -> JobOut:
    video = owned_video(db, user, video_id)
    if video.status not in (VideoStatus.READY.value,):
        raise ConflictError("Wait for the video to finish processing before transcribing.", code="video_not_ready")
    if not video.has_audio:
        raise ConflictError("This video has no audio track to transcribe.", code="no_audio_track")
    active = JobService.active_for_user(db, user.id, video.project_id)
    if any(job.type == JobType.VIDEO_TRANSCRIBE.value and job.video_id == video.id for job in active):
        raise ConflictError("Transcription is already running for this video.", code="transcription_running")
    UsageService.check(db, user, "minutes_processed", (video.duration_seconds or 0) / 60.0)
    project = db.get(Project, video.project_id)
    if project:
        project.status = ProjectStatus.PROCESSING.value
        db.commit()
    job = JobService.create(
        db,
        type_=JobType.VIDEO_TRANSCRIBE.value,
        payload={"video_id": video.id, "language": language},
        user_id=user.id,
        project_id=video.project_id,
        video_id=video.id,
        priority=20,
    )
    return JobOut.model_validate(job)


@router.post("/videos/{video_id}/analyze-framing", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def analyze_framing(video_id: str, db: DbSession, user: CurrentUser, aspect_ratio: str | None = Query(default=None)) -> JobOut:
    video = owned_video(db, user, video_id)
    if video.status != VideoStatus.READY.value:
        raise ConflictError("Wait for the video to finish processing.", code="video_not_ready")
    active = JobService.active_for_user(db, user.id, video.project_id)
    if any(job.type == JobType.VIDEO_ANALYZE_FRAMING.value and job.video_id == video.id for job in active):
        raise ConflictError("Framing analysis is already running.", code="framing_running")
    job = JobService.create(
        db,
        type_=JobType.VIDEO_ANALYZE_FRAMING.value,
        payload={"video_id": video.id, "aspect_ratio": aspect_ratio or ""},
        user_id=user.id,
        project_id=video.project_id,
        video_id=video.id,
        priority=25,
    )
    return JobOut.model_validate(job)


@router.post("/videos/{video_id}/probe", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def reprobe_video(video_id: str, db: DbSession, user: CurrentUser) -> JobOut:
    video = owned_video(db, user, video_id)
    job = JobService.create(
        db,
        type_=JobType.VIDEO_PROBE.value,
        payload={"video_id": video.id},
        user_id=user.id,
        project_id=video.project_id,
        video_id=video.id,
        priority=10,
    )
    return JobOut.model_validate(job)


# ------------------------------------------------------------------ streams ---
@router.get("/videos/{video_id}/stream")
def stream_video(
    video_id: str,
    db: DbSession,
    user: CurrentUser,
    request: Request,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
    thumbnail: bool = Query(default=False, description="Serve the poster frame instead of the video"),
) -> Response:
    """Range-enabled media streaming for the in-browser player."""
    video = owned_video(db, user, video_id)
    key = video.thumbnail_key if thumbnail else video.storage_key
    if not key:
        raise NotFoundError("That media file is not available.", code="media_missing")
    content_type = "image/jpeg" if thumbnail else (video.content_type or "video/mp4")
    return _stream_key(key, range_header, content_type)


@router.get("/clips/{clip_id}/render/{render_id}/stream", include_in_schema=False)
def stream_render(clip_id: str, render_id: str, db: DbSession, user: CurrentUser, range_header: Annotated[str | None, Header(alias="Range")] = None) -> Response:
    from app.api.deps import owned_clip, owned_render

    clip = owned_clip(db, user, clip_id)
    render = owned_render(db, user, render_id)
    if render.clip_id != clip.id or not render.storage_key:
        raise NotFoundError("That render is not available.", code="render_missing")
    return _stream_key(render.storage_key, range_header, "video/mp4")


@router.get("/files/{key:path}")
def stream_file(key: str, db: DbSession, user: CurrentUser, range_header: Annotated[str | None, Header(alias="Range")] = None) -> Response:
    """Serve any object the user owns, with ownership enforced by key prefix.

    Keys are namespaced as `projects/{project_id}/...`; access is granted only
    when the current user owns that project.
    """
    safe_key = validate_key(key)
    parts = safe_key.split("/")
    if len(parts) < 2 or parts[0] != "projects":
        raise NotFoundError("File not found.", code="file_not_found")
    project = db.get(Project, parts[1])
    if project is None or project.user_id != user.id:
        # Do not disclose whether the object exists.
        raise NotFoundError("File not found.", code="file_not_found")
    content_type = "video/mp4"
    if safe_key.endswith((".jpg", ".jpeg")):
        content_type = "image/jpeg"
    elif safe_key.endswith(".png"):
        content_type = "image/png"
    elif safe_key.endswith(".ass"):
        content_type = "text/plain"
    return _stream_key(safe_key, range_header, content_type)


@router.get("/videos/{video_id}/signed-url", response_model=SignedUrlOut)
def video_signed_url(video_id: str, db: DbSession, user: CurrentUser, ttl: int = Query(default=3600, ge=60, le=86400)) -> SignedUrlOut:
    video = owned_video(db, user, video_id)
    storage = get_storage()
    url = storage.signed_url(video.storage_key, ttl)
    return SignedUrlOut(url=url, expires_in=ttl)


# ------------------------------------------------------------------ helpers ---
def _status_payload(session: UploadSession) -> UploadStatusResponse:
    received = sorted(int(k) for k in (session.received_chunks or {}))
    progress = (session.received_bytes / session.size_bytes * 100) if session.size_bytes else 0.0
    return UploadStatusResponse(
        upload_id=session.upload_id,
        video_id=session.video_id or "",
        project_id=session.project_id,
        filename=session.filename,
        status=session.status,
        size_bytes=session.size_bytes,
        received_bytes=session.received_bytes,
        received_chunks=received,
        total_chunks=session.total_chunks,
        chunk_size=session.chunk_size,
        progress=round(progress, 2),
    )


def _parse_range(range_header: str | None, size: int) -> RangeSpec | None:
    if not range_header:
        return None
    match = RANGE_RE.match(range_header.strip())
    if not match:
        return None
    start_text, end_text = match.group(1), match.group(2)
    if not start_text and not end_text:
        return None
    if not start_text:  # suffix range: last N bytes
        length = int(end_text)
        start = max(0, size - length)
        end = size - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    if start >= size:
        raise ValidationError("Requested range is outside the file.", field="range")
    end = min(end, size - 1)
    if end < start:
        raise ValidationError("Invalid byte range.", field="range")
    return RangeSpec(start=start, end=end)


def _stream_key(key: str, range_header: str | None, content_type: str) -> Response:
    storage = get_storage()
    info = storage.stat(key)
    size = info.size
    byte_range = _parse_range(range_header, size)

    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=60",
        "Content-Type": content_type,
        "X-Content-Type-Options": "nosniff",
    }
    if byte_range is None:
        headers["Content-Length"] = str(size)
        return StreamingResponse(storage.open_stream(key), status_code=200, headers=headers)

    headers["Content-Length"] = str(byte_range.length)
    headers["Content-Range"] = f"bytes {byte_range.start}-{byte_range.end}/{size}"
    return StreamingResponse(storage.open_stream(key, byte_range), status_code=206, headers=headers)
