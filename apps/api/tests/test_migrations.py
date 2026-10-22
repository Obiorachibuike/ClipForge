"""Alembic is the production schema-management path; it must match the models.

These tests run the real Alembic commands against temporary SQLite databases.
PostgreSQL cannot be started in this sandbox, so the PostgreSQL-specific path is
covered statically (the migration uses SQLAlchemy constructs, and the URL is
resolved from DATABASE_URL at run time) — see the implementation report.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from app.models import Base
from sqlalchemy import create_engine, inspect

from alembic import command

REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
HEAD_REVISION = "0001_initial_schema"


def _config(url: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture()
def migrated_database(tmp_path):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    command.upgrade(_config(url), "head")
    return url


def test_upgrade_head_creates_the_complete_schema(migrated_database):
    engine = create_engine(migrated_database)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names()) - {"alembic_version"}

    assert tables == set(Base.metadata.tables)
    for table in Base.metadata.tables.values():
        db_columns = {column["name"] for column in inspector.get_columns(table.name)}
        model_columns = {column.name for column in table.columns}
        assert db_columns == model_columns, f"{table.name} columns drifted from the migration"
        db_indexes = {index["name"] for index in inspector.get_indexes(table.name)}
        model_indexes = {index.name for index in table.indexes}
        assert model_indexes <= db_indexes, f"{table.name} is missing indexes {model_indexes - db_indexes}"


def test_foreign_keys_and_cascade_rules_match_the_models(migrated_database):
    inspector = inspect(create_engine(migrated_database))

    jobs = {
        tuple(fk["constrained_columns"]): (fk["referred_table"], fk["options"].get("ondelete"))
        for fk in inspector.get_foreign_keys("jobs")
    }
    assert jobs[("project_id",)] == ("projects", "CASCADE")
    assert jobs[("user_id",)] == ("users", "CASCADE")
    assert jobs[("video_id",)] == ("videos", "SET NULL")

    # Every model FK exists in the migrated schema with the same target.
    for table in Base.metadata.tables.values():
        migrated = {
            (tuple(fk["constrained_columns"]), fk["referred_table"])
            for fk in inspector.get_foreign_keys(table.name)
        }
        for column in table.columns:
            for fk in column.foreign_keys:
                assert ((column.name,), fk.column.table.name) in migrated


def test_revision_is_recorded_in_alembic_version(migrated_database):
    engine = create_engine(migrated_database)
    with engine.connect() as connection:
        from sqlalchemy import text

        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == HEAD_REVISION


def test_migrations_are_idempotent(migrated_database):
    # A second upgrade must be a no-op rather than re-running create_table.
    command.upgrade(_config(migrated_database), "head")
    inspector = inspect(create_engine(migrated_database))
    assert set(Base.metadata.tables) <= set(inspector.get_table_names())


def test_the_migration_matches_the_models_without_pending_changes(migrated_database):
    """`alembic check` is how CI catches a model change with no migration."""
    command.check(_config(migrated_database))


def test_downgrade_base_removes_the_application_schema(tmp_path):
    url = f"sqlite:///{tmp_path / 'rollback.db'}"
    config = _config(url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    inspector = inspect(create_engine(url))
    remaining = set(inspector.get_table_names()) - {"alembic_version"}
    assert remaining == set(), f"downgrade left tables behind: {sorted(remaining)}"
