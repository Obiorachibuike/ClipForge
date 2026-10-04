"""Candidates, clips, captions, headlines and crop control.

This router holds the clip review + lightweight editor surface. Every mutation
writes durable edit state that the renderer consumes, so the browser never holds
the source of truth.
"""
from __future__ import annotations

from fastapi import APIRouter, Query, status
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession, PageParams, owned_clip, owned_project, owned_video
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models import (
    CandidateStatus,
    Clip,
    ClipCandidate,
    ClipStatus,
    Project,
    RenderStatus,
    User,
    Video,
    utcnow,
)
from app.schemas.clips import (
    CandidateBulkUpdate,
    CandidateOut,
    CandidateScoreDetail,
    CandidateUpdate,
    CaptionCue,
    CaptionResponse,
    CaptionStyleCreate,
    CaptionStyleOut,
    CaptionUpdateRequest,
    CaptionWord,
    ClipCreate,
    ClipCropUpdate,
    ClipDetail,
    ClipDuplicateRequest,
    ClipOut,
    ClipUpdate,
    HeadlineGenerateRequest,
    HeadlineResponse,
    HeadlineSelectRequest,
    RenderOut,
)
from app.schemas.common import OkResponse, Page
from app.schemas.media import JobOut, VideoOut
from app.services.caption_service import CaptionService
from app.services.clip_service import ClipService
from app.services.headline_service import HeadlineService
from app.services.media.ass import CAPTION_PRESETS
from app.services.pipeline.analyzer import DEFAULT_WEIGHTS

log = get_logger(__name__)
router = APIRouter(tags=["Clips"])


# --------------------------------------------------------------- candidates ---
@router.get("/projects/{project_id}/candidates", response_model=Page[CandidateOut])
def list_candidates(
    project_id: str,
    db: DbSession,
    user: CurrentUser,
    params: PageParams,
    status_filter: str | None = Query(default=None, alias="status"),
    min_score: int | None = Query(default=None, ge=0, le=100),
    sort: str = Query(default="score", pattern="^(score|start|rank)$"),
) -> Page[CandidateOut]:
    owned_project(db, user, project_id)
    stmt = select(ClipCandidate).where(ClipCandidate.project_id == project_id)
    count_stmt = select(func.count(ClipCandidate.id)).where(ClipCandidate.project_id == project_id)
    if status_filter:
        stmt = stmt.where(ClipCandidate.status == status_filter)
        count_stmt = count_stmt.where(ClipCandidate.status == status_filter)
    if min_score is not None:
        stmt = stmt.where(ClipCandidate.score >= min_score)
        count_stmt = count_stmt.where(ClipCandidate.score >= min_score)
    order = {
        "score": ClipCandidate.score.desc(),
        "start": ClipCandidate.start_time.asc(),
        "rank": ClipCandidate.rank.asc(),
    }[sort]
    total = int(db.execute(count_stmt).scalar_one())
    rows = list(db.execute(stmt.order_by(order).limit(params.limit).offset(params.offset)).scalars())
    return Page.build([CandidateOut.model_validate(row) for row in rows], total, params.limit, params.offset)


@router.get("/candidates/{candidate_id}", response_model=CandidateOut)
def read_candidate(candidate_id: str, db: DbSession, user: CurrentUser) -> CandidateOut:
    candidate = _candidate(db, user, candidate_id)
    return CandidateOut.model_validate(candidate)


@router.get("/candidates/{candidate_id}/score", response_model=CandidateScoreDetail)
def candidate_score(candidate_id: str, db: DbSession, user: CurrentUser) -> CandidateScoreDetail:
    """Full score transparency: components, weights and the measured reason."""
    candidate = _candidate(db, user, candidate_id)
    return CandidateScoreDetail(
        candidate_id=candidate.id,
        score=candidate.score,
        components=dict(candidate.scores or {}),
        weights=DEFAULT_WEIGHTS,
        analyzer=candidate.analyzer,
        explanation=candidate.reason,
    )


@router.patch("/candidates/{candidate_id}", response_model=CandidateOut)
def update_candidate(candidate_id: str, payload: CandidateUpdate, db: DbSession, user: CurrentUser) -> CandidateOut:
    candidate = _candidate(db, user, candidate_id)
    if payload.status:
        candidate.status = payload.status
    if payload.title is not None:
        candidate.title = payload.title
    if payload.hook is not None:
        candidate.hook = payload.hook
    db.commit()
    db.refresh(candidate)
    return CandidateOut.model_validate(candidate)


