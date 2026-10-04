"""Application bootstrap: directories, system caption presets, safety checks."""
from __future__ import annotations

from app.core.config import settings
from app.core.db import get_session_factory, init_engine_if_needed
from app.core.logging import get_logger
from app.services.caption_service import CaptionService
from app.services.pipeline.workspace import WORK_ROOT
from app.services.storage import get_storage
from app.services.upload_service import STAGING_ROOT

log = get_logger(__name__)


def prepare_directories() -> None:
    for path in (settings.local_storage_root, WORK_ROOT, STAGING_ROOT):
        path.mkdir(parents=True, exist_ok=True)
    log.info("bootstrap.directories", storage=str(settings.local_storage_root), work=str(WORK_ROOT))


def seed_system_data() -> None:
    init_engine_if_needed()
    db = get_session_factory()()
    try:
        created = CaptionService.ensure_system_styles(db)
        if created:
            log.info("bootstrap.caption_styles", created=len(created))
    finally:
        db.close()


def verify_environment() -> dict:
    """Log (and return) what this deployment can actually do."""
    from app.services.ai.registry import available_providers, vision_capability
    from app.services.media.ffmpeg import ffmpeg_bin, ffprobe_bin
    from app.services.queue import get_queue

    capabilities = {
        "ffmpeg": ffmpeg_bin(),
        "ffprobe": ffprobe_bin() or "(not found - using PyAV probing)",
        "storage": get_storage().name,
        "queue": get_queue().backend_name(),
        "vision": vision_capability().get("active"),
        "providers": available_providers(),
    }
    if not capabilities["ffmpeg"]:
        log.error(
            "bootstrap.ffmpeg_missing",
            detail="Media processing is disabled until FFmpeg is available. Set MEDIA_FFMPEG_PATH.",
        )
    log.info(
        "bootstrap.environment",
        storage=capabilities["storage"],
        queue=capabilities["queue"],
        environment=settings.environment,
    )
    return capabilities


def bootstrap(seed: bool = True) -> dict:
    prepare_directories()
    if seed:
        seed_system_data()
    return verify_environment()
