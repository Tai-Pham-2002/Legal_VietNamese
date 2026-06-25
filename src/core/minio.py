"""
MinIO (S3-compatible) wrapper. SDK của MinIO là sync -> wrap qua asyncio.to_thread
trong các API async.

Key layout (giúp multi-tenancy + dễ list):
  tenants/{tenant_id}/docs/{doc_id}/raw.{ext}
  tenants/{tenant_id}/docs/{doc_id}/parsed.md
"""

from __future__ import annotations

import asyncio
import io
from typing import Any

from minio import Minio
from minio.error import S3Error

from .logging import get_logger
from .settings import get_settings

log = get_logger(__name__)
_client: Minio | None = None


def get_minio() -> Minio:
    global _client
    if _client is None:
        s = get_settings().minio
        _client = Minio(
            s.minio_endpoint,
            access_key=s.minio_access_key.get_secret_value(),
            secret_key=s.minio_secret_key.get_secret_value(),
            secure=s.minio_secure,
        )
    return _client


async def ensure_bucket() -> None:
    s = get_settings().minio
    c = get_minio()

    def _ensure() -> None:
        if not c.bucket_exists(s.minio_bucket):
            c.make_bucket(s.minio_bucket)
            log.info("minio_bucket_created", bucket=s.minio_bucket)

    await asyncio.to_thread(_ensure)


async def put_object(key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    s = get_settings().minio
    c = get_minio()

    def _put() -> None:
        c.put_object(
            s.minio_bucket,
            key,
            io.BytesIO(data),
            length=len(data),
            content_type=content_type,
        )

    await asyncio.to_thread(_put)


async def get_object_bytes(key: str) -> bytes:
    s = get_settings().minio
    c = get_minio()

    def _get() -> bytes:
        resp = c.get_object(s.minio_bucket, key)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    return await asyncio.to_thread(_get)


async def presigned_get_url(key: str, expires_s: int = 3600) -> str:
    from datetime import timedelta

    s = get_settings().minio
    c = get_minio()
    return await asyncio.to_thread(
        c.presigned_get_object, s.minio_bucket, key, timedelta(seconds=expires_s)
    )


async def healthcheck() -> dict[str, Any]:
    try:
        s = get_settings().minio
        c = get_minio()
        ok = await asyncio.to_thread(c.bucket_exists, s.minio_bucket)
        return {"ok": bool(ok)}
    except S3Error as e:  # pragma: no cover
        return {"ok": False, "error": str(e)}
