"""Database engine and session management (SQLAlchemy 2.0, sync).

Sync sessions with threadpooled request handlers keep the code simple and
predictable; heavy media work never happens inside a request anyway.
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _build_engine(url: str | None = None) -> Engine:
    target = url or settings.database_url
    kwargs: dict = {"echo": settings.db_echo, "pool_pre_ping": True, "future": True}
    if target.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        kwargs.pop("pool_pre_ping")
    else:
        kwargs["pool_size"] = settings.db_pool_size
        kwargs["max_overflow"] = settings.db_max_overflow
    engine = create_engine(target, **kwargs)
    if target.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, autocommit=False, expire_on_commit=False)
    return _SessionLocal


def dispose_engine() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session scope for workers and scripts."""
    db = get_session_factory()()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()


_initialized = False
_init_lock = threading.Lock()


def init_engine_if_needed() -> Engine:
    """Create tables when configured to (development/single-node), once per process."""
    global _initialized
    engine = get_engine()
    if _initialized:
        return engine
    with _init_lock:
        if _initialized:
            return engine
        import app.models  # noqa: F401  (register mappers before create_all)
        from app.models import Base

        if settings.auto_create_tables:
            Base.metadata.create_all(bind=engine)
        _initialized = True
    return engine


def reset_initialization() -> None:
    """Test helper."""
    global _initialized
    with _init_lock:
        _initialized = False
