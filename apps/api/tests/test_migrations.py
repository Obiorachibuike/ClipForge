"""Migration process tests.

These run Alembic against a throwaway SQLite database exactly the way a
deployment would (`alembic upgrade head`), then assert the migrated schema is
identical to the ORM metadata and that the downgrade is available.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
API_ROOT = REPO_ROOT / "apps" / "api"
ALEMBIC = Path(sys.executable).with_name("alembic")


def _alembic(database_url: str, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "ENVIRONMENT": "test",
        # Keep the CLI independent of any developer .env values.
        "AUTO_CREATE_TABLES": "false",
    }
    assert ALEMBIC.exists(), f"alembic console script not found at {ALEMBIC}"
    return subprocess.run(
        [str(ALEMBIC), "-c", "alembic.ini", *args],
        cwd=API_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _tables(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        connection.close()


def test_migrations_create_the_full_schema_and_downgrade_cleanly(tmp_path):
    sys.path.insert(0, str(API_ROOT))
    from app.models import Base

    database = tmp_path / "migrated.db"
    url = f"sqlite:///{database}"

    upgraded = _alembic(url, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr[-3000:]

    migrated = _tables(database) - {"alembic_version"}
    expected = set(Base.metadata.tables)
    assert migrated == expected, {
        "missing": sorted(expected - migrated),
        "unexpected": sorted(migrated - expected),
    }

    # Running the same upgrade again is a no-op, not an error.
    again = _alembic(url, "upgrade", "head")
    assert again.returncode == 0, again.stderr[-2000:]

    # The schema matches the models exactly (no drift awaiting a new revision).
    check = _alembic(url, "check")
    assert check.returncode == 0, check.stdout + check.stderr[-2000:]

    downgraded = _alembic(url, "downgrade", "base")
    assert downgraded.returncode == 0, downgraded.stderr[-3000:]
    assert _tables(database) - {"alembic_version"} == set()


def test_a_fresh_database_can_be_migrated_then_served(tmp_path):
    """A database created only by migrations must satisfy the application."""
    sys.path.insert(0, str(API_ROOT))
    from app.models import Base

    database = tmp_path / "fresh.db"
    url = f"sqlite:///{database}"
    result = _alembic(url, "upgrade", "head")
    assert result.returncode == 0, result.stderr[-3000:]

    # Spot-check a few tables and columns that the pipeline depends on.
    connection = sqlite3.connect(database)
    try:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(transcript_words)")
        }
        assert {"transcript_id", "start_time", "end_time", "word", "confidence"} <= columns
        job_columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        assert {"status", "payload", "attempts", "run_after", "cancel_requested"} <= job_columns
    finally:
        connection.close()
    assert "transcript_words" in Base.metadata.tables
