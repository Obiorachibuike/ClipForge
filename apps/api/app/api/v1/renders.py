"""Render queue and export centre."""
from __future__ import annotations

from fastapi import APIRouter, Header, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession, PageParams, owned_clip, owned_project, owned_render
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models import (
    Clip,
    ClipStatus,
    Export,
    JobStatus,
    RenderJob,
    RenderStatus,
    utcnow,
)
from app.schemas.clips import ExportOut, ExportWithClip, RenderOut, RenderPresetOut, RenderRequest
from app.schemas.common import OkResponse, Page
from app.services.clip_service import ClipService
from app.services.job_service import JobService
from app.services.media.render_spec import RENDER_PRESETS
from app.services.storage import RangeSpec, get_storage

log = get_logger(__name__)
router = APIRouter(tags=["Renders & Exports"])


@router.get("/render-presets", response_model=list[RenderPresetOut])
def render_presets(user: CurrentUser) -> list[RenderPresetOut]:
    return [
        RenderPresetOut(
            key=key,
            label=preset["label"],
            width=preset["width"],
            height=preset["height"],
            aspect_ratio=preset["aspect_ratio"],
            fps=preset["fps"],
            bitrate=preset["bitrate"],
            max_seconds=preset["max_seconds"],
        )
        for key, preset in RENDER_PRESETS.items()
    ]


@router.post("/clips/{clip_id}/render", response_model=RenderOut, status_code=status.HTTP_202_ACCEPTED)
def start_render(
    clip_id: str,
    db: DbSession,
    user: CurrentUser,
    payload: RenderRequest | None = None,
) -> RenderOut:
    clip = owned_clip(db, user, clip_id)
    body = payload or RenderRequest()
    render = ClipService.enqueue_render(
        db,
        user,
        clip,
        preset=body.preset,
        aspect_ratio=body.aspect_ratio,
        fps=body.fps,
        priority=body.priority,
    )
    return RenderOut.model_validate(render)


@router.get("/clips/{clip_id}/renders", response_model=list[RenderOut])
def clip_renders(clip_id: str, db: DbSession, user: CurrentUser) -> list[RenderOut]:
    clip = owned_clip(db, user, clip_id)
    rows = db.execute(
        select(RenderJob).where(RenderJob.clip_id == clip.id).order_by(RenderJob.created_at.desc())
    ).scalars()
    return [RenderOut.model_validate(row) for row in rows]


@router.get("/projects/{project_id}/renders", response_model=Page[RenderOut])
def project_renders(
    project_id: str,
    db: DbSession,
    user: CurrentUser,
    params: PageParams,
    status_filter: str | None = Query(default=None, alias="status"),
) -> Page[RenderOut]:
    owned_project(db, user, project_id)
    stmt = select(RenderJob).where(RenderJob.project_id == project_id)
    count_stmt = select(func.count(RenderJob.id)).where(RenderJob.project_id == project_id)
    if status_filter:
        stmt = stmt.where(RenderJob.status == status_filter)
        count_stmt = count_stmt.where(RenderJob.status == status_filter)
    total = int(db.execute(count_stmt).scalar_one())
    rows = db.execute(
        stmt.order_by(RenderJob.created_at.desc()).limit(params.limit).offset(params.offset)
    ).scalars()
    return Page.build([RenderOut.model_validate(row) for row in rows], total, params.limit, params.offset)


@router.get("/renders/{render_id}", response_model=RenderOut)
def read_render(render_id: str, db: DbSession, user: CurrentUser) -> RenderOut:
    render = owned_render(db, user, render_id)
    return RenderOut.model_validate(render)


