"""WebSocket authentication, the client protocol, and push delivery.

The socket always opens with a two-frame preamble — `system.ready` then an
initial `system.resync` carrying the caller's current job snapshot — so tests
use `open_socket()` to consume that preamble and then assert on what follows.
"""
from __future__ import annotations

import json
from contextlib import contextmanager

import pytest


def _ticket(api) -> str:
    response = api.post("/api/v1/ws-ticket")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ticket"]
    assert body["expires_in"] > 0
    return body["ticket"]


@contextmanager
def open_socket(api, **query):
    """Connect and consume the ready + initial resync preamble."""
    ticket = _ticket(api)
    suffix = "".join(f"&{key}={value}" for key, value in query.items())
    with api.client.websocket_connect(f"/ws/v1/events?ticket={ticket}{suffix}") as socket:
        ready = socket.receive_json()
        assert ready["type"] == "system.ready"
        assert ready["payload"]["connection_id"]
        snapshot = socket.receive_json()
        assert snapshot["type"] == "system.resync"
        assert "jobs" in snapshot["payload"]
        yield socket


def test_ticket_requires_authentication(client):
    assert client.post("/api/v1/ws-ticket").status_code == 401


def test_ticket_is_single_use(auth_api, ticket_api):
    """A ticket authenticates exactly once.

    `ticket_api` deliberately carries no session cookie, otherwise the second
    connect would succeed via the cookie and the test would prove nothing.
    """
    ticket = _ticket(auth_api)

    with ticket_api.client.websocket_connect(f"/ws/v1/events?ticket={ticket}") as socket:
        assert socket.receive_json()["type"] == "system.ready"

    with ticket_api.client.websocket_connect(f"/ws/v1/events?ticket={ticket}") as socket:
        frame = socket.receive_json()
        assert frame["type"] == "system.error"
        assert frame["payload"]["code"] == "not_authenticated"


def test_connection_without_credentials_is_refused_not_silently_accepted(client):
    with client.websocket_connect("/ws/v1/events") as socket:
        frame = socket.receive_json()
        assert frame["type"] == "system.error"
        assert frame["payload"]["code"] == "not_authenticated"


def test_cookie_session_alone_can_connect(auth_api):
    """The browser path: a plain session cookie and no ticket."""
    from app.core.config import settings

    assert auth_api.client.cookies.get(settings.session_cookie_name)
    with auth_api.client.websocket_connect("/ws/v1/events") as socket:
        assert socket.receive_json()["type"] == "system.ready"


def test_ping_and_subscribe_protocol(auth_api):
    project_id = auth_api.post("/api/v1/projects", json={"name": "Realtime"}).json()["id"]

    with open_socket(auth_api) as socket:
        socket.send_json({"action": "ping"})
        heartbeat = socket.receive_json()
        assert heartbeat["type"] == "system.heartbeat"
        assert heartbeat["payload"]["pong"] is True

        socket.send_json({"action": "subscribe", "channels": [f"project:{project_id}"]})
        subscribed = socket.receive_json()
        assert subscribed["type"] == "system.subscribed"
        assert f"project:{project_id}" in subscribed["payload"]["channels"]

        socket.send_json({"action": "resync", "project_id": project_id})
        resync = socket.receive_json()
        assert resync["type"] == "system.resync"
        assert isinstance(resync["payload"]["jobs"], list)

        socket.send_json({"action": "unsubscribe", "channels": [f"project:{project_id}"]})
        removed = socket.receive_json()
        assert removed["type"] == "system.subscribed"
        assert f"project:{project_id}" not in removed["payload"]["channels"]


def test_subscribing_to_someone_elses_project_is_refused(auth_api, second_api):
    foreign_project = auth_api.post("/api/v1/projects", json={"name": "Not yours"}).json()["id"]

    with open_socket(second_api) as socket:
        socket.send_json({"action": "subscribe", "channels": [f"project:{foreign_project}"]})
        response = socket.receive_json()
        assert response["type"] == "system.subscribed"
        # Silently dropped from the channel set rather than leaking the project.
        assert f"project:{foreign_project}" not in response["payload"]["channels"]

        # And a resync for that project returns nothing rather than its data.
        socket.send_json({"action": "resync", "project_id": foreign_project})
        assert socket.receive_json()["payload"]["jobs"] == []


def test_job_events_reach_the_owning_subscriber(auth_api):
    from app.services import events as ev

    project_id = auth_api.post("/api/v1/projects", json={"name": "Events"}).json()["id"]
    user_id = auth_api.user["id"]

    with open_socket(auth_api, project_id=project_id) as socket:
        ev.get_event_bus().emit(
            "job.progress",
            user_id=user_id,
            project_id=project_id,
            job_id="test-job",
            payload={"progress": 42.0, "stage": "transcoding"},
        )
        event = socket.receive_json()
        assert event["type"] == "job.progress"
        assert event["job_id"] == "test-job"
        assert event["payload"]["progress"] == 42.0


def test_events_are_not_delivered_to_other_users(auth_api, second_api):
    from app.services import events as ev

    project_id = auth_api.post("/api/v1/projects", json={"name": "Isolated"}).json()["id"]

    with open_socket(second_api) as other_socket:
        ev.get_event_bus().emit(
            "job.progress",
            user_id=auth_api.user["id"],
            project_id=project_id,
            job_id="not-theirs",
            payload={"progress": 10.0},
        )
        # If the event had leaked, it would be queued ahead of this heartbeat.
        other_socket.send_json({"action": "ping"})
        frame = other_socket.receive_json()
        assert frame["type"] == "system.heartbeat"


