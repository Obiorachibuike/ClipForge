"""Storage abstraction.

Business logic never talks to S3/MinIO/disk directly; it asks for a
`StorageBackend`. Media code asks for a *local path* (materializing from object
storage into a temp workspace when needed) so FFmpeg keeps operating on files.
"""
from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import StorageError, ValidationError

_UNSAFE_KEY = re.compile(r"(^/)|(\\),|(\.\.)|(^[a-zA-Z]:)", re.IGNORECASE)


def validate_key(key: str) -> str:
    """Reject traversal and absolute paths in storage keys."""
    if not key or len(key) > 1024:
        raise ValidationError("Invalid storage key.")
    if key.startswith("/") or key.startswith("\\") or ".." in key.split("/") or "\\" in key:
        raise ValidationError("Invalid storage key.")
    if re.match(r"^[a-zA-Z]:", key):
        raise ValidationError("Invalid storage key.")
    parts = [p for p in key.split("/") if p not in ("", ".")]
    if any(p in ("..",) for p in parts):
        raise ValidationError("Invalid storage key.")
    return "/".join(parts)


def safe_filename(name: str, *, fallback: str = "upload") -> str:
    """Sanitize a client-supplied filename to a single safe path segment."""
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    base = re.sub(r"[^\w.\-() ]+", "_", base).strip(" .")
    if not base or base in {".", ".."}:
        return fallback
    return base[:200]


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    content_type: str = ""


@dataclass(frozen=True)
class RangeSpec:
    start: int
    end: int  # inclusive

    @property
    def length(self) -> int:
        return self.end - self.start + 1


class StorageBackend(ABC):
    """Provider-independent object storage."""

    name: str = "abstract"

    @abstractmethod
    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> ObjectInfo: ...

    @abstractmethod
    def put_file(self, key: str, path: Path, content_type: str = "application/octet-stream") -> ObjectInfo: ...

    @abstractmethod
    def open_stream(self, key: str, byte_range: RangeSpec | None = None) -> Iterator[bytes]: ...

    @abstractmethod
    def read_bytes(self, key: str, byte_range: RangeSpec | None = None) -> bytes: ...

    @abstractmethod
    def stat(self, key: str) -> ObjectInfo: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def delete_prefix(self, prefix: str) -> int: ...

    @abstractmethod
    def list_keys(self, prefix: str) -> list[ObjectInfo]: ...

    @abstractmethod
    def signed_url(self, key: str, ttl_seconds: int | None = None) -> str: ...

    @abstractmethod
    def local_path(self, key: str) -> Path | None:
        """Return a directly readable path when the backend is local."""

    def materialize(self, key: str, workdir: Path) -> Path:
        """Ensure `key` exists on local disk and return its path."""
        local = self.local_path(key)
        if local is not None and local.exists():
            return local
        target = workdir / safe_filename(Path(key).name, fallback="object.bin")
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as fh:
            for chunk in self.open_stream(key):
                fh.write(chunk)
        return target

    @contextmanager
    def temporary_local(self, key: str, workdir: Path, *, keep: bool = False) -> Iterator[Path]:
        """Context manager yielding a local copy; the copy is removed if it was
        downloaded (never removes files that live in local storage)."""
        local = self.local_path(key)
        if local is not None and local.exists():
            yield local
            return
        path = self.materialize(key, workdir)
        try:
            yield path
        finally:
            if not keep:
                path.unlink(missing_ok=True)

    def cleanup(self) -> None:  # pragma: no cover - default no-op
        return None


class NoopStorage(StorageBackend):
    """Fails loudly. Used when no storage backend could be constructed."""

    name = "none"

    def _fail(self) -> None:
        raise StorageError("No storage backend is configured.")

    def put_bytes(self, key: str, data: bytes, content_type: str = "") -> ObjectInfo:
        self._fail()

    def put_file(self, key: str, path: Path, content_type: str = "") -> ObjectInfo:
        self._fail()

    def open_stream(self, key: str, byte_range: RangeSpec | None = None) -> Iterator[bytes]:
        self._fail()

    def read_bytes(self, key: str, byte_range: RangeSpec | None = None) -> bytes:
        self._fail()

    def stat(self, key: str) -> ObjectInfo:
        self._fail()

    def exists(self, key: str) -> bool:
        self._fail()

    def delete(self, key: str) -> None:
        self._fail()

    def delete_prefix(self, prefix: str) -> int:
        self._fail()

    def list_keys(self, prefix: str) -> list[ObjectInfo]:
        self._fail()

    def signed_url(self, key: str, ttl_seconds: int | None = None) -> str:
        self._fail()

    def local_path(self, key: str) -> Path | None:
        return None
