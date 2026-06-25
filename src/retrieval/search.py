"""
Vector search trên Qdrant với tenant/user filter bắt buộc.

Sau này muốn hybrid (sparse + dense), thêm sparse vector vào collection và
dùng `qc.query_points` với fusion. Hiện chỉ dense vì user dùng Gemini embedding.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from qdrant_client.http import models as qm

from src.core.qdrant import get_qdrant
from src.core.settings import get_settings
from src.llm.client import get_embedder


@dataclass(slots=True)
class SearchHit:
    point_id: str
    score: float
    doc_id: str
    doc_title: str
    chunk_index: int
    heading_path: str | None
    page_from: int | None
    page_to: int | None
    text: str


async def vector_search(
    query: str,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    doc_ids: list[uuid.UUID] | None = None,
    top_k: int = 20,
) -> list[SearchHit]:
    """
    Search docs của tenant. Nếu `user_id`/`doc_ids` truyền vào -> filter thêm.
    """
    s = get_settings()
    emb = get_embedder()
    qc = get_qdrant()

    (vector,) = await emb.embed([query])

    must: list[Any] = [
        qm.FieldCondition(key="tenant_id", match=qm.MatchValue(value=str(tenant_id))),
    ]
    if user_id is not None:
        must.append(qm.FieldCondition(key="user_id", match=qm.MatchValue(value=str(user_id))))
    if doc_ids:
        must.append(
            qm.FieldCondition(
                key="doc_id",
                match=qm.MatchAny(any=[str(d) for d in doc_ids]),
            )
        )

    hits = await qc.search(
        collection_name=s.qdrant.qdrant_collection_docs,
        query_vector=vector,
        limit=top_k,
        query_filter=qm.Filter(must=must),
        with_payload=True,
    )

    out: list[SearchHit] = []
    for h in hits:
        if not h.payload:
            continue
        out.append(
            SearchHit(
                point_id=str(h.id),
                score=float(h.score),
                doc_id=str(h.payload.get("doc_id", "")),
                doc_title=str(h.payload.get("doc_title", "")),
                chunk_index=int(h.payload.get("chunk_index", -1)),
                heading_path=h.payload.get("heading_path"),
                page_from=h.payload.get("page_from"),
                page_to=h.payload.get("page_to"),
                text=str(h.payload.get("text", "")),
            )
        )
    return out
