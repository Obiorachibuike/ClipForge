"""Project, upload and job lifecycle behaviour."""
from __future__ import annotations

# Above the server's minimum-upload guard, small enough to keep tests fast.
CHUNK_SIZE = 4096


def test_project_crud_and_ownership(auth_api, second_api):
    created = auth_api.post("/api/v1/projects", json={"name": "Launch video"}).json()
    assert created["name"] == "Launch video"
    assert created["status"]
    project_id = created["id"]

    listing = auth_api.get("/api/v1/projects").json()
    assert [item["id"] for item in listing["items"]] == [project_id]
    assert listing["total"] == 1

    # A different account must not be able to reach it, and must not be told it exists.
    assert second_api.get(f"/api/v1/projects/{project_id}").status_code == 404
    assert second_api.get(f"/api/v1/projects/{project_id}/videos").status_code == 404
    assert second_api.patch(f"/api/v1/projects/{project_id}", json={"name": "Stolen"}).status_code == 404
    assert second_api.delete(f"/api/v1/projects/{project_id}").status_code == 404
    assert second_api.get("/api/v1/projects").json()["total"] == 0

    updated = auth_api.patch(f"/api/v1/projects/{project_id}", json={"name": "Launch video (v2)"}).json()
    assert updated["name"] == "Launch video (v2)"

    assert auth_api.delete(f"/api/v1/projects/{project_id}").status_code in (200, 204)
    assert auth_api.get(f"/api/v1/projects/{project_id}").status_code == 404


def test_project_without_a_name_is_rejected(auth_api):
    assert auth_api.post("/api/v1/projects", json={"name": ""}).status_code == 422
    assert auth_api.post("/api/v1/projects", json={}).status_code == 422


def test_upload_rejects_bad_extension_and_mime(auth_api):
    project_id = auth_api.post("/api/v1/projects", json={"name": "Guards"}).json()["id"]

    bad_extension = auth_api.post(
        "/api/v1/uploads/init",
        json={
            "filename": "payload.sh",
            "size_bytes": 1024,
            "content_type": "video/mp4",
            "project_id": project_id,
        },
    )
    assert bad_extension.status_code == 422
    assert bad_extension.json()["code"]

    bad_mime = auth_api.post(
        "/api/v1/uploads/init",
        json={
            "filename": "clip.mp4",
            "size_bytes": 1024,
            "content_type": "application/octet-stream",
            "project_id": project_id,
        },
    )
    assert bad_mime.status_code in (415, 422)

    too_big = auth_api.post(
        "/api/v1/uploads/init",
        json={
            "filename": "huge.mp4",
            "size_bytes": 50 * 1024 * 1024 * 1024,
            "content_type": "video/mp4",
            "project_id": project_id,
        },
    )
    assert too_big.status_code in (402, 413, 422)


def test_traversal_in_filename_is_neutralised(auth_api):
    project_id = auth_api.post("/api/v1/projects", json={"name": "Paths"}).json()["id"]
    response = auth_api.post(
        "/api/v1/uploads/init",
        json={
            "filename": "../../../../etc/passwd.mp4",
            "size_bytes": 2048,

            "content_type": "video/mp4",
            "project_id": project_id,
        },
    )
    # Either rejected outright, or stored under a sanitised name — never the raw path.
    assert response.status_code in (200, 201, 422)
    if response.status_code in (200, 201):
        body = response.json()
        assert ".." not in body.get("upload_id", "")
        status = auth_api.get(f"/api/v1/uploads/{body['upload_id']}").json()
        assert "/" not in status.get("filename", "").replace("\\", "")


