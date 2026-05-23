"""
Long-term memory: lưu user facts vào Postgres + Qdrant `memory`, retrieve bằng
vector search filter user_id.

Dedupe: trước khi insert, search vector tương tự nhất; nếu similarity > 0.92
và cùng `key`, coi như duplicate -> bump confidence thay vì insert mới.
"""

from __future__ import annotations

import uuid

from qdrant_client.http import models as qm

from Legal_VietNamese.src.core.db import session_scope
from Legal_VietNamese.src.core.logging import get_logger
from Legal_VietNamese.src.core.qdrant import get_qdrant
from Legal_VietNamese.src.core.settings import get_settings
from Legal_VietNamese.src.db.repositories import UserFactRepo
from Legal_VietNamese.src.llm.client import get_embedder

log = get_logger(__name__)

_DEDUPE_THRESHOLD = 0.92


async def save_fact(
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    key: str,
    value: str,
    confidence: float = 0.8,
    source_message_ids: list[uuid.UUID] | None = None,
) -> bool:
    """
    Save fact với dedupe. Trả True nếu đã insert mới, False nếu skip dup.
    """
    s = get_settings()
    emb = get_embedder()
    qc = get_qdrant()
    col = s.qdrant.qdrant_collection_memory

    # 1) embed
    fact_text = f"{key}: {value}"
    (vector,) = await emb.embed([fact_text])

    # 2) dedupe search (top-1 same user)
    hits = await qc.search(
        collection_name=col,
        query_vector=vector,
        limit=1,
        query_filter=qm.Filter(
            must=[
                qm.FieldCondition(key="user_id", match=qm.MatchValue(value=str(user_id))),
                qm.FieldCondition(key="key", match=qm.MatchValue(value=key)),
            ]
        ),
        with_payload=False,
    )
    if hits and hits[0].score >= _DEDUPE_THRESHOLD:
        log.info("memory_fact_dup_skip", key=key, score=hits[0].score)
        return False

    # 3) insert Postgres + Qdrant
    point_id = uuid.uuid4()
    async with session_scope() as session:
        repo = UserFactRepo(session)
        await repo.add(
            user_id=user_id,
            tenant_id=tenant_id,
            key=key,
            value=value,
            confidence=confidence,
            source_message_ids=source_message_ids or [],
            qdrant_point_id=point_id,
        )

    payload = {
        "user_id": str(user_id),
        "tenant_id": str(tenant_id),
        "key": key,
        "value": value,
        "confidence": confidence,
    }
    await qc.upsert(
        collection_name=col,
        points=[qm.PointStruct(id=str(point_id), vector=vector, payload=payload)],
        wait=True,
    )
    return True


async def retrieve_user_facts(
    *,
    user_id: uuid.UUID,
    query: str,
    top_k: int = 5,
) -> list[dict]:
    """Vector search facts liên quan đến `query` của user."""
    s = get_settings()
    emb = get_embedder()
    qc = get_qdrant()
    col = s.qdrant.qdrant_collection_memory

    (vector,) = await emb.embed([query])
    hits = await qc.search(
        collection_name=col,
        query_vector=vector,
        limit=top_k,
        query_filter=qm.Filter(
            must=[qm.FieldCondition(key="user_id", match=qm.MatchValue(value=str(user_id)))]
        ),
        with_payload=True,
    )
    return [
        {
            "key": h.payload.get("key"),
            "value": h.payload.get("value"),
            "confidence": h.payload.get("confidence"),
            "score": h.score,
        }
        for h in hits
        if h.payload
    ]
