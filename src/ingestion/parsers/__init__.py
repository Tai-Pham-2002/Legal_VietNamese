"""Parsers — chuyển file binary thành markdown/plain text + metadata page."""

from .base import ParsedPage, ParseResult, parse_file

__all__ = ["ParsedPage", "ParseResult", "parse_file"]
