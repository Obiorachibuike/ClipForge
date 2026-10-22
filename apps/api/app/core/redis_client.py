"""Redis access with an explicit availability probe and recoverable failures.

Production can require Redis for its durable job queue. Development may opt in
to in-process fallbacks, but failed startup probes are retried so a temporary
Redis outage does not pin a long-lived API process to "unavailable" forever.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_lock = threading.Lock()
_client: Any = None
_probed = False
_available: bool | None = None
_last_probe = 0.0
_PROBE_RETRY_SECONDS = 5.0


def _build_client():
    import redis

    return redis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=5,
        health_check_interval=30,
    )


def redis_available() -> bool:
    global _probed, _available, _last_probe
    now = time.monotonic()
    with _lock:
        if _available is True and _probed:
            return True
        if _available is False and _probed and now - _last_probe < _PROBE_RETRY_SECONDS:
            return False
        if settings.queue_backend == "memory" and settings.cache_backend == "memory":
            _probed, _available, _last_probe = True, False, now
            return False
        try:
            client = _build_client()
            client.ping()
            _probed, _available, _last_probe = True, True, now
        except Exception as exc:
            log.info("redis.unavailable", error=str(exc), detail="using in-process fallbacks where configured")
            _probed, _available, _last_probe = True, False, now
    return bool(_available)


def get_redis():
    """Return a live redis client or None when Redis is not currently usable."""
    global _client
    if not redis_available():
        return None
    if _client is None:
        with _lock:
            if _client is None:
                try:
                    _client = _build_client()
                except Exception as exc:  # pragma: no cover
                    log.warning("redis.client_failed", error=str(exc))
                    return None
    return _client


def reset_redis_state() -> None:
    """Test helper."""
    global _client, _probed, _available, _last_probe
    with _lock:
        _client, _probed, _available, _last_probe = None, False, None, 0.0
