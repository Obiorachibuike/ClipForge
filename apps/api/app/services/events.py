"""Event bus for live processing updates.

Publishers (API request handlers and worker threads) call `publish()`;
subscribers (WebSocket connections, which are async) receive events through an
asyncio queue. With Redis configured the bus is multi-process, so workers in
separate containers reach browser sockets attached to any API replica. Without
Redis it degrades to a correct in-process bus.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.core.redis_client import get_redis

log = get_logger(__name__)

REDIS_CHANNEL_PREFIX = "clipforge:events:"

# Canonical event names (kept in sync with packages/shared-types and the web app)
EVENT_JOB_CREATED = "job.created"
EVENT_JOB_STARTED = "job.started"
EVENT_JOB_PROGRESS = "job.progress"
EVENT_JOB_COMPLETED = "job.completed"
EVENT_JOB_FAILED = "job.failed"
EVENT_JOB_CANCELLED = "job.cancelled"
EVENT_JOB_RETRYING = "job.retrying"
EVENT_CLIP_CREATED = "clip.created"
EVENT_CANDIDATE_CREATED = "candidate.created"
EVENT_CLIP_UPDATED = "clip.updated"
EVENT_CANDIDATES_READY = "candidates.ready"
EVENT_TRANSCRIPT_READY = "transcript.ready"
EVENT_VIDEO_UPDATED = "video.updated"
EVENT_UPLOAD_PROGRESS = "upload.progress"
EVENT_RENDER_STARTED = "render.started"
EVENT_RENDER_PROGRESS = "render.progress"
EVENT_RENDER_COMPLETED = "render.completed"
EVENT_RENDER_FAILED = "render.failed"
EVENT_RENDER_CANCELLED = "render.cancelled"
EVENT_EXPORT_READY = "export.ready"
EVENT_USAGE_UPDATED = "usage.updated"
EVENT_SYSTEM_NOTICE = "system.notice"


@dataclass
class Event:
    type: str
    channel: str
    payload: dict[str, Any] = field(default_factory=dict)
    user_id: str | None = None
    project_id: str | None = None
    job_id: str | None = None
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), default=str)

    @staticmethod
    def from_json(raw: str | bytes) -> Event:
        data = json.loads(raw)
        return Event(**data)


def user_channel(user_id: str) -> str:
    return f"user:{user_id}"


def project_channel(project_id: str) -> str:
    return f"project:{project_id}"


def job_channel(job_id: str) -> str:
    return f"job:{job_id}"


class _Subscriber:
    __slots__ = ("channels", "queue", "loop", "_seen", "_seen_order", "_seen_lock")

    def __init__(self, channels: set[str], loop: asyncio.AbstractEventLoop, maxsize: int = 500) -> None:
        self.channels = channels
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self.loop = loop
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._seen_lock = threading.Lock()

    def matches(self, event: Event) -> bool:
        return event.channel in self.channels

    def should_deliver(self, event: Event) -> bool:
        """A multi-channel event is delivered once per socket, not once/channel."""
        with self._seen_lock:
            if event.event_id in self._seen:
                return False
            self._seen.add(event.event_id)
            self._seen_order.append(event.event_id)
            while len(self._seen_order) > 1_000:
                self._seen.discard(self._seen_order.popleft())
        return True

    def deliver(self, event: Event) -> None:
        """Thread-safe delivery into the subscriber's asyncio queue."""
        try:
            self.loop.call_soon_threadsafe(self._put, event)
        except RuntimeError:  # loop closed
            return

    def _put(self, event: Event) -> None:
        if self.queue.full():
            try:
                self.queue.get_nowait()  # drop oldest rather than stall publishers
            except asyncio.QueueEmpty:  # pragma: no cover
                pass
        self.queue.put_nowait(event)


