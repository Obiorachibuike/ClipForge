"""WebSocket endpoint: /ws/v1/events

Protocol
--------
Client -> server
    {"action": "subscribe", "channels": ["project:<id>"]}
    {"action": "unsubscribe", "channels": [...]}
    {"action": "resync", "project_id": "<id>"}
    {"action": "ping"}

Server -> client
    {"type": "job.progress", "job_id": ..., "payload": {...}}
    {"type": "system.resync", "payload": {"jobs": [...]}}
    {"type": "system.heartbeat"|"system.ready"|"system.error", ...}

Authentication uses the same HTTP-only session cookie as the REST API. A
short-lived ticket (`?ticket=` from /ws-ticket) is also accepted for clients
that cannot send cookies.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from urllib.parse import urlsplit

from app.core.config import settings
from app.core.db import get_session_factory
from app.core.logging import get_logger
from app.core.security import decode_session_token, token_fingerprint
from app.models import Job, Project, User
from app.models import Session as SessionModel
from app.services.events import (
    get_event_bus,
    project_channel,
    user_channel,
)
from app.websocket.manager import manager, resync_snapshot
from app.websocket.tickets import consume_ticket
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select

log = get_logger(__name__)


def _normalise_origin(origin: str) -> str:
    return origin.strip().rstrip("/").lower()


def origin_allowed(origin: str | None, host: str) -> bool:
    """Validate a WebSocket Origin.

    An `endswith` comparison is not an origin check: with
    `https://example.com` configured it also accepted
    `https://evil-example.com`. Only a scheme+host exact match against the
    configured origins (or the opt-in `CORS_ORIGIN_REGEX`), or a match against
    the Host header the upgrade arrived on, is a grant. Requests without an
    Origin header are native clients, not browsers, and keep working — auth is
    still required.
    """
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    # An Origin is a scheme + authority. Anything with a path, query, fragment
    # or embedded credentials is not a browser origin and is refused.
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return False
    if parsed.username or parsed.password:
        return False

    candidate = _normalise_origin(f"{parsed.scheme}://{parsed.netloc}")
    if host and candidate == _normalise_origin(f"{parsed.scheme}://{host}"):
        return True
    allowed = {_normalise_origin(value) for value in settings.cors_origin_list if value}
    if candidate in allowed:
        return True
    if settings.cors_origin_regex:
        try:
            return re.fullmatch(settings.cors_origin_regex, origin) is not None
        except re.error:  # pragma: no cover - configuration error
            log.warning("ws.invalid_cors_origin_regex")
            return False
    return False


router = APIRouter()


def _authenticate(websocket: WebSocket, ticket: str | None) -> tuple[str, str] | None:
    """Return (user_id, session_id) or None."""
    db = get_session_factory()()
    try:
        if ticket:
            payload = consume_ticket(ticket)
            if payload:
                return payload["user_id"], "ticket"
        token = websocket.cookies.get(settings.session_cookie_name)
        if not token:
            return None
        claims = decode_session_token(token)
        session = db.execute(
            select(SessionModel).where(SessionModel.token_hash == token_fingerprint(token))
        ).scalars().first()
        if session is None or session.revoked_at is not None:
            return None
        user = db.get(User, session.user_id)
        if user is None or not user.is_active:
            return None
        if claims.get("sid") != session.id:
            return None
        return user.id, session.id
    except Exception as exc:
        log.info("ws.auth_failed", error=str(exc)[:160])
        return None
    finally:
        db.close()


def _owned_project_ids(user_id: str) -> list[str]:
    db = get_session_factory()()
    try:
        rows = db.execute(select(Project.id).where(Project.user_id == user_id)).scalars()
        return list(rows)
    finally:
        db.close()


def _owned_job_ids(user_id: str, job_ids: set[str]) -> set[str]:
    """Return only the requested job ids that belong to `user_id`."""
    if not job_ids:
        return set()
    db = get_session_factory()()
    try:
        rows = db.execute(
            select(Job.id).where(Job.id.in_(sorted(job_ids)), Job.user_id == user_id)
        ).scalars()
        return set(rows)
    finally:
        db.close()


def _snapshot(user_id: str, project_id: str | None) -> list[dict]:
    db = get_session_factory()()
    try:
        return resync_snapshot(db, user_id, project_id)
    finally:
        db.close()


@router.websocket("/ws/v1/events")
async def events_socket(
    websocket: WebSocket,
    ticket: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
) -> None:
    origin = websocket.headers.get("origin")
    if not origin_allowed(origin, websocket.headers.get("host", "")):
        # Browser origins must be explicitly configured (CORS_ORIGINS /
        # CORS_ORIGIN_REGEX) or match the host the request arrived on.
        log.info("ws.origin_rejected", origin=origin or "")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Origin not allowed")
        return

    identity = _authenticate(websocket, ticket)
    if identity is None:
        await websocket.accept()
        await websocket.send_text(
            json.dumps({"type": "system.error", "payload": {"code": "not_authenticated", "message": "Sign in to receive live updates."}})
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Not authenticated")
        return

    user_id, _session_id = identity
    await websocket.accept()
    channels = {user_channel(user_id)}
    if project_id and project_id in _owned_project_ids(user_id):
        # The query parameter is a subscription: authorise it like one.
        channels.add(project_channel(project_id))
    connection = await manager.register(websocket, user_id, channels)

    bus = get_event_bus()
    subscriber = await bus.subscribe(set(channels))
    heartbeat = asyncio.create_task(manager.start_heartbeat(connection))
    connection.task = heartbeat

    await manager.send(
        connection,
        {
            "type": "system.ready",
            "channel": "system",
            "ts": time.time(),
            "event_id": uuid.uuid4().hex,
            "payload": {"connection_id": connection.id, "channels": sorted(channels)},
        },
    )
    await manager.send(
        connection,
        {
            "type": "system.resync",
            "channel": "system",
            "ts": time.time(),
            "event_id": uuid.uuid4().hex,
            "payload": {"jobs": _snapshot(user_id, project_id)},
        },
    )

    async def pump_events() -> None:
        while True:
            event = await subscriber.queue.get()
            if event.channel in connection.channels:
                await manager.handle_event(connection, event)

    pump = asyncio.create_task(pump_events())
    try:
        while True:
            raw = await websocket.receive_text()
            connection.touch()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                message = None
            if not isinstance(message, dict):
                # Valid JSON that is not an object (a list, a bare string, a
                # number) is still a client bug — report it and keep the socket.
                await manager.send(
                    connection,
                    {
                        "type": "system.error",
                        "payload": {"code": "bad_message", "message": "Messages must be JSON objects."},
                    },
                )
                continue
            action = str(message.get("action") or "")
            if action == "ping":
                await manager.send(connection, {"type": "system.heartbeat", "ts": time.time(), "payload": {"pong": True}})
            elif action == "subscribe":
                requested = {str(c) for c in (message.get("channels") or [])}
                allowed = {user_channel(user_id)}
                owned_projects = set(_owned_project_ids(user_id))
                requested_jobs = {
                    channel.split(":", 1)[1] for channel in requested if channel.startswith("job:")
                }
                owned_jobs = _owned_job_ids(user_id, requested_jobs)
                for channel in requested:
                    if channel.startswith("project:"):
                        if channel.split(":", 1)[1] in owned_projects:
                            allowed.add(channel)
                    elif channel.startswith("job:"):
                        # Job events carry the full job payload (result keys
                        # included), so a job channel is as sensitive as the job.
                        if channel.split(":", 1)[1] in owned_jobs:
                            allowed.add(channel)
                connection.channels |= allowed
                subscriber.channels |= allowed
                await manager.send(
                    connection,
                    {"type": "system.subscribed", "payload": {"channels": sorted(connection.channels)}},
                )
            elif action == "unsubscribe":
                removed = {str(c) for c in (message.get("channels") or [])}
                connection.channels -= removed
                subscriber.channels -= removed
                await manager.send(
                    connection,
                    {"type": "system.subscribed", "payload": {"channels": sorted(connection.channels)}},
                )
            elif action == "resync":
                requested_project = message.get("project_id")
                if requested_project and requested_project not in _owned_project_ids(user_id):
                    requested_project = None
                await manager.send(
                    connection,
                    {
                        "type": "system.resync",
                        "channel": "system",
                        "ts": time.time(),
                        "event_id": uuid.uuid4().hex,
                        "payload": {"jobs": _snapshot(user_id, requested_project)},
                    },
                )
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # pragma: no cover - unexpected socket failure
        log.warning("ws.error", connection_id=connection.id, error=str(exc)[:200])
    finally:
        pump.cancel()
        heartbeat.cancel()
        bus.unsubscribe(subscriber)
        await manager.unregister(connection)


def create_ticket(user: User, ttl_seconds: int = 60) -> dict:
    """Mint a short-lived, single-use ticket for non-cookie clients.

    Registered in `app.main` with the authenticated-user dependency so the
    signature stays transport-agnostic and documentable.
    """
    from app.websocket.tickets import issue_ticket

    token = issue_ticket(user.id, ttl_seconds=max(10, min(300, ttl_seconds)))
    return {"ticket": token, "expires_in": max(10, min(300, ttl_seconds))}
