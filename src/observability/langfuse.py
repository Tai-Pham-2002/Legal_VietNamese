"""
Langfuse wrapper.

Lý do tách:
- Nếu Langfuse chưa cấu hình (dev/local), no-op tránh app crash.
- Cho phép flush khi shutdown để không mất trace cuối.
- `observe` decorator dùng trong agent nodes / retrieval functions để có trace tự động.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from Legal_VietNamese.src.core.logging import get_logger
from Legal_VietNamese.src.core.settings import get_settings

log = get_logger(__name__)

_lf: Any = None


def init_langfuse() -> None:
    """Khởi tạo Langfuse global client nếu được cấu hình."""
    global _lf
    s = get_settings().langfuse
    if not s.is_configured:
        log.info("langfuse_disabled")
        return
    try:
        from Legal_VietNamese.src.observability.langfuse import Langfuse  # noqa: I001

        _lf = Langfuse(
            host=s.langfuse_host,
            public_key=s.langfuse_public_key.get_secret_value(),  # type: ignore[union-attr]
            secret_key=s.langfuse_secret_key.get_secret_value(),  # type: ignore[union-attr]
        )
        log.info("langfuse_initialized", host=s.langfuse_host)
    except Exception as e:
        log.warning("langfuse_init_failed", error=str(e))
        _lf = None


def get_langfuse() -> Any | None:
    return _lf


def flush() -> None:
    if _lf is not None:
        try:
            _lf.flush()
        except Exception as e:  # pragma: no cover
            log.warning("langfuse_flush_failed", error=str(e))


def observe(name: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    Decorator nhẹ — nếu langfuse có thì delegate sang `@langfuse.observe`,
    không thì no-op.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if _lf is None:
            return fn
        try:
            from langfuse.decorators import observe as lf_observe  # type: ignore[import-untyped]

            return lf_observe(name=name)(fn)
        except Exception:
            return fn

    return decorator


def trace_metadata(**kwargs: Any) -> None:
    """Gắn metadata vào trace hiện tại nếu có context Langfuse."""
    if _lf is None:
        return
    try:
        from langfuse.decorators import langfuse_context  # type: ignore[import-untyped]

        langfuse_context.update_current_observation(metadata=kwargs)
    except Exception:
        pass


def trace_score(name: str, value: float, comment: str | None = None) -> None:
    if _lf is None:
        return
    try:
        from langfuse.decorators import langfuse_context  # type: ignore[import-untyped]

        langfuse_context.score_current_observation(name=name, value=value, comment=comment)
    except Exception:
        pass


# Wrap helper: cung cấp 1 decorator để inline trace 1 LLM call thủ công
def manual_generation(
    name: str,
    *,
    model: str,
    input: Any,
    output: Any,
    usage: dict[str, int] | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    if _lf is None:
        return
    try:
        _lf.generation(
            name=name,
            model=model,
            input=input,
            output=output,
            usage=usage,
            metadata=metadata,
        )
    except Exception:
        pass