@router.post("/projects/{project_id}/candidates/bulk", response_model=OkResponse)
def bulk_update_candidates(project_id: str, payload: CandidateBulkUpdate, db: DbSession, user: CurrentUser) -> OkResponse:
    owned_project(db, user, project_id)
    rows = db.execute(
        select(ClipCandidate).where(
            ClipCandidate.project_id == project_id, ClipCandidate.id.in_(payload.ids)
        )
    ).scalars()
    updated = 0
    for row in rows:
        row.status = payload.status
        updated += 1
    db.commit()
    return OkResponse(message=f"Updated {updated} suggestion{'s' if updated != 1 else ''}.")


@router.post("/candidates/{candidate_id}/clip", response_model=ClipOut, status_code=status.HTTP_201_CREATED)
def create_clip_from_candidate(
    candidate_id: str, db: DbSession, user: CurrentUser, payload: ClipCreate | None = None
) -> ClipOut:
    candidate = _candidate(db, user, candidate_id)
    body = payload or ClipCreate()
    clip = ClipService.create_from_candidate(
        db,
        user,
        candidate,
        title=body.title,
        headline=body.headline,
        preset=body.preset,
        aspect_ratio=body.aspect_ratio,
        caption_preset=body.caption_preset,
        auto_framing=body.auto_framing,
    )
    return ClipOut.model_validate(clip)


# -------------------------------------------------------------------- clips ---
@router.get("/projects/{project_id}/clips", response_model=Page[ClipOut])
def list_clips(
    project_id: str,
    db: DbSession,
    user: CurrentUser,
    params: PageParams,
    status_filter: str | None = Query(default=None, alias="status"),
    favorite: bool | None = Query(default=None),
) -> Page[ClipOut]:
    owned_project(db, user, project_id)
    stmt = select(Clip).where(Clip.project_id == project_id)
    count_stmt = select(func.count(Clip.id)).where(Clip.project_id == project_id)
    if status_filter:
        stmt = stmt.where(Clip.status == status_filter)
        count_stmt = count_stmt.where(Clip.status == status_filter)
    if favorite is not None:
        stmt = stmt.where(Clip.is_favorite.is_(favorite))
        count_stmt = count_stmt.where(Clip.is_favorite.is_(favorite))
    total = int(db.execute(count_stmt).scalar_one())
    rows = list(db.execute(stmt.order_by(Clip.created_at.asc()).limit(params.limit).offset(params.offset)).scalars())
    return Page.build([ClipOut.model_validate(row) for row in rows], total, params.limit, params.offset)


@router.post("/projects/{project_id}/clips", response_model=ClipOut, status_code=status.HTTP_201_CREATED)
def create_manual_clip(project_id: str, payload: ClipCreate, db: DbSession, user: CurrentUser) -> ClipOut:
    project = owned_project(db, user, project_id)
    video = db.execute(
        select(Video).where(Video.project_id == project.id).order_by(Video.created_at.desc())
    ).scalars().first()
    if video is None:
        raise NotFoundError("Upload a video before creating clips.", code="video_required")
    if payload.candidate_id:
        candidate = _candidate(db, user, payload.candidate_id)
        clip = ClipService.create_from_candidate(
            db,
            user,
            candidate,
            title=payload.title,
            headline=payload.headline,
            preset=payload.preset,
            aspect_ratio=payload.aspect_ratio,
            caption_preset=payload.caption_preset,
            auto_framing=payload.auto_framing,
        )
        return ClipOut.model_validate(clip)
    if payload.start_time is None or payload.end_time is None:
        raise ValidationError("Provide a candidate, or a start and end time.", field="start_time")
    clip = ClipService.create_manual(
        db,
        user,
        project,
        video,
        start=payload.start_time,
        end=payload.end_time,
        title=payload.title,
        preset=payload.preset,
        aspect_ratio=payload.aspect_ratio,
        caption_preset=payload.caption_preset,
    )
    return ClipOut.model_validate(clip)


@router.get("/clips/{clip_id}", response_model=ClipDetail)
def read_clip(clip_id: str, db: DbSession, user: CurrentUser) -> ClipDetail:
    clip = owned_clip(db, user, clip_id)
    # Validate only the scalar clip fields first. ClipDetail.video is a dict,
    # while the ORM relationship is a `Video` object; validating `clip` against
    # the richer schema directly therefore raises before we can serialize the
    # relationship below.
    detail = ClipDetail.model_validate(ClipOut.model_validate(clip).model_dump())
    video = db.get(Video, clip.video_id)
    if video is not None:
        detail.video = VideoOut.model_validate(video).model_dump()
    if clip.candidate_id:
        candidate = db.get(ClipCandidate, clip.candidate_id)
        if candidate:
            detail.candidate = CandidateOut.model_validate(candidate)
    render = ClipService.latest_render(db, clip.id)
    if render:
        detail.latest_render = RenderOut.model_validate(render)
    return detail


