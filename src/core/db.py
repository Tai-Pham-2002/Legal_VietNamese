"""
Async SQLAlchemy engine + session management.

Pattern:
- 1 engine duy nhất per process.
- 1 sessionmaker, mỗi request lấy 1 session qua dependency `get_session`.
- Pool size cấu hình qua env; với PgBouncer (transaction mode) đặt pool_size
  nhỏ + max_overflow vừa phải để Postgres không bị flood connection.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, MappedAsDataclass

from .settings import get_settings


class Base(MappedAsDataclass, DeclarativeBase):
    """Base ORM — dùng MappedAsDataclass cho type-safe init."""


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        s = get_settings().db
        _engine = create_async_engine(
            s.postgres_dsn,
            pool_size=s.db_pool_size,
            max_overflow=s.db_max_overflow,
            pool_timeout=s.db_pool_timeout_s,
            pool_pre_ping=True,
            echo=s.db_echo,
            future=True,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Context manager: commit on success, rollback on error, always close."""
    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# FastAPI dependency
async def get_session() -> AsyncIterator[AsyncSession]:
    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None


async def healthcheck() -> dict[str, Any]:
    from sqlalchemy import text

    try:
        async with session_scope() as s:
            r = await s.execute(text("SELECT 1"))
            r.scalar_one()
        return {"ok": True}
    except Exception as e:  # pragma: no cover
        return {"ok": False, "error": str(e)}
