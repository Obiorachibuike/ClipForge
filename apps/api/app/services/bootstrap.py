"""Application bootstrap without worker-only media requirements."""
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
    """Create only scratch directories required by this process role.

    The Vercel API uses its writable temporary directory for small multipart
    uploads. It does not create local media storage or worker workspaces; those
    belong to the configured object store and the external Docker worker.
    """
    paths = [STAGING_ROOT]
    if settings.worker_inline and not settings.is_vercel:
        paths.append(WORK_ROOT)
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
    log.info(
        "bootstrap.directories",
        staging=str(STAGING_ROOT),
        worker_inline=settings.worker_inline and not settings.is_vercel,
    )


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
    """Log available API dependencies without requiring local media binaries."""
    from app.services.queue import get_queue

    capabilities = settings.capabilities()
    try:
        storage_name = get_storage().name
    except Exception as exc:
        storage_name = f"unavailable ({type(exc).__name__})"
        log.warning("bootstrap.storage_unavailable", error=str(exc)[:200])
    try:
        queue_name = get_queue().backend_name()
    except Exception as exc:
        queue_name = f"unavailable ({type(exc).__name__})"
        log.warning("bootstrap.queue_unavailable", error=str(exc)[:200])

    result = {
        "ffmpeg": capabilities["ffmpeg"],
        "ffprobe": capabilities["ffprobe"],
        "media_pipeline": capabilities["media_pipeline"],
        "storage": storage_name,
        "queue": queue_name,
        "vision": capabilities["vision"].get("active"),
        "providers": {
            key: capabilities[key]
            for key in ("transcription", "llm", "embeddings")
        },
        "worker_mode": (
            "external"
            if settings.worker_external
            else "inline"
            if settings.worker_inline and not settings.is_vercel
            else "disabled"
        ),
    }
    if not result["media_pipeline"]:
        log.warning(
            "bootstrap.local_media_unavailable",
            detail="This API process has no FFmpeg; configure an external Docker worker or install worker dependencies.",
        )
    log.info(
        "bootstrap.environment",
        storage=storage_name,
        queue=queue_name,
        worker_mode=result["worker_mode"],
        environment=settings.environment,
    )
    return result


def enforce_production_configuration() -> None:
    """Fail closed on insecure production secrets before serving any request."""
    errors = settings.production_config_errors()
    if errors:
        raise RuntimeError(
            "Refusing to start with an insecure production configuration: " + " ".join(errors)
        )
    for warning in settings.production_config_warnings():
        log.warning("bootstrap.config_warning", detail=warning)


def bootstrap(seed: bool = True) -> dict:
    enforce_production_configuration()
    prepare_directories()
    if seed:
        seed_system_data()
    return verify_environment()
