"""Document + DocumentChunk — track ingestion."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from Legal_VietNamese.src.core.db import Base

from ._mixins import TimestampMixin, uuid_pk


class Document(Base, TimestampMixin):
    """
    Metadata file. File gốc + markdown ở MinIO (storage_key, markdown_key).
    """

    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_doc_user", "user_id"),
        Index("ix_doc_tenant", "tenant_id"),
        Index("ix_doc_status", "status"),
        UniqueConstraint("tenant_id", "checksum_sha256", name="uq_doc_checksum_per_tenant"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)

    # pending | parsing | chunking | embedding | indexed | failed
    status: Mapped[str] = mapped_column(String(32), default="pending")
    n_chunks: Mapped[int | None] = mapped_column(Integer, default=None)
    markdown_key: Mapped[str | None] = mapped_column(String(512), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default_factory=dict)
    indexed_at: Mapped[datetime | None] = mapped_column(default=None)

    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        init=False,
    )


class DocumentChunk(Base, TimestampMixin):
    """
    Lưu metadata chunk + text. Vector ở Qdrant (qdrant_point_id link).
    Mục đích lưu chunk trong Postgres: phục vụ rerank/citation không phải fetch
    payload từ Qdrant mỗi lần (Postgres rẻ hơn cho fan-out).
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        Index("ix_chunk_doc", "document_id", "chunk_index"),
        UniqueConstraint("document_id", "chunk_index", name="uq_chunk_doc_idx"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    n_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path: Mapped[str | None] = mapped_column(String(1024), default=None)
    page_from: Mapped[int | None] = mapped_column(Integer, default=None)
    page_to: Mapped[int | None] = mapped_column(Integer, default=None)
    qdrant_point_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), default=None
    )

    document: Mapped[Document] = relationship(back_populates="chunks", init=False)
