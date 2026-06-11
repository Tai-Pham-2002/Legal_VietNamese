"""Unit tests cho reranker: Cohere mapping, ordering, dispatcher + LLM fallback."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.retrieval import rerank as R
from src.retrieval.search import SearchHit


def _hit(pid: str, score: float, text: str = "noi dung") -> SearchHit:
    return SearchHit(
        point_id=pid,
        score=score,
        doc_id="d1",
        doc_title="Luat",
        chunk_index=0,
        heading_path=None,
        page_from=1,
        page_to=1,
        text=text,
    )


class _FakeCohere:
    """Giả lập AsyncClientV2: trả results theo thứ tự relevance giảm dần."""

    def __init__(self, ranking: list[tuple[int, float]]):
        self._ranking = ranking
        self.called_with: dict = {}

    async def rerank(self, *, model, query, documents, top_n):
        self.called_with = {
            "model": model,
            "query": query,
            "documents": documents,
            "top_n": top_n,
        }
        results = [
            SimpleNamespace(index=i, relevance_score=s) for i, s in self._ranking
        ]
        return SimpleNamespace(results=results)


@pytest.mark.asyncio
async def test_cohere_rerank_maps_index_and_orders(monkeypatch):
    hits = [_hit("a", 0.9), _hit("b", 0.8), _hit("c", 0.7)]
    # Cohere đảo thứ tự: hit index 2 liên quan nhất, rồi 0, rồi 1
    fake = _FakeCohere([(2, 0.95), (0, 0.50), (1, 0.10)])
    monkeypatch.setattr(R, "_get_cohere", lambda: fake)

    out = await R.cohere_rerank("câu hỏi", hits, top_k=3, use_cache=False)

    assert [r.hit.point_id for r in out] == ["c", "a", "b"]
    assert out[0].score == pytest.approx(0.95)
    # gửi đúng model + top_n
    assert fake.called_with["model"] == "rerank-v3.5"
    assert fake.called_with["top_n"] == 3


@pytest.mark.asyncio
async def test_cohere_rerank_truncates_long_doc(monkeypatch):
    long_text = "x" * 5000
    hits = [_hit("a", 0.9, text=long_text), _hit("b", 0.8)]
    fake = _FakeCohere([(0, 0.9), (1, 0.5)])
    monkeypatch.setattr(R, "_get_cohere", lambda: fake)

    await R.cohere_rerank("q", hits, top_k=2, use_cache=False)

    sent = fake.called_with["documents"][0]
    assert len(sent) <= R._COHERE_DOC_MAX_CHARS


@pytest.mark.asyncio
async def test_dispatcher_falls_back_to_llm_on_cohere_error(monkeypatch):
    hits = [_hit("a", 0.9), _hit("b", 0.8)]

    async def boom(*a, **k):
        raise RuntimeError("cohere down / quota exceeded")

    sentinel = [R.RerankResult(hits[0], 1.0)]

    async def fake_llm(query, h, *, top_k, use_cache):
        return sentinel

    # Ép provider=cohere + có key -> cohere_enabled True
    monkeypatch.setattr(R, "cohere_rerank", boom)
    monkeypatch.setattr(R, "llm_rerank", fake_llm)

    out = await R.rerank("q", hits, top_k=2, use_cache=False)
    assert out is sentinel  # đã fallback sang LLM


@pytest.mark.asyncio
async def test_dispatcher_uses_llm_when_provider_is_llm(monkeypatch):
    hits = [_hit("a", 0.9)]
    called = {"cohere": False, "llm": False}

    async def fake_cohere(*a, **k):
        called["cohere"] = True
        return []

    async def fake_llm(*a, **k):
        called["llm"] = True
        return []

    monkeypatch.setattr(R, "cohere_rerank", fake_cohere)
    monkeypatch.setattr(R, "llm_rerank", fake_llm)

    # provider=llm -> cohere_enabled False
    s = R.get_settings()
    monkeypatch.setattr(s.rerank, "rerank_provider", "llm")

    await R.rerank("q", hits, top_k=1, use_cache=False)
    assert called["llm"] and not called["cohere"]


@pytest.mark.asyncio
async def test_empty_and_single_hit():
    assert await R.cohere_rerank("q", [], top_k=5, use_cache=False) == []
    one = [_hit("a", 0.42)]
    out = await R.cohere_rerank("q", one, top_k=5, use_cache=False)
    assert len(out) == 1 and out[0].score == 0.42


def test_materialize_fallback_scales_missing():
    hits = [_hit("a", 0.8), _hit("b", 0.6)]
    # chỉ index 0 được rerank; index 1 thiếu -> dùng dense*0.5
    out = R._materialize(hits, {0: 0.9}, top_k=2)
    assert out[0].hit.point_id == "a" and out[0].score == 0.9
    assert out[1].hit.point_id == "b" and out[1].score == pytest.approx(0.3)
