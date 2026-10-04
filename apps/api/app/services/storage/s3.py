"""S3-compatible storage backend (AWS S3, Cloudflare R2, MinIO, Backblaze)."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from app.core.errors import StorageError
from app.core.logging import get_logger
from app.services.storage.base import ObjectInfo, RangeSpec, StorageBackend, validate_key

log = get_logger(__name__)
CHUNK = 1024 * 1024


class S3Storage(StorageBackend):
    name = "s3"

    def __init__(
        self,
        *,
        bucket: str,
        endpoint: str = "",
        region: str = "us-east-1",
        access_key: str = "",
        secret_key: str = "",
        prefix: str = "",
        public_base_url: str = "",
        signed_url_ttl: int = 3600,
        path_style: bool = True,
    ) -> None:
        import boto3
        from botocore.config import Config

        if not bucket:
            raise StorageError("S3_BUCKET is not configured.")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.public_base_url = public_base_url.rstrip("/")
        self.signed_url_ttl = signed_url_ttl
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint or None,
            region_name=region,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path" if path_style else "virtual"},
                retries={"max_attempts": 4, "mode": "standard"},
                max_pool_connections=32,
            ),
        )

    # ------------------------------------------------------------ internal ---
    def _full(self, key: str) -> str:
        safe = validate_key(key)
        return f"{self.prefix}/{safe}" if self.prefix else safe

    def reachable(self, timeout: float = 3.0) -> tuple[bool, str]:
        """Is this bucket actually reachable? Answered by talking to S3.

        Used by ``storage_backend=auto``: a configured bucket name must not make
        the app pick a backend it cannot use.
        """
        from botocore.config import Config as BotoConfig

        try:
            probe = self.client.meta.client if hasattr(self.client, "meta") else self.client
            probe.head_bucket(Bucket=self.bucket)
            return True, ""
        except Exception as exc:
            return False, f"{type(exc).__name__}: {str(exc)[:160]}"

    def ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception:
            try:
                self.client.create_bucket(Bucket=self.bucket)
                log.info("storage.bucket_created", bucket=self.bucket)
            except Exception as exc:  # pragma: no cover
                log.warning("storage.bucket_unavailable", bucket=self.bucket, error=str(exc))

    # ---------------------------------------------------------- interface ---
    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> ObjectInfo:
        self.client.put_object(Bucket=self.bucket, Key=self._full(key), Body=data, ContentType=content_type)
        return ObjectInfo(key=key, size=len(data), content_type=content_type)

    def put_file(self, key: str, path: Path, content_type: str = "application/octet-stream") -> ObjectInfo:
        size = Path(path).stat().st_size
        self.client.upload_file(
            str(path),
            self.bucket,
            self._full(key),
            ExtraArgs={"ContentType": content_type},
        )
        return ObjectInfo(key=key, size=size, content_type=content_type)

    def open_stream(self, key: str, byte_range: RangeSpec | None = None) -> Iterator[bytes]:
        kwargs: dict = {"Bucket": self.bucket, "Key": self._full(key)}
        if byte_range:
            kwargs["Range"] = f"bytes={byte_range.start}-{byte_range.end}"
        body = self.client.get_object(**kwargs)["Body"]
        try:
            while True:
                chunk = body.read(CHUNK)
                if not chunk:
                    break
                yield chunk
        finally:
            body.close()

    def read_bytes(self, key: str, byte_range: RangeSpec | None = None) -> bytes:
        kwargs: dict = {"Bucket": self.bucket, "Key": self._full(key)}
        if byte_range:
            kwargs["Range"] = f"bytes={byte_range.start}-{byte_range.end}"
        try:
            return self.client.get_object(**kwargs)["Body"].read()
        except Exception as exc:
            raise StorageError("Unable to read object from storage.", key=key) from exc

    def stat(self, key: str) -> ObjectInfo:
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=self._full(key))
        except Exception as exc:
            raise StorageError("Object not found in storage.", key=key) from exc
        return ObjectInfo(
            key=key,
            size=int(head.get("ContentLength", 0)),
            content_type=head.get("ContentType", ""),
        )

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._full(key))
            return True
        except Exception:
            return False

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._full(key))

    def delete_prefix(self, prefix: str) -> int:
        removed = 0
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self._full(prefix)):
            batch = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if batch:
                self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": batch})
                removed += len(batch)
        return removed

    def list_keys(self, prefix: str) -> list[ObjectInfo]:
        result: list[ObjectInfo] = []
        paginator = self.client.get_paginator("list_objects_v2")
        full_prefix = self._full(prefix)
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if self.prefix and key.startswith(self.prefix + "/"):
                    key = key[len(self.prefix) + 1 :]
                result.append(ObjectInfo(key=key, size=int(obj.get("Size", 0))))
        return result

    def signed_url(self, key: str, ttl_seconds: int | None = None) -> str:
        if self.public_base_url:
            return f"{self.public_base_url}/{self._full(key)}"
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._full(key)},
            ExpiresIn=ttl_seconds or self.signed_url_ttl,
        )

    def local_path(self, key: str) -> Path | None:
        return None

    def multipart_upload(self, key: str, content_type: str = "application/octet-stream") -> str:
        """Start a provider-native multipart upload for large files."""
        resp = self.client.create_multipart_upload(
            Bucket=self.bucket, Key=self._full(key), ContentType=content_type
        )
        return resp["UploadId"]

    def multipart_part(self, key: str, upload_id: str, part_number: int, data: bytes) -> dict:
        resp = self.client.upload_part(
            Bucket=self.bucket,
            Key=self._full(key),
            UploadId=upload_id,
            PartNumber=part_number,
            Body=data,
        )
        return {"PartNumber": part_number, "ETag": resp["ETag"]}

    def multipart_complete(self, key: str, upload_id: str, parts: list[dict]) -> ObjectInfo:
        self.client.complete_multipart_upload(
            Bucket=self.bucket, Key=self._full(key), UploadId=upload_id, MultipartUpload={"Parts": parts}
        )
        return self.stat(key)
