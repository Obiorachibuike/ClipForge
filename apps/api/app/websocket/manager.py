"""WebSocket connection management.

Connections authenticate with the session cookie (or a short-lived ticket), and
subscribe to a set of channels. The manager owns heartbeats and cleanup; the
event bus owns fan-out. On every (re)connect the server sends a
`system.resync` snapshot so a client that was offline - or a browser that was
refreshed mid-render - converges to the true job state immediately.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field

from fastapi import WebSocket
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.services.events import Event, get_event_bus, job_channel, project_channel, user_channel
from app.services.job_service import JobService

log = get_logger(__name__)

MAX_CONNECTIONS_PER_USER = 8
HEARTBEAT_INTERVAL = 20.0
IDLE_TIMEOUT = 120.0


@dataclass
class Connection:
    id: str
    websocket: WebSocket
    user_id: str
    channels: set[str]
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    sent: int = 0
    task: asyncio.Task | None = None

    def touch(self) -> None:
        self.last_seen = time.time()


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, Connection] = {}
        self._lock = asyncio.Lock()

    async def register(self, websocket: WebSocket, user_id: str, channels: set[str]) -> Connection:
        async with self._lock:
            mine = [c for c in self._connections.values() if c.user_id == user_id]
            if len(mine) >= MAX_CONNECTIONS_PER_USER:
                oldest = min(mine, key=lambda c: c.created_at)
                await self._close(oldest, code=1008, reason="Too many concurrent connections")
            connection = Connection(id=uuid.uuid4().hex, websocket=websocket, user_id=user_id, channels=channels)
            self._connections[connection.id] = connection
        log.info("ws.connected", connection_id=connection.id, user_id=user_id, channels=sorted(channels))
        return connection

    async def unregister(self, connection: Connection) -> None:
        async with self._lock:
            self._connections.pop(connection.id, None)
        if connection.task and not connection.task.done():
            connection.task.cancel()
        log.info("ws.disconnected", connection_id=connection.id, user_id=connection.user_id, sent=connection.sent)

    async def _close(self, connection: Connection, code: int = 1000, reason: str = "") -> None:
        try:
            await connection.websocket.close(code=code, reason=reason)
        except Exception:
            pass
        self._connections.pop(connection.id, None)

    async def send(self, connection: Connection, payload: dict) -> None:
        try:
            await connection.websocket.send_text(json.dumps(payload, default=str))
            connection.sent += 1
        except Exception as exc:
            log.info("ws.send_failed", connection_id=connection.id, error=str(exc)[:120])
            await self.unregister(connection)

    async def broadcast(self, channels: set[str], payload: dict) -> int:
        targets = [c for c in self._connections.values() if c.channels & channels]
        for connection in targets:
            await self.send(connection, payload)
        return len(targets)

    async def handle_event(self, connection: Connection, event: Event) -> None:
        connection.touch()
        await self.send(connection, event.to_dict())

    def stats(self) -> dict:
        return {
            "connections": len(self._connections),
            "users": len({c.user_id for c in self._connections.values()}),
            "channels": sorted({channel for c in self._connections.values() for channel in c.channels}),
        }

    def channels_for(self, user_id: str, project_ids: list[str] | None = None) -> set[str]:
        channels = {user_channel(user_id)}
        for project_id in project_ids or []:
            channels.add(project_channel(project_id))
        return channels

    async def start_heartbeat(self, connection: Connection) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if time.time() - connection.last_seen > IDLE_TIMEOUT:
                await self._close(connection, code=1001, reason="Idle timeout")
                return
            await self.send(
                connection,
                {"type": "system.heartbeat", "ts": time.time(), "channel": "system", "payload": {}},
            )


def resync_snapshot(db: Session, user_id: str, project_id: str | None = None) -> list[dict]:
    """Full current state of everything in flight for a user."""
    events: list[dict] = []
    jobs = JobService.active_for_user(db, user_id, project_id)
    for job in jobs:
        events.append(
            {
                "type": "job.progress" if job.status == "running" else "job.created",
                "channel": job_channel(job.id),
                "job_id": job.id,
                "project_id": job.project_id,
                "user_id": job.user_id,
                "payload": JobService.serialize(job),
                "ts": time.time(),
                "event_id": uuid.uuid4().hex,
            }
        )
    return events


manager = ConnectionManager()
