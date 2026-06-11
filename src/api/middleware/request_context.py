"""
Gắn request_id vào structlog contextvars để mọi log thuộc request có ID.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from src.core.logging import bind_contextvars, clear_contextvars, get_logger

log = get_logger("http")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        req_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        bind_contextvars(request_id=req_id, path=request.url.path, method=request.method)
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = req_id
            return response
        finally:
            # Chạy cả khi call_next raise — KHÔNG truy cập `response` ở đây
            # (có thể chưa được gán) để tránh nuốt mất exception gốc.
            elapsed_ms = (time.perf_counter() - start) * 1000
            log.info(
                "request_done",
                status=status_code,
                elapsed_ms=round(elapsed_ms, 2),
            )
            clear_contextvars()