def test_chunked_upload_lifecycle(auth_api):
    project_id = auth_api.post("/api/v1/projects", json={"name": "Uploads"}).json()["id"]
    init = auth_api.post(
        "/api/v1/uploads/init",
        json={
            "filename": "take.mp4",
            "size_bytes": CHUNK_SIZE,
            "content_type": "video/mp4",
            "project_id": project_id,
        },
    )
    assert init.status_code in (200, 201), init.text
    session = init.json()
    assert session["total_chunks"] >= 1
    assert session["received_chunks"] == []  # a list of chunk indexes, not a count
    upload_id = session["upload_id"]

    status = auth_api.get(f"/api/v1/uploads/{upload_id}").json()
    assert status["received_chunks"] == []

    payload = b"not-a-real-video!" * (CHUNK_SIZE // 17 + 1)
    payload = payload[:CHUNK_SIZE]
    received = 0
    for index in range(session["total_chunks"]):
        chunk = auth_api.put(
            f"/api/v1/uploads/{upload_id}/chunk/{index}",
            content=payload,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert chunk.status_code in (200, 201, 204), chunk.text
        received += 1
    status = auth_api.get(f"/api/v1/uploads/{upload_id}").json()
    assert len(status["received_chunks"]) == received
    assert status["progress"] > 0

    complete = auth_api.post(f"/api/v1/uploads/{upload_id}/complete")
    if complete.status_code in (200, 201):
        body = complete.json()
        assert body["video"]["id"]
        assert body["job"]["type"]
        # Never processed inline: the request returns a queued job, not a result.
        assert body["job"]["status"] in {"queued", "running"}
        assert body["video"]["status"] in {"uploaded", "uploading", "processing", "ready"}
    else:
        # Rejecting non-media on completion is also correct behaviour.
        assert complete.status_code in (415, 422)


def test_upload_cannot_be_completed_by_another_account(auth_api, second_api):
    project_id = auth_api.post("/api/v1/projects", json={"name": "Private upload"}).json()["id"]
    upload_id = auth_api.post(
        "/api/v1/uploads/init",
        json={
            "filename": "mine.mp4",
            "size_bytes": CHUNK_SIZE,
            "content_type": "video/mp4",
            "project_id": project_id,
        },
    ).json()["upload_id"]

    assert second_api.get(f"/api/v1/uploads/{upload_id}").status_code == 404
    assert second_api.delete(f"/api/v1/uploads/{upload_id}").status_code == 404


def test_remote_video_import_validates_platform_and_queues_work(auth_api):
    project_id = auth_api.post("/api/v1/projects", json={"name": "Remote source"}).json()["id"]

    rejected = auth_api.post(
        f"/api/v1/projects/{project_id}/videos/import-url",
        json={"url": "http://127.0.0.1/private-video.mp4"},
    )
    assert rejected.status_code == 422

    queued = auth_api.post(
        f"/api/v1/projects/{project_id}/videos/import-url",
        json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
    )
    assert queued.status_code == 202, queued.text
    body = queued.json()
    assert body["type"] == "video.import_url"
    assert body["project_id"] == project_id
    assert body["status"] == "queued"


def test_job_endpoints_are_owned_and_never_synchronous(auth_api, second_api):
    jobs = auth_api.get("/api/v1/jobs").json()
    assert "items" in jobs and "total" in jobs

    project_id = auth_api.post("/api/v1/projects", json={"name": "Jobs"}).json()["id"]
    discovered = auth_api.post(f"/api/v1/projects/{project_id}/discover", json={})
    # Discovering moments on a project with no transcript must fail cleanly,
    # never with an unhandled 500.
    assert discovered.status_code in (400, 404, 409, 422, 429)
    if discovered.status_code == 429:
        return
    assert discovered.json()["code"]
    assert "Traceback" not in discovered.text

    for job in auth_api.get("/api/v1/jobs").json()["items"]:
        assert second_api.get(f"/api/v1/jobs/{job['id']}").status_code == 404


def test_health_and_readiness_endpoints(client):
    assert client.get("/healthz").status_code == 200
    ready = client.get("/readyz")
    assert ready.status_code in (200, 503)


def test_openapi_document_is_served(client):
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "ClipForge API"
    paths = schema["paths"]
    for required in (
        "/api/v1/auth/login",
        "/api/v1/projects",
        "/api/v1/videos/{video_id}",
        "/api/v1/transcripts/{transcript_id}",
        "/api/v1/clips/{clip_id}",
        "/api/v1/renders/{render_id}",
        "/api/v1/exports/{export_id}",
        "/api/v1/settings",
    ):
        assert required in paths, f"{required} is missing from the OpenAPI document"


def test_error_responses_are_structured(client):
    response = client.get("/api/v1/projects")
    body = response.json()
    assert set(body) >= {"code", "message", "request_id"}
    assert response.headers.get("x-request-id") or body["request_id"]


def test_billing_plans_are_public_but_the_current_marker_needs_a_session(anon_api, auth_api):
    """Pricing must render on the landing page for signed-out visitors."""
    anonymous = anon_api.get("/api/v1/billing/plans")
    assert anonymous.status_code == 200, anonymous.text
    body = anonymous.json()
    keys = [plan["key"] for plan in body["plans"]]
    assert {"free", "pro", "lifetime"} <= set(keys)
    assert all(plan["current"] is False for plan in body["plans"]), "nobody is signed in"

    signed_in = auth_api.get("/api/v1/billing/plans").json()
    free = next(plan for plan in signed_in["plans"] if plan["key"] == "free")
    assert free["current"] is True
    for plan in signed_in["plans"]:
        assert plan["features"] and plan["tagline"]
        assert plan["limits"]["max_upload_bytes"] > 0
