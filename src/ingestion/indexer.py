"""
Indexer — embed chunks và upsert vào Qdrant.

Tách function ra để vừa gọi từ worker (ingestion), vừa từ unit test.
"""

from __future__ import annotations

import uuid

from qdrant_client.http import models as qm

from Legal_VietNamese.src.core.logging import get_logger
from Legal_VietNamese.src.core.qdrant import get_qdrant
from Legal_VietNamese.src.core.settings import get_settings
from Legal_VietNamese.src.db.models import DocumentChunk
from Legal_VietNamese.src.ingestion.chunkers import Chunk
from Legal_VietNamese.src.llm.client import get_embedder

log = get_logger(__name__)


async def index_chunks(
    chunks: list[Chunk],
    *,
    doc_id: uuid.UUID,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    doc_title: str,
) -> list[DocumentChunk]:
    """Embed + upsert + return ORM DocumentChunk objects (chưa flush)."""
    if not chunks:
        return []

    s = get_settings()
    emb = get_embedder()
    qc = get_qdrant()

    texts = [c.text for c in chunks]
    log.info("embedding_chunks", n=len(texts), doc_id=str(doc_id))
    vectors = await emb.embed(texts)

    points: list[qm.PointStruct] = []
    orm_rows: list[DocumentChunk] = []

    for c, v in zip(chunks, vectors, strict=True):
        point_id = uuid.uuid4()
        payload = {
            "tenant_id": str(tenant_id),
            "user_id": str(user_id),
            "doc_id": str(doc_id),
            "doc_title": doc_title,
            "chunk_index": c.index,
            "heading_path": c.heading_path,
            "page_from": c.page_from,
            "page_to": c.page_to,
            "text": c.text,
            "n_tokens": c.n_tokens,
        }
        points.append(qm.PointStruct(id=str(point_id), vector=v, payload=payload))
        orm_rows.append(
            DocumentChunk(
                document_id=doc_id,
                chunk_index=c.index,
                text=c.text,
                n_tokens=c.n_tokens,
                heading_path=c.heading_path,
                page_from=c.page_from,
                page_to=c.page_to,
                qdrant_point_id=point_id,
            )
        )

    # Upsert theo batch để tránh single payload khổng lồ
    batch = 128
    for i in range(0, len(points), batch):
        await qc.upsert(
            collection_name=s.qdrant.qdrant_collection_docs,
            points=points[i : i + batch],
            wait=True,
        )

    log.info("indexed_chunks", n=len(points), doc_id=str(doc_id))
    return orm_rows


async def delete_doc_vectors(doc_id: uuid.UUID) -> None:
    """Xoá toàn bộ vectors thuộc 1 document. Dùng khi xoá file."""
    s = get_settings()
    qc = get_qdrant()
    await qc.delete(
        collection_name=s.qdrant.qdrant_collection_docs,
        points_selector=qm.FilterSelector(
            filter=qm.Filter(
                must=[qm.FieldCondition(key="doc_id", match=qm.MatchValue(value=str(doc_id)))]
            )
        ),
        wait=True,
    )
