"""
Reranker — 2 backend:

1) Cohere Rerank API (mặc định): model `rerank-v3.5`, multilingual, chất lượng cao
   cho tiếng Việt. Trả relevance_score [0..1] cho mỗi document.
2) LLM-as-reranker (Gemini Flash): fallback khi Cohere lỗi/quota/không cấu hình.

Entry point dùng chung: `rerank(query, hits, top_k=...)` — tự chọn backend theo
settings và tự fallback sang LLM nếu Cohere lỗi.

Tại sao LLM rerank chỉ trả ID + score thay vì re-emit text: giảm token output, nhanh.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from src.core.logging import get_logger
from src.core.redis import cache_get, cache_set, make_key
from src.core.settings import get_settings
from src.llm.client import get_llm

from .search import SearchHit

log = get_logger(__name__)

# Cohere giới hạn độ dài mỗi document; cắt bớt để vừa token budget + giảm cost.
_COHERE_DOC_MAX_CHARS = 2000

_cohere_client: Any = None


def _get_cohere() -> Any:
    """Singleton AsyncClientV2 của Cohere. Import lazy để không bắt buộc có lib."""
    global _cohere_client
    if _cohere_client is None:
        import cohere

        s = get_settings().rerank
        assert s.cohere_api_key is not None  # cohere_enabled đã kiểm tra
        _cohere_client = cohere.AsyncClientV2(
            api_key=s.cohere_api_key.get_secret_value(),
            timeout=s.rerank_timeout_s,
        )
    return _cohere_client

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
    # Nếu reranker bỏ sót id nào -> fallback dùng dense score (scale xuống 0.5x
    # để ưu tiên hits đã được rerank).
    out: list[RerankResult] = []
    for i, h in enumerate(hits):
        if i in id_to_score:
            out.append(RerankResult(h, id_to_score[i]))
        else:
            out.append(RerankResult(h, h.score * 0.5))
    out.sort(key=lambda r: r.score, reverse=True)
    return out[:top_k]


# ---- Cohere rerank ---------------------------------------------------------
async def cohere_rerank(
    query: str,
    hits: list[SearchHit],
    *,
    top_k: int = 5,
    use_cache: bool = True,
) -> list[RerankResult]:
    """Rerank bằng Cohere Rerank API. Raise nếu Cohere lỗi (caller xử lý fallback)."""
    if not hits:
        return []
    if len(hits) == 1:
        return [RerankResult(hits[0], hits[0].score)]

    s = get_settings().rerank
    cache_key = make_key(
        "rerank", s.cohere_rerank_model, query, [h.point_id for h in hits]
    )
    if use_cache and (cached := await cache_get(cache_key)):
        return _materialize(
            hits, {int(k): float(v) for k, v in cached.items()}, top_k
        )

    documents = [
        f"{h.heading_path or h.doc_title}\n{h.text}"[:_COHERE_DOC_MAX_CHARS] for h in hits
    ]

    co = _get_cohere()
    # top_n = toàn bộ candidate: Cohere tính phí theo SỐ document (không theo top_n),
    # nên lấy hết score rồi tự cắt top_k -> tránh điểm dense-fallback lẫn vào kết quả.
    resp = await co.rerank(
        model=s.cohere_rerank_model,
        query=query,
        documents=documents,
        top_n=len(documents),
    )

    id_to_score: dict[int, float] = {}
    for r in resp.results:
        idx = int(r.index)
        if 0 <= idx < len(hits):
            id_to_score[idx] = float(r.relevance_score)

    if use_cache and id_to_score:
        await cache_set(
            cache_key, {str(k): v for k, v in id_to_score.items()}, ttl_s=600
        )
    return _materialize(hits, id_to_score, top_k)


# ---- dispatcher ------------------------------------------------------------
async def rerank(
    query: str,
    hits: list[SearchHit],
    *,
    top_k: int = 5,
    use_cache: bool = True,
) -> list[RerankResult]:
    """
    Entry point dùng chung. Chọn backend theo settings:
      - rerank_provider="cohere" + có API key -> Cohere, lỗi thì fallback LLM.
      - ngược lại -> LLM rerank.
    """
    if not hits:
        return []

    s = get_settings().rerank
    # Cắt pool quá lớn trước khi gửi đi rerank (giới hạn cost/latency).
    if len(hits) > s.rerank_max_candidates:
        hits = hits[: s.rerank_max_candidates]

    if s.cohere_enabled:
        try:
            return await cohere_rerank(query, hits, top_k=top_k, use_cache=use_cache)
        except Exception as e:
            log.warning("cohere_rerank_failed_fallback_llm", error=str(e))
    return await llm_rerank(query, hits, top_k=top_k, use_cache=use_cache)
