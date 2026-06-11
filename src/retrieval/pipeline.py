"""
End-to-end retrieve pipeline.

Steps:
1) Optional query rewriting (1 -> N sub-queries) -> tăng recall cho multi-hop.
2) Parallel vector search cho mỗi sub-query.
3) Dedup theo point_id, giữ max(score).
4) LLM rerank top-50 -> top-K.
5) Trả về list `RetrievedChunk` với metadata đủ cho citation.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass

from src.core.logging import get_logger
from src.core.redis import cache_get, cache_set, make_key
from src.core.settings import get_settings
from src.llm.client import get_llm
from src.observability.langfuse import observe

from .rerank import rerank
from .search import SearchHit, vector_search

log = get_logger(__name__)

REWRITE_SYSTEM = """Bạn là bộ viết lại truy vấn cho hệ thống tìm kiếm tài liệu pháp luật.
Cho 1 câu hỏi của user, sinh tối đa 3 truy vấn TIẾNG VIỆT cô đọng, súc tích,
mỗi cái nhấn 1 khía cạnh khác nhau (đồng nghĩa, mở rộng khái niệm, từ khoá luật).
Trả JSON: {"queries": ["...", "..."]}.
Nếu câu hỏi đã cụ thể, trả 1 query duy nhất.
KHÔNG thêm giải thích."""


@dataclass(slots=True)
class RetrievedChunk:
    doc_id: str
    chunk_id: str            # qdrant point_id
    doc_title: str
    heading_path: str | None
    page_from: int | None
    page_to: int | None
    text: str
    score: float


async def rewrite_query(query: str, *, use_cache: bool = True) -> list[str]:
    s = get_settings()
    cache_key = make_key("rewrite", s.llm.llm_model_default, query)
    if use_cache and (cached := await cache_get(cache_key)) is not None:
        return list(cached)

    llm = get_llm()
    resp = await llm.complete(
        [
            {"role": "system", "content": REWRITE_SYSTEM},
            {"role": "user", "content": query},
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
        max_tokens=200,
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        obj = json.loads(raw)
        qs = [q for q in (obj.get("queries") or []) if isinstance(q, str) and q.strip()]
    except json.JSONDecodeError:
        qs = []
    if not qs:
        qs = [query]
    qs = qs[:3]
    if use_cache:
        await cache_set(cache_key, qs, ttl_s=600)
    return qs


@observe(name="retrieve_and_rerank")
async def retrieve_and_rerank(
    query: str,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    doc_ids: list[uuid.UUID] | None = None,
    top_k_search: int = 20,
    top_k_final: int = 5,
    rewrite: bool = True,
) -> list[RetrievedChunk]:
    queries = await rewrite_query(query) if rewrite else [query]

    # parallel search
    search_tasks = [
        vector_search(
            q, tenant_id=tenant_id, user_id=user_id, doc_ids=doc_ids, top_k=top_k_search
        )
        for q in queries
    ]
    results: list[list[SearchHit]] = await asyncio.gather(*search_tasks)

    # dedup by point_id keep max score
    by_id: dict[str, SearchHit] = {}
    for batch in results:
        for h in batch:
            existing = by_id.get(h.point_id)
            if existing is None or h.score > existing.score:
                by_id[h.point_id] = h

    pool = sorted(by_id.values(), key=lambda x: x.score, reverse=True)[: top_k_search * 2]
    log.info(
        "retrieve_pool", n_queries=len(queries), n_unique=len(by_id), pool=len(pool)
    )

    # rerank (Cohere mặc định, tự fallback LLM nếu lỗi)
    ranked = await rerank(query, pool, top_k=top_k_final)

    return [
        RetrievedChunk(
            doc_id=r.hit.doc_id,
            chunk_id=r.hit.point_id,
            doc_title=r.hit.doc_title,
            heading_path=r.hit.heading_path,
            page_from=r.hit.page_from,
            page_to=r.hit.page_to,
            text=r.hit.text,
            score=r.score,
        )
        for r in ranked
    ]
