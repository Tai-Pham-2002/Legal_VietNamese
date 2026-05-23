"""Memory node — load short-term buffer + retrieve long-term facts."""

from __future__ import annotations

from Legal_VietNamese.src.memory import get_buffer, retrieve_user_facts
from Legal_VietNamese.src.observability.langfuse import observe

from ..state import AgentState


@observe(name="agent_node.load_memory")
async def load_memory_node(state: AgentState) -> AgentState:
    buf = await get_buffer(state["conversation_id"])
    state["short_term_history"] = buf.to_chat_format()
    facts = await retrieve_user_facts(
        user_id=state["user_id"], query=state["user_message"], top_k=5
    )
    state["long_term_facts"] = facts
    return state
