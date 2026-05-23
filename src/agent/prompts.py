"""Prompt templates cho agent. Tách file để dễ A/B test."""

from __future__ import annotations

SYSTEM_ANSWER = """Bạn là trợ lý AI chuyên trả lời câu hỏi dựa trên TÀI LIỆU được cung cấp.

QUY TẮC:
1. Chỉ trả lời dựa trên TÀI LIỆU bên dưới. Nếu thông tin không có hoặc không đủ,
   nói rõ "Tôi không có thông tin về vấn đề này trong tài liệu được cung cấp."
   KHÔNG bịa thông tin.
2. Khi trích dẫn, dùng cú pháp [#1], [#2]... tương ứng số thứ tự đoạn trong TÀI LIỆU.
3. Trả lời ngắn gọn, đúng trọng tâm. Ưu tiên tiếng Việt rõ ràng.
4. Với câu hỏi pháp lý: nêu rõ điều/khoản/văn bản nếu có trong tài liệu.
5. Nếu user hỏi tiếp với "đó", "vấn đề trên"..., dùng NGỮ CẢNH HỘI THOẠI để hiểu.

NGỮ CẢNH NGƯỜI DÙNG (nếu có): nhớ vai trò / sở thích đã ghi nhận để cá nhân hoá."""


def format_context(retrieved: list[dict]) -> str:
    """Format retrieved chunks thành block đánh số."""
    if not retrieved:
        return "(Không có tài liệu liên quan)"
    parts = []
    for i, r in enumerate(retrieved, start=1):
        head = r.get("heading_path") or r.get("doc_title") or "Tài liệu"
        page = ""
        if r.get("page_from"):
            if r.get("page_to") and r["page_to"] != r["page_from"]:
                page = f", tr.{r['page_from']}–{r['page_to']}"
            else:
                page = f", tr.{r['page_from']}"
        parts.append(f"[#{i}] {head}{page}\n{r['text']}")
    return "\n\n---\n\n".join(parts)


def format_facts(facts: list[dict]) -> str:
    if not facts:
        return ""
    bullets = [f"- {f.get('key', '?')}: {f.get('value', '')}" for f in facts]
    return "NGỮ CẢNH NGƯỜI DÙNG:\n" + "\n".join(bullets)


def build_answer_messages(
    *,
    user_message: str,
    history: list[dict[str, str]],
    summary: str | None,
    facts: list[dict],
    retrieved: list[dict],
) -> list[dict[str, str]]:
    sys_parts = [SYSTEM_ANSWER]
    if summary:
        sys_parts.append(f"\nTÓM TẮT HỘI THOẠI TRƯỚC ĐÓ:\n{summary}")
    fa = format_facts(facts)
    if fa:
        sys_parts.append("\n" + fa)
    sys_parts.append("\n\nTÀI LIỆU:\n" + format_context(retrieved))

    msgs: list[dict[str, str]] = [{"role": "system", "content": "\n".join(sys_parts)}]
    # short-term history
    msgs.extend(history)
    msgs.append({"role": "user", "content": user_message})
    return msgs
