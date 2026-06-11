"""Conversation + Message repository."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Conversation, Message


class ConversationRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    # ----- conversations --------------------------------------------------
    async def create(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, title: str = "New conversation"
    ) -> Conversation:
        c = Conversation(tenant_id=tenant_id, user_id=user_id, title=title)
        self.s.add(c)
        await self.s.flush()
        return c

    async def get(self, conv_id: uuid.UUID, *, user_id: uuid.UUID) -> Conversation | None:
        q = select(Conversation).where(
            Conversation.id == conv_id, Conversation.user_id == user_id
        )
        return (await self.s.execute(q)).scalar_one_or_none()

    async def list_for_user(
        self, *, user_id: uuid.UUID, limit: int = 50, offset: int = 0
    ) -> list[Conversation]:
        q = (
            select(Conversation)
            .where(Conversation.user_id == user_id, Conversation.archived.is_(False))
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self.s.execute(q)).scalars().all())

    async def archive(self, conv_id: uuid.UUID, user_id: uuid.UUID) -> None:
        await self.s.execute(
            update(Conversation)
            .where(Conversation.id == conv_id, Conversation.user_id == user_id)
            .values(archived=True)
        )

    async def rename(self, conv_id: uuid.UUID, user_id: uuid.UUID, title: str) -> None:
        await self.s.execute(
            update(Conversation)
            .where(Conversation.id == conv_id, Conversation.user_id == user_id)
            .values(title=title[:255])
        )

    async def set_summary(self, conv_id: uuid.UUID, summary: str) -> None:
        await self.s.execute(
            update(Conversation).where(Conversation.id == conv_id).values(summary=summary)
        )

    # ----- messages -------------------------------------------------------
    async def add_message(
        self,
        *,
        conversation_id: uuid.UUID,
        role: str,
        content: str,
        meta: dict[str, Any] | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        latency_ms: float | None = None,
    ) -> Message:
        m = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            meta=meta or {},
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
        )
        self.s.add(m)
        # update conversation counters
        conv = await self.s.get(Conversation, conversation_id)
        if conv:
            conv.message_count = (conv.message_count or 0) + 1
            conv.last_message_at = datetime.now(UTC)
        await self.s.flush()
        return m

    async def messages(
        self, *, conversation_id: uuid.UUID, limit: int = 50
    ) -> list[Message]:
        q = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc())
            .limit(limit)
        )
        return list((await self.s.execute(q)).scalars().all())

    async def recent_messages(
        self, *, conversation_id: uuid.UUID, n: int
    ) -> list[Message]:
        """N message gần nhất, theo thứ tự cũ -> mới (cho prompt)."""
        q = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc())
            .limit(n)
        )
        rows = list((await self.s.execute(q)).scalars().all())
        return list(reversed(rows))
