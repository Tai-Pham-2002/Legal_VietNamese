"""
Chunking strategy.

Bước 1: detect legal structure (Chương / Điều / Khoản).
Bước 2: split theo Điều — semantic boundary tự nhiên.
Bước 3: nếu Điều quá dài, fallback recursive splitter.
Bước 4: gắn metadata `heading_path`, `page_from/to`.

Lưu ý: dùng tiktoken `cl100k_base` để đếm token (xấp xỉ với Gemini).
Quan trọng nhất là consistency, không phải accuracy tuyệt đối.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import tiktoken
from langchain_text_splitters import RecursiveCharacterTextSplitter

from src.ingestion.parsers import ParseResult

# ---- token counter (lazy) --------------------------------------------------
_enc: tiktoken.Encoding | None = None


def _encoder() -> tiktoken.Encoding:
    global _enc
    if _enc is None:
        _enc = tiktoken.get_encoding("cl100k_base")
    return _enc


def count_tokens(text: str) -> int:
    return len(_encoder().encode(text))


# ---- chunk type ------------------------------------------------------------
@dataclass(slots=True)
class Chunk:
    index: int
    text: str
    n_tokens: int
    heading_path: str | None = None
    page_from: int | None = None
    page_to: int | None = None
    meta: dict[str, str | int] = field(default_factory=dict)


# ---- legal detectors -------------------------------------------------------
_RE_PAGE = re.compile(r"^##\s+\[Page\s+(\d+)\]\s*$", re.MULTILINE)
_RE_CHUONG = re.compile(r"^\s*Chương\s+([IVXLCDM\d]+)\b.*$", re.MULTILINE | re.IGNORECASE)
_RE_DIEU = re.compile(r"^\s*Điều\s+(\d+)\b.*$", re.MULTILINE)
_RE_KHOAN = re.compile(r"^\s*(\d+)\.\s", re.MULTILINE)


def _is_legal_doc(md: str) -> bool:
    """Heuristic: nếu có ít nhất 3 'Điều' và 1 'Chương' thì coi là legal."""
    return len(_RE_DIEU.findall(md)) >= 3 and bool(_RE_CHUONG.search(md))


def _split_by_dieu(md: str) -> list[tuple[str, str]]:
    """
    Trả về list (heading, body). Heading bao gồm cả 'Chương X' nếu có ở trên.
    Body có thể empty.
    """
    matches = list(_RE_DIEU.finditer(md))
    if not matches:
        return [("", md)]

    chunks: list[tuple[str, str]] = []

    # phần trước Điều đầu tiên (preamble / Chương) -> prepend vào Điều đầu
    preamble = md[: matches[0].start()].strip()

    # Tìm Chương mapping theo position
    chuong_positions = [(m.start(), m.group(0).strip()) for m in _RE_CHUONG.finditer(md)]

    def _chuong_at(pos: int) -> str:
        current = ""
        for p, h in chuong_positions:
            if p <= pos:
                current = h
            else:
                break
        return current

    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        section = md[start:end].strip()

        # Heading: tách dòng đầu của Điều
        first_line, _, body = section.partition("\n")
        chuong = _chuong_at(start)
        heading_path = " > ".join(p for p in (chuong, first_line.strip()) if p)
        full_body = (preamble + "\n\n" + body).strip() if i == 0 and preamble else body.strip()
        chunks.append((heading_path, full_body if full_body else first_line))
    return chunks


def _page_range_for(text: str) -> tuple[int | None, int | None]:
    """Lấy page_from / page_to dựa trên các marker `## [Page N]` trong text."""
    pages = [int(m.group(1)) for m in _RE_PAGE.finditer(text)]
    if not pages:
        return None, None
    return min(pages), max(pages)


def _strip_page_markers(text: str) -> str:
    return _RE_PAGE.sub("", text).strip()


# ---- main chunker ----------------------------------------------------------
def chunk_document(
    parsed: ParseResult,
    *,
    max_tokens: int = 800,
    overlap_tokens: int = 100,
) -> list[Chunk]:
    md = parsed.markdown
    is_legal = _is_legal_doc(md)

    fallback = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        encoding_name="cl100k_base",
        chunk_size=max_tokens,
        chunk_overlap=overlap_tokens,
        separators=["\n\n", "\n", ". ", " "],
    )

    chunks: list[Chunk] = []
    idx = 0
    last_page: int | None = None  # trang gần nhất đã thấy, để chunk giữa 2 marker kế thừa

    def _emit(text: str, heading: str | None) -> None:
        nonlocal idx, last_page
        # Tính page range TỪ text của chunk này, TRƯỚC khi strip marker. Tính trên
        # toàn bộ parsed.markdown thì mọi chunk sẽ nhận cùng range -> citation sai.
        # Marker có thể bị splitter tách ra mảnh riêng (rồi strip thành rỗng), nên
        # nếu chunk này không chứa marker thì kế thừa trang gần nhất (`last_page`).
        # Cập nhật last_page TRƯỚC khi strip/return để mảnh marker-only vẫn ghi nhận.
        pf, pt = _page_range_for(text)
        if pt is not None:
            last_page = pt
        else:
            pf = pt = last_page
        text = _strip_page_markers(text).strip()
        if not text:
            return
        chunks.append(
            Chunk(
                index=idx,
                text=text,
                n_tokens=count_tokens(text),
                heading_path=heading,
                page_from=pf,
                page_to=pt,
            )
        )
        idx += 1

    if is_legal:
        sections = _split_by_dieu(md)
        for heading, body in sections:
            text = body if not heading else f"{heading}\n\n{body}"
            n_tok = count_tokens(text)
            if n_tok <= max_tokens:
                _emit(text, heading or None)
            else:
                # Quá dài: split tiếp bằng recursive splitter, giữ heading ở đầu mỗi sub-chunk
                pieces = fallback.split_text(body)
                for piece in pieces:
                    sub_text = f"{heading}\n\n{piece}" if heading else piece
                    _emit(sub_text, heading or None)
    else:
        pieces = fallback.split_text(md)
        for piece in pieces:
            _emit(piece, None)

    return chunks
