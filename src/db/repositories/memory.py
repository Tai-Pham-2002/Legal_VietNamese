"""User fact repository."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from Legal_VietNamese.src.db.models import UserFact


class UserFactRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def add(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        key: str,
        value: str,
        confidence: float = 0.8,
        source_message_ids: list[uuid.UUID] | None = None,
        qdrant_point_id: uuid.UUID | None = None,
    ) -> UserFact:
        f = UserFact(
            user_id=user_id,
            tenant_id=tenant_id,
            key=key[:128],
            value=value,
            confidence=confidence,
            source_message_ids=source_message_ids or [],
            qdrant_point_id=qdrant_point_id,
        )
        self.s.add(f)
        await self.s.flush()
        return f

    async def list_for_user(self, user_id: uuid.UUID, limit: int = 100) -> list[UserFact]:
        q = (
            select(UserFact)
            .where(UserFact.user_id == user_id)
            .order_by(UserFact.updated_at.desc())
            .limit(limit)
        )
        return list((await self.s.execute(q)).scalars().all())

    async def get_by_ids(self, ids: list[uuid.UUID]) -> list[UserFact]:
        if not ids:
            return []
        q = select(UserFact).where(UserFact.id.in_(ids))
        return list((await self.s.execute(q)).scalars().all())
