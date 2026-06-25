"""
FastAPI dependencies — auth, current user, ARQ pool, session.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.cache import RateLimitExceeded, allow_request
from src.core.db import get_session
from src.core.security import TokenPayload, decode_token
from src.core.settings import get_settings
from src.db.repositories import UserRepo

# ----- ARQ pool singleton (1 per process) -----------------------------------
_arq_pool: ArqRedis | None = None


def _redis_settings() -> RedisSettings:
    from urllib.parse import urlparse

    url = urlparse(get_settings().redis.redis_url)
    return RedisSettings(
        host=url.hostname or "localhost",
        port=url.port or 6379,
        database=int((url.path or "/0").lstrip("/") or 0),
        password=url.password,
    )


async def get_arq_pool() -> ArqRedis:
    global _arq_pool
    if _arq_pool is None:
        _arq_pool = await create_pool(_redis_settings())
    return _arq_pool


async def close_arq_pool() -> None:
    global _arq_pool
    if _arq_pool is not None:
        await _arq_pool.aclose()
        _arq_pool = None


# ----- auth -----------------------------------------------------------------
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    return authorization.split(" ", 1)[1].strip()


async def current_token(
    authorization: Annotated[str | None, Header()] = None,
) -> TokenPayload:
    token = _bearer(authorization)
    try:
        payload = decode_token(token)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token") from e
    if payload.typ != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="wrong token type")
    return payload


CurrentTokenDep = Annotated[TokenPayload, Depends(current_token)]


async def current_user(
    token: CurrentTokenDep,
    session: SessionDep,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Return (user_id, tenant_id). Cheap path — không load full ORM."""
    user_id = uuid.UUID(token.sub)
    tenant_id = uuid.UUID(token.tid)
    # Optional: kiểm tra is_active
    repo = UserRepo(session)
    u = await repo.by_id(user_id)
    if u is None or not u.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user inactive")
    return user_id, tenant_id


CurrentUserDep = Annotated[tuple[uuid.UUID, uuid.UUID], Depends(current_user)]


# ----- rate-limit dependency (factory) --------------------------------------
def rate_limit(bucket: str, limit: int, window_s: int):
    async def _dep(current: CurrentUserDep) -> None:
        user_id, _ = current
        try:
            await allow_request(str(user_id), bucket, limit=limit, window_s=window_s)
        except RateLimitExceeded as e:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"rate limited; retry after {e.retry_after_s:.1f}s",
                headers={"Retry-After": str(int(e.retry_after_s) + 1)},
            ) from e

    return _dep
