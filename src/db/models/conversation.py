"""Conversation + Message — multi-turn chat persistence."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.db import Base

from ._mixins import TimestampMixin, uuid_pk


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conv_user_updated", "user_id", "updated_at"),
        Index("ix_conv_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), default="New conversation")
    # Tóm tắt rolling khi buffer > N — giảm token cho prompt.
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    archived: Mapped[bool] = mapped_column(default=False)
    last_message_at: Mapped[datetime | None] = mapped_column(default=None)

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        init=False,
    )


class Message(Base, TimestampMixin):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_msg_conv_created", "conversation_id", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user|assistant|tool|system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # citations / tool calls / token usage / model info đi vào meta
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default_factory=dict)
    tokens_in: Mapped[int | None] = mapped_column(Integer, default=None)
    tokens_out: Mapped[int | None] = mapped_column(Integer, default=None)
    latency_ms: Mapped[float | None] = mapped_column(Float, default=None)

    conversation: Mapped[Conversation] = relationship(back_populates="messages", init=False)
