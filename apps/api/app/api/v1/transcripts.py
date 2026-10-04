"""Transcript endpoints: read, search, edit words, and window queries."""
from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, owned_clip, owned_video
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models import (
    CandidateStatus,
    ClipCandidate,
    JobType,
    Project,
    ProjectStatus,
    Transcript,
    TranscriptSegment,
    TranscriptStatus,
    TranscriptWord,
    User,
    Video,
    VideoStatus,
)
from app.schemas.common import OkResponse
from app.schemas.media import (
    JobOut,
    TranscriptDetail,
    TranscriptOut,
    TranscriptSegmentOut,
    TranscriptWindow,
    TranscriptWordOut,
    WordEdit,
)
from app.services.job_service import JobService

log = get_logger(__name__)
router = APIRouter(tags=["Transcripts"])


def _build_detail(db, transcript: Transcript, *, include_words: bool, word_limit: int) -> TranscriptDetail:
    """Assemble the full transcript payload.

    Shared by both read routes. It takes plain values rather than FastAPI
    `Query` defaults on purpose: calling a route function directly leaves those
    defaults as `Query` objects, which then fail deep inside SQLAlchemy.
    """
    segments = [
        TranscriptSegmentOut.model_validate(row)
        for row in db.execute(
            select(TranscriptSegment)
            .where(TranscriptSegment.transcript_id == transcript.id)
            .order_by(TranscriptSegment.index)
        ).scalars()
    ]
    words: list[TranscriptWordOut] = []
    if include_words:
        rows = db.execute(
            select(TranscriptWord)
            .where(TranscriptWord.transcript_id == transcript.id)
            .order_by(TranscriptWord.start_time)
            .limit(word_limit)
        ).scalars()
        words = [TranscriptWordOut.model_validate(row) for row in rows]
    detail = TranscriptDetail.model_validate(transcript)
    detail.segments = segments
    detail.words = words
    return detail


@router.get("/transcripts/{transcript_id}", response_model=TranscriptDetail)
def read_transcript(
    transcript_id: str,
    db: DbSession,
    user: CurrentUser,
    include_words: bool = Query(default=True),
    word_limit: int = Query(default=5000, ge=1, le=60000),
) -> TranscriptDetail:
    transcript = db.get(Transcript, transcript_id)
    if transcript is None:
        raise NotFoundError("Transcript not found.", code="transcript_not_found")
    _assert_transcript_access(db, transcript, user)
    return _build_detail(db, transcript, include_words=include_words, word_limit=word_limit)


@router.get("/videos/{video_id}/transcript", response_model=TranscriptDetail | None)
def video_transcript(video_id: str, db: DbSession, user: CurrentUser, include_words: bool = Query(default=True)) -> TranscriptDetail | None:
    video = owned_video(db, user, video_id)
    transcript = db.execute(
        select(Transcript).where(Transcript.video_id == video.id).order_by(Transcript.created_at.desc())
    ).scalars().first()
    if transcript is None:
        return None
    return _build_detail(db, transcript, include_words=include_words, word_limit=5000)


@router.get("/transcripts/{transcript_id}/window", response_model=TranscriptWindow)
def transcript_window(
    transcript_id: str,
    db: DbSession,
    user: CurrentUser,
    start: float = Query(ge=0),
    end: float = Query(gt=0),
) -> TranscriptWindow:
    """Words inside a time window - exactly what captions render from."""
    if end <= start:
        raise ValidationError("The window end must be after its start.", field="end")
    transcript = db.get(Transcript, transcript_id)
    if transcript is None:
        raise NotFoundError("Transcript not found.", code="transcript_not_found")
    _assert_transcript_access(db, transcript, user)
    rows = list(
        db.execute(
            select(TranscriptWord)
            .where(
                TranscriptWord.transcript_id == transcript.id,
                TranscriptWord.end_time >= start,
                TranscriptWord.start_time <= end,
            )
            .order_by(TranscriptWord.start_time)
        ).scalars()
    )
    return TranscriptWindow(
        transcript_id=transcript.id,
        start=start,
        end=end,
        words=[TranscriptWordOut.model_validate(row) for row in rows],
        text=" ".join(row.word for row in rows),
    )


