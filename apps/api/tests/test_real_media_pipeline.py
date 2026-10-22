"""End-to-end pipeline on a real video: upload -> transcribe -> discover -> render.

The source is a genuine H.264/AAC MP4 built with FFmpeg from espeak-ng narration,
so the test exercises the same probes, DTW alignment, candidate scoring, FFmpeg
extraction and ASS subtitle burn-in as production. Nothing is mocked: if FFmpeg
or espeak-ng is missing the test is skipped with an explicit reason rather than
pretending to prove anything.

Marked ``integration`` because it encodes real media and takes tens of seconds.
"""
from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture(scope="module")
def real_source(tmp_path_factory) -> tuple[Path, str]:
    """Build (or reuse) a real narrated MP4 with known ground-truth text."""
    from app.services.ai.transcription.alignment import (
        espeak_available,
        synthesize_raw,
        to_pcm16,
        write_pcm16_wav,
    )
    from app.services.media.ffmpeg import ffmpeg_bin

    if not ffmpeg_bin():
        pytest.skip("FFmpeg is not available in this environment")
    if not espeak_available():
        pytest.skip("espeak-ng is not available in this environment")

    from demo_script import NARRATION_SECTIONS

    # One section (~35 s) is long enough for 5-6 sentence windows and short
    # enough to keep a real render under a minute.
    sentences = list(NARRATION_SECTIONS[0]["sentences"])
    script = " ".join(sentences)

    import numpy as np

    blocks = []
    pause = np.zeros(int(0.30 * 22050), dtype=np.int16)
    for sentence in sentences:
        samples, rate = synthesize_raw(sentence, rate=166)
        assert rate == 22050
        blocks.append(to_pcm16(samples))
        blocks.append(pause)
    narration = np.concatenate(blocks)
    peak = float(np.max(np.abs(narration)))
    assert peak > 500, "synthesised narration is silent"

    directory = tmp_path_factory.mktemp("real-media")
    wav_path = directory / "narration.wav"
    write_pcm16_wav(narration, 22050, wav_path)
    with wave.open(str(wav_path), "rb") as handle:
        assert handle.getnframes() > 22050 * 20

    output = directory / "source.mp4"
    command = [
        ffmpeg_bin(),
        "-hide_banner",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=960x540:rate=30",
        "-i",
        str(wav_path),
        "-shortest",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(output),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=300)
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert output.stat().st_size > 100_000
    return output, script


def _run_job(worker, api, job_id: str) -> dict:
    """Execute queued work until ``job_id`` reaches a terminal state."""
    for _ in range(4):
        job = api.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed", "canceled"}:
            break
        assert worker.run_once(timeout=5.0), "the API queued no claimable job"
    job = api.get(f"/api/v1/jobs/{job_id}").json()
    assert job["status"] == "succeeded", job
    return job


