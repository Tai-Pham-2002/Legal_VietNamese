"""Document + DocumentChunk repository."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Document, DocumentChunk


class DocumentRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def create(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        title: str,
        mime_type: str,
        size_bytes: int,
        checksum: str,
        storage_key: str,
    ) -> Document:
        d = Document(
            tenant_id=tenant_id,
            user_id=user_id,
            title=title[:512],
            mime_type=mime_type,
            size_bytes=size_bytes,
            checksum_sha256=checksum,
            storage_key=storage_key,
        )
        self.s.add(d)
        await self.s.flush()
        return d

    async def get(self, doc_id: uuid.UUID, *, user_id: uuid.UUID) -> Document | None:
        q = select(Document).where(Document.id == doc_id, Document.user_id == user_id)
        return (await self.s.execute(q)).scalar_one_or_none()

    async def get_internal(self, doc_id: uuid.UUID) -> Document | None:
        """Worker dùng — không filter user_id."""
        return await self.s.get(Document, doc_id)

    async def by_checksum(self, *, tenant_id: uuid.UUID, checksum: str) -> Document | None:
        q = select(Document).where(
            Document.tenant_id == tenant_id, Document.checksum_sha256 == checksum
        )
        return (await self.s.execute(q)).scalar_one_or_none()

    async def list_for_user(
        self, *, user_id: uuid.UUID, limit: int = 50, offset: int = 0
    ) -> list[Document]:
        q = (
            select(Document)
            .where(Document.user_id == user_id)
            .order_by(Document.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self.s.execute(q)).scalars().all())

    async def set_status(
        self,
        doc_id: uuid.UUID,
        status: str,
        *,
        error: str | None = None,
        n_chunks: int | None = None,
        markdown_key: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        values: dict[str, Any] = {"status": status}
        if error is not None:
            values["error"] = error
        if n_chunks is not None:
            values["n_chunks"] = n_chunks
        if markdown_key is not None:
            values["markdown_key"] = markdown_key
        if meta is not None:
            values["meta"] = meta
        if status == "indexed":
            values["indexed_at"] = datetime.now(UTC)
        await self.s.execute(update(Document).where(Document.id == doc_id).values(**values))

    # ----- chunks ---------------------------------------------------------
    async def bulk_insert_chunks(self, chunks: list[DocumentChunk]) -> None:
        self.s.add_all(chunks)
        await self.s.flush()

    async def get_chunks_by_ids(self, chunk_ids: list[uuid.UUID]) -> list[DocumentChunk]:
        if not chunk_ids:
            return []
        q = select(DocumentChunk).where(DocumentChunk.id.in_(chunk_ids))
        return list((await self.s.execute(q)).scalars().all())
