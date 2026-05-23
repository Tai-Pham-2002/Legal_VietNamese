"""
Redis client wrapper — 1 connection pool, async, dùng cho:
- Cache (LLM, embedding, retrieval).
- Short-term memory buffer (conversation).
- Pub/Sub (progress events).
- Rate limiting (sliding window).

Lý do tách wrapper:
- Inject ở 1 chỗ -> test mock dễ.
- Helper `cache_get/cache_set` orjson-encode + TTL chuẩn.
"""

from __future__ import annotations

import hashlib
from typing import Any

import orjson
from redis.asyncio import ConnectionPool, Redis

from .settings import get_settings

_pool: ConnectionPool | None = None
_redis: Redis | None = None


def get_redis() -> Redis:
    global _pool, _redis
    if _redis is None:
        s = get_settings().redis
        _pool = ConnectionPool.from_url(
            s.redis_url,
            max_connections=s.redis_max_connections,
            decode_responses=False,  # binary -> orjson handle
        )
        _redis = Redis(connection_pool=_pool)
    return _redis


async def close_redis() -> None:
    global _redis, _pool
    if _redis is not None:
        await _redis.aclose()
        _redis = None
    if _pool is not None:
        await _pool.disconnect()
        _pool = None


def make_key(namespace: str, *parts: Any) -> str:
    """
    Build cache key. Parts được hash sha256 nếu là string dài,
    giúp tránh key quá dài / chứa ký tự lạ.
    """
    h = hashlib.sha256()
    for p in parts:
        if isinstance(p, (bytes, bytearray)):
            h.update(p)
        elif isinstance(p, str):
            h.update(p.encode("utf-8"))
        else:
            h.update(orjson.dumps(p, option=orjson.OPT_SORT_KEYS))
        h.update(b"|")
    return f"{namespace}:{h.hexdigest()}"


async def cache_get(key: str) -> Any | None:
    r = get_redis()
    raw = await r.get(key)
    if raw is None:
        return None
    return orjson.loads(raw)


async def cache_set(key: str, value: Any, ttl_s: int) -> None:
    r = get_redis()
    await r.set(key, orjson.dumps(value), ex=ttl_s)


async def cache_del(key: str) -> None:
    r = get_redis()
    await r.delete(key)


async def healthcheck() -> dict[str, Any]:
    try:
        r = get_redis()
        pong = await r.ping()
        return {"ok": bool(pong)}
    except Exception as e:  # pragma: no cover
        return {"ok": False, "error": str(e)}
