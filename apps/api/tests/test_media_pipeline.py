"""Real-media integration test for the complete clipping pipeline.

This test does not mock FFmpeg, transcription, discovery or rendering. It
synthesises real speech, builds a real MP4, uploads it through the storage
abstraction, and runs the real probe -> transcribe -> discover -> render jobs. It
then decodes the rendered file and verifies that its audio matches the selected
source interval.

Marked `integration`: needs FFmpeg and the espeak-ng library bundled by
`espeakng-loader` (both are worker/development requirements).
"""
from __future__ import annotations

import math
import subprocess
import wave
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

SCRIPT = (
    "Most people get this wrong. "
    "Here is what actually works when you publish every day. "
    "Pick one problem and solve it well."
)

# The rendered audio segment is compared against the source over this span.
COMPARE_SECONDS = 4.0


def _require_ffmpeg() -> str:
    from app.services.media.ffmpeg import ffmpeg_bin

    binary = ffmpeg_bin()
    if not binary:
        pytest.skip("FFmpeg is not available in this environment")
    return binary


def _build_source(tmp_path: Path) -> tuple[Path, float]:
    """A real H.264/AAC MP4 with real synthesised narration."""
    from app.services.ai.transcription.alignment import synthesize_raw, to_pcm16

    try:
        samples, rate = synthesize_raw(SCRIPT)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"speech synthesis unavailable: {exc}")

    pcm = to_pcm16(samples)
    duration = len(pcm) / float(rate)
    if duration < 5.0:
        pytest.skip("synthesised narration is too short to exercise the pipeline")

    audio_path = tmp_path / "narration.wav"
    with wave.open(str(audio_path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())

    source = tmp_path / "source.mp4"
    command = [
        _require_ffmpeg(),
        "-y",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c=0x18213a:s=640x360:r=30:d={duration:.3f}",
        "-i",
        str(audio_path),
        "-shortest",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        str(source),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr[-2000:]
    assert source.stat().st_size > 1024
    return source, duration


def _decode_audio(path: Path, rate: int = 16000):  # noqa: ANN202
    import av
    import numpy as np

    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
        parts = []
        for frame in container.decode(stream):
            for resampled in resampler.resample(frame):
                parts.append(resampled.to_ndarray().reshape(-1))
        for resampled in resampler.resample(None):
            parts.append(resampled.to_ndarray().reshape(-1))
    return np.concatenate(parts).astype("float32") / 32768.0


def _decode_video_info(path: Path) -> dict:
    import av

    with av.open(str(path)) as container:
        video = container.streams.video[0]
        frames = sum(1 for _ in container.decode(video=0))
        return {
            "duration": float(container.duration) / 1_000_000 if container.duration else 0.0,
            "width": video.codec_context.width,
            "height": video.codec_context.height,
            "codec": video.codec_context.name,
            "frames": frames,
        }


def test_real_media_runs_the_whole_pipeline(auth_api, db, tmp_path):
    from app.core.errors import AppError
    from app.models import (
        ClipStatus,
        Job,
        JobType,
        Project,
        RenderJob,
        RenderStatus,
        Transcript,
        TranscriptStatus,
        User,
        Video,
        VideoStatus,
        utcnow,
    )
    from app.services.clip_service import ClipService
    from app.services.job_service import JobService
    from app.services.pipeline import discover_job, render_job, transcribe, video_jobs
    from app.services.storage import get_storage

    source, expected_duration = _build_source(tmp_path)
    storage = get_storage()
    user_id = auth_api.user["id"]

    project = Project(
        user_id=user_id,
        name="Real media",
        source_language="en",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    key = f"projects/{project.id}/videos/real/source/source.mp4"
    storage.put_file(key, source, "video/mp4")
    video = Video(
        project_id=project.id,
        user_id=user_id,
        original_filename="source.mp4",
        storage_key=key,
        size_bytes=source.stat().st_size,
        status=VideoStatus.UPLOADING.value,
        narration_script=SCRIPT,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(video)
    db.commit()
    db.refresh(video)

    def run_job(type_: str, payload: dict, **kwargs):  # noqa: ANN001
        job = JobService.create(
            db,
            type_=type_,
            payload=payload,
            user_id=user_id,
            project_id=project.id,
            video_id=video.id,
            enqueue=False,
            **kwargs,
        )
        try:
            result = {
                JobType.VIDEO_PROBE.value: video_jobs.run_probe,
                JobType.VIDEO_TRANSCRIBE.value: transcribe.run,
                JobType.CLIP_RENDER.value: render_job.run,
            }[type_](db, job)
        except AppError as exc:
            JobService.mark_failed(db, job, exc)
            raise
        JobService.mark_succeeded(db, job, result or {})
        return job, result

    # ---------------------------------------------------------------- probe ---
    _, probe = run_job(JobType.VIDEO_PROBE.value, {"video_id": video.id})
    db.refresh(video)
    assert video.status == VideoStatus.READY.value
    assert abs(probe["duration"] - expected_duration) < 1.0
    assert probe["width"] == 640 and probe["height"] == 360
    assert probe["has_audio"] is True
    assert video.thumbnail_key

    # ---------------------------------------------------------- transcribe ---
    _, transcript_result = run_job(JobType.VIDEO_TRANSCRIBE.value, {"video_id": video.id})
    transcript = db.get(Transcript, transcript_result["transcript_id"])
    assert transcript.status == TranscriptStatus.COMPLETED.value
    assert transcript.word_count >= 10
    assert transcript.meta.get("has_word_timings") is True

    from app.models import TranscriptWord
    from sqlalchemy import select

    words = list(
        db.execute(
            select(TranscriptWord)
            .where(TranscriptWord.transcript_id == transcript.id)
            .order_by(TranscriptWord.start_time)
        ).scalars()
    )
    starts = [row.start_time for row in words]
    ends = [row.end_time for row in words]
    assert starts == sorted(starts), "word timings must be monotonic"
    assert all(0.0 <= start <= expected_duration for start in starts)
    assert all(start < end for start, end in zip(starts, ends))
    assert all(end <= expected_duration + 0.5 for end in ends)
    # The aligned text is the real script, not an invented transcript.
    joined = " ".join(row.word for row in words).lower()
    assert "publish" in joined

    # ------------------------------------------------------------- discover ---
    discovery = JobService.create(
        db,
        type_=JobType.PROJECT_DISCOVER_CLIPS.value,
        payload={
            "project_id": project.id,
            "video_id": video.id,
            "max_candidates": 3,
            "min_duration": 5.0,
            "max_duration": 20.0,
        },
        user_id=user_id,
        project_id=project.id,
        video_id=video.id,
        enqueue=False,
    )
    candidates_result = discover_job.run(db, discovery)
    JobService.mark_succeeded(db, discovery, candidates_result)
    assert candidates_result["candidates"] >= 1

    from app.models import ClipCandidate

    candidates = list(
        db.execute(
            select(ClipCandidate)
            .where(ClipCandidate.project_id == project.id)
            .order_by(ClipCandidate.rank)
        ).scalars()
    )
    assert candidates
    top = candidates[0]
    assert 0.0 <= top.start_time < top.end_time <= expected_duration + 0.5
    assert top.transcript_text
    assert top.scores and top.reason

    # ---------------------------------------------------------------- clip ---
    user = db.get(User, user_id)
    clip = ClipService.create_from_candidate(db, user, top)
    assert clip.status == ClipStatus.DRAFT.value
    assert clip.start_time == top.start_time

    # -------------------------------------------------------------- render ---
    render = ClipService.enqueue_render(db, user, clip)
    render_job.run(db, db.get(Job, render.job_id))

    db.expire_all()
    rendered = db.get(RenderJob, render.id)
    assert rendered.status == RenderStatus.SUCCEEDED.value
    assert rendered.storage_key
    assert "captions:none_available" not in (rendered.warnings or [])

    local = tmp_path / "rendered.mp4"
    with local.open("wb") as handle:
        for chunk in storage.open_stream(rendered.storage_key):
            handle.write(chunk)

    info = _decode_video_info(local)
    assert info["codec"] == "h264"
    assert (info["width"], info["height"]) == (1080, 1920)
    assert info["frames"] > 10
    assert math.isclose(info["duration"], clip.duration, abs_tol=1.0)

    # The rendered audio must correspond to the *selected original interval*:
    # correlate it against the source starting at clip.start_time. A wrong seek
    # (or a clip taken from elsewhere) collapses this correlation.
    import numpy as np

    source_audio = _decode_audio(source)
    rendered_audio = _decode_audio(local)
    rate = 16000
    offset = int(clip.start_time * rate)
    reference = source_audio[offset : offset + len(rendered_audio)]
    length = min(len(reference), len(rendered_audio), int(COMPARE_SECONDS * rate))
    assert length > rate
    correlation = float(np.corrcoef(reference[:length], rendered_audio[:length])[0, 1])
    assert correlation > 0.9, f"rendered audio does not match the selected source interval ({correlation:.3f})"

    # Negative control: a genuinely different interval must correlate worse.
    # (Skipped when the clip spans nearly the whole source, where no disjoint
    # window exists.)
    wrong_start = 0.0 if clip.start_time > 1.0 else max(0.0, expected_duration - clip.duration)
    if abs(wrong_start - clip.start_time) > 1.0:
        wrong_offset = int(wrong_start * rate)
        wrong = source_audio[wrong_offset : wrong_offset + length]
        if len(wrong) == length:
            wrong_correlation = float(np.corrcoef(wrong, rendered_audio[:length])[0, 1])
            assert correlation > wrong_correlation

    # ------------------------------------------------------------- captions ---
    from app.services.caption_service import CaptionService

    caption_words, has_word_timings, available = CaptionService.caption_words_for_clip(db, clip)
    assert available and has_word_timings
    assert caption_words
    assert all(0.0 <= row["start"] < row["end"] <= clip.duration + 0.5 for row in caption_words)
    assert " ".join(row["word"] for row in caption_words).strip()
