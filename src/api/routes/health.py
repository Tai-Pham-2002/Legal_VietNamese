"""Health check endpoints (liveness vs readiness)."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from src.core import db, minio, qdrant
from src.core import redis as redis_mod

router = APIRouter()


@router.get("/health/live")
async def liveness() -> dict:
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(response: Response) -> dict:
    """Check tất cả deps. Trả 200 khi all OK, 503 khi degraded.

    K8s readiness probe dựa vào HTTP status code (không đọc body), nên phải
    set 503 khi có dep down — nếu không pod degraded vẫn nhận traffic.
    """
    results = {
        "db": await db.healthcheck(),
        "redis": await redis_mod.healthcheck(),
        "qdrant": await qdrant.healthcheck(),
        "minio": await minio.healthcheck(),
    }
    ok = all(v.get("ok") for v in results.values())
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if ok else "degraded", "components": results}
