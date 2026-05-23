"""Retrieval node — gọi retrieve_and_rerank, gắn vào state."""

from __future__ import annotations

from Legal_VietNamese.src.observability.langfuse import observe
from Legal_VietNamese.src.retrieval import retrieve_and_rerank

from ..state import AgentState


@observe(name="agent_node.retrieve_docs")
async def retrieve_docs_node(state: AgentState) -> AgentState:
    chunks = await retrieve_and_rerank(
        state["user_message"],
        tenant_id=state["tenant_id"],
        user_id=state["user_id"],
        top_k_search=20,
        top_k_final=5,
        rewrite=True,
    )
    state["retrieved"] = [
        {
            "doc_id": c.doc_id,
            "chunk_id": c.chunk_id,
            "doc_title": c.doc_title,
            "heading_path": c.heading_path,
            "page_from": c.page_from,
            "page_to": c.page_to,
            "text": c.text,
            "score": c.score,
        }
        for c in chunks
    ]
    state["citations"] = [
        {
            "doc_id": c.doc_id,
            "chunk_id": c.chunk_id,
            "doc_title": c.doc_title,
            "heading_path": c.heading_path,
            "page_from": c.page_from,
            "page_to": c.page_to,
            "score": c.score,
        }
        for c in chunks
    ]
    return state