@router.post("/renders/{render_id}/cancel", response_model=RenderOut)
def cancel_render(render_id: str, db: DbSession, user: CurrentUser) -> RenderOut:
    render = owned_render(db, user, render_id)
    if render.status not in (RenderStatus.QUEUED.value, RenderStatus.RUNNING.value):
        raise ConflictError("This render has already finished.", code="render_finished")
    if render.job_id:
        job = JobService.get(db, render.job_id, user.id)
        JobService.request_cancel(db, job)
    if render.status == RenderStatus.QUEUED.value:
        render.status = RenderStatus.CANCELED.value
        render.stage = "cancelled"
        render.message = "Cancelled before starting"
        clip = db.get(Clip, render.clip_id)
        if clip and clip.status == ClipStatus.RENDERING.value:
            clip.status = ClipStatus.READY.value
    else:
        render.message = "Cancelling…"
    db.commit()
    db.refresh(render)
    return RenderOut.model_validate(render)


@router.post("/renders/{render_id}/retry", response_model=RenderOut, status_code=status.HTTP_202_ACCEPTED)
def retry_render(render_id: str, db: DbSession, user: CurrentUser) -> RenderOut:
    render = owned_render(db, user, render_id)
    if render.status not in (RenderStatus.FAILED.value, RenderStatus.CANCELED.value):
        raise ConflictError("Only failed or cancelled renders can be retried.", code="render_not_retryable")
    clip = db.get(Clip, render.clip_id)
    if clip is None:
        raise NotFoundError("Clip not found.", code="clip_not_found")
    new_render = ClipService.enqueue_render(
        db,
        user,
        clip,
        preset=render.preset,
        aspect_ratio=render.aspect_ratio,
        fps=render.fps,
        priority=40,
    )
    return RenderOut.model_validate(new_render)


@router.delete("/renders/{render_id}", response_model=OkResponse)
def delete_render(render_id: str, db: DbSession, user: CurrentUser) -> OkResponse:
    render = owned_render(db, user, render_id)
    if render.status in (RenderStatus.RUNNING.value, RenderStatus.QUEUED.value):
        raise ConflictError("Cancel the render before deleting it.", code="render_in_progress")
    storage = get_storage()
    if render.storage_key:
        try:
            storage.delete(render.storage_key)
        except Exception as exc:
            log.warning("render.delete_storage_failed", render_id=render.id, error=str(exc))
    db.delete(render)
    db.commit()
    return OkResponse(message="Render deleted.")


# ------------------------------------------------------------------ exports ---
@router.get("/projects/{project_id}/exports", response_model=Page[ExportWithClip])
def project_exports(
    project_id: str,
    db: DbSession,
    user: CurrentUser,
    params: PageParams,
    platform: str | None = Query(default=None, max_length=40),
) -> Page[ExportWithClip]:
    owned_project(db, user, project_id)
    stmt = select(Export).where(Export.project_id == project_id, Export.deleted_at.is_(None))
    count_stmt = select(func.count(Export.id)).where(Export.project_id == project_id, Export.deleted_at.is_(None))
    if platform:
        stmt = stmt.where(Export.platform == platform)
        count_stmt = count_stmt.where(Export.platform == platform)
    total = int(db.execute(count_stmt).scalar_one())
    rows = list(db.execute(stmt.order_by(Export.created_at.desc()).limit(params.limit).offset(params.offset)).scalars())
    clips = {
        clip.id: clip
        for clip in db.execute(select(Clip).where(Clip.id.in_([row.clip_id for row in rows]))).scalars()
    } if rows else {}
    items = []
    for row in rows:
        clip = clips.get(row.clip_id)
        item = ExportWithClip.model_validate(row)
        item.clip_title = clip.title if clip else ""
        item.clip_headline = clip.headline if clip else ""
        items.append(item)
    return Page.build(items, total, params.limit, params.offset)


