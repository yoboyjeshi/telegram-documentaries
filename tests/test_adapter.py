"""Tests for the Telegram update adapter (typed boundary)."""

from __future__ import annotations

import logging
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


def test_message_without_text_is_other_kind() -> None:
    parsed = extract_chat_input(_update(None))
    assert parsed is not None
    assert parsed.kind == "other"
    assert parsed.text is None


def test_message_without_text_logs_warning(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        extract_chat_input(_update(None))

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings


def test_chat_id_is_normalised_to_int() -> None:
    parsed = extract_chat_input(_update("hi", chat_id="456"))
    assert parsed is not None
    assert parsed.chat_id == 456
    assert isinstance(parsed.chat_id, int)


def _photo_update(photo: object, chat_id: object = 123) -> SimpleNamespace:
    message = SimpleNamespace(text=None, photo=photo, chat=SimpleNamespace(id=chat_id))
    return SimpleNamespace(effective_message=message, message=message)


def test_photo_maps_to_photo_kind_with_largest_file_id() -> None:
    small = SimpleNamespace(file_id="small", file_size=10)
    large = SimpleNamespace(file_id="large", file_size=1000)
    parsed = extract_chat_input(_photo_update([small, large]))
    assert parsed is not None
    assert parsed.kind == "photo"
    assert parsed.file_id == "large"


def test_photo_chat_id_is_normalised_to_int() -> None:
    parsed = extract_chat_input(_photo_update([SimpleNamespace(file_id="f", file_size=1)], chat_id="77"))
    assert parsed is not None
    assert parsed.chat_id == 77
    assert isinstance(parsed.chat_id, int)


def test_photo_without_file_id_is_other_kind() -> None:
    parsed = extract_chat_input(_photo_update([SimpleNamespace(file_id=None, file_size=1)]))
    assert parsed is not None
    assert parsed.kind == "other"


def test_photo_adapter_does_not_download() -> None:
    """The adapter is pure: a photo yields a file_id, never fetched bytes."""
    parsed = extract_chat_input(_photo_update([SimpleNamespace(file_id="f", file_size=1)]))
    assert parsed is not None
    assert not hasattr(parsed, "image_bytes")
