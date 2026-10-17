"""Handler send budgets cancel cooperative stalls and reject returning late sends.

``app.concurrent_updates == 1`` and the handlers are blocking, so every send
performed before a callback returns -- the unsupported-upload guidance, the
final reply, the friendly-error notice -- holds up every later update for that
bot, including resets in other chats. These drive the public ``dispatch`` and
``dispatch_safely`` wiring with a transport whose send waits until cancelled.
They do not prove a hard wall-clock bound on a transport that ignores cancellation
forever. The late-send fake swallows cancellation but returns, enabling expiry checks.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import bot as wiring
from app import early_flow
from app.bot import dispatch, dispatch_safely
from app.conversion_tasks import ConversionTasks
from app.handlers import GREETING_TEXT, UNSUPPORTED_UPLOAD_PHOTO_TEXT
from app.logging_config import FRIENDLY_ERROR
from app.session_store import InMemorySessionStore
from app.states import Stage

# A generous ceiling for the whole callback: the app's own send budget is set
# far below it, so only an unbounded send can reach this.
CALLBACK_BUDGET = 1
SEND_BUDGET = 0.01


class HangingSend:
    """A transport send that never completes on its own."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.texts: list[str] = []
        self._never = asyncio.Event()

    async def send(self, **kwargs) -> None:
        self.texts.append(kwargs["text"])
        self.entered.set()
        await self._never.wait()


class LateSend(HangingSend):
    """A send that swallows its deadline's cancellation and lands far too late.

    A late-return case: the cancellation is absorbed and the send
    reports success, so the deadline has to be re-checked explicitly rather
    than inferred from a raised ``CancelledError``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.cancellations = 0

    async def send(self, **kwargs) -> None:
        self.texts.append(kwargs["text"])
        self.entered.set()
        try:
            await self._never.wait()
        except asyncio.CancelledError:
            self.cancellations += 1


def _unsupported_update(chat_id: int = 1) -> SimpleNamespace:
    message = SimpleNamespace(text=None, chat=SimpleNamespace(id=chat_id))
    return SimpleNamespace(effective_message=message, message=message)


def _text_update(text: str, chat_id: int = 1) -> SimpleNamespace:
    message = SimpleNamespace(text=text, chat=SimpleNamespace(id=chat_id))
    return SimpleNamespace(effective_message=message, message=message)


def _bot_with(sender: HangingSend) -> AsyncMock:
    bot = AsyncMock()
    bot.send_message.side_effect = sender.send
    return bot


async def _within_callback_budget(callback: Awaitable[str | None]) -> str | None:
    """Await one update callback, failing loudly if a send parks it."""
    try:
        async with asyncio.timeout(CALLBACK_BUDGET) as ceiling:
            result = await callback
    except TimeoutError:
        pytest.fail("the update callback never returned: a handler send is unbounded")
    assert not ceiling.expired(), "the update callback outlived the whole-callback ceiling"
    return result


def test_unsupported_upload_guidance_send_is_bounded(monkeypatch, caplog) -> None:
    async def scenario():
        monkeypatch.setattr(early_flow, "EARLY_SEND_TIMEOUT_SECONDS", SEND_BUDGET)
        store = InMemorySessionStore()
        store.set_stage(1, Stage.AWAITING_PHOTO)
        tasks = ConversionTasks(store)
        sender = HangingSend()

        reply = await _within_callback_budget(
            dispatch(store, _unsupported_update(), _bot_with(sender), conversion_tasks=tasks)
        )

        assert reply is None
        assert sender.texts == [UNSUPPORTED_UPLOAD_PHOTO_TEXT]
        assert store.get(1).stage is Stage.AWAITING_PHOTO
        await tasks.close()

    with caplog.at_level("ERROR"):
        asyncio.run(scenario())

    assert any("TimeoutError" in record.getMessage() for record in caplog.records)
    assert all(record.exc_info is None for record in caplog.records)


def test_final_reply_send_is_bounded(monkeypatch) -> None:
    async def scenario():
        monkeypatch.setattr(early_flow, "EARLY_SEND_TIMEOUT_SECONDS", SEND_BUDGET)
        store = InMemorySessionStore()
        # Mid-generation, where a reset matters most; set directly because
        # GREETING -> BOUNCER_CHECK is not a legal transition.
        store.get(1).stage = Stage.BOUNCER_CHECK
        tasks = ConversionTasks(store)
        sender = HangingSend()

        reply = await _within_callback_budget(
            dispatch(store, _text_update("/start"), _bot_with(sender), conversion_tasks=tasks)
        )

        # The reset is committed even though its greeting never landed.
        assert reply == GREETING_TEXT
        assert sender.texts == [GREETING_TEXT]
        assert store.get(1).stage is Stage.AWAITING_PHOTO
        await tasks.close()

    asyncio.run(scenario())


def test_friendly_error_notice_send_is_bounded(monkeypatch) -> None:
    async def scenario():
        monkeypatch.setattr(early_flow, "EARLY_SEND_TIMEOUT_SECONDS", SEND_BUDGET)

        async def boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("provider down")

        monkeypatch.setattr(wiring, "dispatch", boom)
        store = InMemorySessionStore()
        sender = HangingSend()

        reply = await _within_callback_budget(
            dispatch_safely(store, _text_update("hello"), _bot_with(sender))
        )

        assert reply is None
        assert sender.texts == [FRIENDLY_ERROR]

    asyncio.run(scenario())


def test_a_send_that_outlives_its_deadline_is_reported_as_undelivered(monkeypatch) -> None:
    async def scenario():
        monkeypatch.setattr(early_flow, "EARLY_SEND_TIMEOUT_SECONDS", SEND_BUDGET)
        store = InMemorySessionStore()
        store.set_stage(1, Stage.AWAITING_PHOTO)
        sender = LateSend()
        bot = _bot_with(sender)

        delivered = await _within_callback_budget(wiring.safe_send_message(bot, 1, "a notice"))

        assert delivered is False
        assert sender.cancellations == 1  # the deadline cancelled it; it returned anyway
        # The chat is untouched: an expired send commits nothing on its own.
        assert store.get(1).stage is Stage.AWAITING_PHOTO

    asyncio.run(scenario())
