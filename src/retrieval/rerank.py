"""
LLM-as-reranker dùng Gemini Flash.

Input: query + N candidates (kèm chunk_id).
Output: top-K candidates ranked.

Tại sao trả về chỉ ID + score thay vì re-emit text: giảm token output, nhanh.
Format JSON cho parse chắc chắn.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from Legal_VietNamese.src.core.logging import get_logger
from Legal_VietNamese.src.core.redis import cache_get, cache_set, make_key
from Legal_VietNamese.src.core.settings import get_settings
from Legal_VietNamese.src.llm.client import get_llm

from .search import SearchHit

log = get_logger(__name__)

RERANK_SYSTEM = """Bạn là bộ đánh giá mức độ liên quan giữa CÂU HỎI và các ĐOẠN TÀI LIỆU.
Trả về JSON object: {"ranked": [{"id": <int>, "score": <0.0-1.0>}, ...]}
- Sắp xếp theo score giảm dần.
- Score 1.0 = trả lời trực tiếp, 0.0 = không liên quan.
- Bỏ qua các đoạn rõ ràng không liên quan (score < 0.2).
- Chỉ dùng id đã cho trong input."""


@dataclass(slots=True)
class RerankResult:
    hit: SearchHit
    score: float


async def llm_rerank(
    query: str,
    hits: list[SearchHit],
    *,
    top_k: int = 5,
    use_cache: bool = True,
) -> list[RerankResult]:
    if not hits:
        return []
    if len(hits) == 1:
        return [RerankResult(hits[0], hits[0].score)]

    s = get_settings()
    cache_key = make_key("rerank", s.llm.llm_model_default, query, [h.point_id for h in hits])
    if use_cache:
        if cached := await cache_get(cache_key):
            id_to_score = {int(k): float(v) for k, v in cached.items()}
            return _materialize(hits, id_to_score, top_k)

    # build candidate list (truncate text để tránh token blow-up)
    candidates_text = "\n\n".join(
        f"[{i}] {h.heading_path or h.doc_title}\n{h.text[:500]}"
        for i, h in enumerate(hits)
    )
    user_msg = (
        f"CÂU HỎI: {query}\n\nDANH SÁCH ĐOẠN:\n{candidates_text}\n\n"
        f"Trả JSON như đã hướng dẫn, chỉ giữ top {top_k}."
    )

    llm = get_llm()  # default = flash
    resp = await llm.complete(
        [
            {"role": "system", "content": RERANK_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
        max_tokens=600,
    )

    raw = resp.choices[0].message.content or "{}"
    try:
        obj = json.loads(raw)
        ranked = obj.get("ranked", []) or []
    except json.JSONDecodeError:
        log.warning("rerank_invalid_json", raw=raw[:200])
        ranked = []

    id_to_score: dict[int, float] = {}
    for r in ranked:
        try:
            i = int(r["id"])
            sc = float(r["score"])
            if 0 <= i < len(hits):
                id_to_score[i] = sc
        except (KeyError, ValueError, TypeError):
            continue

    if use_cache and id_to_score:
        await cache_set(
            cache_key, {str(k): v for k, v in id_to_score.items()},
            ttl_s=600,
        )
    return _materialize(hits, id_to_score, top_k)


def _materialize(
    hits: list[SearchHit], id_to_score: dict[int, float], top_k: int
) -> list[RerankResult]:
    # Nếu LLM bỏ sót id nào -> fallback dùng dense score (scale xuống 0.5x để
    # ưu tiên hits đã được rerank).
    out: list[RerankResult] = []
    for i, h in enumerate(hits):
        if i in id_to_score:
            out.append(RerankResult(h, id_to_score[i]))
        else:
            out.append(RerankResult(h, h.score * 0.5))
    out.sort(key=lambda r: r.score, reverse=True)
    return out[:top_k]
