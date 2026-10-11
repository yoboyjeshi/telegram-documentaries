"""Tests for the Telegram update adapter (typed boundary)."""

from __future__ import annotations

from types import SimpleNamespace

from app.telegram_adapter import extract_chat_input


def _update(text: object, chat_id: object = 123) -> SimpleNamespace:
    message = SimpleNamespace(text=text, chat=SimpleNamespace(id=chat_id))
    return SimpleNamespace(effective_message=message, message=message)


def test_command_is_parsed() -> None:
    parsed = extract_chat_input(_update("/start"))
    assert parsed is not None
    assert parsed.kind == "command"
    assert parsed.command == "start"
    assert parsed.chat_id == 123


def test_command_strips_bot_username() -> None:
    parsed = extract_chat_input(_update("/restart@MyBot"))
    assert parsed is not None
    assert parsed.command == "restart"


def test_plain_text_is_parsed() -> None:
    parsed = extract_chat_input(_update("hello there"))
    assert parsed is not None
    assert parsed.kind == "text"
    assert parsed.text == "hello there"


def test_unsupported_update_returns_none() -> None:
    update = SimpleNamespace(effective_message=None, message=None)
    assert extract_chat_input(update) is None


def test_chat_id_is_normalised_to_int() -> None:
    parsed = extract_chat_input(_update("hi", chat_id="456"))
    assert parsed is not None
    assert parsed.chat_id == 456
    assert isinstance(parsed.chat_id, int)
