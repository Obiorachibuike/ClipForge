"""Alembic environment for ClipForge.

The database URL comes from the application configuration (`DATABASE_URL`), so
credentials live in the deployment environment, never in `alembic.ini`.
SQLite runs migrations in batch mode so future ALTERs work on a development
database; PostgreSQL runs them directly.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

REPO_ROOT = Path(__file__).resolve().parents[1]
API_ROOT = REPO_ROOT / "apps" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from app.core.config import settings  # noqa: E402
from app.models import Base  # noqa: E402  (imports every model module)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def database_url() -> str:
    """Resolve the target database.

    Priority: an explicit `-x sqlalchemy.url=...` / programmatic override, then
    `DATABASE_URL`, then the application settings default. A placeholder value
    in `alembic.ini` ("driver://...") is ignored on purpose.
    """
    override = context.get_x_argument(as_dictionary=True).get("url")
    if override:
        return override
    configured = (config.get_main_option("sqlalchemy.url") or "").strip()
    if configured.startswith(("postgresql", "postgres", "sqlite")):
        return configured
    return os.environ.get("DATABASE_URL") or settings.database_url


def is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def run_migrations_offline() -> None:
    url = database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=is_sqlite(url),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = database_url()
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = url
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            render_as_batch=is_sqlite(url),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
