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
from inspect import isasyncgenfunction, iscoroutinefunction
from typing import Any

from src.core.logging import get_logger
from src.core.settings import get_settings

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
        from langfuse import Langfuse  # noqa: I001

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

    Lưu ý: decorator này thường được áp ở module-level (import-time), TRƯỚC khi
    `init_langfuse()` chạy. Vì vậy phải quyết định delegate vs no-op tại CALL-time
    (qua wrapper), không phải decoration-time — nếu không trace sẽ luôn bị tắt.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if not (iscoroutinefunction(fn) or isasyncgenfunction(fn)):

            @wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                wrapped = _maybe_wrap(fn, name)
                return wrapped(*args, **kwargs)

            return sync_wrapper

        @wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            wrapped = _maybe_wrap(fn, name)
            return await wrapped(*args, **kwargs)

        return async_wrapper

    return decorator


def _maybe_wrap(fn: Callable[..., Any], name: str | None) -> Callable[..., Any]:
    """Trả về fn đã wrap bởi langfuse `observe` nếu đã init, ngược lại trả fn gốc."""
    if _lf is None:
        return fn
    try:
        from langfuse.decorators import observe as lf_observe  # type: ignore[import-untyped]

        return lf_observe(name=name)(fn)  # type: ignore[no-any-return]
    except Exception:
        return fn


def trace_metadata(**kwargs: Any) -> None:
    """Gắn metadata vào trace hiện tại nếu có context Langfuse."""
    if _lf is None:
        return
    try:
        from langfuse.decorators import langfuse_context  # type: ignore[import-untyped]

        langfuse_context.update_current_observation(metadata=kwargs)
    except Exception as e:
        # Observability phải không bao giờ làm crash request -> chỉ log debug.
        log.debug("langfuse_trace_metadata_failed", error=str(e))


def trace_score(name: str, value: float, comment: str | None = None) -> None:
    if _lf is None:
        return
    try:
        from langfuse.decorators import langfuse_context  # type: ignore[import-untyped]

        langfuse_context.score_current_observation(name=name, value=value, comment=comment)
    except Exception as e:
        log.debug("langfuse_trace_score_failed", error=str(e))


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
    except Exception as e:
        log.debug("langfuse_manual_generation_failed", error=str(e))
