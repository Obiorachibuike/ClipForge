"""Regression tests for the audit's confirmed defects.

Each test here failed (or would have failed) against the pre-fix code and is
written against the real API/service path, not a mock of it.
"""
from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import select


# ------------------------------------------------------------------ helpers ---
def _project(db, user_id: str, name: str = "Hardening"):
    from app.models import Project, utcnow

    project = Project(user_id=user_id, name=name, created_at=utcnow(), updated_at=utcnow())
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _video(db, project, *, duration: float = 120.0, status: str = "ready"):
    from app.models import Video, VideoStatus, utcnow

    video = Video(
        project_id=project.id,
        user_id=project.user_id,
        original_filename="source.mp4",
        storage_key=f"projects/{project.id}/videos/{project.id}/source/source.mp4",
        size_bytes=2048,
        status=status or VideoStatus.READY.value,
        has_audio=True,
        duration_seconds=duration,
        width=1920,
        height=1080,
        fps=30.0,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(video)
    db.commit()
    db.refresh(video)
    return video


def _transcript(db, video, *, status: str = "completed", has_word_timings: bool = True):
    from app.models import Transcript, TranscriptStatus, utcnow

    transcript = Transcript(
        video_id=video.id,
        project_id=video.project_id,
        status=status or TranscriptStatus.COMPLETED.value,
        provider="test",
        language="en",
        duration_seconds=float(video.duration_seconds or 0),
        meta={"has_word_timings": has_word_timings},
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(transcript)
    db.commit()
    db.refresh(transcript)
    return transcript


# ------------------------------------------------- 1. discovery input safety ---
def test_discovery_rejects_extra_identifiers_instead_of_merging_them(auth_api, db):
    """The body used to be spread into the job payload, letting a caller replace
    `video_id` with another account's video. Unknown keys are now refused."""
    user_id = auth_api.user["id"]
    project = _project(db, user_id, "Discovery")
    _video(db, project)
    _transcript(db, _video(db, project, duration=60.0))

    injected = auth_api.post(
        f"/api/v1/projects/{project.id}/discover",
        json={"video_id": "someone-elses-video", "max_candidates": 5},
    )
    assert injected.status_code == 422, injected.text

    response = auth_api.post(
        f"/api/v1/projects/{project.id}/discover",
        json={"max_candidates": 5, "min_duration": 12.0, "max_duration": 60.0},
    )
    assert response.status_code == 202, response.text

    from app.models import Job, Video

    job = db.get(Job, response.json()["id"])
    assert job.payload["project_id"] == project.id
    own_video_ids = {
        row.id for row in db.execute(select(Video)).scalars() if row.project_id == project.id
    }
    assert job.payload["video_id"] in own_video_ids
    assert job.payload["max_candidates"] == 5
    assert "someone-elses-video" not in str(job.payload)


def test_discovery_job_refuses_a_video_from_another_project(auth_api, db, second_api):
    """Defense in depth: a durable job payload naming a foreign video must not be
    analyzed, even if it was written before the endpoint was tightened."""
    from app.core.errors import NotFoundError
    from app.models import JobType
    from app.services.job_service import JobService
    from app.services.pipeline import discover_job

    owner_id = auth_api.user["id"]
    other_id = second_api.user["id"]
    mine = _project(db, owner_id, "Mine")
    theirs = _project(db, other_id, "Theirs")
    foreign_video = _video(db, theirs)
    _transcript(db, foreign_video)

    job = JobService.create(
        db,
        type_=JobType.PROJECT_DISCOVER_CLIPS.value,
        payload={"project_id": mine.id, "video_id": foreign_video.id},
        user_id=owner_id,
        project_id=mine.id,
        enqueue=False,
    )
    with pytest.raises(NotFoundError):
        discover_job.run(db, job)


def test_discovery_clamps_untrusted_duration_settings(db):
    """Payload values are clamped inside the job as well as at the API boundary."""
    from app.services.pipeline.discover_job import (
        DURATION_CEILING,
        DURATION_FLOOR,
        MAX_CANDIDATES_LIMIT,
    )

    assert max(1, min(100_000, MAX_CANDIDATES_LIMIT)) == MAX_CANDIDATES_LIMIT
    assert max(DURATION_FLOOR, min(-50.0, DURATION_CEILING)) == DURATION_FLOOR


# --------------------------------------------------------- 2. queue safety ---
def test_queue_release_requeues_without_deadlocking():
    """`release()` held the condition lock and then called `enqueue()`, which
    re-acquired the same non-reentrant lock: the worker thread hung forever."""
    from app.services.queue import InMemoryJobQueue

    queue = InMemoryJobQueue()
    queue.enqueue("job-1")
    assert queue.claim("worker", timeout=0.5) == "job-1"

    done = threading.Event()
    error: list[BaseException] = []

    def release() -> None:
        try:
            queue.release("job-1", delay_seconds=0.0)
        except BaseException as exc:  # pragma: no cover - failure detail
            error.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=release, daemon=True)
    thread.start()
    assert done.wait(2.0), "release() deadlocked the calling thread"
    thread.join(1.0)
    assert not error
    assert queue.is_queued("job-1")
    assert queue.claim("worker-2", timeout=0.5) == "job-1"


def test_worker_survives_transient_queue_failures():
    """A Redis blip used to escape `run_forever` and kill the worker process."""
    from app.services.queue import QueueUnavailable
    from app.workers.worker import Worker

    class FlakyQueue:
        name = "flaky"

        def __init__(self) -> None:
            self.calls = 0

        def claim(self, worker_id: str, timeout: float = 1.0):  # noqa: ANN001
            self.calls += 1
            if self.calls == 1:
                raise QueueUnavailable("redis went away")
            worker._stop.set()  # stop cleanly on the second poll
            return None

        def ack(self, job_id: str) -> None:  # pragma: no cover - not reached
            return None

        def backend_name(self) -> str:
            return self.name

    worker = Worker(concurrency=1, name="test-worker")
    fake = FlakyQueue()
    worker.queue = fake

    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()
    thread.join(5.0)
    assert not thread.is_alive(), "worker did not recover from a claim failure"
    assert fake.calls >= 2


# --------------------------------------------------------- 3. upload bounds ---
def _init_upload(auth_api, size_bytes: int = 4096) -> dict:
    project = auth_api.post("/api/v1/projects", json={"name": "Uploads"}).json()
    response = auth_api.post(
        "/api/v1/uploads/init",
        json={
            "filename": "clip.mp4",
            "size_bytes": size_bytes,
            "content_type": "video/mp4",
            "project_id": project["id"],
            "chunk_size": 256 * 1024,
        },
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


def test_oversized_chunk_is_rejected_before_being_buffered(auth_api):
    session = _init_upload(auth_api)
    expected = min(session["chunk_size"], 4096)
    response = auth_api.put(
        f"/api/v1/uploads/{session['upload_id']}/chunk/0",
        content=b"x" * (expected + 4096),
    )
    assert response.status_code == 413, response.text
    assert response.json()["code"] == "payload_too_large"

    # The rejected body never became a stored chunk.
    status = auth_api.get(f"/api/v1/uploads/{session['upload_id']}").json()
    assert status["received_chunks"] == []


def test_chunk_index_is_validated_before_reading_the_body(auth_api):
    session = _init_upload(auth_api)
    response = auth_api.put(
        f"/api/v1/uploads/{session['upload_id']}/chunk/99",
        content=b"x" * 1024,
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_error"


def test_a_chunk_of_the_exact_declared_size_still_works(auth_api):
    session = _init_upload(auth_api)
    expected = min(session["chunk_size"], 4096)
    response = auth_api.put(
        f"/api/v1/uploads/{session['upload_id']}/chunk/0",
        content=b"x" * expected,
    )
    assert response.status_code == 200, response.text


# ------------------------------------------------------ 4. production config ---
def test_production_refuses_placeholder_or_short_secrets():
    from app.core.config import Settings, validate_runtime_settings

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        validate_runtime_settings(
            Settings(
                _env_file=None,
                environment="production",
                secret_key="dev-only-insecure-secret-change-me",
                jwt_secret="j" * 48,
            )
        )
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        validate_runtime_settings(
            Settings(_env_file=None, environment="production", secret_key="short", jwt_secret="j" * 48)
        )


def test_production_accepts_real_secrets_and_flags_soft_risks():
    from app.core.config import Settings, validate_runtime_settings

    secure = Settings(
        _env_file=None,
        environment="production",
        secret_key="s" * 48,
        jwt_secret="j" * 48,
        cookie_secure=True,
        auto_create_tables=False,
        storage_backend="s3",
    )
    assert validate_runtime_settings(secure) == []

    warnings = validate_runtime_settings(
        Settings(
            _env_file=None,
            environment="production",
            secret_key="s" * 48,
            jwt_secret="j" * 48,
            cookie_secure=False,
            auto_create_tables=True,
        )
    )
    assert any("COOKIE_SECURE" in item for item in warnings)
    assert any("AUTO_CREATE_TABLES" in item for item in warnings)


def test_development_settings_are_not_rejected():
    from app.core.config import Settings, validate_runtime_settings

    assert validate_runtime_settings(Settings(_env_file=None, environment="test")) == []


# --------------------------------------------------------- 5. clip timelines ---
def test_manual_clip_rejects_negative_start(auth_api, db):
    user_id = auth_api.user["id"]
    project = _project(db, user_id, "Clips")
    video = _video(db, project, duration=60.0, status="ready")
    response = auth_api.post(
        f"/api/v1/projects/{project.id}/clips",
        json={"start_time": -5.0, "end_time": 20.0, "title": "negative"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_error"
    assert "negative" in response.json()["message"].lower()
    assert video.duration_seconds == 60.0


def test_manual_clip_rejects_start_after_the_source_ends(auth_api, db):
    user_id = auth_api.user["id"]
    project = _project(db, user_id, "Clips2")
    _video(db, project, duration=30.0)
    response = auth_api.post(
        f"/api/v1/projects/{project.id}/clips",
        json={"start_time": 45.0, "end_time": 55.0},
    )
    assert response.status_code == 422, response.text


def test_service_rejects_non_finite_timestamps(db):
    from app.core.errors import ValidationError
    from app.models import User, utcnow
    from app.services.clip_service import ClipService

    user = User(
        email=f"nan-{time.time_ns()}@example.com",
        name="NaN",
        hashed_password="x",
        is_active=True,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(user)
    db.commit()
    project = _project(db, user.id, "NaN")
    video = _video(db, project, duration=60.0)

    with pytest.raises(ValidationError):
        ClipService.snap_and_validate(db, video, float("nan"), 10.0)
    with pytest.raises(ValidationError):
        ClipService.snap_and_validate(db, video, 10.0, float("inf"))


# ------------------------------------------------- 6. caption timing honesty ---
def test_captions_use_segment_timing_when_no_word_timings_exist(db):
    from app.models import Clip, ClipStatus, TranscriptSegment, User, utcnow
    from app.services.caption_service import CaptionService

    user = User(
        email=f"captions-{time.time_ns()}@example.com",
        name="Captions",
        hashed_password="x",
        is_active=True,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(user)
    db.commit()
    project = _project(db, user.id, "Captions")
    video = _video(db, project, duration=60.0)
    transcript = _transcript(db, video, has_word_timings=False)
    db.add_all(
        [
            TranscriptSegment(
                transcript_id=transcript.id,
                index=0,
                start_time=5.0,
                end_time=9.0,
                text="first spoken sentence",
                word_count=3,
                avg_confidence=0.8,
                created_at=utcnow(),
                updated_at=utcnow(),
            ),
            TranscriptSegment(
                transcript_id=transcript.id,
                index=1,
                start_time=9.5,
                end_time=14.0,
                text="second spoken sentence",
                word_count=3,
                avg_confidence=0.8,
                created_at=utcnow(),
                updated_at=utcnow(),
            ),
        ]
    )
    db.commit()
    clip = Clip(
        project_id=project.id,
        video_id=video.id,
        user_id=user.id,
        title="Segment captions",
        status=ClipStatus.READY.value,
        start_time=5.0,
        end_time=15.0,
        duration=10.0,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(clip)
    db.commit()

    words, has_word_timings, available = CaptionService.caption_words_for_clip(db, clip)
    assert available is True
    assert has_word_timings is False
    assert len(words) == 2
    assert words[0]["word"] == "first spoken sentence"
    assert words[0]["start"] == 0.0
    assert words[0]["end"] == 4.0
    assert words[1]["start"] == 4.5
    assert words[1]["end"] == 9.0
    # No invented sub-segment boundaries.
    assert {round(w["start"], 3) for w in words} == {0.0, 4.5}


# ------------------------------------------------------------ 7. export 404 ---
def test_export_download_reports_a_missing_file(auth_api, db):
    from app.models import (
        Clip,
        ClipStatus,
        Export,
        RenderJob,
        RenderStatus,
        utcnow,
    )

    user_id = auth_api.user["id"]
    project = _project(db, user_id, "Exports")
    video = _video(db, project)
    clip = Clip(
        project_id=project.id,
        video_id=video.id,
        user_id=user_id,
        title="Export clip",
        status=ClipStatus.RENDERED.value,
        start_time=0.0,
        end_time=10.0,
        duration=10.0,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(clip)
    db.commit()
    render = RenderJob(
        clip_id=clip.id,
        project_id=project.id,
        user_id=user_id,
        status=RenderStatus.SUCCEEDED.value,
        storage_key="renders/missing.mp4",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(render)
    db.commit()
    export = Export(
        render_id=render.id,
        clip_id=clip.id,
        project_id=project.id,
        user_id=user_id,
        filename="missing.mp4",
        storage_key="renders/does-not-exist.mp4",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(export)
    db.commit()

    response = auth_api.get(f"/api/v1/exports/{export.id}/download")
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "export_file_missing"


# ---------------------------------------------------- 8. render sanity check ---
def test_render_duration_tolerance_detects_a_wrong_cut():
    from app.services.pipeline.render_job import duration_matches

    assert duration_matches(43.0, 42.98) is True
    assert duration_matches(42.2, 42.98) is True
    assert duration_matches(30.0, 42.98) is False
    assert duration_matches(59.0, 42.98) is False


# ------------------------------------------------ 9. URL host safety (SSRF) ---
def test_remote_url_validation_rejects_loopback_and_lookalike_hosts():
    from app.core.errors import ValidationError
    from app.services.remote_video import validate_remote_video_url

    for bad in (
        "http://127.0.0.1/video",
        "http://localhost/video",
        "https://youtube.com.evil.example/watch?v=1",
        "https://notyoutube.com/watch?v=1",
        "https://youtube.com@127.0.0.1/watch?v=1",
        "ftp://youtube.com/watch?v=1",
    ):
        with pytest.raises(ValidationError):
            validate_remote_video_url(bad)

    assert validate_remote_video_url("https://www.youtube.com/watch?v=abc") == (
        "https://www.youtube.com/watch?v=abc"
    )


def test_public_host_check_rejects_private_dns_answers(monkeypatch):
    import socket

    from app.core.errors import MediaError
    from app.services import remote_video

    def fake_getaddrinfo(host, port, **kwargs):  # noqa: ANN001
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port))]

    monkeypatch.setattr(remote_video.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(MediaError) as excinfo:
        remote_video.assert_public_host("youtube.com")
    assert excinfo.value.code == "blocked_video_host"

    def loopback(host, port, **kwargs):  # noqa: ANN001
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", port, 0, 0)),
        ]

    monkeypatch.setattr(remote_video.socket, "getaddrinfo", loopback)
    with pytest.raises(MediaError):
        remote_video.assert_public_host("youtube.com")


def test_public_host_check_allows_public_answers(monkeypatch):
    import socket

    from app.services import remote_video

    def public(host, port, **kwargs):  # noqa: ANN001
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(remote_video.socket, "getaddrinfo", public)
    remote_video.assert_public_host("youtube.com")


# ------------------------------------------------ 10. probe failure states ---
def test_failed_probe_marks_the_video_failed(auth_api, db):
    from app.core.errors import AppError
    from app.models import JobType, VideoStatus
    from app.services.job_service import JobService
    from app.services.pipeline import video_jobs
    from app.services.storage import get_storage

    user_id = auth_api.user["id"]
    project = _project(db, user_id, "Probe")
    video = _video(db, project, status="uploading")
    get_storage().put_bytes(video.storage_key, b"this is definitely not a video" * 64, "video/mp4")

    job = JobService.create(
        db,
        type_=JobType.VIDEO_PROBE.value,
        payload={"video_id": video.id},
        user_id=user_id,
        project_id=project.id,
        video_id=video.id,
        enqueue=False,
    )
    with pytest.raises(AppError):
        video_jobs.run_probe(db, job)

    db.expire_all()
    refreshed = db.get(type(video), video.id)
    assert refreshed.status == VideoStatus.FAILED.value
    assert refreshed.error_message


# ---------------------------------------- 11. transcription state handling ---
def test_failed_transcription_is_persisted_and_keeps_the_previous_transcript(auth_api, db):
    from app.core.errors import AppError
    from app.models import JobType, Transcript, TranscriptStatus
    from app.services.job_service import JobService
    from app.services.pipeline import transcribe

    user_id = auth_api.user["id"]
    project = _project(db, user_id, "Transcribe")
    video = _video(db, project, duration=30.0)
    video.narration_script = "A short narration script that does not exist in storage."
    db.commit()
    previous = _transcript(db, video)
    assert previous.status == TranscriptStatus.COMPLETED.value

    job = JobService.create(
        db,
        type_=JobType.VIDEO_TRANSCRIBE.value,
        payload={"video_id": video.id},
        user_id=user_id,
        project_id=project.id,
        video_id=video.id,
        enqueue=False,
    )
    with pytest.raises(AppError):
        transcribe.run(db, job)

    db.expire_all()
    rows = list(db.execute(select(Transcript).where(Transcript.video_id == video.id)).scalars())
    statuses = sorted(row.status for row in rows)
    assert TranscriptStatus.FAILED.value in statuses
    # The previously valid transcript is untouched, so a later render still works.
    still_there = [row for row in rows if row.id == previous.id][0]
    assert still_there.status == TranscriptStatus.COMPLETED.value
    failed = [row for row in rows if row.status == TranscriptStatus.FAILED.value][0]
    assert failed.error_message


def test_transcription_retry_reuses_the_pending_row(db):
    from app.models import User, utcnow
    from app.services.pipeline.transcribe import _prepare_transcript

    user = User(
        email=f"retry-{time.time_ns()}@example.com",
        name="Retry",
        hashed_password="x",
        is_active=True,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(user)
    db.commit()
    project = _project(db, user.id, "Retry")
    video = _video(db, project)

    first = _prepare_transcript(db, video, None)  # type: ignore[arg-type]
    second = _prepare_transcript(db, video, None)  # type: ignore[arg-type]
    assert first.id == second.id


# ------------------------------------------- 12. cross-process cancellation ---
def test_cancel_requested_in_another_process_stops_progress(auth_api, db):
    """The HTTP process sets `cancel_requested`; the worker must see it.

    `registry` is process-local, so a worker in another container only learns
    about a cancel through the database (or the Redis flag). Progress reporting
    is the loop that has to check it.
    """
    from app.core.errors import CancelledError_
    from app.models import JobType
    from app.services import job_service
    from app.services.job_service import JobService

    user_id = auth_api.user["id"]
    job = JobService.create(
        db,
        type_=JobType.CLIP_RENDER.value,
        payload={},
        user_id=user_id,
        project_id=None,
        enqueue=False,
    )
    job_service._last_cancel_poll.pop(job.id, None)
    assert job_service._durable_cancel_requested(db, job) is False

    job.cancel_requested = True
    db.commit()
    job_service._last_cancel_poll.pop(job.id, None)
    assert job_service._durable_cancel_requested(db, job) is True

    job_service._last_cancel_poll.pop(job.id, None)
    with pytest.raises(CancelledError_):
        JobService.report_progress(db, job, stage="encoding", progress=50, force=True)


def test_render_timeout_is_bounded():
    from app.services.pipeline.render_job import (
        MAX_RENDER_TIMEOUT,
        MIN_RENDER_TIMEOUT,
        render_timeout,
    )

    assert render_timeout(1.0) == MIN_RENDER_TIMEOUT
    assert render_timeout(60.0) == 60.0 * 40.0
    assert render_timeout(10 * 3600) == MAX_RENDER_TIMEOUT


# --------------------------------------------- 13. upload content sniffing ---
def test_upload_rejects_a_file_that_is_not_media(auth_api):
    """A PDF, HTML page or text file renamed to .mp4 must not become a video."""
    project_id = auth_api.post("/api/v1/projects", json={"name": "Sniff"}).json()["id"]
    payload = b"%PDF-1.7\n" + b"not a video at all\n" * 200
    response = auth_api.post(
        f"/api/v1/projects/{project_id}/videos",
        files={"file": ("holiday.mp4", payload, "video/mp4")},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_error"
    assert "video" in response.json()["message"].lower()


def test_container_sniffing_keeps_accepting_real_headers():
    from app.services.upload_service import definitely_not_media, looks_like_media

    assert looks_like_media(b"\x00\x00\x00\x20ftypisom", "clip.mp4")
    assert not definitely_not_media(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 40)
    assert not definitely_not_media(b"\x1a\x45\xdf\xa3" + b"\x00" * 40)
    assert definitely_not_media(b"<html><body>error</body></html>")
    assert definitely_not_media(b"1\n00:00:00,000 --> 00:00:02,000\nhello\n")