def test_unknown_action_keeps_the_socket_alive(auth_api):
    """An unrecognised action is ignored; the connection stays usable."""
    with open_socket(auth_api) as socket:
        socket.send_json({"action": "do-something-unsupported"})
        socket.send_json({"action": "ping"})
        assert socket.receive_json()["type"] == "system.heartbeat"


def test_malformed_frame_does_not_kill_the_connection(auth_api):
    with open_socket(auth_api) as socket:
        socket.send_text("{not json at all")
        response = socket.receive_json()
        assert response["type"] == "system.error"
        assert response["payload"]["code"] == "bad_message"

        socket.send_json({"action": "ping"})
        assert socket.receive_json()["type"] == "system.heartbeat"


def test_disconnect_cleans_up_the_connection(auth_api):
    from app.websocket.manager import manager

    with open_socket(auth_api) as socket:
        socket.send_json({"action": "ping"})
        socket.receive_json()
        during = manager.stats()["connections"]

    assert during >= 1
    assert manager.stats()["connections"] == during - 1


def test_non_object_json_is_rejected_without_dropping_the_socket(auth_api):
    with open_socket(auth_api) as socket:
        socket.send_text(json.dumps([1, 2, 3]))
        error = socket.receive_json()
        assert error["type"] == "system.error"
        assert error["payload"]["code"] == "bad_message"

        socket.send_json({"action": "ping"})
        assert socket.receive_json()["type"] == "system.heartbeat"


@pytest.mark.parametrize("query", [{}, {"project_id": "does-not-exist"}])
def test_resync_snapshot_is_scoped_to_owned_projects(auth_api, query):
    """A resync for a project the caller does not own returns an empty snapshot."""
    with open_socket(auth_api, **query) as socket:
        socket.send_json({"action": "resync", "project_id": "not-a-real-project"})
        payload = socket.receive_json()
        assert payload["type"] == "system.resync"
        assert payload["payload"]["jobs"] == []


def test_connect_query_cannot_subscribe_to_a_foreign_project(auth_api, second_api):
    """The `?project_id=` parameter is a subscription request and must be authorised.

    Regression: the channel was added before any ownership check, so a caller who
    knew another user's project id received that project's live events.
    """
    from app.services import events as ev

    foreign_project = auth_api.post("/api/v1/projects", json={"name": "Private"}).json()["id"]

    response = second_api.post("/api/v1/ws-ticket")
    assert response.status_code == 200, response.text
    ticket = response.json()["ticket"]
    with second_api.client.websocket_connect(
        f"/ws/v1/events?ticket={ticket}&project_id={foreign_project}"
    ) as socket:
        ready = socket.receive_json()
        assert ready["type"] == "system.ready"
        assert f"project:{foreign_project}" not in ready["payload"]["channels"]
        socket.receive_json()  # initial resync

        ev.get_event_bus().emit(
            "candidate.created",
            user_id=auth_api.user["id"],
            project_id=foreign_project,
            payload={"title": "leaked"},
        )
        socket.send_json({"action": "ping"})
        assert socket.receive_json()["type"] == "system.heartbeat"


def test_subscribing_to_another_users_job_channel_is_refused(auth_api, second_api, db):
    """Job channels carry every job payload; they need the same ownership rule."""
    from app.models import JobType
    from app.services import events as ev
    from app.services.job_service import JobService

    foreign_job = JobService.create(
        db,
        type_=JobType.VIDEO_PROBE.value,
        payload={"video_id": "x"},
        user_id=auth_api.user["id"],
        enqueue=False,
    )

    with open_socket(second_api) as socket:
        socket.send_json({"action": "subscribe", "channels": [f"job:{foreign_job.id}"]})
        response = socket.receive_json()
        assert response["type"] == "system.subscribed"
        assert f"job:{foreign_job.id}" not in response["payload"]["channels"]

        ev.get_event_bus().emit(
            "job.completed",
            user_id=auth_api.user["id"],
            job_id=foreign_job.id,
            payload={"result": {"storage_key": "secret"}},
        )
        socket.send_json({"action": "ping"})
        assert socket.receive_json()["type"] == "system.heartbeat"


def _first_frame_with_origin(api, origin: str) -> dict:
    response = api.post("/api/v1/ws-ticket")
    assert response.status_code == 200, response.text
    ticket = response.json()["ticket"]
    with api.client.websocket_connect(
        f"/ws/v1/events?ticket={ticket}", headers={"origin": origin}
    ) as socket:
        return socket.receive_json()


def test_websocket_accepts_a_configured_origin(auth_api):
    frame = _first_frame_with_origin(auth_api, "http://localhost:5173")
    assert frame["type"] == "system.ready"


def test_websocket_rejects_an_origin_that_only_shares_a_suffix(auth_api):
    """Regression: `origin.endswith(host)` admitted look-alike origins."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        _first_frame_with_origin(auth_api, "http://evil-localhost:5173")


def test_websocket_origin_regex_is_opt_in(auth_api, monkeypatch):
    from app.core.config import settings
    from starlette.websockets import WebSocketDisconnect

    origin = "https://1234-preview.e2b.app"
    with pytest.raises(WebSocketDisconnect):
        _first_frame_with_origin(auth_api, origin)

    monkeypatch.setattr(settings, "cors_origin_regex", r"https://.*\.e2b\.app")
    assert _first_frame_with_origin(auth_api, origin)["type"] == "system.ready"
