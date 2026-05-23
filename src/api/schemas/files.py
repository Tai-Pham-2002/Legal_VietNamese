"""File / document schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class DocumentOut(BaseModel):
    id: uuid.UUID
    title: str
    mime_type: str
    size_bytes: int
    status: str
    n_chunks: int | None
    error: str | None
    created_at: datetime
    indexed_at: datetime | None


class UploadResponse(BaseModel):
    documents: list[DocumentOut]
    job_ids: list[str]