@router.patch("/clips/{clip_id}", response_model=ClipOut)
def update_clip(clip_id: str, payload: ClipUpdate, db: DbSession, user: CurrentUser) -> ClipOut:
    clip = owned_clip(db, user, clip_id)
    data = payload.model_dump(exclude_unset=True)
    if "status" in data and data["status"] not in {s.value for s in ClipStatus}:
        raise ValidationError("Unknown clip status.", field="status")
    ClipService.apply_update(db, clip, data)
    return ClipOut.model_validate(clip)


@router.delete("/clips/{clip_id}", response_model=OkResponse)
def delete_clip(clip_id: str, db: DbSession, user: CurrentUser) -> OkResponse:
    clip = owned_clip(db, user, clip_id)
    db.delete(clip)
    db.commit()
    return OkResponse(message="Clip deleted.")


@router.post("/clips/{clip_id}/duplicate", response_model=ClipOut, status_code=status.HTTP_201_CREATED)
def duplicate_clip(clip_id: str, db: DbSession, user: CurrentUser, payload: ClipDuplicateRequest | None = None) -> ClipOut:
    clip = owned_clip(db, user, clip_id)
    copy = ClipService.duplicate(db, user, clip, (payload.title if payload else None))
    return ClipOut.model_validate(copy)


@router.post("/clips/{clip_id}/crop", response_model=ClipOut)
def set_crop(clip_id: str, payload: ClipCropUpdate, db: DbSession, user: CurrentUser) -> ClipOut:
    """Manual crop override (also used to reset back to automatic tracking)."""
    clip = owned_clip(db, user, clip_id)
    crop = dict(clip.crop or {})
    crop.update({"mode": payload.mode, "focus_x": payload.focus_x, "focus_y": payload.focus_y, "zoom": payload.zoom})
    if payload.mode in ("auto", "track"):
        framing = ClipService.framing_analysis(db, clip.video_id)
        crop["keyframes"] = framing.get("keyframes", [])
        crop["strategy"] = framing.get("framing_strategy", "")
        crop["detector"] = framing.get("detector", "")
    clip.crop = crop
    db.commit()
    db.refresh(clip)
    return ClipOut.model_validate(clip)


@router.post("/clips/{clip_id}/keep", response_model=ClipOut)
def keep_clip(clip_id: str, db: DbSession, user: CurrentUser) -> ClipOut:
    clip = owned_clip(db, user, clip_id)
    clip.status = ClipStatus.READY.value
    db.commit()
    return ClipOut.model_validate(clip)


@router.post("/clips/{clip_id}/reject", response_model=OkResponse)
def reject_clip(clip_id: str, db: DbSession, user: CurrentUser) -> OkResponse:
    clip = owned_clip(db, user, clip_id)
    if clip.candidate_id:
        candidate = db.get(ClipCandidate, clip.candidate_id)
        if candidate:
            candidate.status = CandidateStatus.REJECTED.value
    clip.status = ClipStatus.DRAFT.value
    db.commit()
    return OkResponse(message="Clip rejected.")


# ----------------------------------------------------------------- captions ---
@router.get("/clips/{clip_id}/captions", response_model=CaptionResponse)
def read_captions(clip_id: str, db: DbSession, user: CurrentUser) -> CaptionResponse:
    clip = owned_clip(db, user, clip_id)
    words, transcript_available = CaptionService.words_for_clip(db, clip)
    config = CaptionService.resolve_config(clip)
    cues = CaptionService.build_cues(words, config) if words else []
    style_name = clip.caption_style.name if clip.caption_style else CAPTION_PRESETS.get(
        str((clip.caption_config or {}).get("preset") or "bold"), {}
    ).get("name", "")
    return CaptionResponse(
        clip_id=clip.id,
        enabled=bool((clip.caption_config or {}).get("enabled", True)),
        style=config,
        style_name=str(style_name),
        cues=[CaptionCue(**cue) for cue in cues],
        words=[
            CaptionWord(
                index=word["index"],
                word=word["word"],
                start=word["start"],
                end=word["end"],
                speaker=word.get("speaker", ""),
                is_edited=bool(word.get("is_edited")),
            )
            for word in words
        ],
        transcript_available=transcript_available,
        message="" if transcript_available else "Transcribe this video to unlock word-level captions.",
    )


