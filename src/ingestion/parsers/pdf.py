"""
PDF parser dùng PyMuPDF (fitz). Chiến lược:
- Mỗi page -> string text (preserve newline).
- Markdown output kèm marker `## [Page N]` để chunker / citation track được page.
- Nếu page rỗng (scanned PDF) -> giữ entry rỗng, downstream sẽ skip.

Hiện tại không OCR theo yêu cầu user (legal docs text-only). Khi cần OCR,
thêm fallback `pytesseract` ở đây.
"""

from __future__ import annotations

import io

import pymupdf  # PyMuPDF

from .base import ParsedPage, ParseResult


def parse_pdf(data: bytes) -> ParseResult:
    pages: list[ParsedPage] = []
    md_parts: list[str] = []

    with pymupdf.open(stream=io.BytesIO(data), filetype="pdf") as doc:
        n_pages = doc.page_count
        for i in range(n_pages):
            page = doc.load_page(i)
            # "text" giữ flow đọc tốt; "blocks" giúp lấy bbox nhưng phức tạp hơn
            text = page.get_text("text") or ""
            text = text.strip()
            pages.append(ParsedPage(page_number=i + 1, text=text))
            if text:
                md_parts.append(f"## [Page {i + 1}]\n\n{text}")

        meta: dict[str, str | int] = {
            "format": "pdf",
            "n_pages": n_pages,
            "title": (doc.metadata or {}).get("title", "") or "",
            "author": (doc.metadata or {}).get("author", "") or "",
        }

    return ParseResult(pages=pages, markdown="\n\n".join(md_parts), meta=meta)
