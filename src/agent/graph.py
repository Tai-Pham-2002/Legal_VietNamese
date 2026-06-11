"""
LangGraph definition.

Hai chế độ chạy:
- `run_agent` (non-stream): full graph, dùng cho batch / eval.
- `run_agent_stream` (stream): chạy 2 node đầu (memory + retrieve) qua graph,
  sau đó stream token từ LLM trực tiếp cho UX.

Tại sao tách stream: LangGraph hỗ trợ stream token nhưng tích hợp với SSE
endpoint cần custom. Chia rõ 2 path giúp code dễ hiểu hơn.

Checkpointer: Postgres - resumeable state nếu crash giữa graph.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph

from src.core.logging import get_logger
from src.core.settings import get_settings
from src.llm.client import get_llm

from .nodes.generate import generate_node
from .nodes.memory import load_memory_node
from .nodes.retrieval import retrieve_docs_node
from .prompts import build_answer_messages
from .state import AgentState

log = get_logger(__name__)

_checkpointer: AsyncPostgresSaver | None = None
_graph: Any | None = None


async def get_checkpointer() -> AsyncPostgresSaver:
    """Lazy init checkpointer + setup tables."""
    global _checkpointer
    if _checkpointer is None:
        s = get_settings().db
        # Async checkpointer dùng psycopg async
        cp = AsyncPostgresSaver.from_conn_string(
            s.sync_dsn.replace("postgresql+psycopg://", "postgresql://")
        )
        # context manager -> __aenter__
        _checkpointer = await cp.__aenter__()  # type: ignore[attr-defined]
        await _checkpointer.setup()
    return _checkpointer


def build_graph(checkpointer: Any | None = None) -> Any:
    """Compile graph. Có thể inject checkpointer (test) hoặc dùng singleton."""
    g = StateGraph(AgentState)
    g.add_node("load_memory", load_memory_node)
    g.add_node("retrieve_docs", retrieve_docs_node)
    g.add_node("generate", generate_node)

    g.add_edge(START, "load_memory")
    g.add_edge("load_memory", "retrieve_docs")
    g.add_edge("retrieve_docs", "generate")
    g.add_edge("generate", END)

    return g.compile(checkpointer=checkpointer) if checkpointer else g.compile()


async def get_graph() -> Any:
    global _graph
    if _graph is None:
        cp = await get_checkpointer()
        _graph = build_graph(cp)
    return _graph


# ---------- streaming runner ------------------------------------------------
async def run_agent_stream(
    *,
    user_message: str,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    summary: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """
    Yield events theo thứ tự:
      {"type": "tool_call", "name": "load_memory"}
      {"type": "tool_call", "name": "retrieve_docs"}
      {"type": "citations", "data": [...]}
      {"type": "token", "data": "..."}    (nhiều lần)
      {"type": "done", "data": {"answer": "...", "usage": {...}}}
    """
    state: AgentState = {
        "user_message": user_message,
        "conversation_id": conversation_id,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "summary": summary,
        "iteration": 0,
    }

    # ---- run memory + retrieve qua graph (không cần stream) ----
    yield {"type": "tool_call", "name": "load_memory"}
    state = await load_memory_node(state)

    yield {"type": "tool_call", "name": "retrieve_docs"}
    state = await retrieve_docs_node(state)

    yield {"type": "citations", "data": state.get("citations", [])}

    # ---- stream LLM directly ----
    msgs = build_answer_messages(
        user_message=user_message,
        history=state.get("short_term_history", []),
        summary=state.get("summary"),
        facts=state.get("long_term_facts", []),
        retrieved=state.get("retrieved", []),
    )

    full_text: list[str] = []
    usage: dict[str, int] = {}
    llm = get_llm()
    try:
        async for chunk in llm.complete_stream(msgs, temperature=0.2):
            if chunk.choices:
                delta = chunk.choices[0].delta
                if delta.content:
                    full_text.append(delta.content)
                    yield {"type": "token", "data": delta.content}
            # Một số provider gửi usage ở chunk cuối
            if getattr(chunk, "usage", None):
                u = chunk.usage
                usage = {
                    "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
                    "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
                    "total_tokens": getattr(u, "total_tokens", 0) or 0,
                }
    except Exception as e:
        log.exception("agent_stream_error", error=str(e))
        yield {"type": "error", "data": str(e)}
        return

    yield {
        "type": "done",
        "data": {
            "answer": "".join(full_text),
            "citations": state.get("citations", []),
            "usage": usage,
        },
    }
