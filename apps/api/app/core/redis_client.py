"""Redis access with an explicit availability probe.

The platform is designed to run with PostgreSQL + Redis (docker compose), but
must also be usable on a single small box without Redis. Components ask this
module for a client; when it returns None they use their in-process fallback.
"""
from __future__ import annotations

import threading
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_lock = threading.Lock()
_client: Any = None
_probed = False
_available: bool | None = None


def _build_client():  # noqa: ANN202
    import redis

    return redis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=5,
        health_check_interval=30,
    )


def redis_available() -> bool:
    global _probed, _available
    with _lock:
        if _available is not None and _probed:
            return _available
        if settings.queue_backend == "memory" and settings.cache_backend == "memory":
            _probed, _available = True, False
            return False
        try:
            client = _build_client()
            client.ping()
            _probed, _available = True, True
        except Exception as exc:
            log.info("redis.unavailable", error=str(exc), detail="using in-process fallbacks")
            _probed, _available = True, False
    return bool(_available)


def get_redis():
    """Return a live redis client or None when Redis is not usable."""
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
    global _client, _probed, _available
    with _lock:
        _client, _probed, _available = None, False, None
