"""
Long-term memory extraction. Sau N messages hoặc trigger từ API,
gọi LLM trích xuất facts về user và lưu Postgres + Qdrant.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from Legal_VietNamese.src.core.db import session_scope
from Legal_VietNamese.src.core.logging import get_logger
from Legal_VietNamese.src.db.repositories import ConversationRepo
from Legal_VietNamese.src.memory.long_term import save_fact

log = get_logger(__name__)


EXTRACTION_SYSTEM = """Bạn là trợ lý trích xuất "user facts" từ hội thoại.
- Chỉ trích xuất sự kiện/sở thích/vai trò/ngữ cảnh ổn định về USER.
- KHÔNG trích xuất nội dung hội thoại, câu hỏi, hay thông tin tạm thời.
- Trả JSON: {"facts": [{"key": "user.<slug>", "value": "...", "confidence": 0.0-1.0}]}
- Nếu không có fact đáng lưu, trả {"facts": []}.
- key dùng tiếng Anh snake_case (e.g. user.role, user.interest, user.preferred_language)."""


async def extract_facts(ctx: dict[str, Any], conversation_id_str: str) -> dict[str, Any]:
    """Trích xuất facts từ messages của conversation."""
    from Legal_VietNamese.src.llm.client import get_llm  # avoid cycle at import time

    conv_id = uuid.UUID(conversation_id_str)
    log.info("memory_extraction_start", conv_id=str(conv_id))

    async with session_scope() as session:
        repo = ConversationRepo(session)
        msgs = await repo.recent_messages(conversation_id=conv_id, n=20)
        if not msgs:
            return {"facts": 0}
        # Cần biết user/tenant
        # messages.conversation back-populates conversation -> dùng lookup riêng
        from Legal_VietNamese.src.db.models import Conversation

        conv = await session.get(Conversation, conv_id)
        if conv is None:
            return {"facts": 0}
        user_id = conv.user_id
        tenant_id = conv.tenant_id
        source_ids = [m.id for m in msgs if m.role == "user"]

    transcript = "\n".join(f"[{m.role}] {m.content}" for m in msgs)

    llm = get_llm()
    resp = await llm.complete(
        [
            {"role": "system", "content": EXTRACTION_SYSTEM},
            {"role": "user", "content": transcript[:8000]},
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        obj = json.loads(raw)
        facts = obj.get("facts", []) or []
    except json.JSONDecodeError:
        log.warning("memory_invalid_json", raw=raw[:200])
        facts = []

    saved = 0
    for f in facts:
        key = (f.get("key") or "").strip()
        value = (f.get("value") or "").strip()
        conf = float(f.get("confidence") or 0.8)
        if not key or not value:
            continue
        ok = await save_fact(
            user_id=user_id,
            tenant_id=tenant_id,
            key=key,
            value=value,
            confidence=conf,
            source_message_ids=source_ids,
        )
        if ok:
            saved += 1

    log.info("memory_extraction_done", conv_id=str(conv_id), saved=saved)
    return {"facts": saved}