@router.patch("/clips/{clip_id}/captions", response_model=CaptionResponse)
def update_captions(clip_id: str, payload: CaptionUpdateRequest, db: DbSession, user: CurrentUser) -> CaptionResponse:
    """Update caption style and/or text. Timing information is never rewritten."""
    clip = owned_clip(db, user, clip_id)
    changed = False
    if payload.enabled is not None:
        clip.caption_config = {**(clip.caption_config or {}), "enabled": payload.enabled}
        changed = True
    if payload.style_id:
        from app.models import CaptionStyle

        style = db.get(CaptionStyle, payload.style_id)
        if style is None or (style.user_id not in (None, user.id)):
            raise NotFoundError("Caption style not found.", code="style_not_found")
        clip.caption_style_id = style.id
        merged = {**CaptionService.resolve_config(clip), **(style.config or {})}
        clip.caption_config = merged
        changed = True
    if payload.style:
        merged = CaptionService.resolve_config(clip, payload.style)
        clip.caption_config = merged
        changed = True
    if payload.word_edits is not None:
        CaptionService.apply_word_edits(clip, payload.word_edits)
        changed = True
    if changed:
        db.commit()
        db.refresh(clip)
    return read_captions(clip_id, db, user)


@router.get("/caption-presets")
def caption_presets(user: CurrentUser) -> dict:
    return {"presets": CaptionService.presets()}


@router.get("/caption-styles", response_model=list[CaptionStyleOut])
def list_caption_styles(db: DbSession, user: CurrentUser) -> list[CaptionStyleOut]:
    return [CaptionStyleOut.model_validate(style) for style in CaptionService.list_styles(db, user)]


@router.post("/caption-styles", response_model=CaptionStyleOut, status_code=status.HTTP_201_CREATED)
def create_caption_style(payload: CaptionStyleCreate, db: DbSession, user: CurrentUser) -> CaptionStyleOut:
    style = CaptionService.create_style(
        db,
        user,
        name=payload.name,
        preset_key=payload.preset_key,
        description=payload.description,
        config=payload.config,
    )
    return CaptionStyleOut.model_validate(style)


@router.delete("/caption-styles/{style_id}", response_model=OkResponse)
def delete_caption_style(style_id: str, db: DbSession, user: CurrentUser) -> OkResponse:
    CaptionService.delete_style(db, user, style_id)
    return OkResponse(message="Caption style deleted.")


# ---------------------------------------------------------------- headlines ---
@router.get("/clips/{clip_id}/headlines", response_model=HeadlineResponse)
def read_headlines(clip_id: str, db: DbSession, user: CurrentUser, count: int = Query(default=5, ge=1, le=10)) -> HeadlineResponse:
    clip = owned_clip(db, user, clip_id)
    stored = [{"text": text, "source": "saved", "provider": ""} for text in (clip.headline_variants or [])]
    if stored:
        return HeadlineResponse(clip_id=clip.id, current=clip.headline, suggestions=stored[:count], provider="")
    return HeadlineResponse(**HeadlineService.generate(db, user, clip, count=count))


@router.post("/clips/{clip_id}/headlines", response_model=HeadlineResponse)
def generate_headlines(
    clip_id: str, db: DbSession, user: CurrentUser, payload: HeadlineGenerateRequest | None = None
) -> HeadlineResponse:
    clip = owned_clip(db, user, clip_id)
    body = payload or HeadlineGenerateRequest()
    result = HeadlineService.generate(db, user, clip, count=body.count, tone=body.tone, refresh=True)
    return HeadlineResponse(**result)


@router.post("/clips/{clip_id}/headlines/select", response_model=HeadlineResponse)
def select_headline(clip_id: str, payload: HeadlineSelectRequest, db: DbSession, user: CurrentUser) -> HeadlineResponse:
    """Choosing a headline is always an explicit user action."""
    clip = owned_clip(db, user, clip_id)
    clip.headline = payload.text.strip()[:300]
    db.commit()
    return HeadlineResponse(clip_id=clip.id, current=clip.headline, suggestions=[], provider="")


@router.delete("/clips/{clip_id}/headlines", response_model=HeadlineResponse)
def clear_headline(clip_id: str, db: DbSession, user: CurrentUser) -> HeadlineResponse:
    clip = owned_clip(db, user, clip_id)
    clip.headline = ""
    db.commit()
    return HeadlineResponse(clip_id=clip.id, current="", suggestions=[], provider="")


# ------------------------------------------------------------------ helpers ---
def _candidate(db: DbSession, user: User, candidate_id: str) -> ClipCandidate:
    candidate = db.get(ClipCandidate, candidate_id)
    if candidate is None:
        raise NotFoundError("Suggestion not found.", code="candidate_not_found")
    project = db.get(Project, candidate.project_id)
    if project is None or project.user_id != user.id:
        raise NotFoundError("Suggestion not found.", code="candidate_not_found")
    return candidate