@router.get("/transcripts/{transcript_id}/search")
def search_transcript(
    transcript_id: str,
    db: DbSession,
    user: CurrentUser,
    q: str = Query(min_length=2, max_length=120),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    transcript = db.get(Transcript, transcript_id)
    if transcript is None:
        raise NotFoundError("Transcript not found.", code="transcript_not_found")
    _assert_transcript_access(db, transcript, user)
    pattern = f"%{q.strip().lower()}%"
    rows = db.execute(
        select(TranscriptWord)
        .where(TranscriptWord.transcript_id == transcript.id, TranscriptWord.word.ilike(pattern))
        .order_by(TranscriptWord.start_time)
        .limit(limit)
    ).scalars()
    matches = [TranscriptWordOut.model_validate(row) for row in rows]
    return {"query": q, "count": len(matches), "matches": [match.model_dump() for match in matches]}


@router.patch("/transcripts/{transcript_id}/words", response_model=OkResponse)
def edit_words(transcript_id: str, payload: WordEdit, db: DbSession, user: CurrentUser) -> OkResponse:
    """Edit transcript text without touching timing.

    Word timings are immutable here by design: captions stay synchronised no
    matter how the text is corrected.
    """
    transcript = db.get(Transcript, transcript_id)
    if transcript is None:
        raise NotFoundError("Transcript not found.", code="transcript_not_found")
    _assert_transcript_access(db, transcript, user)
    if len(payload.words) > 2000:
        raise ValidationError("Too many words in a single edit request.", field="words")

    updated = 0
    for key, value in payload.words.items():
        text = str(value).strip()[:80]
        if not text:
            continue
        row = None
        if key.isdigit():
            row = db.execute(
                select(TranscriptWord).where(
                    TranscriptWord.transcript_id == transcript.id, TranscriptWord.index == int(key)
                )
            ).scalars().first()
        if row is None:
            row = db.get(TranscriptWord, key)
            if row is not None and row.transcript_id != transcript.id:
                row = None
        if row is None:
            continue
        row.word = text
        row.is_edited = True
        updated += 1
    db.commit()
    return OkResponse(message=f"Updated {updated} word{'s' if updated != 1 else ''}. Timing preserved.")


@router.post("/transcripts/{transcript_id}/retranscribe", response_model=JobOut)
def retranscribe(transcript_id: str, db: DbSession, user: CurrentUser) -> JobOut:
    transcript = db.get(Transcript, transcript_id)
    if transcript is None:
        raise NotFoundError("Transcript not found.", code="transcript_not_found")
    _assert_transcript_access(db, transcript, user)
    video = db.get(Video, transcript.video_id)
    if video is None:
        raise NotFoundError("Video not found.", code="video_not_found")
    active = JobService.active_for_user(db, user.id, video.project_id)
    if any(job.type == JobType.VIDEO_TRANSCRIBE.value and job.video_id == video.id for job in active):
        raise ConflictError("Transcription is already running.", code="transcription_running")
    job = JobService.create(
        db,
        type_=JobType.VIDEO_TRANSCRIBE.value,
        payload={"video_id": video.id, "replace_transcript_id": transcript.id},
        user_id=user.id,
        project_id=video.project_id,
        video_id=video.id,
        priority=20,
    )
    return JobOut.model_validate(job)


@router.delete("/transcripts/{transcript_id}", response_model=OkResponse)
def delete_transcript(transcript_id: str, db: DbSession, user: CurrentUser) -> OkResponse:
    transcript = db.get(Transcript, transcript_id)
    if transcript is None:
        raise NotFoundError("Transcript not found.", code="transcript_not_found")
    _assert_transcript_access(db, transcript, user)
    db.delete(transcript)
    db.commit()
    return OkResponse(message="Transcript deleted.")


def _assert_transcript_access(db: DbSession, transcript: Transcript, user: User) -> None:
    project = db.get(Project, transcript.project_id)
    if project is None or project.user_id != user.id:
        raise NotFoundError("Transcript not found.", code="transcript_not_found")


def _status_label(status: str) -> str:
    return "completed" if status == TranscriptStatus.COMPLETED.value else status
