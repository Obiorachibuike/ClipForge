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
import time
import uuid

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select

from app.core.config import settings
from app.core.db import get_session_factory
from app.core.logging import get_logger
from app.core.security import decode_session_token, token_fingerprint
from app.models import Project, Session as SessionModel, User
from app.services.events import get_event_bus, job_channel, project_channel, user_channel
from app.websocket.manager import manager, resync_snapshot
from app.websocket.tickets import consume_ticket

log = get_logger(__name__)
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
    if origin and settings.cors_origin_list and origin not in settings.cors_origin_list:
        # Same-origin previews and custom hosts are permitted via CORS_ORIGINS.
        allowed = any(origin.endswith(url.split("//")[-1]) for url in settings.cors_origin_list)
        if not allowed:
            log.info("ws.origin_rejected", origin=origin)
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
    if project_id:
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
                owned = set(_owned_project_ids(user_id))
                for channel in requested:
                    if channel.startswith("project:"):
                        if channel.split(":", 1)[1] in owned:
                            allowed.add(channel)
                    elif channel.startswith("job:"):
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


def create_ticket(user: "User", ttl_seconds: int = 60) -> dict:
    """Mint a short-lived, single-use ticket for non-cookie clients.

    Registered in `app.main` with the authenticated-user dependency so the
    signature stays transport-agnostic and documentable.
    """
    from app.websocket.tickets import issue_ticket

    token = issue_ticket(user.id, ttl_seconds=max(10, min(300, ttl_seconds)))
    return {"ticket": token, "expires_in": max(10, min(300, ttl_seconds))}
