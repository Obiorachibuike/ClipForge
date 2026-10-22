"""Every user-owned resource must be unreachable by another account.

The API returns 404 (not 403) for foreign ids so object ids cannot be probed for
existence. These are backend checks; hiding a button in the UI proves nothing.
"""
from __future__ import annotations

from app.models import (
    CandidateStatus,
    Clip,
    ClipCandidate,
    ClipStatus,
    Export,
    Project,
    RenderJob,
    RenderStatus,
    Video,
    VideoStatus,
    utcnow,
)


def _owner_resources(auth_api, db) -> dict[str, str]:
    user_id = auth_api.user["id"]
    project = Project(user_id=user_id, name="Owned", status="ready")
    db.add(project)
    db.flush()

    video = Video(
        project_id=project.id,
        user_id=user_id,
        original_filename="source.mp4",
        storage_key=f"projects/{project.id}/videos/{project.id}/source/source.mp4",
        status=VideoStatus.READY.value,
        duration_seconds=120.0,
        has_audio=True,
    )
    db.add(video)
    db.flush()

    candidate = ClipCandidate(
        project_id=project.id,
        video_id=video.id,
        start_time=10.0,
        end_time=40.0,
        duration=30.0,
        title="A real moment",
        reason="Measured hook and payoff.",
        score=77,
        status=CandidateStatus.PENDING.value,
    )
    db.add(candidate)
    db.flush()

    clip = Clip(
        project_id=project.id,
        video_id=video.id,
        candidate_id=candidate.id,
        user_id=user_id,
        title="Owned clip",
        start_time=10.0,
        end_time=40.0,
        duration=30.0,
        status=ClipStatus.DRAFT.value,
    )
    db.add(clip)
    db.flush()

    render = RenderJob(
        clip_id=clip.id,
        project_id=project.id,
        user_id=user_id,
        preset="tiktok",
        aspect_ratio="9:16",
        width=1080,
        height=1920,
        fps=30,
        status=RenderStatus.SUCCEEDED.value,
        storage_key=f"projects/{project.id}/clips/{clip.id}/renders/{clip.id}.mp4",
        output_size_bytes=1024,
        output_duration=30.0,
    )
    db.add(render)
    db.flush()

    export = Export(
        render_id=render.id,
        clip_id=clip.id,
        project_id=project.id,
        user_id=user_id,
        platform="tiktok",
        filename="owned.mp4",
        storage_key=render.storage_key,
        size_bytes=1024,
        duration_seconds=30.0,
        width=1080,
        height=1920,
        created_at=utcnow(),
    )
    db.add(export)
    db.commit()

    return {
        "project": project.id,
        "video": video.id,
        "candidate": candidate.id,
        "clip": clip.id,
        "render": render.id,
        "export": export.id,
    }


def test_a_second_account_cannot_read_or_mutate_any_owned_resource(auth_api, second_api, db):
    ids = _owner_resources(auth_api, db)

    # The owner can read each one.
    assert auth_api.get(f"/api/v1/candidates/{ids['candidate']}").status_code == 200
    assert auth_api.get(f"/api/v1/clips/{ids['clip']}").status_code == 200
    assert auth_api.get(f"/api/v1/renders/{ids['render']}").status_code == 200
    assert auth_api.get(f"/api/v1/projects/{ids['project']}/candidates").status_code == 200

    forbidden: list[str] = [
        f"/api/v1/candidates/{ids['candidate']}",
        f"/api/v1/candidates/{ids['candidate']}/score",
        f"/api/v1/clips/{ids['clip']}",
        f"/api/v1/clips/{ids['clip']}/renders",
        f"/api/v1/clips/{ids['clip']}/captions",
        f"/api/v1/renders/{ids['render']}",
        f"/api/v1/exports/{ids['export']}/thumbnail",
        f"/api/v1/videos/{ids['video']}",
        f"/api/v1/videos/{ids['video']}/transcript",
        f"/api/v1/videos/{ids['video']}/narration-script",
        f"/api/v1/videos/{ids['video']}/stream",
        f"/api/v1/videos/{ids['video']}/signed-url",
        f"/api/v1/projects/{ids['project']}/candidates",
        f"/api/v1/projects/{ids['project']}/videos",
        f"/api/v1/projects/{ids['project']}/renders",
        f"/api/v1/projects/{ids['project']}/exports",
        f"/api/v1/files/projects/{ids['project']}/videos/{ids['video']}/source/source.mp4",
    ]
    for path in forbidden:
        response = second_api.get(path)
        assert response.status_code == 404, f"{path} leaked to another account ({response.status_code})"
        assert "Traceback" not in response.text

    mutations = [
        ("patch", f"/api/v1/candidates/{ids['candidate']}", {"status": "kept"}),
        ("post", f"/api/v1/candidates/{ids['candidate']}/clip", {}),
        ("patch", f"/api/v1/clips/{ids['clip']}", {"title": "stolen"}),
        ("post", f"/api/v1/clips/{ids['clip']}/duplicate", {}),
        ("post", f"/api/v1/clips/{ids['clip']}/render", {}),
        ("post", f"/api/v1/renders/{ids['render']}/cancel", {}),
        ("post", f"/api/v1/renders/{ids['render']}/retry", {}),
        ("delete", f"/api/v1/renders/{ids['render']}", None),
        ("delete", f"/api/v1/exports/{ids['export']}", None),
        ("delete", f"/api/v1/videos/{ids['video']}", None),
    ]
    for method, path, body in mutations:
        request = getattr(second_api, method)
        response = request(path, json=body) if body is not None else request(path)
        assert response.status_code == 404, f"{method.upper()} {path} was not owner-scoped ({response.status_code})"

    db.expire_all()
    from sqlalchemy import select

    assert db.get(Clip, ids["clip"]).title == "Owned clip"
    assert db.get(Export, ids["export"]) is not None
    assert db.execute(select(RenderJob).where(RenderJob.id == ids["render"])).scalars().first() is not None


def test_a_foreign_download_route_is_404_before_any_storage_access(second_api, auth_api, db):
    ids = _owner_resources(auth_api, db)
    assert second_api.get(f"/api/v1/renders/{ids['render']}/download").status_code == 404
    assert second_api.get(f"/api/v1/exports/{ids['export']}/download").status_code == 404
