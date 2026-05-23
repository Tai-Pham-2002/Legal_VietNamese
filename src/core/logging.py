"""
Structured logging dùng structlog.

Lý do:
- JSON output -> grep được, ship vào Loki/ELK dễ.
- Context vars (request_id, user_id) tự propagate qua async stack -> trace
  được toàn bộ request mà không phải truyền tay.
- Dev mode in pretty-color cho dễ đọc.
"""

from __future__ import annotations

import logging
import sys

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars, merge_contextvars
from structlog.types import EventDict, Processor

from .settings import get_settings


def _drop_color_message_key(_logger, _name, event_dict: EventDict) -> EventDict:
    # uvicorn nhét color_message khi format console -> không cần trong JSON
    event_dict.pop("color_message", None)
    return event_dict


def setup_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.app.log_level.upper(), logging.INFO)

    shared_processors: list[Processor] = [
        merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.stdlib.add_logger_name,
        _drop_color_message_key,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.app.env == "dev":
        renderer: Processor = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        # stdlib LoggerFactory: produces stdlib Loggers (with `.name`), so the
        # `add_logger_name` processor above doesn't crash. Also keeps a single
        # output path with the ProcessorFormatter bridge below.
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Bridge stdlib logging -> structlog để uvicorn/sqlalchemy/... cũng JSON hoá
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=renderer,
            foreign_pre_chain=shared_processors,
        )
    )
    root = logging.getLogger()
    # remove default handlers (uvicorn sẽ add lại) để tránh duplicate
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Giảm noise của vài lib quen thuộc
    for noisy in ("uvicorn.access", "httpx", "httpcore", "asyncpg"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


__all__ = ["setup_logging", "get_logger", "bind_contextvars", "clear_contextvars"]
