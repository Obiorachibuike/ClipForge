"""Compatibility shim.

The storage layer lives in `app.services.storage`; this module keeps
`app.storage` imports working for external integrations.
"""
from app.services.storage import (
    LocalStorage,
    NoopStorage,
    ObjectInfo,
    RangeSpec,
    StorageBackend,
    build_storage,
    get_storage,
    safe_filename,
    set_storage,
    validate_key,
)

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
