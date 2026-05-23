"""Agent state — TypedDict cho LangGraph."""

from __future__ import annotations

import uuid
from typing import Any, TypedDict


class Citation(TypedDict):
    doc_id: str
    chunk_id: str
    doc_title: str
    heading_path: str | None
    page_from: int | None
    page_to: int | None
    score: float


class RetrievedDoc(TypedDict):
    doc_id: str
    chunk_id: str
    doc_title: str
    heading_path: str | None
    page_from: int | None
    page_to: int | None
    text: str
    score: float


class AgentState(TypedDict, total=False):
    # ---- input ----
    user_message: str
    conversation_id: uuid.UUID
    user_id: uuid.UUID
    tenant_id: uuid.UUID

    # ---- context ----
    short_term_history: list[dict[str, str]]  # [{"role", "content"}]
    summary: str | None
    long_term_facts: list[dict[str, Any]]

    # ---- retrieval ----
    rewritten_queries: list[str]
    retrieved: list[RetrievedDoc]
    citations: list[Citation]

    # ---- output ----
    answer: str

    # ---- control ----
    iteration: int
    needs_more_info: bool
    error: str | None
