"""Conversion and scripting share background tasks; update handlers stay sequential."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from app.session_store import InMemorySessionStore

logger = logging.getLogger("telegram_documentaries")
Work = Callable[[int], Coroutine[Any, Any, None]]


class ConversionTasks:
    """Track active and retired tasks until completion, with per-session ownership."""

    def __init__(self, store: InMemorySessionStore) -> None:
        self.store = store
        self._active: dict[int, asyncio.Task[None]] = {}
        self._all: set[asyncio.Task[None]] = set()
        self._closed = False

    def active(self, chat_id: int) -> bool:
        task = self._active.get(chat_id)
        return task is not None and not task.done()

    def start(self, chat_id: int, work: Work) -> bool:
        if self._closed or self.active(chat_id):
            return False
        version = self.store.version(chat_id)
        task = asyncio.create_task(work(version), name=f"pipeline-{chat_id}-{version}")
        self._active[chat_id] = task
        self._all.add(task)

        def finished(done: asyncio.Task[None]) -> None:
            self._all.discard(done)
            if self._active.get(chat_id) is done:
                del self._active[chat_id]
            if not done.cancelled():
                error = done.exception()
                if error is not None:
                    logger.error("Pipeline task failed chat_id=%s type=%s",
                                 chat_id, type(error).__name__)
        task.add_done_callback(finished)
        return True

    def cancel(self, chat_id: int) -> None:
        """Invalidate before cancelling; never wait on a cancellation-resistant SDK."""
        self.store.invalidate(chat_id)
        task = self._active.pop(chat_id, None)
        if task is not None:
            task.cancel()

    async def wait(self) -> None:
        """Drain currently tracked tasks (useful for deterministic offline tests)."""
        if self._all:
            await asyncio.gather(*tuple(self._all), return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        # No reset owns cleanup here. Retain file ownership while cancelling;
        # orchestrator also checks Task.cancelling() before any send or commit.
        self._active.clear()
        for task in tuple(self._all):
            task.cancel()
        if self._all:
            _, pending = await asyncio.wait(tuple(self._all), timeout=5)
            if pending:
                logger.warning("Pipeline shutdown pending count=%s", len(pending))
