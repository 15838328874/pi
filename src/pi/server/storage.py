"""Object storage (S3-compatible MinIO) for the user file pipeline.

The server is a *signer + bookkeeper*, not a byte mover: clients/sandboxes get
presigned URLs and talk to MinIO directly (the mainstream "direct handshake").
http metadata lives in MySQL `files` (see db.FileRow). Every boto3 call is
synchronous, so it is wrapped in asyncio.to_thread to keep the event loop free.

Enabled only when PI_S3_ENDPOINT + access/secret keys are set (see
ServerSettings.s3_enabled); otherwise endpoints answer 503 with a clear message.
"""

from __future__ import annotations

import asyncio
import logging

from pi.server.config import ServerSettings

log = logging.getLogger("pi.server.storage")

PRESIGN_PUT_EXPIRES_S = 900  # uploads: up to 15 minutes to PUT after signing
PRESIGN_GET_EXPIRES_S = 900  # downloads/sandbox pulls: 15 min, matches turn TTL


class ObjectStore:
    """Lazy boto3 S3 client pointed at the configured MinIO endpoint."""

    def __init__(self, settings: ServerSettings):
        self._settings = settings
        self._client = None

    @property
    def enabled(self) -> bool:
        return self._settings.s3_enabled

    def _client_sync(self):
        if self._client is None:
            import boto3
            from botocore.config import Config

            # SigV4 强制：boto3 对非 AWS endpoint 默认退到 SigV2，其签名依赖
            # Content-Type header，curl/PUT 客户端 header 稍有出入就 403
            # SignatureDoesNotMatch。MinIO 完全支持 SigV4 且更稳。
            self._client = boto3.client(
                "s3",
                endpoint_url=self._settings.s3_endpoint,
                aws_access_key_id=self._settings.s3_access_key,
                aws_secret_access_key=self._settings.s3_secret_key,
                region_name=self._settings.s3_region,
                config=Config(signature_version="s3v4"),
            )
        return self._client

    # -- lifecycle ---------------------------------------------------------

    async def ensure_buckets(self) -> None:
        """Create the two buckets if absent (idempotent, best-effort at boot)."""
        if not self.enabled:
            return
        await asyncio.to_thread(self._ensure_buckets_sync)

    def _ensure_buckets_sync(self) -> None:
        c = self._client_sync()
        for bucket in {self._settings.s3_bucket_files, self._settings.s3_bucket_artifacts}:
            try:
                c.head_bucket(Bucket=bucket)
            except Exception:  # noqa: BLE001 - 404/网络都视为"不存在则建"
                try:
                    c.create_bucket(Bucket=bucket)
                except Exception:  # noqa: BLE001 - 已存在/权限，boot 不因此失败
                    log.warning("ensure bucket %s failed", bucket, exc_info=True)

    # -- presign -----------------------------------------------------------

    async def presign_put(self, object_key: str, bucket: str, expires: int = PRESIGN_PUT_EXPIRES_S) -> str:
        return await asyncio.to_thread(self._presign_put_sync, object_key, bucket, expires)

    def _presign_put_sync(self, object_key: str, bucket: str, expires: int) -> str:
        return self._client_sync().generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": object_key},
            ExpiresIn=expires,
        )

    async def presign_get(self, object_key: str, bucket: str, expires: int = PRESIGN_GET_EXPIRES_S) -> str:
        return await asyncio.to_thread(self._presign_get_sync, object_key, bucket, expires)

    def _presign_get_sync(self, object_key: str, bucket: str, expires: int) -> str:
        return self._client_sync().generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": object_key},
            ExpiresIn=expires,
        )

    # -- object ops --------------------------------------------------------

    async def head(self, object_key: str, bucket: str) -> tuple[int, str] | None:
        """Return (size, content_type) or None if absent."""
        return await asyncio.to_thread(self._head_sync, object_key, bucket)

    def _head_sync(self, object_key: str, bucket: str) -> tuple[int, str] | None:
        try:
            meta = self._client_sync().head_object(Bucket=bucket, Key=object_key)
            return meta.get("ContentLength", 0), meta.get("ContentType", "")
        except Exception:  # noqa: BLE001 - 404 → 不存在
            return None

    async def delete(self, object_key: str, bucket: str) -> None:
        await asyncio.to_thread(self._delete_sync, object_key, bucket)

    def _delete_sync(self, object_key: str, bucket: str) -> None:
        self._client_sync().delete_object(Bucket=bucket, Key=object_key)