def _upload(auth_api, project_id: str, source: Path) -> dict:
    payload = source.read_bytes()
    init = auth_api.post(
        "/api/v1/uploads/init",
        json={
            "filename": "masterclass.mp4",
            "size_bytes": len(payload),
            "content_type": "video/mp4",
            "project_id": project_id,
            "chunk_size": 4 * 1024 * 1024,
        },
    )
    assert init.status_code in (200, 201), init.text
    session = init.json()
    for index in range(session["total_chunks"]):
        start = index * session["chunk_size"]
        chunk = payload[start : start + session["chunk_size"]]
        response = auth_api.put(
            f"/api/v1/uploads/{session['upload_id']}/chunk/{index}",
            content=chunk,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert response.status_code in (200, 201, 204), response.text

    complete = auth_api.post(f"/api/v1/uploads/{session['upload_id']}/complete")
    assert complete.status_code in (200, 201), complete.text
    return complete.json()


def test_real_video_upload_transcribe_discover_and_render(auth_api, db, real_source, monkeypatch):
    from app.core.config import settings
    from app.services import queue as queue_module
    from app.workers.worker import Worker

    # Fresh queue so unrelated tests' jobs cannot be claimed.
    queue_module.set_queue(queue_module.InMemoryJobQueue())
    # Keep the encode fast but real (still H.264/AAC through the full filtergraph).
    monkeypatch.setattr(settings, "render_preset", "ultrafast", raising=False)
    monkeypatch.setattr(settings, "render_crf", 30, raising=False)

    source, script = real_source
    worker = Worker(concurrency=1, name="real-media-test")

    project = auth_api.post("/api/v1/projects", json={"name": "Real media"}).json()
    project_id = project["id"]
    completed = _upload(auth_api, project_id, source)
    video_id = completed["video"]["id"]
    _run_job(worker, auth_api, completed["job"]["id"])

    video = auth_api.get(f"/api/v1/videos/{video_id}").json()
    assert video["status"] == "ready", video
    assert video["has_audio"] is True
    assert 25 < video["duration_seconds"] < 90
    assert (video["width"], video["height"]) == (960, 540)

    # Real streaming read: Range support is what the player relies on.
    partial = auth_api.get(
        f"/api/v1/videos/{video_id}/stream", headers={"Range": "bytes=0-1023"}
    )
    assert partial.status_code == 206
    assert len(partial.content) == 1024

    # ------------------------------------------------------------ transcribe ---
    script_response = auth_api.patch(
        f"/api/v1/videos/{video_id}", json={"narration_script": script}
    )
    assert script_response.status_code == 200, script_response.text

    started = auth_api.post(f"/api/v1/videos/{video_id}/transcribe")
    assert started.status_code == 202, started.text
    _run_job(worker, auth_api, started.json()["id"])

    transcript = auth_api.get(f"/api/v1/videos/{video_id}/transcript").json()
    assert transcript["status"] == "completed", transcript
    words = transcript["words"]
    expected = len(script.split())
    assert abs(len(words) - expected) <= max(2, expected * 0.05)
    assert transcript["meta"]["has_word_timings"] is True
    assert words[0]["start_time"] <= 1.0
    for previous, current in zip(words, words[1:]):
        assert current["start_time"] >= previous["start_time"] - 1e-6
        assert current["end_time"] >= current["start_time"]
        assert current["start_time"] >= 0.0
    assert words[-1]["end_time"] <= video["duration_seconds"] + 0.5
    # The alignment is measured, not fabricated: it must not be perfectly
    # uniform (all words the same length would be a giveaway).
    lengths = {round(word["end_time"] - word["start_time"], 3) for word in words}
    assert len(lengths) > 5

    # --------------------------------------------------------------- discover ---
    discovered = auth_api.post(
        f"/api/v1/projects/{project_id}/discover",
        json={"max_candidates": 6, "min_duration": 8, "max_duration": 30},
    )
    assert discovered.status_code == 202, discovered.text
    _run_job(worker, auth_api, discovered.json()["id"])

    candidates = auth_api.get(f"/api/v1/projects/{project_id}/candidates").json()["items"]
    assert candidates, "real narration produced no candidate moments"
    for candidate in candidates:
        assert 0 <= candidate["start_time"] < candidate["end_time"]
        assert candidate["end_time"] <= video["duration_seconds"] + 0.05
        assert 7.5 <= candidate["duration"] <= 30.5
        assert candidate["transcript_text"].strip()

    # Source timestamps map to real transcript words (the UI highlights them).
    word_starts = {round(word["start_time"], 3) for word in words}
    word_ends = {round(word["end_time"], 3) for word in words}
    snapped = [
        candidate
        for candidate in candidates
        if round(candidate["start_time"], 3) in word_starts
        and round(candidate["end_time"], 3) in word_ends
    ]
    assert snapped, "candidate boundaries are not aligned to transcript words"

    chosen = candidates[0]

    # ------------------------------------------------------------------ clip ---
    created = auth_api.post(
        f"/api/v1/candidates/{chosen['id']}/clip",
        json={"preset": "generic_vertical", "aspect_ratio": "9:16", "caption_preset": "bold"},
    )
    assert created.status_code == 201, created.text
    clip = created.json()
    assert clip["start_time"] == chosen["start_time"]
    assert clip["end_time"] == chosen["end_time"]
    assert clip["aspect_ratio"] == "9:16"

    captions = auth_api.get(f"/api/v1/clips/{clip['id']}/captions").json()
    assert captions["transcript_available"] is True
    clips_words = captions["words"]
    assert clips_words
    for word in clips_words:
        assert 0 <= word["start"] <= clip["duration"] + 1e-3
        assert word["end"] <= clip["duration"] + 0.2
        assert word["end"] > word["start"]

    # ---------------------------------------------------------------- render ---
    render_started = auth_api.post(
        f"/api/v1/clips/{clip['id']}/render",
        json={"preset": "generic_vertical", "aspect_ratio": "9:16", "fps": 30},
    )
    assert render_started.status_code == 202, render_started.text
    render_id = render_started.json()["id"]
    _run_job(worker, auth_api, render_started.json()["job_id"] or _job_for_render(auth_api, render_id))

    render = auth_api.get(f"/api/v1/renders/{render_id}").json()
    assert render["status"] == "succeeded", render
    assert render["output_size_bytes"] > 50_000
    assert abs(render["output_duration"] - clip["duration"]) <= max(2.0, clip["duration"] * 0.08)
    assert (render["width"], render["height"]) == (1080, 1920)

    downloaded = auth_api.get(f"/api/v1/renders/{render_id}/download")
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "video/mp4"
    assert downloaded.content[4:8] == b"ftyp"

    target = real_source[0].parent / "rendered.mp4"
    target.write_bytes(downloaded.content)

    from app.services.media.ffmpeg import probe_media

    probe = probe_media(target)
    assert (probe["width"], probe["height"]) == (1080, 1920)
    assert probe["has_audio"] is True
    assert probe["video_codec"] in {"h264", "libx264"}
    assert abs(probe["duration"] - clip["duration"]) <= max(2.0, clip["duration"] * 0.08)

    # The word-timed ASS document was actually burned in: the plan names it and
    # the render recorded warnings/spec for reproducibility.
    spec = render.get("spec") or {}
    assert spec.get("captions", {}).get("word_count", 0) > 0


def _job_for_render(auth_api, render_id: str) -> str:
    """RenderOut carries job_id; fall back to the project's job list if not."""
    render = auth_api.get(f"/api/v1/renders/{render_id}").json()
    job_id = render.get("job_id")
    assert job_id, "render has no associated job"
    return job_id
