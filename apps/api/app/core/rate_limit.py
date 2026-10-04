"""Distributed rate limiting backed by Redis, with an in-process fallback.

Fixed-window counters per identity (user id or client IP) and bucket, so a
missing Redis never means unlimited traffic.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from app.core.config import settings
from app.core.errors import RateLimitedError
from app.core.logging import get_logger
from app.core.redis_client import get_redis

log = get_logger(__name__)

_memory_lock = threading.Lock()
_memory_hits: dict[str, deque[float]] = defaultdict(deque)


def _memory_hit(key: str, limit: int, window: int) -> tuple[bool, int]:
    now = time.time()
    with _memory_lock:
        bucket = _memory_hits[key]
        while bucket and bucket[0] <= now - window:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after = max(1, int(window - (now - bucket[0])))
            return False, retry_after
        bucket.append(now)
        if not bucket:
            _memory_hits.pop(key, None)
    return True, 0


def check_rate_limit(identity: str, bucket: str, limit: int, window_seconds: int) -> None:
    if not settings.rate_limit_enabled or limit <= 0:
        return
    key = f"rl:{bucket}:{identity}"
    client = get_redis()
    if client is not None:
        try:
            count = client.incr(key)
            if count == 1:
                client.expire(key, window_seconds)
            if count > limit:
                ttl = client.ttl(key)
                raise RateLimitedError(retry_after=int(ttl) if ttl and ttl > 0 else window_seconds)
            return
        except RateLimitedError:
            raise
        except Exception as exc:  # Redis hiccup -> fail over to memory
            log.warning("rate_limit.redis_failed", error=str(exc))
    allowed, retry_after = _memory_hit(key, limit, window_seconds)
    if not allowed:
        raise RateLimitedError(retry_after=retry_after)


def reset_memory_limits() -> None:
    """Test helper."""
    with _memory_lock:
        _memory_hits.clear()
