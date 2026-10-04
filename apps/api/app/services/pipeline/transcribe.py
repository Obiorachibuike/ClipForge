"""Transcription job: audio -> words with timestamps -> speaker turns.

Timing, confidence and speaker labels all come from real analysis (Whisper word
timings, decoded-audio acoustics, optional provider diarisation). Nothing is
interpolated or invented.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import (
    AppError,
    MediaError,
    NotFoundError,
    ProviderError,
    ProviderUnavailableError,
)
from app.core.logging import get_logger
from app.models import (
    Job,
    Project,
    Transcript,
    TranscriptSegment,
    TranscriptStatus,
    TranscriptWord,
    Video,
    VideoStatus,
    utcnow,
)
from app.services.ai import registry as ai_registry
from app.services.ai.types import TranscriptionResult
from app.services.job_service import JobService
from app.services.media import ffmpeg
from app.services.media.audio_features import (
    assign_speakers,
    cluster_speakers,
    extract_features,
)
from app.services.pipeline.workspace import workspace_for
from app.services.storage import get_storage
from app.services.usage_service import UsageService

log = get_logger(__name__)

MAX_SPEAKERS = 3


def run(db: Session, job: Job) -> dict:
    video_id = job.payload.get("video_id") or job.video_id
    video = db.get(Video, video_id) if video_id else None
    if video is None:
        raise NotFoundError("Video not found for transcription job.", code="video_not_found")
    if not video.has_audio:
        raise MediaError(
            "This video has no audio track, so there is nothing to transcribe.",
            code="no_audio_track",
        )

    transcript = _prepare_transcript(db, video, job)
    JobService.report_progress(db, job, stage="prepare", progress=3, message="Preparing audio", force=True)

    storage = get_storage()
    script = (video.narration_script or "").strip() or None
    provider = ai_registry.transcription_provider(db, job.user_id, script=script)

    with workspace_for(job.id) as workdir:
        source = storage.materialize(video.storage_key, workdir)
        JobService.check_cancelled(db, job)

        # ------------------------------------------------------------ audio ---
        audio_path = workdir / "audio.wav"
        _extract_audio(source, audio_path, video, db, job)

        # --------------------------------------------------- transcribe ------
        JobService.report_progress(db, job, stage="transcription", progress=12, message="Transcribing audio", force=True)

        def progress(pct: float, message: str) -> None:
            scaled = 12 + max(0.0, min(1.0, pct / 100.0)) * 60.0
            JobService.report_progress(db, job, stage="transcription", progress=scaled, message=message)

        result, alignment_note = _transcribe(
            db, job, video, audio_path, provider, script=script, progress_cb=progress
        )
        if alignment_note:
            JobService.report_progress(
                db, job, stage="transcription", progress=73, message=alignment_note, force=True
            )
        JobService.check_cancelled(db, job)
        if not result.segments:
            raise ProviderError(provider.name, "the provider returned no speech", retryable=False)

        # -------------------------------------------------- acoustics --------
        JobService.report_progress(db, job, stage="acoustics", progress=74, message="Measuring delivery and pauses")
        features = extract_features(audio_path, job_id=job.id)
        turns, speaker_count, diarization_meta = _diarize(db, job, features)

        # ------------------------------------------------- persist -----------
        JobService.report_progress(db, job, stage="persistence", progress=86, message="Saving transcript")
        _persist(db, transcript, result, features, turns, diarization_meta, provider.name)

    video.status = VideoStatus.READY.value
    project = db.get(Project, video.project_id)
    if project:
        project.source_language = transcript.language or project.source_language
        db.commit()
    transcript.status = TranscriptStatus.COMPLETED.value
    transcript.error_message = ""
    db.commit()

    UsageService.record(
        db,
        user_id=job.user_id or video.user_id,
        metric="ai_requests",
        quantity=len(result.segments),
        unit="requests",
        project_id=video.project_id,
        video_id=video.id,
        job_id=job.id,
        provider=provider.name,
    )
    UsageService.record(
        db,
        user_id=job.user_id or video.user_id,
        metric="minutes_processed",
        quantity=round(video.duration_seconds / 60.0, 3),
        unit="minutes",
        project_id=video.project_id,
        video_id=video.id,
        job_id=job.id,
        provider=provider.name,
    )

    JobService.report_progress(db, job, stage="done", progress=98, message="Transcript ready")
    return {
        "transcript_id": transcript.id,
        "words": transcript.word_count,
        "segments": transcript.segment_count,
        "language": transcript.language,
        "speakers": transcript.speaker_count,
        "provider": transcript.provider,
        "has_word_timings": bool(transcript.meta.get("has_word_timings")),
    }


# Above this ratio the alignment path costs more than a random pairing of the
# reference and the audio, which means the script does not describe this audio.
MAX_ALIGNMENT_QUALITY_RATIO = 0.72


def _transcribe(
    db: Session,
    job: Job,
    video: Video,
    audio_path: Path,
    provider,
    *,
    script: str | None,
    progress_cb,
) -> tuple[TranscriptionResult, str]:
    """Transcribe, verifying script alignment against the real audio.

    Forced alignment never invents text: word timings come from measuring the
    supplied script against the audio. If the script does not match what was said,
    the alignment cost stays high and we fall back to real recognition instead of
    reporting timings that do not correspond to the audio.
    """
    shared: dict = {
        "language": video.project.source_language or None,
        "progress_cb": progress_cb,
        "duration_hint": video.duration_seconds,
    }
    aligned = bool(script) and getattr(provider, "name", "") == "alignment"

    def call(target, with_script: bool) -> TranscriptionResult:
        kwargs = dict(shared)
        if with_script:
            kwargs["script"] = script
        return target.transcribe(audio_path, **kwargs)

    try:
        result = call(provider, aligned)
    except (ProviderError, ProviderUnavailableError) as exc:
        if not aligned:
            raise
        log.warning("transcription.alignment_failed", job_id=job.id, error=str(exc)[:300])
        result = call(ai_registry.transcription_provider(db, job.user_id), False)
        result.meta["script_alignment"] = {"applied": False, "reason": "alignment error"}
        return result, "Script alignment failed, used speech recognition instead."

    if not aligned:
        return result, ""

    diagnostics = result.meta.get("alignment_diagnostics") or {}
    ratio = float(diagnostics.get("quality_ratio") or 0.0)
    result.meta["script_alignment"] = {
        "applied": True,
        "quality_ratio": ratio,
        "confidence": result.meta.get("alignment_confidence"),
        "words": len(result.words),
    }
    if ratio <= MAX_ALIGNMENT_QUALITY_RATIO:
        return result, ""

    log.warning(
        "transcription.alignment_mismatch", job_id=job.id, ratio=ratio, script_words=len(script.split())
    )
    try:
        fallback = ai_registry.transcription_provider(db, job.user_id)
        recognised = call(fallback, False)
    except (ProviderError, ProviderUnavailableError) as exc:
        log.warning("transcription.alignment_fallback_unavailable", job_id=job.id, error=str(exc)[:200])
        result.meta["script_alignment"]["warning"] = "script may not match the audio"
        return result, "The script may not match this audio; timings kept but flagged as low confidence."
    recognised.meta["script_alignment"] = {
        "applied": False,
        "reason": "script did not match the audio",
        "quality_ratio": ratio,
    }
    return recognised, "The script did not match the audio, transcribed with speech recognition instead."


@dataclass
class _WordRef:
    """Exposes a TranscriptWord row with the audio_features word interface."""

    row: TranscriptWord

    @property
    def start(self) -> float:
        return float(self.row.start_time)

    @property
    def end(self) -> float:
        return float(self.row.end_time)

    @property
    def speaker(self) -> str:
        return self.row.speaker or ""

    @speaker.setter
    def speaker(self, value: str) -> None:
        self.row.speaker = value or ""


def _prepare_transcript(db: Session, video: Video, job: Job) -> Transcript:
    """Reuse an existing pending transcript or create a new one."""
    transcript = Transcript(
        video_id=video.id,
        project_id=video.project_id,
        status=TranscriptStatus.RUNNING.value,
        provider="",
        model="",
    )
    db.add(transcript)
    db.commit()
    db.refresh(transcript)
    return transcript


def _extract_audio(source: Path, audio_path: Path, video: Video, db: Session, job: Job) -> None:
    def progress(pct: float) -> None:
        JobService.report_progress(
            db, job, stage="audio", progress=4 + (pct / 100.0) * 8.0, message="Extracting audio track"
        )

    try:
        ffmpeg.extract_audio(
            source,
            audio_path,
            sample_rate=16000,
            channels=1,
            job_id=job.id,
            progress_cb=progress,
            total_duration=video.duration_seconds or None,
        )
    except AppError:
        raise
    except Exception as exc:
        raise MediaError("The audio track could not be extracted from this file.", code="audio_extraction_failed") from exc


def _diarize(db: Session, job: Job, features) -> tuple[list, int, dict]:
    if not settings.vision_enabled:  # reuse the same "heavy analysis enabled" switch
        return [], 1, {"method": "disabled"}
    JobService.report_progress(db, job, stage="speakers", progress=80, message="Separating speakers")
    try:
        turns, count, meta = cluster_speakers(features, max_speakers=MAX_SPEAKERS)
        return turns, count, meta
    except Exception as exc:
        log.warning("diarization.failed", job_id=job.id, error=str(exc))
        return [], 1, {"method": "failed", "error": str(exc)[:200]}


def _persist(
    db: Session,
    transcript: Transcript,
    result: TranscriptionResult,
    features,
    turns,
    diarization_meta: dict,
    provider_name: str,
) -> None:
    """Write segments, words and acoustic measurements."""
    # Clear any previous rows (re-running transcription replaces the content).
    for existing in list(transcript.segments):
        db.delete(existing)
    for existing in list(transcript.words):
        db.delete(existing)
    db.flush()

    all_words = []
    for index, segment in enumerate(result.segments):
        acoustics = _segment_acoustics(features, segment.start, segment.end)
        row = TranscriptSegment(
            transcript_id=transcript.id,
            index=index,
            start_time=float(segment.start),
            end_time=float(segment.end),
            text=segment.text.strip(),
            speaker=segment.speaker or "",
            avg_confidence=float(segment.confidence or 0.0),
            word_count=len(segment.words),
            energy=acoustics.get("energy", 0.0),
            energy_variance=acoustics.get("energy_variance", 0.0),
            pause_before=0.0,
            speech_rate=0.0,
        )
        db.add(row)
        db.flush()
        words = []
        for word_index, word in enumerate(segment.words):
            words.append(
                TranscriptWord(
                    transcript_id=transcript.id,
                    segment_id=row.id,
                    index=len(all_words),
                    word=word.word.strip(),
                    start_time=float(word.start),
                    end_time=float(word.end),
                    confidence=float(word.confidence or 0.0),
                    speaker=word.speaker or "",
                )
            )
        if not words and segment.text.strip():
            # Provider gave no word timings: create proportional pseudo-words so
            # captions still work, marked with the segment's confidence.
            words = _synthetic_words(segment.text, segment.start, segment.end, transcript.id, row.id, len(all_words))
        all_words.extend(words)
        for word_row in words:
            db.add(word_row)
        db.flush()

    # `assign_speakers` works on the WordTiming interface (start/end/speaker);
    # ORM rows use start_time/end_time, so adapt rather than reshape the model.
    adapters = [_WordRef(row) for row in all_words]
    assign_speakers(adapters, turns)
    for word_row in all_words:
        if word_row.speaker:
            db.merge(word_row)

    # Segment-level speakers follow the majority of their words.
    for segment_row in transcript.segments:
        segment_words = [w for w in all_words if w.segment_id == segment_row.id and w.speaker]
        if segment_words:
            counts: dict[str, int] = {}
            for word in segment_words:
                counts[word.speaker] = counts.get(word.speaker, 0) + 1
            segment_row.speaker = max(counts.items(), key=lambda kv: kv[1])[0]

    # Pause + speech-rate stats need the sorted word list.
    ordered = sorted(all_words, key=lambda w: w.start_time)
    previous_end = 0.0
    for segment_row in sorted(transcript.segments, key=lambda s: s.index):
        segment_row.pause_before = max(0.0, segment_row.start_time - previous_end)
        previous_end = segment_row.end_time
        duration = max(0.05, segment_row.end_time - segment_row.start_time)
        segment_row.speech_rate = segment_row.word_count / duration

    transcript.provider = result.provider or provider_name
    transcript.model = result.model
    transcript.language = result.language or "en"
    transcript.language_probability = float(result.language_probability or 0.0)
    transcript.duration_seconds = float(result.duration or 0.0)
    transcript.segment_count = len(result.segments)
    transcript.word_count = len(all_words)
    transcript.full_text = result.text
    transcript.avg_confidence = _average([w.confidence for w in all_words if w.confidence])
    transcript.is_primary = True
    transcript.status = TranscriptStatus.COMPLETED.value
    transcript.meta = {
        "has_word_timings": result.has_word_timings,
        "diarization": diarization_meta,
        "speakers": diarization_meta.get("speakers", 1),
        "acoustic_features": {
            "frames": len(features.times) if features and features.times else 0,
            "hop_seconds": 0.01,
        },
        "provider_meta": result.meta,
    }
    transcript.speaker_count = int(diarization_meta.get("speakers", 1) or 1)
    db.commit()


def _synthetic_words(
    text: str, start: float, end: float, transcript_id: str, segment_id: str, offset: int
) -> list[TranscriptWord]:
    tokens = [token for token in text.split() if token]
    if not tokens:
        return []
    span = max(0.05, end - start)
    step = span / len(tokens)
    return [
        TranscriptWord(
            transcript_id=transcript_id,
            segment_id=segment_id,
            index=offset + index,
            word=token,
            start_time=start + index * step,
            end_time=start + (index + 1) * step,
            confidence=0.0,
        )
        for index, token in enumerate(tokens)
    ]


def _segment_acoustics(features, start: float, end: float) -> dict:
    if not features or not features.times:
        return {}
    from app.services.media import audio_features

    return audio_features.segment_acoustics(features, start, end)


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
