"""
FastAPI app factory + lifespan.

Lifespan:
- Setup logging + Langfuse.
- Ensure Qdrant collections + MinIO bucket.
- Init ARQ pool (lazy).
- On shutdown: dispose engine, close redis, qdrant, ARQ.

Middleware:
- CORS (config).
- Request context (request_id, structlog binding).
- Prometheus instrumentation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from src.core.db import dispose_engine
from src.core.logging import get_logger, setup_logging
from src.core.minio import ensure_bucket
from src.core.qdrant import close_qdrant, ensure_collections
from src.core.redis import close_redis
from src.core.settings import get_settings
from src.observability.langfuse import flush as lf_flush
from src.observability.langfuse import init_langfuse

from .deps import close_arq_pool, get_arq_pool
from .middleware.request_context import RequestContextMiddleware
from .routes import api_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    log = get_logger(__name__)
    log.info("api_starting")

    init_langfuse()

    # bootstrap external deps (idempotent)
    await ensure_bucket()
    await ensure_collections()
    await get_arq_pool()

    log.info("api_ready")
    try:
        yield
    finally:
        log.info("api_shutting_down")
        lf_flush()
        await close_arq_pool()
        await close_redis()
        await close_qdrant()
        await dispose_engine()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title="Production Agentic RAG",
        version="0.1.0",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
        default_response_class=ORJSONResponse,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.app.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestContextMiddleware)

    # Prometheus /metrics (chỉ expose nếu env != prod hoặc dùng auth khác)
    Instrumentator(
        excluded_handlers=["/health/live", "/health/ready", "/metrics"]
    ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

    app.include_router(api_router)
    return app


app = create_app()
