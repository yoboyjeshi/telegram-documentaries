"""Logging setup and handler decorators.

Keeps logging out of business logic: ``@logged`` adds structured entry/exit
logging and ``@guard_errors`` ensures a handler can never crash the process.
Secret values and full message bodies are never logged.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Callable
from typing import Any, TypeVar, cast

logger = logging.getLogger("telegram_documentaries")

FRIENDLY_ERROR = "Sorry — something went wrong on my side. Please try again."

F = TypeVar("F", bound=Callable[..., Any])


def configure_logging(level: int = logging.INFO) -> None:
    """Configure the root logger once at startup."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # The HTTP client logs full request URLs at INFO. Telegram request URLs
    # embed the bot token, so keep httpx quiet to avoid leaking secrets.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _chat_id_from(args: tuple[Any, ...]) -> int | None:
    """Best-effort extraction of a ``chat_id`` from handler args, for logging."""
    for arg in args:
        if isinstance(arg, int):
            return arg
        chat_id = getattr(arg, "chat_id", None)
        if isinstance(chat_id, int):
            return chat_id
    return None


def logged(func: F) -> F:
    """Log entry/exit of a handler without leaking bodies or secrets."""
    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            logger.info("handler=%s chat_id=%s start", func.__name__, _chat_id_from(args))
            result = await func(*args, **kwargs)
            logger.info("handler=%s chat_id=%s done", func.__name__, _chat_id_from(args))
            return result

        return cast(F, async_wrapper)

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        logger.info("handler=%s chat_id=%s start", func.__name__, _chat_id_from(args))
        result = func(*args, **kwargs)
        logger.info("handler=%s chat_id=%s done", func.__name__, _chat_id_from(args))
        return result

    return cast(F, wrapper)


def guard_errors(func: F) -> F:
    """Catch any exception from a handler: log it, return a friendly message."""
    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except Exception:
                logger.exception("handler=%s failed", func.__name__)
                return FRIENDLY_ERROR

        return cast(F, async_wrapper)

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception:
            logger.exception("handler=%s failed", func.__name__)
            return FRIENDLY_ERROR

    return cast(F, wrapper)
