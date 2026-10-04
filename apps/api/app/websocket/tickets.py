"""Short-lived, single-use WebSocket tickets.

Useful for clients that cannot attach cookies (native shells, tests) without
weakening the cookie-based session model. Tickets live in Redis when available
and in process memory otherwise.
"""
from __future__ import annotations

import secrets
import threading
import time

from app.core.logging import get_logger
from app.core.redis_client import get_redis

log = get_logger(__name__)

_lock = threading.Lock()
_memory: dict[str, tuple[str, float]] = {}
TICKET_PREFIX = "clipforge:ws-ticket:"


def issue_ticket(user_id: str, ttl_seconds: int = 60) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + ttl_seconds
    client = get_redis()
    if client is not None:
        try:
            client.setex(f"{TICKET_PREFIX}{token}", ttl_seconds, user_id)
            return token
        except Exception as exc:  # pragma: no cover
            log.warning("ws_ticket.redis_failed", error=str(exc)[:120])
    with _lock:
        _memory[token] = (user_id, expires_at)
        _prune()
    return token


def consume_ticket(token: str) -> dict | None:
    """Validate and burn a ticket. Returns {"user_id": ...} or None."""
    if not token:
        return None
    client = get_redis()
    if client is not None:
        try:
            key = f"{TICKET_PREFIX}{token}"
            user_id = client.get(key)
            if user_id:
                client.delete(key)
                return {"user_id": str(user_id)}
            return _consume_memory(token)
        except Exception as exc:  # pragma: no cover
            log.warning("ws_ticket.redis_failed", error=str(exc)[:120])
    return _consume_memory(token)


def _consume_memory(token: str) -> dict | None:
    with _lock:
        entry = _memory.pop(token, None)
    if entry is None:
        return None
    user_id, expires_at = entry
    if expires_at < time.time():
        return None
    return {"user_id": user_id}


def _prune() -> None:
    now = time.time()
    for token in [key for key, (_, expires) in _memory.items() if expires < now]:
        _memory.pop(token, None)


def reset_tickets() -> None:
    """Test helper."""
    with _lock:
        _memory.clear()
