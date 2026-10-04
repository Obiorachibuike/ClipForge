"""Narration-script editing and render downloads.

Both are user-facing controls that existed in the UI before they existed in the
API (the script textarea had no route at all, and the editor's Download button
pointed at a 404). These tests pin the routes so that cannot regress.
"""
from __future__ import annotations

SCRIPT = "Most people get this wrong. Here is what actually works when you publish every day."


def _project_and_video(auth_api) -> tuple[str, str]:
    """Minimal real rows: a project, and a video produced by a completed upload."""
    project_id = auth_api.post("/api/v1/projects", json={"name": "Scripts"}).json()["id"]
    init = auth_api.post(
        "/api/v1/uploads/init",
        json={
            "filename": "take.mp4",
            "size_bytes": 4096,
            "content_type": "video/mp4",
            "project_id": project_id,
        },
    )
    assert init.status_code in (200, 201), init.text
    session = init.json()
    payload = (b"not-a-real-video!" * 300)[:4096]
    for index in range(session["total_chunks"]):
        auth_api.put(f"/api/v1/uploads/{session['upload_id']}/chunk/{index}", content=payload)
    complete = auth_api.post(f"/api/v1/uploads/{session['upload_id']}/complete")
    assert complete.status_code in (200, 201), complete.text
    video_id = complete.json()["video"]["id"]
    return project_id, video_id


def test_narration_script_round_trip(auth_api):
    _, video_id = _project_and_video(auth_api)

    # Nothing stored yet, and the flag is honest about it.
    fresh = auth_api.get(f"/api/v1/videos/{video_id}/narration-script").json()
    assert fresh["narration_script"] == ""
    assert fresh["has_narration_script"] is False
    assert fresh["word_count"] == 0

    saved = auth_api.patch(f"/api/v1/videos/{video_id}", json={"narration_script": SCRIPT})
    assert saved.status_code == 200, saved.text

    detail = auth_api.get(f"/api/v1/videos/{video_id}").json()
    assert detail["has_narration_script"] is True
    # The script text itself is not part of the (list-shaped) video payload.
    assert "narration_script" not in detail

    fetched = auth_api.get(f"/api/v1/videos/{video_id}/narration-script").json()
    assert fetched["narration_script"] == SCRIPT
    assert fetched["character_count"] == len(SCRIPT)
    assert fetched["word_count"] == len(SCRIPT.split())


def test_clearing_the_script_sets_the_flag_back(auth_api):
    _, video_id = _project_and_video(auth_api)
    auth_api.patch(f"/api/v1/videos/{video_id}", json={"narration_script": SCRIPT})
    cleared = auth_api.patch(f"/api/v1/videos/{video_id}", json={"narration_script": "   "})

    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["has_narration_script"] is False
    assert auth_api.get(f"/api/v1/videos/{video_id}/narration-script").json()["narration_script"] == ""


