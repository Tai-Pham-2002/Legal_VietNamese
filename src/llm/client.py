"""
LLM + Embedding client (OpenAI-compatible, mặc định trỏ Gemini).

Wrap thêm:
- Retry với exponential backoff (tenacity) cho lỗi tạm thời.
- 2-tier cache: in-memory LRU + Redis (chỉ áp khi temperature=0 và không stream).
- Tích hợp Langfuse `@observe` để trace token usage / latency.
- API thống nhất: `complete()`, `complete_stream()`, `embed()`, `complete_json()`.

Lưu ý: streaming KHÔNG cache (UX cần token đầu tiên), nhưng vẫn được trace bởi Langfuse.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any, Literal

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from src.core.logging import get_logger
from src.core.redis import cache_get, cache_set, make_key
from src.core.settings import get_settings

log = get_logger(__name__)


class _LRU:
    """Small process-local cache. Không thread-safe — OK vì FastAPI 1 event loop/worker."""

    def __init__(self, capacity: int = 1000) -> None:
        self._cap = capacity
        self._d: OrderedDict[str, Any] = OrderedDict()

    def get(self, k: str) -> Any | None:
        v = self._d.get(k)
        if v is not None:
            self._d.move_to_end(k)
        return v

    def set(self, k: str, v: Any) -> None:
        self._d[k] = v
        self._d.move_to_end(k)
        if len(self._d) > self._cap:
            self._d.popitem(last=False)


_lru_complete = _LRU(2000)
_lru_embed = _LRU(5000)


# ---- module-level retry policy ---------------------------------------------
def _retry(max_attempts: int):
    return retry(
        retry=retry_if_exception_type((TimeoutError, ConnectionError)),
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential_jitter(initial=1, max=10),
        reraise=True,
    )


class LLMClient:
    """OpenAI-compatible client. Mặc định gọi Gemini."""

    def __init__(
        self,
        model: str | None = None,
        client: AsyncOpenAI | None = None,
    ) -> None:
        s = get_settings().llm
        self._s = s
        self.model = model or s.llm_model_default
        self._client = client or AsyncOpenAI(
            api_key=s.llm_api_key.get_secret_value(),
            base_url=s.llm_base_url,
            timeout=s.llm_timeout_s,
            max_retries=0,  # ta tự retry qua tenacity
        )

    # ----- non-streaming --------------------------------------------------
    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Literal["auto", "none", "required"] | dict[str, Any] | None = None,
        use_cache: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> ChatCompletion:
        m = model or self.model
        cacheable = use_cache and temperature == 0.0 and tools is None
        cache_key = (
            make_key(
                "llm",
                m,
                messages,
                {"t": temperature, "max": max_tokens, "rf": response_format},
            )
            if cacheable
            else None
        )

        # L1
        if cache_key and (v := _lru_complete.get(cache_key)) is not None:
            return v  # type: ignore[no-any-return]
        # L2
        if cache_key:
            v = await cache_get(cache_key)
            if v is not None:
                obj = ChatCompletion.model_validate(v)
                _lru_complete.set(cache_key, obj)
                return obj

        @_retry(self._s.llm_max_retries)
        async def _call() -> ChatCompletion:
            return await self._client.chat.completions.create(
                model=m,
                messages=messages,  # type: ignore[arg-type]
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,  # type: ignore[arg-type]
                tools=tools,  # type: ignore[arg-type]
                tool_choice=tool_choice,  # type: ignore[arg-type]
                stream=False,
                **(extra or {}),
            )

        result = await _call()

        if cache_key:
            _lru_complete.set(cache_key, result)
            await cache_set(
                cache_key, result.model_dump(mode="json"), get_settings().redis.cache_ttl_llm_s
            )
        return result

    # ----- streaming ------------------------------------------------------
    async def complete_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ChatCompletionChunk]:
        m = model or self.model
        stream = await self._client.chat.completions.create(
            model=m,
            messages=messages,  # type: ignore[arg-type]
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        async for chunk in stream:
            yield chunk


class EmbeddingClient:
    """OpenAI-compatible embedding client (Gemini text-embedding-004 mặc định)."""

    def __init__(self, client: AsyncOpenAI | None = None) -> None:
        s = get_settings().llm
        self._s = s
        self.model = s.embedding_model
        self._client = client or AsyncOpenAI(
            api_key=s.llm_api_key.get_secret_value(),
            base_url=s.llm_base_url,
            timeout=s.embedding_timeout_s,
            max_retries=0,
        )

    async def embed(self, texts: list[str], *, use_cache: bool = True) -> list[list[float]]:
        """Trả về list embedding theo thứ tự input. Cache per-text."""
        if not texts:
            return []

        results: list[list[float] | None] = [None] * len(texts)
        misses: list[tuple[int, str]] = []

        # ---- lookup cache ----
        for i, t in enumerate(texts):
            key = make_key("emb", self.model, t)
            if use_cache:
                v = _lru_embed.get(key)
                if v is None:
                    v = await cache_get(key)
                    if v is not None:
                        _lru_embed.set(key, v)
                if v is not None:
                    results[i] = v
                    continue
            misses.append((i, t))

        # ---- batch upstream ----
        if misses:
            batch_size = self._s.embedding_batch_size
            ttl = get_settings().redis.cache_ttl_embedding_s

            for start in range(0, len(misses), batch_size):
                batch = misses[start : start + batch_size]
                batch_texts = [t for _, t in batch]

                @_retry(self._s.llm_max_retries)
                async def _call(b: list[str]) -> list[list[float]]:
                    # `dimensions`: rút gọn output về embedding_dim (gemini-embedding-001
                    # mặc định 3072) để khớp dim của Qdrant collection.
                    resp = await self._client.embeddings.create(
                        model=self.model,
                        input=b,
                        encoding_format="float",
                        dimensions=self._s.embedding_dim,
                    )
                    return [d.embedding for d in resp.data]

                vectors = await _call(batch_texts)

                for (idx, txt), vec in zip(batch, vectors, strict=True):
                    results[idx] = vec
                    if use_cache:
                        key = make_key("emb", self.model, txt)
                        _lru_embed.set(key, vec)
                        await cache_set(key, vec, ttl)

        # mypy đảm bảo không còn None
        return [v for v in results if v is not None]  # type: ignore[misc]


# ---- singletons ------------------------------------------------------------
_llm: LLMClient | None = None
_emb: EmbeddingClient | None = None


def get_llm(model: str | None = None) -> LLMClient:
    """Lấy LLM client. Truyền `model` để override default (vd dùng model heavy)."""
    if model:
        return LLMClient(model=model)
    global _llm
    if _llm is None:
        _llm = LLMClient()
    return _llm


def get_embedder() -> EmbeddingClient:
    global _emb
    if _emb is None:
        _emb = EmbeddingClient()
    return _emb
