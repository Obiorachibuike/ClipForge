"""Storage backend selection."""
from __future__ import annotations

import threading

from app.core.config import settings
from app.core.logging import get_logger
from app.services.storage.base import NoopStorage, ObjectInfo, RangeSpec, StorageBackend, safe_filename, validate_key
from app.services.storage.local import LocalStorage

log = get_logger(__name__)

_lock = threading.Lock()
_backend: StorageBackend | None = None


def build_storage() -> StorageBackend:
    mode = settings.storage_backend
    if mode == "auto" and not _s3_configured():
        log.info("storage.local", root=str(settings.local_storage_root), reason="s3_not_configured")
        return LocalStorage(settings.local_storage_root)
    if mode in ("auto", "s3") and settings.s3_bucket:
        try:
            from app.services.storage.s3 import S3Storage

            store = S3Storage(
                bucket=settings.s3_bucket,
                endpoint=settings.s3_endpoint,
                region=settings.s3_region,
                access_key=settings.s3_access_key,
                secret_key=settings.s3_secret_key,
                prefix=settings.s3_prefix,
                public_base_url=settings.s3_public_base_url,
                signed_url_ttl=settings.signed_url_ttl_seconds,
                path_style=settings.s3_path_style,
            )
            if mode == "auto":
                # A bucket name alone is not proof the backend works: in auto mode
                # we only adopt S3 after it answers.
                reachable, detail = store.reachable()
                if not reachable:
                    log.warning("storage.s3_unreachable", bucket=settings.s3_bucket, detail=detail)
                    return LocalStorage(settings.local_storage_root)
            log.info("storage.s3", bucket=settings.s3_bucket, endpoint=settings.s3_endpoint or "aws")
            return store
        except Exception as exc:
            if mode == "s3":
                raise
            log.warning("storage.s3_unavailable", error=str(exc), fallback="local")
    if mode == "auto" or mode == "local":
        log.info("storage.local", root=str(settings.local_storage_root))
        return LocalStorage(settings.local_storage_root)
    return NoopStorage()


def _s3_configured() -> bool:
    """S3 needs an explicit endpoint (MinIO/R2/self-hosted) or explicit credentials."""
    return bool(settings.s3_bucket and (settings.s3_endpoint or settings.s3_access_key))


def get_storage() -> StorageBackend:
    global _backend
    if _backend is None:
        with _lock:
            if _backend is None:
                _backend = build_storage()
    return _backend


def set_storage(backend: StorageBackend | None) -> None:
    """Test helper."""
    global _backend
    _backend = backend


__all__ = [
    "LocalStorage",
    "NoopStorage",
    "ObjectInfo",
    "RangeSpec",
    "StorageBackend",
    "build_storage",
    "get_storage",
    "safe_filename",
    "set_storage",
    "validate_key",
]
