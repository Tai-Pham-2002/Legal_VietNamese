"""
ARQ Worker entrypoint.

Worker mở ra cùng Redis connection pool, xử lý job từ stream.
Mỗi job là 1 function async tham số `(ctx, *args)`.
"""

from __future__ import annotations

from typing import Any

from arq.connections import RedisSettings

from Legal_VietNamese.src.core.logging import setup_logging
from Legal_VietNamese.src.core.qdrant import close_qdrant, ensure_collections
from Legal_VietNamese.src.core.redis import close_redis
from Legal_VietNamese.src.core.settings import get_settings
from Legal_VietNamese.src.observability.langfuse import flush as lf_flush
from Legal_VietNamese.src.observability.langfuse import init_langfuse

from .tasks.ingestion import process_document
from .tasks.memory import extract_facts


async def startup(ctx: dict[str, Any]) -> None:
    setup_logging()
    init_langfuse()
    # Đảm bảo qdrant collections tồn tại (worker chạy ingestion cần)
    await ensure_collections()


async def shutdown(ctx: dict[str, Any]) -> None:
    lf_flush()
    await close_qdrant()
    await close_redis()


def _redis_settings() -> RedisSettings:
    """Parse REDIS_URL -> ARQ RedisSettings."""
    from urllib.parse import urlparse

    url = urlparse(get_settings().redis.redis_url)
    return RedisSettings(
        host=url.hostname or "localhost",
        port=url.port or 6379,
        database=int((url.path or "/0").lstrip("/") or 0),
        password=url.password,
    )


class WorkerSettings:
    """ARQ entry — `arq src.worker.main.WorkerSettings`."""

    redis_settings = _redis_settings()
    functions = [process_document, extract_facts]
    on_startup = startup
    on_shutdown = shutdown

    # Concurrency: tổng job đồng thời / worker. Embedding API là I/O nên cao OK,
    # nhưng PDF parse là CPU -> giữ vừa phải.
    max_jobs = 8
    job_timeout = 600  # 10 phút / job
    keep_result = 3600
    health_check_interval = 30
    queue_name = "default"
