"""
DOCX parser nhẹ — dùng zipfile + xml parsing để tránh thêm dependency.
Đủ cho legal docs đơn giản; cần phức tạp hơn thì dùng python-docx.
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

from .base import ParsedPage, ParseResult

_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def parse_docx(data: bytes) -> ParseResult:
    paragraphs: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        with z.open("word/document.xml") as f:
            # NOTE(security): parses user-uploaded XML. stdlib ElementTree is used
            # by design (no extra dep). For hardening against entity-expansion
            # (billion-laughs) DoS, swap to `defusedxml.ElementTree.parse`.
            tree = ET.parse(f)  # noqa: S314 — accepted risk; see note above
        for p in tree.iter(f"{{{_NS['w']}}}p"):
            texts = [t.text or "" for t in p.iter(f"{{{_NS['w']}}}t")]
            line = "".join(texts).strip()
            if line:
                paragraphs.append(line)

    text = "\n\n".join(paragraphs)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return ParseResult(
        pages=[ParsedPage(page_number=1, text=text)],
        markdown=text,
        meta={"format": "docx"},
    )
