"""
Generate node — KHÔNG streaming ở đây (graph trả về answer cuối).

Streaming token cho client được làm ở `graph.run_agent_stream`, dùng LLM
trực tiếp với `complete_stream` để bypass cache.

Node này dùng cho non-streaming use case (eval, batch).
"""

from __future__ import annotations

from Legal_VietNamese.src.llm.client import get_llm
from Legal_VietNamese.src.observability.langfuse import observe

from ..prompts import build_answer_messages
from ..state import AgentState


@observe(name="agent_node.generate")
async def generate_node(state: AgentState) -> AgentState:
    msgs = build_answer_messages(
        user_message=state["user_message"],
        history=state.get("short_term_history", []),
        summary=state.get("summary"),
        facts=state.get("long_term_facts", []),
        retrieved=state.get("retrieved", []),
    )
    llm = get_llm()
    resp = await llm.complete(msgs, temperature=0.2, max_tokens=1500, use_cache=False)
    state["answer"] = resp.choices[0].message.content or ""
    return state
