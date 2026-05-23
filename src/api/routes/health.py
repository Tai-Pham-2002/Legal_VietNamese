"""Health check endpoints (liveness vs readiness)."""

from __future__ import annotations

from Legal_VietNamese.src.core import db, minio, qdrant
from fastapi import APIRouter

from Legal_VietNamese.src.core import redis as redis_mod

router = APIRouter()


@router.get("/health/live")
async def liveness() -> dict:
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness() -> dict:
    """Check tất cả deps. 200 chỉ khi all OK."""
    results = {
        "db": await db.healthcheck(),
        "redis": await redis_mod.healthcheck(),
        "qdrant": await qdrant.healthcheck(),
        "minio": await minio.healthcheck(),
    }
    ok = all(v.get("ok") for v in results.values())
    return {"status": "ok" if ok else "degraded", "components": results}
