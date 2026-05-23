"""Plain-text / Markdown parser. Coi cả file là 1 page logic."""

from __future__ import annotations

from .base import ParsedPage, ParseResult


def parse_text(data: bytes) -> ParseResult:
    text = data.decode("utf-8", errors="replace").strip()
    return ParseResult(
        pages=[ParsedPage(page_number=1, text=text)],
        markdown=text,
        meta={"format": "text"},
    )