def test_script_change_supersedes_a_completed_transcript(auth_api, db):
    """Stale word timings must never keep presenting as current."""
    from sqlalchemy import select

    from app.models import Transcript, TranscriptStatus, Video, utcnow

    _, video_id = _project_and_video(auth_api)
    video = db.get(Video, video_id)

    transcript = Transcript(
        video_id=video_id,
        project_id=video.project_id,
        status=TranscriptStatus.COMPLETED.value,
        provider="alignment",
        language="en",
        full_text="old words",
        word_count=2,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(transcript)
    db.commit()

    response = auth_api.patch(f"/api/v1/videos/{video_id}", json={"narration_script": SCRIPT})
    assert response.status_code == 200, response.text

    db.expire_all()
    refreshed = db.execute(select(Transcript).where(Transcript.video_id == video_id)).scalars().one()
    assert refreshed.status == TranscriptStatus.SUPERSEDED.value
    assert "script changed" in refreshed.error_message.lower()


def test_script_is_length_limited_and_owner_scoped(auth_api, anon_api, second_api):
    _, video_id = _project_and_video(auth_api)

    too_long = auth_api.patch(f"/api/v1/videos/{video_id}", json={"narration_script": "x" * 200_001})
    assert too_long.status_code == 422

    # Signed out entirely: the route is not reachable.
    assert anon_api.get(f"/api/v1/videos/{video_id}/narration-script").status_code == 401
    # Signed in as somebody else: the video does not exist for them.
    assert second_api.get(f"/api/v1/videos/{video_id}/narration-script").status_code == 404
    assert (
        second_api.patch(f"/api/v1/videos/{video_id}", json={"narration_script": SCRIPT}).status_code == 404
    )


def test_render_download_serves_real_bytes_and_ranges(auth_api, second_api, db, tmp_path):
    from app.models import Clip, ClipStatus, Project, RenderJob, RenderStatus, Video, utcnow
    from app.services.storage import get_storage

    project_id, video_id = _project_and_video(auth_api)
    video = db.get(Video, video_id)
    storage = get_storage()

    # Put real bytes where the render service would put them.
    blob = b"\x00\x00\x00\x20ftypisom" + bytes(range(256)) * 8
    key = f"renders/{video_id}/test-output.mp4"
    stored = storage.put_bytes(key, blob, content_type="video/mp4")
    assert stored.size == len(blob)

    clip = Clip(
        project_id=project_id,
        video_id=video_id,
        user_id=video.user_id,
        title="Script test clip",
        status=ClipStatus.READY.value,
        start_time=0.0,
        end_time=12.0,
        duration=12.0,
        aspect_ratio="9:16",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(clip)
    db.commit()

    render = RenderJob(
        clip_id=clip.id,
        project_id=project_id,
        user_id=video.user_id,
        preset="tiktok",
        aspect_ratio="9:16",
        width=1080,
        height=1920,
        fps=30,
        status=RenderStatus.SUCCEEDED.value,
        storage_key=key,
        output_size_bytes=len(blob),
        output_duration=12.0,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(render)
    db.commit()

    # A queued render is not downloadable — no empty files, no lying.
    render.status = RenderStatus.QUEUED.value
    db.commit()
    assert auth_api.get(f"/api/v1/renders/{render.id}/download").status_code == 409
    render.status = RenderStatus.SUCCEEDED.value
    render.storage_key = ""
    db.commit()
    assert auth_api.get(f"/api/v1/renders/{render.id}/download").status_code == 409
    render.storage_key = key
    db.commit()

    full = auth_api.get(f"/api/v1/renders/{render.id}/download")
    assert full.status_code == 200, full.text
    assert full.headers["content-type"] == "video/mp4"
    assert full.headers["accept-ranges"] == "bytes"
    assert "attachment" in full.headers["content-disposition"]
    assert full.content == blob

    # Inline mode is what an in-page player needs.
    inline = auth_api.get(f"/api/v1/renders/{render.id}/download?attachment=false")
    assert "inline" in inline.headers["content-disposition"]

    partial = auth_api.get(f"/api/v1/renders/{render.id}/download", headers={"Range": "bytes=10-19"})
    assert partial.status_code == 206
    assert partial.content == blob[10:20]
    assert partial.headers["content-range"] == f"bytes 10-19/{len(blob)}"

    # Ownership is enforced on the byte path too.
    assert second_api.get(f"/api/v1/renders/{render.id}/download").status_code == 404


def test_render_download_reports_a_missing_file(auth_api, db):
    from app.models import Clip, ClipStatus, RenderJob, RenderStatus, Video, utcnow

    project_id, video_id = _project_and_video(auth_api)
    video = db.get(Video, video_id)

    clip = Clip(
        project_id=project_id,
        video_id=video_id,
        user_id=video.user_id,
        title="Gone",
        status=ClipStatus.READY.value,
        start_time=0.0,
        end_time=5.0,
        duration=5.0,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(clip)
    db.commit()

    render = RenderJob(
        clip_id=clip.id,
        project_id=project_id,
        user_id=video.user_id,
        preset="tiktok",
        status=RenderStatus.SUCCEEDED.value,
        storage_key="renders/does-not-exist.mp4",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(render)
    db.commit()

    response = auth_api.get(f"/api/v1/renders/{render.id}/download")
    assert response.status_code == 404
    assert response.json()["code"] in ("render_file_missing", "render_not_found")


def test_video_out_reports_the_script_flag_without_the_text(auth_api, db):
    """The list endpoint stays small even with a long script stored."""
    from sqlalchemy import select

    from app.models import Video

    _, video_id = _project_and_video(auth_api)
    auth_api.patch(f"/api/v1/videos/{video_id}", json={"narration_script": "word " * 4000})

    listing = auth_api.get(f"/api/v1/videos/{video_id}")
    body = listing.json()
    assert body["has_narration_script"] is True
    assert "narration_script" not in body
    assert len(listing.content) < 4000

    db.expire_all()
    video = db.execute(select(Video).where(Video.id == video_id)).scalars().one()
    assert video.narration_script.strip().startswith("word word")


def test_video_transcript_route_returns_words(auth_api, db):
    """Regression: this route used to 500 on every video that had a transcript.

    `video_transcript` called the `read_transcript` handler directly, so the
    `word_limit` parameter arrived as a FastAPI `Query` object and blew up inside
    SQLAlchemy's `.limit()`. It is the endpoint the project page reads, so the
    failure was total rather than cosmetic.
    """
    from app.models import Transcript, TranscriptSegment, TranscriptStatus, TranscriptWord, Video, utcnow

    _, video_id = _project_and_video(auth_api)
    video = db.get(Video, video_id)

    transcript = Transcript(
        video_id=video_id,
        project_id=video.project_id,
        status=TranscriptStatus.COMPLETED.value,
        provider="alignment",
        language="en",
        full_text="one two three",
        word_count=3,
        segment_count=1,
        duration_seconds=3.0,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(transcript)
    db.commit()

    segment = TranscriptSegment(
        transcript_id=transcript.id,
        index=0,
        start_time=0.0,
        end_time=1.6,
        text="one two",
        word_count=2,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(segment)
    db.commit()

    for index, (word, start) in enumerate([("one", 0.0), ("two", 0.6), ("three", 1.6)]):
        db.add(
            TranscriptWord(
                transcript_id=transcript.id,
                segment_id=segment.id if index < 2 else None,
                index=index,
                word=word,
                start_time=start,
                end_time=start + 0.5,
                confidence=0.9,
                speaker="SPEAKER_00",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
    db.commit()

    response = auth_api.get(f"/api/v1/videos/{video_id}/transcript")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == transcript.id
    assert body["status"] == "completed"
    assert [word["word"] for word in body["words"]] == ["one", "two", "three"]
    assert len(body["segments"]) == 1
    assert body["words"][0]["speaker"] == "SPEAKER_00"

    # And a video with no transcript is a clean null, not an error.
    other = auth_api.post("/api/v1/projects", json={"name": "Empty"}).json()
    init = auth_api.post(
        "/api/v1/uploads/init",
        json={
            "filename": "plain.mp4",
            "size_bytes": 4096,
            "content_type": "video/mp4",
            "project_id": other["id"],
        },
    ).json()
    payload = (b"not-a-real-video!" * 300)[:4096]
    for index in range(init["total_chunks"]):
        auth_api.put(f"/api/v1/uploads/{init['upload_id']}/chunk/{index}", content=payload)
    second_video = auth_api.post(f"/api/v1/uploads/{init['upload_id']}/complete").json()["video"]["id"]
    assert auth_api.get(f"/api/v1/videos/{second_video}/transcript").json() is None


def test_clip_detail_route_returns_clip_schema(auth_api, db):
    """The editor's detail endpoint must return actual clip fields, not an error."""
    from app.models import Clip, ClipStatus, Video, utcnow

    project_id, video_id = _project_and_video(auth_api)
    video = db.get(Video, video_id)
    clip = Clip(
        project_id=project_id,
        video_id=video_id,
        user_id=video.user_id,
        title="Clip detail smoke test",
        status=ClipStatus.READY.value,
        start_time=1.0,
        end_time=8.0,
        duration=7.0,
        aspect_ratio="9:16",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(clip)
    db.commit()

    response = auth_api.get(f"/api/v1/clips/{clip.id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == clip.id
    assert body["aspect_ratio"] == "9:16"
    assert body["duration"] == 7.0
    assert isinstance(body["video"], dict)
