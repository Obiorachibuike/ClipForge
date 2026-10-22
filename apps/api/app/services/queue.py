"""Redis-backed job queue with an in-process equivalent.

The important behaviours: FIFO-with-priority ordering, deferred execution
(`run_after`) for retry backoff, atomic claim, cancellation flags visible to
running workers, and depth/active introspection for the ops endpoint.
"""
from __future__ import annotations

import heapq
import threading
import time
from typing import Protocol

from app.core.config import settings
from app.core.logging import get_logger
from app.core.redis_client import get_redis

log = get_logger(__name__)

READY_KEY = "clipforge:jobs:ready"  # sorted set: member=job_id, score=ready_at + priority/1000
ACTIVE_KEY = "clipforge:jobs:active"  # hash: job_id -> worker_id
CANCELLED_KEY = "clipforge:jobs:cancelled"  # set with TTL

_CLAIM_SCRIPT = """
local ready = KEYS[1]
local active = KEYS[2]
local worker = ARGV[1]
local now = tonumber(ARGV[2])
local items = redis.call('ZRANGE', ready, 0, 0, 'WITHSCORES')
if #items == 0 then return nil end
local member = items[1]
local score = tonumber(items[2])
if score > now then return nil end
redis.call('ZREM', ready, member)
redis.call('HSET', active, member, worker)
return member
"""


class QueueUnavailable(RuntimeError):
    pass


class JobQueue(Protocol):
    def enqueue(self, job_id: str, *, priority: int = 100, delay_seconds: float = 0.0) -> None: ...
    def claim(self, worker_id: str, timeout: float = 1.0) -> str | None: ...
    def ack(self, job_id: str) -> None: ...
    def release(self, job_id: str, *, delay_seconds: float = 0.0, priority: int = 100) -> None: ...
    def cancel(self, job_id: str) -> None: ...
    def is_cancelled(self, job_id: str) -> bool: ...
    def clear_cancel(self, job_id: str) -> None: ...
    def active_jobs(self) -> dict[str, str]: ...
    def depth(self) -> int: ...
    def is_active(self, job_id: str) -> bool: ...
    def is_queued(self, job_id: str) -> bool: ...
    def touch(self, job_id: str, worker_id: str) -> None: ...
    def active_since(self) -> dict[str, float]: ...
    def backend_name(self) -> str: ...