class EventBus:
    """Hybrid bus: redis pub/sub when available, always mirrored in-process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[_Subscriber] = []
        self._redis_thread: threading.Thread | None = None
        self._redis_stop = threading.Event()
        self._redis_pubsub = None
        self._recent: deque[Event] = deque(maxlen=200)
        self._seen_event_channels: set[tuple[str, str]] = set()
        self._seen_event_order: deque[tuple[str, str]] = deque()

    # ------------------------------------------------------------ publish ---
    def publish(self, event: Event) -> None:
        self._dispatch(event, relay_to_redis=True)

    def _dispatch(self, event: Event, *, relay_to_redis: bool) -> None:
        if not self._remember_once(event):
            return
        self._recent.append(event)
        with self._lock:
            subscribers = list(self._subscribers)
        for sub in subscribers:
            if sub.matches(event) and sub.should_deliver(event):
                sub.deliver(event)
        if relay_to_redis:
            client = get_redis()
            if client is not None:
                try:
                    client.publish(f"{REDIS_CHANNEL_PREFIX}{event.channel}", event.to_json())
                except Exception as exc:  # pragma: no cover - redis hiccup
                    log.warning("events.publish_failed", error=str(exc))

    def _remember_once(self, event: Event) -> bool:
        key = (event.event_id, event.channel)
        with self._lock:
            if key in self._seen_event_channels:
                return False
            self._seen_event_channels.add(key)
            self._seen_event_order.append(key)
            while len(self._seen_event_order) > 2_000:
                self._seen_event_channels.discard(self._seen_event_order.popleft())
        return True

    def emit(
        self,
        type_: str,
        *,
        user_id: str | None = None,
        project_id: str | None = None,
        job_id: str | None = None,
        payload: dict[str, Any] | None = None,
        channels: list[str] | None = None,
    ) -> Event:
        """Publish to the user channel and any extra channels in one call."""
        targets = set(channels or [])
        if user_id:
            targets.add(user_channel(user_id))
        if project_id:
            targets.add(project_channel(project_id))
        if job_id:
            targets.add(job_channel(job_id))
        if not targets:
            targets.add("system")
        event = Event(
            type=type_,
            channel=next(iter(sorted(targets))),
            payload=payload or {},
            user_id=user_id,
            project_id=project_id,
            job_id=job_id,
        )
        for channel in sorted(targets):
            delivered = Event(
                type=event.type,
                channel=channel,
                payload=event.payload,
                user_id=event.user_id,
                project_id=event.project_id,
                job_id=event.job_id,
                event_id=event.event_id,
                ts=event.ts,
            )
            self.publish(delivered)
        return event

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return [e.to_dict() for e in list(self._recent)[-limit:]]

    # --------------------------------------------------------- subscribe ---
    async def subscribe(self, channels: set[str]) -> _Subscriber:
        sub = _Subscriber(channels, asyncio.get_running_loop())
        with self._lock:
            self._subscribers.append(sub)
        self._ensure_redis_bridge()
        # Replay nothing: the client resynchronises state from REST on connect.
        return sub

    def unsubscribe(self, sub: _Subscriber) -> None:
        with self._lock:
            if sub in self._subscribers:
                self._subscribers.remove(sub)

    # ------------------------------------------------------ redis bridge ---
    def _ensure_redis_bridge(self) -> None:
        if self._redis_thread is not None and self._redis_thread.is_alive():
            return
        if settings.queue_backend == "memory" and settings.cache_backend == "memory":
            return
        # Start even when Redis is temporarily down; the bridge retries in its
        # own thread and existing WebSocket subscribers can recover without a
        # disconnect/reconnect cycle.
        self._redis_stop.clear()
        self._redis_thread = threading.Thread(target=self._redis_loop, name="eventbus-redis", daemon=True)
        self._redis_thread.start()

    def _need_bridge(self) -> bool:
        return True

    def _redis_loop(self) -> None:  # pragma: no cover - requires redis
        import redis as redis_lib

        while not self._redis_stop.is_set():
            try:
                client = get_redis()
                if client is None:
                    time.sleep(2.0)
                    continue
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                self._redis_pubsub = pubsub
                pubsub.psubscribe(f"{REDIS_CHANNEL_PREFIX}*")
                for message in pubsub.listen():
                    if self._redis_stop.is_set():
                        break
                    if message is None or message.get("type") != "pmessage":
                        continue
                    try:
                        event = Event.from_json(message["data"])
                    except Exception:
                        continue
                    # Redis channels are namespaced; ignore unrelated messages
                    # even when a broad subscription is used by an adapter.
                    if not event.channel:
                        continue
                    self._dispatch(event, relay_to_redis=False)
            except redis_lib.RedisError as exc:
                log.warning("events.redis_bridge_error", error=str(exc))
                time.sleep(2.0)
            except Exception as exc:  # pragma: no cover
                log.warning("events.redis_bridge_crashed", error=str(exc))
                return

    def shutdown(self) -> None:
        self._redis_stop.set()
        try:
            if self._redis_pubsub is not None:
                self._redis_pubsub.close()
        except Exception:
            pass


_bus: EventBus | None = None
_bus_lock = threading.Lock()


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        with _bus_lock:
            if _bus is None:
                _bus = EventBus()
    return _bus


def reset_event_bus() -> None:
    """Test helper."""
    global _bus
    if _bus is not None:
        _bus.shutdown()
    _bus = None
