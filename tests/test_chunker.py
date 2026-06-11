"""Unit tests cho chunker — đặc biệt page range phải tính PER-CHUNK (bug đã fix)."""

from __future__ import annotations

from src.ingestion.chunkers.base import chunk_document
from src.ingestion.parsers import ParseResult


def _parsed(md: str) -> ParseResult:
    # ParseResult là dataclass; chỉ cần field markdown cho chunker.
    return ParseResult(markdown=md, pages=[], meta={})


def test_page_range_is_per_chunk_not_global():
    # Tài liệu thường (không legal) trải 2 trang. Mỗi chunk phải mang đúng trang
    # của riêng nó, không phải toàn bộ [1..2].
    md = (
        "## [Page 1]\n" + ("alpha " * 400) + "\n\n"
        "## [Page 2]\n" + ("omega " * 400)
    )
    chunks = chunk_document(_parsed(md), max_tokens=200, overlap_tokens=0)
    assert len(chunks) >= 2
    pages = {(c.page_from, c.page_to) for c in chunks}
    # Nếu bug còn (global), mọi chunk sẽ là (1, 2). Sau fix phải có chunk chỉ ở
    # trang 1 hoặc chỉ trang 2.
    assert pages != {(1, 2)}
    assert any(p == (1, 1) for p in pages) or any(p == (2, 2) for p in pages)


def test_page_markers_stripped_from_text():
    md = "## [Page 5]\n" + "Nội dung điều luật quan trọng."
    chunks = chunk_document(_parsed(md), max_tokens=200, overlap_tokens=0)
    assert chunks
    for c in chunks:
        assert "[Page" not in c.text
    assert chunks[0].page_from == 5 and chunks[0].page_to == 5


def test_legal_doc_split_by_dieu():
    md = (
        "Chương I\n\n"
        "Điều 1. Phạm vi điều chỉnh\nQuy định chung.\n\n"
        "Điều 2. Đối tượng áp dụng\nÁp dụng cho mọi tổ chức.\n\n"
        "Điều 3. Giải thích từ ngữ\nCác định nghĩa.\n"
    )
    chunks = chunk_document(_parsed(md), max_tokens=800)
    # 3 Điều + 1 Chương -> coi là legal -> tách theo Điều
    assert len(chunks) == 3
    assert any("Điều 1" in (c.heading_path or "") for c in chunks)
