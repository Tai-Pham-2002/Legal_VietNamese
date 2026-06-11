"""
Parser dispatcher. Route theo mime_type sang parser cụ thể.

Note: User chốt PDF luật text-only -> ưu tiên PyMuPDF (fast, đủ tốt).
Khi cần OCR/structure sau, swap sang Docling chỉ cần thêm 1 parser ở đây.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from src.core.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class ParsedPage:
    page_number: int  # 1-indexed
    text: str


@dataclass(slots=True)
class ParseResult:
    pages: list[ParsedPage]
    markdown: str  # joined với page markers cho retrieval citation
    meta: dict[str, str | int]


async def parse_file(data: bytes, mime_type: str, filename: str = "") -> ParseResult:
    """Async wrapper. Parser sync (PyMuPDF) -> chạy trong thread pool."""
    name = filename.lower()

    if mime_type == "application/pdf" or name.endswith(".pdf"):
        from .pdf import parse_pdf

        return await asyncio.to_thread(parse_pdf, data)

    if mime_type in ("text/plain", "text/markdown") or name.endswith((".txt", ".md")):
        from .text import parse_text

        return parse_text(data)

    if name.endswith(".docx"):
        from .docx import parse_docx

        return await asyncio.to_thread(parse_docx, data)

    # fallback
    log.warning("parser_fallback", mime=mime_type, filename=filename)
    return ParseResult(
        pages=[ParsedPage(page_number=1, text=data.decode("utf-8", errors="replace"))],
        markdown=data.decode("utf-8", errors="replace"),
        meta={"format": "raw"},
    )