@router.get("/exports", response_model=Page[ExportWithClip])
def list_exports(
    db: DbSession,
    user: CurrentUser,
    params: PageParams,
    project_id: str | None = Query(default=None),
    platform: str | None = Query(default=None, max_length=40),
) -> Page[ExportWithClip]:
    stmt = select(Export).where(Export.user_id == user.id, Export.deleted_at.is_(None))
    count_stmt = select(func.count(Export.id)).where(Export.user_id == user.id, Export.deleted_at.is_(None))
    if project_id:
        owned_project(db, user, project_id)
        stmt = stmt.where(Export.project_id == project_id)
        count_stmt = count_stmt.where(Export.project_id == project_id)
    if platform:
        stmt = stmt.where(Export.platform == platform)
        count_stmt = count_stmt.where(Export.platform == platform)
    total = int(db.execute(count_stmt).scalar_one())
    rows = list(db.execute(stmt.order_by(Export.created_at.desc()).limit(params.limit).offset(params.offset)).scalars())
    clips = {
        clip.id: clip
        for clip in db.execute(select(Clip).where(Clip.id.in_([row.clip_id for row in rows]))).scalars()
    } if rows else {}
    items = []
    for row in rows:
        clip = clips.get(row.clip_id)
        item = ExportWithClip.model_validate(row)
        item.clip_title = clip.title if clip else ""
        item.clip_headline = clip.headline if clip else ""
        items.append(item)
    return Page.build(items, total, params.limit, params.offset)


@router.get("/exports/{export_id}/download")
def download_export(
    export_id: str,
    db: DbSession,
    user: CurrentUser,
    range_header: str | None = Header(default=None, alias="Range"),
    attachment: bool = Query(default=True),
) -> Response:
    export = db.get(Export, export_id)
    if export is None or export.user_id != user.id or export.deleted_at is not None:
        raise NotFoundError("Export not found.", code="export_not_found")
    storage = get_storage()
    info = storage.stat(export.storage_key)
    byte_range: RangeSpec | None = None
    if range_header and range_header.startswith("bytes="):
        try:
            spec = range_header[6:].split("-", 1)
            start = int(spec[0]) if spec[0] else 0
            end = int(spec[1]) if len(spec) > 1 and spec[1] else info.size - 1
            end = min(end, info.size - 1)
            if 0 <= start <= end:
                byte_range = RangeSpec(start=start, end=end)
        except ValueError:
            byte_range = None

    dispatch = "attachment" if attachment else "inline"
    headers = {
        "Content-Type": "video/mp4",
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'{dispatch}; filename="{export.filename or "clip.mp4"}"',
        "Cache-Control": "private, max-age=300",
    }
    status_code = 200
    if byte_range is not None:
        headers["Content-Range"] = f"bytes {byte_range.start}-{byte_range.end}/{info.size}"
        headers["Content-Length"] = str(byte_range.length)
        status_code = 206
    else:
        headers["Content-Length"] = str(info.size)

    export.download_count = int(export.download_count or 0) + 1
    export.last_downloaded_at = utcnow()
    db.commit()
    return StreamingResponse(storage.open_stream(export.storage_key, byte_range), status_code=status_code, headers=headers)


@router.get("/exports/{export_id}/thumbnail")
def export_thumbnail(export_id: str, db: DbSession, user: CurrentUser) -> Response:
    export = db.get(Export, export_id)
    if export is None or export.user_id != user.id or not export.thumbnail_key:
        raise NotFoundError("Thumbnail not found.", code="thumbnail_not_found")
    storage = get_storage()
    return StreamingResponse(
        storage.open_stream(export.thumbnail_key),
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=600"},
    )


@router.delete("/exports/{export_id}", response_model=OkResponse)
def delete_export(export_id: str, db: DbSession, user: CurrentUser) -> OkResponse:
    export = db.get(Export, export_id)
    if export is None or export.user_id != user.id:
        raise NotFoundError("Export not found.", code="export_not_found")
    storage = get_storage()
    for key in (export.storage_key, export.thumbnail_key):
        if key:
            try:
                storage.delete(key)
            except Exception as exc:
                log.warning("export.delete_storage_failed", export_id=export.id, error=str(exc))
    db.delete(export)
    db.commit()
    return OkResponse(message="Export deleted.")
