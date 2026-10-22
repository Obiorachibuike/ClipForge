"""Alembic environment.

The target URL comes from the application's own settings (``DATABASE_URL``), so
migrations always run against the same database the API and worker use. Batch
mode is enabled for SQLite so future ALTER operations work during local
development; PostgreSQL uses normal DDL.
"""
from __future__ import annotations

from logging.config import fileConfig

import app.models  # noqa: F401  (register every mapper before comparing metadata)
from alembic import context
from app.core.config import settings
from app.models import Base
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# ConfigParser treats `%` as interpolation, so escape any in a password.
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

target_metadata = Base.metadata


def _configure(**kwargs) -> None:  # noqa: ANN003
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(
        url=config.get_main_option("sqlalchemy.url"),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _configure(
            connection=connection,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
