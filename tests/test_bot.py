"""Tests for Telegram wiring: guarded sends, dispatch, and app construction."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import TelegramError
from telegram.ext import CommandHandler, MessageHandler

from app import bot as bot_module
from app.bot import build_application, dispatch, dispatch_safely, safe_send_message
from app.config import Settings
from app.handlers import GREETING_TEXT
from app.logging_config import FRIENDLY_ERROR
from app.session_store import InMemorySessionStore
from app.states import Stage


def _update(text: str, chat_id: int = 1) -> SimpleNamespace:
    message = SimpleNamespace(text=text, chat=SimpleNamespace(id=chat_id))
    return SimpleNamespace(effective_message=message, message=message)


def test_safe_send_swallows_telegram_error() -> None:
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramError("boom")
    assert asyncio.run(safe_send_message(bot, 1, "hi")) is False


def test_safe_send_swallows_unexpected_exception() -> None:
    """A send failure that is not a TelegramError must still never crash the bot."""
    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("unexpected network failure")
    assert asyncio.run(safe_send_message(bot, 1, "hi")) is False


def test_dispatch_start_sends_greeting() -> None:
    store = InMemorySessionStore()
    bot = AsyncMock()
    reply = asyncio.run(dispatch(store, _update("/start"), bot))
    assert reply == GREETING_TEXT
    bot.send_message.assert_awaited_once()
    assert store.get(1).stage is Stage.GREETING


def test_dispatch_restart_resets_from_any_stage() -> None:
    store = InMemorySessionStore()
    store.get(1).stage = Stage.INTERVIEW
    bot = AsyncMock()
    reply = asyncio.run(dispatch(store, _update("/restart"), bot))
    assert reply == GREETING_TEXT
    assert store.get(1).stage is Stage.GREETING


def test_dispatch_echoes_text() -> None:
    store = InMemorySessionStore()
    bot = AsyncMock()
    assert asyncio.run(dispatch(store, _update("hello"), bot)) == "hello"
    bot.send_message.assert_awaited_once()


def test_dispatch_survives_send_failure() -> None:
    store = InMemorySessionStore()
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramError("nope")
    assert asyncio.run(dispatch(store, _update("hi"), bot)) == "hi"


def test_dispatch_survives_unexpected_send_failure() -> None:
    store = InMemorySessionStore()
    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("nope")
    assert asyncio.run(dispatch(store, _update("hi"), bot)) == "hi"


def test_dispatch_warns_on_unsupported_message_kind(caplog) -> None:
    store = InMemorySessionStore()
    bot = AsyncMock()
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(dispatch(store, _update(None), bot)) is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings
    assert any("other" in r.getMessage() for r in warnings)


def test_dispatch_safely_returns_reply_on_success() -> None:
    store = InMemorySessionStore()
    bot = AsyncMock()
    assert asyncio.run(dispatch_safely(store, _update("hi"), bot)) == "hi"
    bot.send_message.assert_awaited_once()


def test_dispatch_safely_sends_friendly_message_on_failure(monkeypatch) -> None:
    store = InMemorySessionStore()
    bot = AsyncMock()

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(bot_module, "dispatch", boom)

    assert asyncio.run(dispatch_safely(store, _update("hi"), bot)) is None
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["text"] == FRIENDLY_ERROR


def test_build_application_registers_error_handler() -> None:
    settings = Settings(telegram_bot_token="123:dummy")
    application = build_application(settings)
    assert application.error_handlers


def test_build_application_registers_start_restart_and_text() -> None:
    settings = Settings(telegram_bot_token="123:dummy")
    application = build_application(settings)

    registered_commands: set[str] = set()
    has_text_handler = False
    for handler in application.handlers[0]:
        if isinstance(handler, CommandHandler):
            registered_commands.update(handler.commands)
        elif isinstance(handler, MessageHandler):
            has_text_handler = True

    assert {"start", "restart"} <= registered_commands
    assert has_text_handler
