"""Render job: turn an edited clip into a real MP4 with real progress."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, CancelledError_, MediaError, NotFoundError
from app.core.logging import get_logger
from app.models import (
    AnalysisKind,
    Clip,
    ClipStatus,
    Export,
    Job,
    Project,
    RenderJob,
    RenderStatus,
    Transcript,
    TranscriptStatus,
    TranscriptWord,
    Video,
    VideoAnalysis,
)
from app.services import events as ev
from app.services.job_service import JobService
from app.services.media import ffmpeg
from app.services.media.ass import CaptionConfig, CaptionWord
from app.services.media.render_spec import build_ass, build_ffmpeg_args, build_plan
from app.services.pipeline.workspace import workspace_for
from app.services.storage import get_storage
from app.services.usage_service import UsageService

log = get_logger(__name__)


def run(db: Session, job: Job) -> dict:
    render_id = job.payload.get("render_id") or (job.result or {}).get("render_id")
    render = db.get(RenderJob, render_id) if render_id else _render_for_job(db, job)
    if render is None:
        raise NotFoundError("Render request not found.", code="render_not_found")

    clip = db.get(Clip, render.clip_id)
    if clip is None:
        raise NotFoundError("Clip not found for render.", code="clip_not_found")
    video = db.get(Video, clip.video_id)
    if video is None:
        raise NotFoundError("Source video not found for render.", code="video_not_found")
    project = db.get(Project, clip.project_id)

    render.status = RenderStatus.RUNNING.value
    render.started_at = datetime.now(UTC)
    render.error_code = ""
    render.error_message = ""
    clip.status = ClipStatus.RENDERING.value
    db.commit()

    JobService.report_progress(db, job, stage="prepare", progress=3, message="Preparing render", force=True)
    ev.get_event_bus().emit(
        ev.EVENT_RENDER_STARTED,
        user_id=render.user_id,
        project_id=render.project_id,
        job_id=job.id,
        payload={"render_id": render.id, "clip_id": clip.id, "progress": 0, "status": "running"},
    )

    try:
        result = _render(db, job, render, clip, video, project)
    except CancelledError_:
        _mark_cancelled(db, render, clip, job)
        raise
    except AppError as exc:
        _mark_failed(db, render, clip, job, exc)
        raise
    except Exception as exc:  # unexpected: log full detail, expose a safe message
        log.exception("render.crashed", render_id=render.id)
        _mark_failed(db, render, clip, job, AppError("Rendering failed unexpectedly.", code="render_failed"))
        raise

    render.status = RenderStatus.SUCCEEDED.value
    render.progress = 100.0
    render.stage = "completed"
    render.message = "Ready to download"
    render.finished_at = datetime.now(UTC)
    render.duration_seconds = (
        (render.finished_at - render.started_at).total_seconds() if render.started_at else 0.0
    )
    clip.status = ClipStatus.RENDERED.value
    clip.last_rendered_at = render.finished_at
    clip.render_count = int(clip.render_count or 0) + 1
    db.commit()

    ev.get_event_bus().emit(
        ev.EVENT_RENDER_COMPLETED,
        user_id=render.user_id,
        project_id=render.project_id,
        job_id=job.id,
        payload={
            "render_id": render.id,
            "clip_id": clip.id,
            "progress": 100,
            "status": "succeeded",
            "storage_key": render.storage_key,
            "size_bytes": render.output_size_bytes,
            "duration": render.output_duration,
            "warnings": render.warnings,
        },
    )
    return result


def _render_for_job(db: Session, job: Job) -> RenderJob | None:
    return db.execute(select(RenderJob).where(RenderJob.job_id == job.id)).scalars().first()


def _render(db: Session, job: Job, render: RenderJob, clip: Clip, video: Video, project: Project | None) -> dict:
    storage = get_storage()
    transcript = _primary_transcript(db, video.id)
    words = _clip_words(db, transcript, clip) if transcript else []

    framing = _framing_keyframes(db, video.id, clip)
    plan = build_plan(
        clip_id=clip.id,
        video_id=video.id,
        start=clip.start_time,
        end=clip.end_time,
        source_duration=video.duration_seconds or 0.0,
        source_width=video.width or 1920,
        source_height=video.height or 1080,
        preset=render.preset,
        aspect_ratio=clip.aspect_ratio or render.aspect_ratio,
        fps=render.fps,
        crop=clip.crop,
        background=clip.background,
        audio=clip.audio_config,
        caption_config=clip.caption_config,
        caption_style_name=(clip.caption_style.name if clip.caption_style else ""),
        words=words,
        headline={
            "text": clip.headline,
            "position": clip.headline_position,
            **(clip.headline_style or {}),
        },
        captions_enabled=bool((clip.caption_config or {}).get("enabled", True)),
        framing_keyframes=framing,
    )

    if not words and bool((clip.caption_config or {}).get("enabled", True)) and not clip.headline:
        plan.warnings.append("captions:none_available")

    render.spec = plan.to_dict()
    render.aspect_ratio = plan.aspect_ratio
    render.width = plan.width
    render.height = plan.height
    render.warnings = plan.warnings
    db.commit()

    with workspace_for(job.id) as workdir:
        source = storage.materialize(video.storage_key, workdir)
        JobService.check_cancelled(db, job)

        ass_path: str | None = None
        document = build_ass(plan, edited_words=_edited_words(clip))
        if document:
            ass_file = workdir / "captions.ass"
            ass_file.write_text(document, encoding="utf-8")
            ass_path = str(ass_file)

        music_path: str | None = None
        if plan.audio_mode == "mixed" and plan.music_key:
            try:
                music_path = str(storage.materialize(plan.music_key, workdir / "music"))
            except Exception as exc:
                plan.warnings.append("audio:music_unavailable")
                log.warning("render.music_missing", render_id=render.id, error=str(exc)[:200])

        output = workdir / "output.mp4"
        args = build_ffmpeg_args(
            plan, source_path=str(source), target_path=str(output), ass_path=ass_path, music_path=music_path
        )
        log.info(
            "render.starting",
            render_id=render.id,
            clip_id=clip.id,
            duration=plan.duration,
            resolution=f"{plan.width}x{plan.height}",
            captions=bool(ass_path),
            preset=render.preset,
        )

        def on_progress(pct: float) -> None:
            scaled = 8.0 + (pct / 100.0) * 82.0
            render.progress = round(scaled, 2)
            render.stage = "encoding"
            render.message = f"Encoding {pct:.0f}%"
            JobService.report_progress(
                db,
                job,
                stage="encoding",
                progress=scaled,
                message=f"Rendering {clip.title or 'clip'} · {pct:.0f}%",
                extra={"render_id": render.id, "clip_id": clip.id},
            )

        ffmpeg.run_ffmpeg(
            args,
            job_id=job.id,
            total_duration=plan.duration,
            progress_cb=on_progress,
            timeout=None,
        )
        if not output.exists() or output.stat().st_size == 0:
            raise MediaError("The renderer produced no output.", code="render_empty_output")

        JobService.report_progress(db, job, stage="upload", progress=92, message="Storing rendered clip")
        key = f"projects/{clip.project_id}/clips/{clip.id}/renders/{render.id}.mp4"
        storage.put_file(key, output, "video/mp4")

        thumbnail_key = ""
        try:
            poster = workdir / "poster.jpg"
            ffmpeg.extract_thumbnail(output, poster, at=min(1.0, plan.duration / 3), width=540)
            thumbnail_key = f"projects/{clip.project_id}/clips/{clip.id}/renders/{render.id}.jpg"
            storage.put_file(thumbnail_key, poster, "image/jpeg")
        except Exception as exc:
            log.warning("render.thumbnail_failed", render_id=render.id, error=str(exc)[:200])

        output_duration = ffmpeg.media_duration(output) or plan.duration
        size_bytes = output.stat().st_size

        render.storage_key = key
        render.thumbnail_key = thumbnail_key
        render.output_size_bytes = size_bytes
        render.output_duration = round(output_duration, 3)
        db.commit()

        _record_export(db, render, clip, key, thumbnail_key, size_bytes, output_duration)

    UsageService.record(
        db,
        user_id=render.user_id,
        metric="renders",
        quantity=1,
        unit="renders",
        project_id=render.project_id,
        clip_id=clip.id,
        job_id=job.id,
        provider="ffmpeg",
    )
    UsageService.record(
        db,
        user_id=render.user_id,
        metric="storage_bytes",
        quantity=float(render.output_size_bytes),
        unit="bytes",
        project_id=render.project_id,
        clip_id=clip.id,
        job_id=job.id,
        provider="ffmpeg",
    )
    if project:
        db.commit()

    return {
        "render_id": render.id,
        "clip_id": clip.id,
        "storage_key": render.storage_key,
        "size_bytes": render.output_size_bytes,
        "duration": render.output_duration,
        "resolution": f"{render.width}x{render.height}",
        "warnings": render.warnings,
    }


def _record_export(
    db: Session,
    render: RenderJob,
    clip: Clip,
    storage_key: str,
    thumbnail_key: str,
    size_bytes: int,
    duration: float,
) -> None:
    export = Export(
        render_id=render.id,
        clip_id=clip.id,
        project_id=clip.project_id,
        user_id=render.user_id,
        platform=render.preset,
        filename=f"{_slug(clip.title or clip.headline or 'clip')}-{clip.id[:6]}.mp4",
        storage_key=storage_key,
        thumbnail_key=thumbnail_key,
        size_bytes=size_bytes,
        duration_seconds=duration,
        width=render.width,
        height=render.height,
    )
    db.add(export)
    db.commit()
    ev.get_event_bus().emit(
        ev.EVENT_EXPORT_READY,
        user_id=render.user_id,
        project_id=clip.project_id,
        job_id=None,
        payload={
            "export_id": export.id,
            "clip_id": clip.id,
            "render_id": render.id,
            "platform": export.platform,
            "size_bytes": size_bytes,
            "duration": duration,
            "filename": export.filename,
        },
    )


def _slug(text: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", (text or "clip").lower()).strip("-")
    return (slug or "clip")[:60]


def _mark_cancelled(db: Session, render: RenderJob, clip: Clip, job: Job) -> None:
    render.status = RenderStatus.CANCELED.value
    render.stage = "cancelled"
    render.message = "Cancelled"
    render.finished_at = datetime.now(UTC)
    clip.status = ClipStatus.READY.value
    db.commit()
    ev.get_event_bus().emit(
        ev.EVENT_RENDER_CANCELLED,
        user_id=render.user_id,
        project_id=render.project_id,
        job_id=job.id,
        payload={"render_id": render.id, "clip_id": clip.id, "status": "canceled"},
    )


def _mark_failed(db: Session, render: RenderJob, clip: Clip, job: Job, exc: BaseException) -> None:
    from app.core.errors import user_message

    render.status = RenderStatus.FAILED.value
    render.stage = "failed"
    render.attempts = int(render.attempts or 0) + 1
    render.error_code = exc.code if isinstance(exc, AppError) else "render_failed"
    render.error_message = user_message(exc)
    render.finished_at = datetime.now(UTC)
    clip.status = ClipStatus.FAILED.value
    db.commit()
    ev.get_event_bus().emit(
        ev.EVENT_RENDER_FAILED,
        user_id=render.user_id,
        project_id=render.project_id,
        job_id=job.id,
        payload={
            "render_id": render.id,
            "clip_id": clip.id,
            "status": "failed",
            "error_code": render.error_code,
            "error_message": render.error_message,
        },
    )


# ------------------------------------------------------------------ helpers ---
def _primary_transcript(db: Session, video_id: str) -> Transcript | None:
    return db.execute(
        select(Transcript)
        .where(Transcript.video_id == video_id, Transcript.status == TranscriptStatus.COMPLETED.value)
        .order_by(Transcript.created_at.desc())
    ).scalars().first()


def _clip_words(db: Session, transcript: Transcript | None, clip: Clip) -> list[CaptionWord]:
    """Transcript words inside the clip window, re-based to clip time."""
    if transcript is None:
        return []
    rows = db.execute(
        select(TranscriptWord)
        .where(
            TranscriptWord.transcript_id == transcript.id,
            TranscriptWord.end_time >= clip.start_time,
            TranscriptWord.start_time <= clip.end_time,
        )
        .order_by(TranscriptWord.start_time)
    ).scalars()
    words: list[CaptionWord] = []
    for index, row in enumerate(rows):
        start = max(0.0, row.start_time - clip.start_time)
        end = max(start + 0.05, min(row.end_time, clip.end_time) - clip.start_time)
        words.append(
            CaptionWord(
                text=row.word,
                start=round(start, 3),
                end=round(end, 3),
                index=index,
                speaker=row.speaker or "",
            )
        )
    # Guarantee monotonic, non-overlapping cue timing.
    for previous, current in zip(words, words[1:]):
        if current.start < previous.end:
            midpoint = (previous.start + current.end) / 2.0
            previous.end = round(midpoint, 3)
            current.start = round(midpoint, 3)
    return words


def _edited_words(clip: Clip) -> dict[int, str]:
    overrides = (clip.caption_overrides or {}).get("words") or {}
    result: dict[int, str] = {}
    for key, value in overrides.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        text = str(value).strip()
        if text:
            result[index] = text[:80]
    return result


def _framing_keyframes(db: Session, video_id: str, clip: Clip) -> list[dict]:
    crop_mode = str((clip.crop or {}).get("mode") or "auto")
    if crop_mode not in ("auto", "track"):
        return []
    row = db.execute(
        select(VideoAnalysis)
        .where(VideoAnalysis.video_id == video_id, VideoAnalysis.kind == AnalysisKind.FRAMING.value)
        .order_by(VideoAnalysis.created_at.desc())
    ).scalars().first()
    if row is None or row.status != "completed":
        return []
    keyframes = list((row.data or {}).get("keyframes") or [])
    clip_crop = clip.crop or {}
    if clip_crop.get("strategy"):
        keyframes = [kf for kf in keyframes]  # strategy is informational
    return keyframes
