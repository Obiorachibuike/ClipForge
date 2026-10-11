"""Object-storage upload assembly performed by the independent worker."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.core.errors import MediaError
from app.models import UploadSession
from app.services.pipeline.video_jobs import assemble_upload_parts
from app.services.storage.local import LocalStorage


def _session(size: int, chunk_size: int) -> UploadSession:
    chunks = (size + chunk_size - 1) // chunk_size
    return UploadSession(
        upload_id="upload-test",
        parts_prefix="projects/project/videos/video/parts",
        filename="source.mp4",
        size_bytes=size,
        chunk_size=chunk_size,
        total_chunks=chunks,
        received_chunks={
            str(index): min(chunk_size, size - index * chunk_size)
            for index in range(chunks)
        },
        received_bytes=size,
    )


def test_worker_assembles_shared_storage_parts_in_order_and_hashes_them(tmp_path: Path):
    storage = LocalStorage(tmp_path / "storage")
    first = b"first-part-" * 100
    second = b"second-part" * 70
    data = first + second
    session = _session(len(data), len(first))
    storage.put_bytes(f"{session.parts_prefix}/00000000.part", first)
    storage.put_bytes(f"{session.parts_prefix}/00000001.part", second)
    destination = tmp_path / "work" / "source.mp4"
    progress: list[float] = []

    checksum = assemble_upload_parts(storage, session, destination, progress_cb=progress.append)

    assert destination.read_bytes() == data
    assert checksum == hashlib.sha256(data).hexdigest()
    assert progress == [50.0, 100.0]


def test_worker_refuses_a_missing_storage_part(tmp_path: Path):
    storage = LocalStorage(tmp_path / "storage")
    session = _session(100, 100)

    with pytest.raises(MediaError) as exc:
        assemble_upload_parts(storage, session, tmp_path / "work" / "source.mp4")

    assert exc.value.code == "upload_data_missing"


def test_worker_refuses_a_part_with_an_incorrect_size(tmp_path: Path):
    storage = LocalStorage(tmp_path / "storage")
    session = _session(100, 100)
    storage.put_bytes(f"{session.parts_prefix}/00000000.part", b"too short")

    with pytest.raises(MediaError) as exc:
        assemble_upload_parts(storage, session, tmp_path / "work" / "source.mp4")

    assert exc.value.code == "upload_size_mismatch"
