"""Local filesystem storage backend (development, single-node deployments)."""
from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

from app.core.errors import StorageError
from app.services.storage.base import ObjectInfo, RangeSpec, StorageBackend, validate_key

CHUNK = 1024 * 1024


class LocalStorage(StorageBackend):
    name = "local"

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ internal ---
    def _path(self, key: str) -> Path:
        safe = validate_key(key)
        path = (self.root / safe).resolve()
        if not str(path).startswith(str(self.root)):
            raise StorageError("Resolved storage path escaped the root directory.")
        return path

    # ---------------------------------------------------------- interface ---
    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> ObjectInfo:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return ObjectInfo(key=key, size=len(data), content_type=content_type)

    def put_file(self, key: str, path: Path, content_type: str = "application/octet-stream") -> ObjectInfo:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if Path(path).resolve() != target.resolve():
            shutil.copyfile(path, target)
        return ObjectInfo(key=key, size=target.stat().st_size, content_type=content_type)

    def append_bytes(self, key: str, data: bytes) -> int:
        """Used by chunked upload assembly (local backend only fast path)."""
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab") as fh:
            fh.write(data)
        return path.stat().st_size

    def open_stream(self, key: str, byte_range: RangeSpec | None = None) -> Iterator[bytes]:
        path = self._path(key)
        if not path.exists():
            raise StorageError(f"Object not found: {key}", key=key)
        remaining = byte_range.length if byte_range else None
        with open(path, "rb") as fh:
            if byte_range:
                fh.seek(byte_range.start)
            while True:
                size = CHUNK if remaining is None else min(CHUNK, remaining)
                if size <= 0:
                    break
                chunk = fh.read(size)
                if not chunk:
                    break
                if remaining is not None:
                    remaining -= len(chunk)
                yield chunk

    def read_bytes(self, key: str, byte_range: RangeSpec | None = None) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise StorageError(f"Object not found: {key}", key=key)
        if byte_range is None:
            return path.read_bytes()
        with open(path, "rb") as fh:
            fh.seek(byte_range.start)
            return fh.read(byte_range.length)

    def stat(self, key: str) -> ObjectInfo:
        path = self._path(key)
        if not path.exists():
            raise StorageError(f"Object not found: {key}", key=key)
        return ObjectInfo(key=key, size=path.stat().st_size)

    def exists(self, key: str) -> bool:
        try:
            return self._path(key).exists()
        except StorageError:
            return False

    def delete(self, key: str) -> None:
        path = self._path(key)
        path.unlink(missing_ok=True)

    def delete_prefix(self, prefix: str) -> int:
        base = self._path(prefix)
        removed = 0
        if base.is_file():
            base.unlink()
            return 1
        if base.is_dir():
            for child in base.rglob("*"):
                if child.is_file():
                    child.unlink()
                    removed += 1
            shutil.rmtree(base, ignore_errors=True)
        return removed

    def list_keys(self, prefix: str) -> list[ObjectInfo]:
        base = self._path(prefix)
        result: list[ObjectInfo] = []
        if base.is_file():
            result.append(ObjectInfo(key=prefix, size=base.stat().st_size))
        elif base.is_dir():
            for child in sorted(base.rglob("*")):
                if child.is_file():
                    rel = child.relative_to(self.root).as_posix()
                    result.append(ObjectInfo(key=rel, size=child.stat().st_size))
        return result

    def signed_url(self, key: str, ttl_seconds: int | None = None) -> str:
        """Local storage is served through the authenticated API, not a
        pre-signed URL."""
        return f"/api/v1/files/{validate_key(key)}"

    def local_path(self, key: str) -> Path | None:
        return self._path(key)

    def usage_bytes(self) -> int:
        total = 0
        for path in self.root.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
        return total