class InMemoryJobQueue:
    """Correct single-process queue (dev, tests, small single-node installs)."""

    name = "memory"

    def __init__(self) -> None:
        # Re-entrant: `release()` holds the condition while calling `enqueue()`,
        # which re-acquires it. A plain Lock deadlocked the worker thread on the
        # first release (confirmed by test_queue_release_requeues_without_blocking).
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._heap: list[tuple[float, str]] = []
        self._queued: set[str] = set()
        self._active: dict[str, tuple[str, float]] = {}
        self._cancelled: set[str] = set()

    def enqueue(self, job_id: str, *, priority: int = 100, delay_seconds: float = 0.0) -> None:
        score = time.time() + max(0.0, delay_seconds) + min(priority, 999) / 1000.0
        with self._cond:
            if job_id in self._queued:
                return
            heapq.heappush(self._heap, (score, job_id))
            self._queued.add(job_id)
            self._cond.notify_all()

    def claim(self, worker_id: str, timeout: float = 1.0) -> str | None:
        deadline = time.time() + timeout
        with self._cond:
            while True:
                now = time.time()
                if self._heap:
                    score, job_id = self._heap[0]
                    if score <= now:
                        heapq.heappop(self._heap)
                        self._queued.discard(job_id)
                        self._active[job_id] = (worker_id, now)
                        return job_id
                    wait = min(max(score - now, 0.02), 0.25)
                else:
                    wait = 0.25
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cond.wait(min(wait, remaining))

    def ack(self, job_id: str) -> None:
        with self._lock:
            self._active.pop(job_id, None)

    def release(self, job_id: str, *, delay_seconds: float = 0.0, priority: int = 100) -> None:
        with self._cond:
            self._active.pop(job_id, None)
            self.enqueue(job_id, priority=priority, delay_seconds=delay_seconds)

    def cancel(self, job_id: str) -> None:
        with self._lock:
            self._cancelled.add(job_id)

    def clear_cancel(self, job_id: str) -> None:
        with self._lock:
            self._cancelled.discard(job_id)

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancelled

    def active_jobs(self) -> dict[str, str]:
        with self._lock:
            return {jid: info[0] for jid, info in self._active.items()}

    def active_since(self) -> dict[str, float]:
        with self._lock:
            return {jid: info[1] for jid, info in self._active.items()}

    def is_active(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._active

    def is_queued(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._queued

    def touch(self, job_id: str, worker_id: str) -> None:
        with self._lock:
            if job_id in self._active:
                self._active[job_id] = (worker_id, self._active[job_id][1])

    def depth(self) -> int:
        with self._lock:
            return len(self._heap)

    def backend_name(self) -> str:
        return self.name


class RedisJobQueue:
    name = "redis"

    def __init__(self) -> None:
        self._script_sha: str | None = None

    def _client(self):
        client = get_redis()
        if client is None:
            raise QueueUnavailable("Redis is not available")
        return client

    def enqueue(self, job_id: str, *, priority: int = 100, delay_seconds: float = 0.0) -> None:
        score = time.time() + max(0.0, delay_seconds) + min(priority, 999) / 1000.0
        self._client().zadd(READY_KEY, {job_id: score})

    def claim(self, worker_id: str, timeout: float = 1.0) -> str | None:
        client = self._client()
        deadline = time.time() + timeout
        while True:
            try:
                if self._script_sha is None:
                    self._script_sha = client.script_load(_CLAIM_SCRIPT)
                job_id = client.evalsha(self._script_sha, 2, READY_KEY, ACTIVE_KEY, worker_id, time.time())
            except Exception:
                # A missing cached script (NOSCRIPT after a Redis restart) is
                # expected; reload it. Do not let other connection errors strand
                # the worker: surface them as QueueUnavailable so the caller can
                # back off and retry instead of exiting the process.
                self._script_sha = None
                try:
                    job_id = client.eval(_CLAIM_SCRIPT, 2, READY_KEY, ACTIVE_KEY, worker_id, time.time())
                except Exception as exc:
                    raise QueueUnavailable(f"Redis claim failed: {type(exc).__name__}") from exc
            if job_id:
                return job_id
            if time.time() >= deadline:
                return None
            time.sleep(0.08)

    def ack(self, job_id: str) -> None:
        self._client().hdel(ACTIVE_KEY, job_id)

    def release(self, job_id: str, *, delay_seconds: float = 0.0, priority: int = 100) -> None:
        client = self._client()
        client.hdel(ACTIVE_KEY, job_id)
        self.enqueue(job_id, priority=priority, delay_seconds=delay_seconds)

    def cancel(self, job_id: str) -> None:
        client = self._client()
        client.sadd(CANCELLED_KEY, job_id)
        client.expire(CANCELLED_KEY, 60 * 60 * 24)

    def clear_cancel(self, job_id: str) -> None:
        self._client().srem(CANCELLED_KEY, job_id)

    def is_cancelled(self, job_id: str) -> bool:
        try:
            return bool(self._client().sismember(CANCELLED_KEY, job_id))
        except Exception:
            return False

    def active_jobs(self) -> dict[str, str]:
        raw = self._client().hgetall(ACTIVE_KEY)
        return {str(k): str(v) for k, v in raw.items()}

    def active_since(self) -> dict[str, float]:
        return {}

    def is_active(self, job_id: str) -> bool:
        return self._client().hexists(ACTIVE_KEY, job_id) == 1

    def is_queued(self, job_id: str) -> bool:
        return self._client().zscore(READY_KEY, job_id) is not None

    def touch(self, job_id: str, worker_id: str) -> None:
        self._client().hset(ACTIVE_KEY, job_id, worker_id)

    def depth(self) -> int:
        return int(self._client().zcard(READY_KEY))

    def backend_name(self) -> str:
        return self.name


_queue: JobQueue | None = None
_queue_lock = threading.Lock()


def get_queue() -> JobQueue:
    global _queue
    if _queue is None:
        with _queue_lock:
            if _queue is None:
                if settings.is_production and settings.queue_backend == "memory":
                    raise QueueUnavailable("The in-memory queue is not supported in production")
                use_redis = settings.queue_backend == "redis" or settings.is_production or (
                    settings.queue_backend == "auto" and get_redis() is not None
                )
                if use_redis:
                    try:
                        candidate = RedisJobQueue()
                        candidate.depth()  # connectivity check
                        _queue = candidate
                        log.info("queue.redis")
                    except Exception as exc:
                        if settings.queue_backend == "redis" or settings.is_production:
                            # Production and explicit Redis deployments must not
                            # acknowledge jobs into process-local memory.
                            log.error("queue.redis_required_unavailable", error=str(exc))
                            raise QueueUnavailable("Redis is required for the configured job queue") from exc
                        log.warning("queue.redis_unavailable", error=str(exc), fallback="memory")
                        _queue = InMemoryJobQueue()
                else:
                    _queue = InMemoryJobQueue()
                    log.info("queue.memory")
    return _queue


def set_queue(queue: JobQueue | None) -> None:
    """Test helper."""
    global _queue
    _queue = queue
