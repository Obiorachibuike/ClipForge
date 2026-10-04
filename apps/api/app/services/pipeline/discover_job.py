"""Clip discovery job: transcript -> scored, explainable clip candidates."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import MediaError, NotFoundError
from app.core.logging import get_logger
from app.models import (
    AnalysisKind,
    CandidateStatus,
    ClipCandidate,
    Job,
    Project,
    ProjectStatus,
    Transcript,
    TranscriptStatus,
    TranscriptWord,
    Video,
    VideoAnalysis,
)
from app.services import events as ev
from app.services.ai import registry as ai_registry
from app.services.ai.types import MomentAnalysis, WordTiming
from app.services.job_service import JobService
from app.services.media import ffmpeg
from app.services.pipeline.analyzer import (
    Candidate,
    apply_llm_scores,
    apply_narration,
    build_corpus_idf,
    build_sentences,
    generate_candidates,
)
from app.services.pipeline.workspace import workspace_for
from app.services.storage import get_storage
from app.services.usage_service import UsageService

log = get_logger(__name__)

MAX_LLM_MOMENTS = 14
DEFAULT_MAX_CANDIDATES = 12


def run(db: Session, job: Job) -> dict:
    project_id = job.payload.get("project_id") or job.project_id
    project = db.get(Project, project_id) if project_id else None
    if project is None:
        raise NotFoundError("Project not found.", code="project_not_found")

    video_id = job.payload.get("video_id") or job.video_id
    video = db.get(Video, video_id) if video_id else _primary_video(db, project.id)
    if video is None:
        raise NotFoundError("Upload a video to this project before discovering clips.", code="video_not_found")

    transcript = _primary_transcript(db, video.id)
    if transcript is None or transcript.status != TranscriptStatus.COMPLETED.value:
        raise MediaError(
            "Transcribe the video before running clip discovery.", code="transcript_required"
        )

    max_candidates = int(job.payload.get("max_candidates") or DEFAULT_MAX_CANDIDATES)
    min_duration = float(job.payload.get("min_duration") or 10.0)
    max_duration = float(job.payload.get("max_duration") or 95.0)

    JobService.report_progress(db, job, stage="load", progress=4, message="Loading transcript", force=True)

    words = _load_words(db, transcript.id)
    if len(words) < 12:
        raise MediaError(
            "The transcript is too short to find meaningful moments.", code="transcript_too_short"
        )

    JobService.report_progress(db, job, stage="acoustics", progress=12, message="Measuring audio energy")
    energy_curve = _energy_curve(db, job, video)

    sentences = build_sentences(words, energy_curve=energy_curve)
    if not sentences:
        raise MediaError("No sentences could be built from the transcript.", code="analysis_failed")

    JobService.report_progress(db, job, stage="candidates", progress=30, message="Scoring candidate moments")
    corpus_idf = build_corpus_idf(sentences)
    embedder = ai_registry.embedding_provider([s.clean_text for s in sentences])
    embed_fn = embedder.embed
    similarity_fn = embedder.similarity

    candidates = generate_candidates(
        sentences,
        max_candidates=max_candidates,
        min_duration=min_duration,
        max_duration=max_duration,
        corpus_idf=corpus_idf,
        similarity_fn=similarity_fn,
        embed_fn=embed_fn,
        language=transcript.language or "en",
    )
    if not candidates:
        raise MediaError(
            "No clip candidates met the minimum quality threshold. Try a longer video or transcribe again.",
            code="no_candidates",
        )

    JobService.check_cancelled(db, job)

    # ------------------------------------------------------------- LLM pass ---
    llm = ai_registry.language_model_provider(db, job.user_id)
    provider_name = llm.name if llm else "structural"
    if llm is not None:
        JobService.report_progress(
            db, job, stage="llm", progress=62, message=f"Reviewing moments with {llm.name}"
        )
        top = candidates[: min(MAX_LLM_MOMENTS, len(candidates))]
        try:
            assessments: list[MomentAnalysis] = []
            for index, candidate in enumerate(top):
                JobService.check_cancelled(db, job)
                assessments.append(
                    llm.analyze_moment(
                        {"transcript": candidate.transcript, "duration": candidate.duration}
                    )
                )
                JobService.report_progress(
                    db,
                    job,
                    stage="llm",
                    progress=62 + 18 * (index + 1) / max(1, len(top)),
                    message=f"Reviewing moments with {llm.name}",
                )
            apply_llm_scores(top, assessments, provider_name=llm.name)
            narrations = llm.narrate_clips(
                [
                    {
                        "id": str(index),
                        "duration": candidate.duration,
                        "transcript": candidate.transcript,
                    }
                    for index, candidate in enumerate(top)
                ],
                language=transcript.language or "en",
            )
            apply_narration(top, narrations)
            candidates = sorted(candidates, key=lambda c: -c.score)
            UsageService.record(
                db,
                user_id=job.user_id or project.user_id,
                metric="ai_requests",
                quantity=len(assessments),
                unit="requests",
                project_id=project.id,
                video_id=video.id,
                job_id=job.id,
                provider=llm.name,
            )
        except Exception as exc:
            # The LLM pass is an enhancement: structural scores already stand.
            log.warning("discovery.llm_failed", job_id=job.id, error=str(exc)[:300])
            job.result = {**(job.result or {}), "llm_warning": str(exc)[:200]}
            provider_name = "structural"
    else:
        log.info("discovery.structural_only", project_id=project.id)

    JobService.report_progress(db, job, stage="persist", progress=88, message="Saving clip suggestions")
    saved = _persist(db, project, video, transcript, candidates, provider_name, job)
    db.commit()

    project.status = ProjectStatus.READY.value
    db.commit()

    ev.get_event_bus().emit(
        ev.EVENT_CANDIDATES_READY,
        user_id=project.user_id,
        project_id=project.id,
        job_id=job.id,
        payload={"count": len(saved), "candidate_ids": [c.id for c in saved]},
    )
    UsageService.record(
        db,
        user_id=job.user_id or project.user_id,
        metric="clips_generated",
        quantity=len(saved),
        unit="clips",
        project_id=project.id,
        video_id=video.id,
        job_id=job.id,
        provider=provider_name,
    )

    return {
        "candidates": len(saved),
        "analyzer": provider_name if provider_name == "structural" else f"structural-v1+{provider_name}",
        "top_score": saved[0].score if saved else 0,
        "llm_used": provider_name != "structural",
    }


# ------------------------------------------------------------------ helpers ---
def _primary_video(db: Session, project_id: str) -> Video | None:
    stmt = select(Video).where(Video.project_id == project_id).order_by(Video.created_at.desc())
    return db.execute(stmt).scalars().first()


def _primary_transcript(db: Session, video_id: str) -> Transcript | None:
    stmt = (
        select(Transcript)
        .where(Transcript.video_id == video_id, Transcript.status == TranscriptStatus.COMPLETED.value)
        .order_by(Transcript.created_at.desc())
    )
    return db.execute(stmt).scalars().first()


def _load_words(db: Session, transcript_id: str) -> list[WordTiming]:
    stmt = (
        select(TranscriptWord)
        .where(TranscriptWord.transcript_id == transcript_id)
        .order_by(TranscriptWord.start_time)
    )
    return [
        WordTiming(
            word=row.word,
            start=row.start_time,
            end=row.end_time,
            confidence=row.confidence,
            speaker=row.speaker or "",
        )
        for row in db.execute(stmt).scalars()
    ]


def _energy_curve(db: Session, job: Job, video: Video) -> tuple[list[float], list[float]] | None:
    """Reuse stored framing/energy analysis when present, else measure now."""
    cached = db.execute(
        select(VideoAnalysis).where(VideoAnalysis.video_id == video.id, VideoAnalysis.kind == AnalysisKind.ENERGY.value)
    ).scalars().first()
    if cached and cached.data.get("times"):
        return cached.data["times"], cached.data["rms"]

    storage = get_storage()
    with workspace_for(f"{job.id}-energy") as workdir:
        try:
            source = storage.materialize(video.storage_key, workdir)
            envelope = ffmpeg.energy_envelope(source, hop_seconds=0.05, job_id=job.id)
        except Exception as exc:
            log.warning("discovery.energy_failed", job_id=job.id, error=str(exc)[:200])
            return None
    if not envelope["times"]:
        return None
    db.add(
        VideoAnalysis(
            video_id=video.id,
            kind=AnalysisKind.ENERGY.value,
            status="completed",
            detector="ffmpeg-pcm",
            data={"times": envelope["times"], "rms": envelope["rms"], "zcr": envelope["zcr"]},
        )
    )
    db.commit()
    return envelope["times"], envelope["rms"]


def _word_range(words: list[TranscriptWord], start: float, end: float) -> tuple[int, int]:
    """Index of the first and last transcript word inside a candidate window."""
    indices = [word.index for word in words if word.end_time > start and word.start_time < end]
    if not indices:
        return 0, 0
    return min(indices), max(indices)


def _persist(
    db: Session,
    project: Project,
    video: Video,
    transcript: Transcript,
    candidates: list[Candidate],
    provider_name: str,
    job: Job,
) -> list[ClipCandidate]:
    """Replace pending candidates; keep decisions the user already made."""
    db.execute(
        delete(ClipCandidate).where(
            ClipCandidate.project_id == project.id,
            ClipCandidate.status == CandidateStatus.PENDING.value,
        )
    )
    db.flush()

    # Decisions the user already made survive a re-analysis. The DELETE above only
    # touches pending rows, so these are counted for the job log, not re-inserted.
    preserved = sum(
        1
        for row in db.execute(select(ClipCandidate).where(ClipCandidate.project_id == project.id)).scalars()
        if row.status in (CandidateStatus.KEPT.value, CandidateStatus.CONVERTED.value)
    )

    # Real word positions, so a candidate maps back onto the transcript: the
    # player can highlight the exact words a suggestion covers.
    ordered_words = list(
        db.execute(
            select(TranscriptWord)
            .where(TranscriptWord.transcript_id == transcript.id)
            .order_by(TranscriptWord.start_time)
        ).scalars()
    )

    saved: list[ClipCandidate] = []
    seen_starts: set[float] = set()
    for rank, candidate in enumerate(candidates):
        key = round(float(candidate.start), 3)
        if key in seen_starts:  # pragma: no cover - defensive
            continue
        seen_starts.add(key)
        start_index, end_index = _word_range(ordered_words, candidate.start, candidate.end)
        row = ClipCandidate(
            project_id=project.id,
            video_id=video.id,
            transcript_id=transcript.id,
            start_time=round(candidate.start, 3),
            end_time=round(candidate.end, 3),
            start_word_index=start_index,
            end_word_index=end_index,
            duration=round(candidate.duration, 3),
            title=candidate.title,
            hook=candidate.hook,
            reason=candidate.reason,
            score=int(candidate.score),
            scores={k: round(v, 2) for k, v in candidate.scores.items()},
            keywords=candidate.keywords,
            category=candidate.category,
            transcript_text=candidate.transcript,
            speaker=candidate.speaker,
            status=CandidateStatus.PENDING.value,
            analyzer=candidate.analyzer,
            rank=rank,
            meta={**candidate.meta, "llm_reason": candidate.llm.reason if candidate.llm else ""},
        )
        db.add(row)
        saved.append(row)
    db.flush()
    for row in saved:
        ev.get_event_bus().emit(
            ev.EVENT_CANDIDATE_CREATED,
            user_id=project.user_id,
            project_id=project.id,
            job_id=job.id,
            payload={
                "candidate_id": row.id,
                "start_time": row.start_time,
                "end_time": row.end_time,
                "duration": row.duration,
                "title": row.title,
                "hook": row.hook,
                "reason": row.reason,
                "score": row.score,
                "scores": row.scores,
                "category": row.category,
                "keywords": row.keywords,
                "speaker": row.speaker,
                "transcript": row.transcript_text,
                "status": row.status,
                "rank": row.rank,
            },
        )
    log.info(
        "discovery.completed",
        project_id=project.id,
        candidates=len(saved),
        analyzer=provider_name,
        preserved_decisions=preserved,
    )
    return saved
