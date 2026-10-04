"""Per-job scratch space.

Workers never write outside their own workspace, which keeps render outputs and
intermediate frames contained and trivially cleanable.
"""
from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.core.config import REPO_ROOT, settings
from app.core.logging import get_logger

log = get_logger(__name__)

WORK_ROOT = Path(settings.storage_local_root).expanduser().resolve().parent / "work"


def job_workspace(job_id: str) -> Path:
    path = WORK_ROOT / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def workspace_for(job_id: str, *, keep: bool = False) -> Iterator[Path]:
    """Create a workspace for a job and remove it afterwards unless kept."""
    path = job_workspace(job_id)
    try:
        yield path
    finally:
        if not keep:
            cleanup(path)


def cleanup(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception as exc:  # pragma: no cover
        log.warning("workspace.cleanup_failed", path=str(path), error=str(exc))


def temp_dir(prefix: str = "clipforge-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(WORK_ROOT) if WORK_ROOT.exists() else None))
