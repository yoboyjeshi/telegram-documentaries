"""Tests for Telegram wiring: guarded sends, dispatch, and app construction."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import TelegramError

from app.bot import build_application, dispatch, safe_send_message
from app.config import Settings
from app.handlers import GREETING_TEXT
from app.session_store import InMemorySessionStore
from app.states import Stage


def _update(text: str, chat_id: int = 1) -> SimpleNamespace:
    message = SimpleNamespace(text=text, chat=SimpleNamespace(id=chat_id))
    return SimpleNamespace(effective_message=message, message=message)


def test_safe_send_swallows_telegram_error() -> None:
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramError("boom")
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


def test_build_application_registers_error_handler() -> None:
    settings = Settings(telegram_bot_token="123:dummy")
    application = build_application(settings)
    assert application.error_handlers
