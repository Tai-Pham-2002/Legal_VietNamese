"""
Qdrant client wrapper. Một client async dùng chung.

Bootstrap collections:
- `documents`: vectors của chunks tài liệu (dense, dim từ embedding model).
- `memory`: vectors của user facts (dense, cùng dim).

Payload index trên `tenant_id`, `user_id`, `doc_id` để filter fast O(log n).
"""

from __future__ import annotations

from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm

from .logging import get_logger
from .settings import get_settings

log = get_logger(__name__)
_client: AsyncQdrantClient | None = None


def get_qdrant() -> AsyncQdrantClient:
    global _client
    if _client is None:
        s = get_settings().qdrant
        _client = AsyncQdrantClient(
            url=s.qdrant_url,
            api_key=s.qdrant_api_key.get_secret_value() if s.qdrant_api_key else None,
            timeout=s.qdrant_timeout_s,
            prefer_grpc=False,  # http đủ ở scale này; gRPC bật khi cần
        )
    return _client


async def close_qdrant() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


async def ensure_collections() -> None:
    """Idempotent: tạo collections + payload index nếu chưa có."""
    s = get_settings()
    qc = get_qdrant()
    dim = s.llm.embedding_dim

    for col in (s.qdrant.qdrant_collection_docs, s.qdrant.qdrant_collection_memory):
        exists = await qc.collection_exists(col)
        if not exists:
            log.info("creating_qdrant_collection", name=col, dim=dim)
            await qc.create_collection(
                collection_name=col,
                vectors_config=qm.VectorParams(
                    size=dim,
                    distance=qm.Distance.COSINE,
                    on_disk=False,
                ),
                # Quantization sẽ giảm RAM ~4x, recall giảm <2% — bật khi data lớn
                hnsw_config=qm.HnswConfigDiff(m=16, ef_construct=128),
                optimizers_config=qm.OptimizersConfigDiff(default_segment_number=2),
                shard_number=1,
                replication_factor=1,
            )

        # Payload indexes — phải tạo sau khi collection tồn tại
        for field, schema in (
            ("tenant_id", qm.PayloadSchemaType.KEYWORD),
            ("user_id", qm.PayloadSchemaType.KEYWORD),
            ("doc_id", qm.PayloadSchemaType.KEYWORD),
        ):
            try:
                await qc.create_payload_index(
                    collection_name=col,
                    field_name=field,
                    field_schema=schema,
                )
            except Exception as e:
                # đã tồn tại -> bỏ qua
                if "already exists" not in str(e).lower():
                    log.warning("payload_index_create_failed", col=col, field=field, error=str(e))


async def healthcheck() -> dict[str, Any]:
    try:
        qc = get_qdrant()
        info = await qc.get_collections()
        return {"ok": True, "collections": [c.name for c in info.collections]}
    except Exception as e:  # pragma: no cover
        return {"ok": False, "error": str(e)}
