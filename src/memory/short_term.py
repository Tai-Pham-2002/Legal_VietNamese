"""
Short-term conversation buffer trên Redis.

Tại sao Redis chứ không phải đọc Postgres mỗi lần:
- Latency Redis ~0.5ms vs Postgres ~5-10ms.
- Format buffer = list JSON line, append-only, LTRIM giữ N entries cuối.
- Mỗi conversation có TTL 24h sau message cuối -> tự dọn.

Postgres vẫn là source-of-truth — buffer chỉ là cache cho hot path.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import orjson

from Legal_VietNamese.src.core.redis import get_redis
from Legal_VietNamese.src.core.settings import get_settings


@dataclass(slots=True)
class ShortTermBuffer:
    messages: list[dict[str, Any]]

    def to_chat_format(self) -> list[dict[str, Any]]:
        """Trả về [{role, content}] cho LLM."""
        return [{"role": m["role"], "content": m["content"]} for m in self.messages]


def _key(conv_id: uuid.UUID) -> str:
    return f"conv:buf:{conv_id}"


async def append_message(conv_id: uuid.UUID, role: str, content: str) -> None:
    s = get_settings().redis
    r = get_redis()
    key = _key(conv_id)
    payload = orjson.dumps({"role": role, "content": content})

    pipe = r.pipeline()
    pipe.rpush(key, payload)
    # Giữ N messages cuối
    pipe.ltrim(key, -s.short_term_buffer_size, -1)
    pipe.expire(key, s.short_term_buffer_ttl_s)
    await pipe.execute()


async def get_buffer(conv_id: uuid.UUID) -> ShortTermBuffer:
    r = get_redis()
    raws = await r.lrange(_key(conv_id), 0, -1)
    msgs = [orjson.loads(x) for x in raws] if raws else []
    return ShortTermBuffer(messages=msgs)


async def reset_buffer(conv_id: uuid.UUID) -> None:
    r = get_redis()
    await r.delete(_key(conv_id))


async def warmup_from_db(conv_id: uuid.UUID, messages: list[dict[str, Any]]) -> None:
    """Khi buffer rỗng (Redis restart), warm từ DB."""
    s = get_settings().redis
    r = get_redis()
    key = _key(conv_id)
    if not messages:
        return
    pipe = r.pipeline()
    pipe.delete(key)
    for m in messages[-s.short_term_buffer_size :]:
        pipe.rpush(key, orjson.dumps({"role": m["role"], "content": m["content"]}))
    pipe.expire(key, s.short_term_buffer_ttl_s)
    await pipe.execute()
