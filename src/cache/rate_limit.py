"""
Sliding-window rate limiter trên Redis sorted-set.

Tại sao sliding window thay vì fixed bucket:
- Tránh "burst" tại biên window (vd fix 60req/phút thì có thể bắn 60 cuối phút N +
  60 đầu phút N+1 = 120req trong 2 giây).
- Trade-off: cost ~1 ZADD + ZREMRANGE + ZCARD mỗi request. Pipeline atomic OK.
"""

from __future__ import annotations

import time
import uuid

from src.core.redis import get_redis


class RateLimitExceeded(Exception):  # noqa: N818 — deliberate domain name, not an *Error
    def __init__(self, retry_after_s: float):
        self.retry_after_s = retry_after_s
        super().__init__(f"rate limit exceeded, retry after {retry_after_s:.1f}s")


async def allow_request(
    user_id: str,
    bucket: str,
    *,
    limit: int,
    window_s: int,
) -> tuple[bool, int]:
    """
    Trả (allowed, remaining). Nếu allowed=False, raise lên caller có thể chọn.
    """
    r = get_redis()
    key = f"rl:{bucket}:{user_id}"
    now = time.time()
    cutoff = now - window_s

    pipe = r.pipeline()
    pipe.zremrangebyscore(key, 0, cutoff)
    pipe.zcard(key)
    pipe.zadd(key, {f"{now}-{uuid.uuid4().hex[:8]}": now})
    pipe.expire(key, window_s + 5)
    _, count, _, _ = await pipe.execute()

    if count >= limit:
        # đảo: vừa add, lấy entry cũ nhất để biết retry
        oldest = await r.zrange(key, 0, 0, withscores=True)
        retry_after = window_s - (now - oldest[0][1]) if oldest else float(window_s)
        # rollback bằng xoá entry vừa add (best-effort)
        await r.zremrangebyscore(key, now, now)
        raise RateLimitExceeded(max(0.1, retry_after))
    return True, max(0, limit - count - 1)
