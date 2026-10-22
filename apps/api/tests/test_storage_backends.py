"""Storage boundary behaviour: local containment and the S3 reachability probe."""
from __future__ import annotations

import pytest
from app.core.errors import StorageError, ValidationError
from app.services.storage.base import validate_key
from app.services.storage.local import LocalStorage


def test_local_storage_confines_keys_to_its_root(tmp_path):
    storage = LocalStorage(tmp_path / "storage")
    assert storage._path("projects/p1/videos/v1/source.mp4").is_relative_to(storage.root)

    for key in ("../escape.txt", "a/../../escape.txt", "/etc/passwd", "..\\escape.txt"):
        with pytest.raises((StorageError, ValidationError)):
            storage._path(key)

    # A sibling directory whose name merely starts with the root must not pass.
    sibling = tmp_path / "storage-evil"
    sibling.mkdir()
    assert not sibling.is_relative_to(storage.root)


def test_validate_key_accepts_normal_nested_keys_only():
    assert validate_key("projects/p1/videos/v1/source.mp4") == "projects/p1/videos/v1/source.mp4"
    assert validate_key("a//b/./c") == "a/b/c"
    with pytest.raises(ValidationError):
        validate_key("")


class _FakeS3Client:
    def __init__(self) -> None:
        self.bucket_checks: list[str] = []
        self.created: list[dict] = []
        self.fail_head = False

    def head_bucket(self, *, Bucket: str):
        self.bucket_checks.append(Bucket)
        if self.fail_head:
            raise RuntimeError("AccessDenied")
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def create_bucket(self, **kwargs):
        self.created.append(kwargs)
        return {}


def _s3_with_fake(region: str = "us-east-1", endpoint: str = ""):
    boto3 = pytest.importorskip("boto3")
    from app.services.storage.s3 import S3Storage

    storage = object.__new__(S3Storage)
    storage.bucket = "clipforge"
    storage.region = region
    storage.endpoint = endpoint
    storage.prefix = ""
    storage.public_base_url = ""
    storage.signed_url_ttl = 60
    storage.client = _FakeS3Client()
    assert boto3 is not None
    return storage


def test_reachable_actually_calls_head_bucket():
    """Regression: the probe used a non-existent `client.meta.client` path.

    That made `storage_backend=auto` report every real bucket as unreachable and
    silently fall back to local disk — losing media on an ephemeral filesystem.
    """
    storage = _s3_with_fake()
    ok, detail = storage.reachable()
    assert ok is True, detail
    assert storage.client.bucket_checks == ["clipforge"]


def test_reachable_reports_the_failure_reason():
    storage = _s3_with_fake()
    storage.client.fail_head = True
    ok, detail = storage.reachable()
    assert ok is False
    assert "AccessDenied" in detail


def test_ensure_bucket_passes_a_location_for_non_default_aws_regions():
    storage = _s3_with_fake(region="eu-west-1")
    storage.client.fail_head = True
    storage.ensure_bucket()
    assert storage.client.created[0]["CreateBucketConfiguration"] == {
        "LocationConstraint": "eu-west-1"
    }

    # S3-compatible endpoints (MinIO/R2) do not accept a LocationConstraint.
    storage = _s3_with_fake(region="eu-west-1", endpoint="http://minio:9000")
    storage.client.fail_head = True
    storage.ensure_bucket()
    assert "CreateBucketConfiguration" not in storage.client.created[0]
