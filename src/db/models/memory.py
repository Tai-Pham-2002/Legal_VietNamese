"""Long-term memory — user facts."""

from __future__ import annotations

import uuid

from sqlalchemy import ARRAY, Float, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.core.db import Base

from ._mixins import TimestampMixin, uuid_pk


class UserFact(Base, TimestampMixin):
    """
    1 fact = 1 cặp (key, value) đã trích xuất từ hội thoại.
    Ví dụ: key="user.role", value="luật sư".
    Embedding của fact lưu ở Qdrant collection `memory`, qdrant_point_id link.
    """

    __tablename__ = "user_facts"
    __table_args__ = (
        Index("ix_fact_user", "user_id"),
        Index("ix_fact_user_key", "user_id", "key"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.8)
    source_message_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default_factory=list
    )
    qdrant_point_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
