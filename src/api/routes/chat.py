"""
Chat endpoint — SSE streaming với agent.

Flow:
1) Validate conversation thuộc user.
2) Persist message user vào DB + Redis buffer.
3) Stream agent qua SSE.
4) Sau khi xong, persist message assistant + enqueue extract_facts.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator

import orjson
from fastapi import APIRouter, Depends, HTTPException, status
from sse_starlette.sse import EventSourceResponse

from Legal_VietNamese.src.agent import run_agent_stream
from Legal_VietNamese.src.core.db import session_scope
from Legal_VietNamese.src.core.logging import get_logger
from Legal_VietNamese.src.db.repositories import ConversationRepo
from Legal_VietNamese.src.memory.short_term import append_message, get_buffer, warmup_from_db

from ..deps import CurrentUserDep, SessionDep, get_arq_pool, rate_limit
from ..schemas import ChatRequest

router = APIRouter()
log = get_logger(__name__)


@router.post(
    "/{conv_id}/messages",
    dependencies=[Depends(rate_limit("chat", limit=60, window_s=60))],
)
async def chat_stream(
    conv_id: uuid.UUID,
    req: ChatRequest,
    current: CurrentUserDep,
    session: SessionDep,
) -> EventSourceResponse:
    user_id, tenant_id = current
    repo = ConversationRepo(session)
    conv = await repo.get(conv_id, user_id=user_id)
    if conv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    summary = conv.summary

    # ---- persist user message + buffer ----
    user_msg = await repo.add_message(
        conversation_id=conv_id, role="user", content=req.message
    )
    await session.commit()

    # Đảm bảo buffer có dữ liệu — nếu Redis vừa restart, warm từ DB.
    buf = await get_buffer(conv_id)
    if not buf.messages:
        msgs = await repo.recent_messages(conversation_id=conv_id, n=20)
        await warmup_from_db(
            conv_id, [{"role": m.role, "content": m.content} for m in msgs]
        )
    await append_message(conv_id, "user", req.message)

    arq = await get_arq_pool()

    async def gen() -> AsyncIterator[dict]:
        t0 = time.perf_counter()
        full_answer = ""
        citations: list[dict] = []
        usage: dict = {}

        try:
            async for evt in run_agent_stream(
                user_message=req.message,
                conversation_id=conv_id,
                user_id=user_id,
                tenant_id=tenant_id,
                summary=summary,
            ):
                t = evt["type"]
                if t == "token":
                    full_answer += evt["data"]
                    yield {"event": "token", "data": evt["data"]}
                elif t == "citations":
                    citations = evt["data"]
                    yield {"event": "citations", "data": orjson.dumps(citations).decode()}
                elif t == "tool_call":
                    yield {
                        "event": "tool_call",
                        "data": orjson.dumps({"name": evt["name"]}).decode(),
                    }
                elif t == "error":
                    yield {"event": "error", "data": str(evt["data"])}
                    return
                elif t == "done":
                    usage = evt["data"].get("usage", {})
                    full_answer = evt["data"].get("answer", full_answer)
                    citations = evt["data"].get("citations", citations)

            elapsed_ms = (time.perf_counter() - t0) * 1000

            # ---- persist assistant message + buffer ----
            async with session_scope() as s2:
                await ConversationRepo(s2).add_message(
                    conversation_id=conv_id,
                    role="assistant",
                    content=full_answer,
                    meta={"citations": citations},
                    tokens_in=usage.get("prompt_tokens"),
                    tokens_out=usage.get("completion_tokens"),
                    latency_ms=elapsed_ms,
                )
            await append_message(conv_id, "assistant", full_answer)

            # ---- enqueue background memory extraction ----
            try:
                await arq.enqueue_job("extract_facts", str(conv_id))
            except Exception as e:  # pragma: no cover
                log.warning("memory_enqueue_failed", error=str(e))

            yield {
                "event": "done",
                "data": orjson.dumps(
                    {
                        "user_message_id": str(user_msg.id),
                        "citations": citations,
                        "usage": usage,
                        "latency_ms": round(elapsed_ms, 2),
                    }
                ).decode(),
            }
        except Exception as e:  # pragma: no cover
            log.exception("chat_stream_error", error=str(e))
            yield {"event": "error", "data": str(e)}

    return EventSourceResponse(gen(), ping=15)
