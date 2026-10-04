"""Clip creation, boundary snapping and edit-state management."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models import (
    AnalysisKind,
    CandidateStatus,
    Clip,
    ClipCandidate,
    ClipStatus,
    JobType,
    Project,
    RenderJob,
    RenderPreset,
    RenderStatus,
    Transcript,
    TranscriptStatus,
    TranscriptWord,
    User,
    Video,
    VideoAnalysis,
)
from app.services import events as ev
from app.services.job_service import JobService
from app.services.media.ass import CAPTION_PRESETS, CaptionConfig
from app.services.media.render_spec import RENDER_PRESETS, resolve_dimensions
from app.services.usage_service import UsageService

log = get_logger(__name__)

DEFAULT_SNAP_TOLERANCE = 1.6  # seconds we are willing to expand to reach a clean cut point
MIN_CLIP_DURATION = 3.0
MAX_CLIP_DURATION = 300.0


class ClipService:
    # ------------------------------------------------------------- creation ---
    @staticmethod
    def default_caption_config(preset: str | None = None) -> dict[str, Any]:
        key = (preset or settings.default_caption_preset or "bold").lower()
        if key not in CAPTION_PRESETS:
            key = "bold"
        config = CaptionConfig.from_dict({"preset": key})
        payload = config.to_dict()
        payload["enabled"] = True
        return payload

    @staticmethod
    def default_background() -> dict:
        return {"mode": "smart_fill", "color": "#0B0B12", "blur_sigma": 28.0}

    @staticmethod
    def default_audio_config() -> dict:
        return {"mode": "original", "gain_db": 0.0, "normalize": True, "fade_in": 0.0, "fade_out": 0.0}

    @staticmethod
    def create_from_candidate(
        db: Session,
        user: User,
        candidate: ClipCandidate,
        *,
        title: str = "",
        headline: str = "",
        preset: str = RenderPreset.GENERIC_VERTICAL.value,
        aspect_ratio: str | None = None,
        caption_preset: str | None = None,
        auto_framing: bool = True,
    ) -> Clip:
        video = db.get(Video, candidate.video_id)
        if video is None:
            raise NotFoundError("The source video for this moment is missing.", code="video_not_found")

        width, height, ratio = resolve_dimensions(preset, aspect_ratio or video.aspect_ratio)

        crop = {"mode": "auto" if auto_framing else "center", "focus_x": 0.5, "focus_y": 0.5, "zoom": 1.0}
        framing = ClipService.framing_analysis(db, video.id)
        if framing:
            crop["strategy"] = framing.get("framing_strategy", "")
            crop["keyframes"] = framing.get("keyframes", [])
            crop["detector"] = framing.get("detector", "")

        clip = Clip(
            project_id=candidate.project_id,
            video_id=video.id,
            candidate_id=candidate.id,
            user_id=user.id,
            title=(title or candidate.title or "Untitled clip")[:300],
            headline=(headline or ClipService.default_headline(candidate))[:300],
            headline_position="top",
            headline_style={
                "font_family": "DejaVu Sans",
                "font_size": 46,
                "bold": True,
                "background": "box",
                "background_color": "#0B0B12",
                "background_opacity": 0.62,
                "text_color": "#FFFFFF",
                "margin_v": 140,
            },
            headline_variants=list((candidate.meta or {}).get("headline_variants") or []),
            start_time=float(candidate.start_time),
            end_time=float(candidate.end_time),
            duration=float(candidate.duration),
            aspect_ratio=ratio,
            crop=crop,
            background=ClipService.default_background(),
            caption_config=ClipService.default_caption_config(caption_preset or user.default_caption_preset),
            audio_config=ClipService.default_audio_config(),
            caption_overrides={},
            status=ClipStatus.DRAFT.value,
            source="candidate",
        )
        db.add(clip)
        candidate.status = CandidateStatus.CONVERTED.value
        db.commit()
        db.refresh(clip)
        ev.get_event_bus().emit(
            ev.EVENT_CLIP_CREATED,
            user_id=user.id,
            project_id=clip.project_id,
            payload={
                "clip_id": clip.id,
                "candidate_id": candidate.id,
                "title": clip.title,
                "start_time": clip.start_time,
                "end_time": clip.end_time,
                "duration": clip.duration,
                "status": clip.status,
            },
        )
        log.info("clip.created", clip_id=clip.id, candidate_id=candidate.id, duration=clip.duration)
        return clip

    @staticmethod
    def create_manual(
        db: Session,
        user: User,
        project: Project,
        video: Video,
        *,
        start: float,
        end: float,
        title: str = "",
        preset: str = RenderPreset.GENERIC_VERTICAL.value,
        aspect_ratio: str | None = None,
        caption_preset: str | None = None,
    ) -> Clip:
        start, end = ClipService.snap_and_validate(db, video, start, end)
        _, _, ratio = resolve_dimensions(preset, aspect_ratio or project.target_aspect_ratio)
        clip = Clip(
            project_id=project.id,
            video_id=video.id,
            user_id=user.id,
            title=(title or f"Manual clip {int(start)}s–{int(end)}s")[:300],
            headline="",
            start_time=start,
            end_time=end,
            duration=end - start,
            aspect_ratio=ratio,
            crop={"mode": "auto", "focus_x": 0.5, "focus_y": 0.5, "zoom": 1.0},
            background=ClipService.default_background(),
            caption_config=ClipService.default_caption_config(caption_preset or user.default_caption_preset),
            audio_config=ClipService.default_audio_config(),
            status=ClipStatus.DRAFT.value,
            source="manual",
        )
        db.add(clip)
        db.commit()
        db.refresh(clip)
        return clip

    @staticmethod
    def default_headline(candidate: ClipCandidate) -> str:
        variants = (candidate.meta or {}).get("headline_variants") or []
        if variants:
            return str(variants[0])[:160]
        if candidate.hook:
            text = candidate.hook.strip()
            return (text[:120] + "…") if len(text) > 120 else text
        return (candidate.title or "")[:160]

    # ------------------------------------------------------------- boundary ---
    @staticmethod
    def snap_and_validate(db: Session, video: Video, start: float, end: float) -> tuple[float, float]:
        """Snap cut points to sentence boundaries, natural pauses and speaker
        changes so clips never start mid-word."""
        if end <= start:
            raise ValidationError("The clip end must come after its start.", field="end_time")
        if video.duration_seconds and start > video.duration_seconds:
            raise ValidationError("The clip start is beyond the end of the video.", field="start_time")
        limit = video.duration_seconds or end
        end = min(end, limit)
        duration = end - start
        if duration < MIN_CLIP_DURATION:
            raise ValidationError(
                f"Clips must be at least {MIN_CLIP_DURATION:.0f} seconds long.", field="duration"
            )
        if duration > MAX_CLIP_DURATION:
            raise ValidationError(
                f"Clips cannot exceed {MAX_CLIP_DURATION:.0f} seconds on this deployment.", field="duration"
            )

        transcript = db.execute(
            select(Transcript)
            .where(Transcript.video_id == video.id, Transcript.status == TranscriptStatus.COMPLETED.value)
            .order_by(Transcript.created_at.desc())
        ).scalars().first()
        if transcript is None:
            return round(start, 3), round(end, 3)

        words = list(
            db.execute(
                select(TranscriptWord)
                .where(
                    TranscriptWord.transcript_id == transcript.id,
                    TranscriptWord.start_time >= start - DEFAULT_SNAP_TOLERANCE * 2,
                    TranscriptWord.end_time <= end + DEFAULT_SNAP_TOLERANCE * 2,
                )
                .order_by(TranscriptWord.start_time)
            ).scalars()
        )
        if words:
            start = _snap_start(words, start)
            end = _snap_end(words, end)
        return round(start, 3), round(min(end, limit), 3)

    # ---------------------------------------------------------------- reads ---
    @staticmethod
    def framing_analysis(db: Session, video_id: str) -> dict:
        row = db.execute(
            select(VideoAnalysis)
            .where(VideoAnalysis.video_id == video_id, VideoAnalysis.kind == AnalysisKind.FRAMING.value)
            .order_by(VideoAnalysis.created_at.desc())
        ).scalars().first()
        if row is None or row.status != "completed":
            return {}
        return dict(row.data or {})

    @staticmethod
    def latest_render(db: Session, clip_id: str) -> RenderJob | None:
        return db.execute(
            select(RenderJob).where(RenderJob.clip_id == clip_id).order_by(RenderJob.created_at.desc())
        ).scalars().first()

    # ---------------------------------------------------------------- edits ---
    @staticmethod
    def duplicate(db: Session, user: User, clip: Clip, title: str | None = None) -> Clip:
        copy = Clip(
            project_id=clip.project_id,
            video_id=clip.video_id,
            candidate_id=clip.candidate_id,
            user_id=user.id,
            title=(title or f"{clip.title} (copy)")[:300],
            headline=clip.headline,
            headline_position=clip.headline_position,
            headline_style=dict(clip.headline_style or {}),
            headline_variants=list(clip.headline_variants or []),
            start_time=clip.start_time,
            end_time=clip.end_time,
            duration=clip.duration,
            aspect_ratio=clip.aspect_ratio,
            crop=dict(clip.crop or {}),
            background=dict(clip.background or {}),
            caption_config=dict(clip.caption_config or {}),
            caption_overrides=dict(clip.caption_overrides or {}),
            audio_config=dict(clip.audio_config or {}),
            caption_style_id=clip.caption_style_id,
            status=ClipStatus.DRAFT.value,
            source="duplicate",
        )
        db.add(copy)
        db.commit()
        db.refresh(copy)
        return copy

    @staticmethod
    def apply_update(db: Session, clip: Clip, payload: dict[str, Any]) -> Clip:
        start = float(payload.get("start_time", clip.start_time))
        end = float(payload.get("end_time", clip.end_time))
        if "start_time" in payload or "end_time" in payload:
            video = db.get(Video, clip.video_id)
            if video is not None:
                start, end = ClipService.snap_and_validate(db, video, start, end)
            clip.start_time, clip.end_time = start, end
            clip.duration = round(end - start, 3)

        for field in (
            "title",
            "headline",
            "headline_position",
            "headline_style",
            "aspect_ratio",
            "crop",
            "background",
            "caption_config",
            "caption_overrides",
            "audio_config",
            "caption_style_id",
            "status",
            "is_favorite",
            "notes",
        ):
            if field in payload and payload[field] is not None:
                setattr(clip, field, payload[field])

        if "caption_style_id" in payload and payload["caption_style_id"]:
            from app.models import CaptionStyle

            style = db.get(CaptionStyle, payload["caption_style_id"])
            if style is not None and (style.user_id in (None, clip.user_id)):
                merged = dict(clip.caption_config or {})
                merged.update(style.config or {})
                clip.caption_config = merged

        db.commit()
        db.refresh(clip)
        ev.get_event_bus().emit(
            ev.EVENT_CLIP_UPDATED,
            user_id=clip.user_id,
            project_id=clip.project_id,
            payload={"clip_id": clip.id, "status": clip.status, "updated_at": clip.updated_at.isoformat()},
        )
        return clip

    # --------------------------------------------------------------- render ---
    @staticmethod
    def enqueue_render(
        db: Session,
        user: User,
        clip: Clip,
        *,
        preset: str = RenderPreset.GENERIC_VERTICAL.value,
        aspect_ratio: str | None = None,
        fps: int | None = None,
        priority: int = 50,
    ) -> RenderJob:
        if clip.status == ClipStatus.RENDERING.value:
            raise ConflictError("This clip is already rendering.", code="render_in_progress")
        UsageService.check(db, user, "renders", 1)

        width, height, ratio = resolve_dimensions(preset, aspect_ratio or clip.aspect_ratio)
        if aspect_ratio and aspect_ratio != clip.aspect_ratio:
            clip.aspect_ratio = ratio
            db.commit()

        queued = db.execute(
            select(RenderJob).where(
                RenderJob.clip_id == clip.id,
                RenderJob.status.in_([RenderStatus.QUEUED.value, RenderStatus.RUNNING.value]),
            )
        ).scalars().first()
        if queued is not None:
            raise ConflictError("A render for this clip is already queued.", code="render_queued")

        render = RenderJob(
            clip_id=clip.id,
            project_id=clip.project_id,
            user_id=user.id,
            preset=preset,
            aspect_ratio=ratio,
            width=width,
            height=height,
            fps=int(fps or RENDER_PRESETS.get(preset, {}).get("fps") or 30),
            bitrate=str(RENDER_PRESETS.get(preset, {}).get("bitrate") or settings.render_default_bitrate),
            status=RenderStatus.QUEUED.value,
            stage="queued",
            message="Queued",
            spec={},
        )
        db.add(render)
        db.commit()
        db.refresh(render)

        job = JobService.create(
            db,
            type_=JobType.CLIP_RENDER.value,
            payload={"render_id": render.id, "clip_id": clip.id},
            user_id=user.id,
            project_id=clip.project_id,
            video_id=clip.video_id,
            clip_id=clip.id,
            priority=max(0, min(200, priority)),
            max_attempts=2,
        )
        render.job_id = job.id
        db.commit()
        log.info("render.queued", render_id=render.id, clip_id=clip.id, job_id=job.id)
        return render


# ---------------------------------------------------------------- snapping ---
SENTENCE_PUNCTUATION = (".", "!", "?", "…")


def _snap_start(words, target: float) -> float:
    """Prefer a clean opening: the first word whose predecessor ended a sentence
    or was followed by a pause, within the tolerance window."""
    tolerance = DEFAULT_SNAP_TOLERANCE
    candidates = [
        word
        for word in words
        if abs(word.start_time - target) <= tolerance or (target <= word.start_time <= target + tolerance)
    ]
    if not candidates:
        return target
    for index, word in enumerate(words):
        if word not in candidates:
            continue
        previous = words[index - 1] if index > 0 else None
        gap = (word.start_time - previous.end_time) if previous else 1.0
        ends_sentence = bool(previous and previous.word.strip().endswith(SENTENCE_PUNCTUATION))
        if ends_sentence or gap >= 0.35:
            return float(word.start_time)
    return float(candidates[0].start_time)


def _snap_end(words, target: float) -> float:
    """Prefer a clean closing: the last word of a sentence or before a pause."""
    tolerance = DEFAULT_SNAP_TOLERANCE
    candidates = [
        word
        for word in words
        if abs(word.end_time - target) <= tolerance or (target - tolerance <= word.end_time <= target)
    ]
    if not candidates:
        return target
    for word in reversed(candidates):
        if word.word.strip().endswith(SENTENCE_PUNCTUATION):
            return float(word.end_time)
    for index, word in enumerate(words):
        if word not in candidates:
            continue
        following = words[index + 1] if index + 1 < len(words) else None
        gap = (following.start_time - word.end_time) if following else 1.0
        if gap >= 0.35:
            return float(word.end_time)
    return float(candidates[-1].end_time)
