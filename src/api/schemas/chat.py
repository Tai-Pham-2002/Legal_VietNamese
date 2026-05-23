"""Chat / conversation schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=255)


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str
    message_count: int
    last_message_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    meta: dict[str, Any]
    tokens_in: int | None
    tokens_out: int | None
    created_at: datetime


class ChatRequest(BaseModel):
    """Body cho POST /v1/chat/{conversation_id}/messages (SSE stream)."""

    message: str = Field(min_length=1, max_length=8000)
    # Có thể chọn doc_ids cụ thể để hỏi (RAG narrow scope)
    doc_ids: list[uuid.UUID] | None = None
