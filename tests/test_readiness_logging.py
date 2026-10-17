"""Privacy regressions for shared decorators reached by early-stage handlers."""
from __future__ import annotations

import asyncio
import logging

import pytest

from app.logging_config import FRIENDLY_ERROR, guard_errors, logged
from app.telegram_adapter import ChatInput

SENTINEL = "SECRET_KEY_PORTRAIT_QUESTION_ANSWER_SUMMARY"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_guard_logs_metadata_not_exception_payload(caplog, asynchronous):
    @guard_errors
    def sync_failure(chat_id):
        raise RuntimeError(SENTINEL)

    @guard_errors
    async def async_failure(chat_id):
        raise RuntimeError(SENTINEL)

    with caplog.at_level(logging.DEBUG):
        result = asyncio.run(async_failure(7)) if asynchronous else sync_failure(7)
    assert result == FRIENDLY_ERROR
    assert "type=RuntimeError" in caplog.text
    assert "handler=" in caplog.text
    assert SENTINEL not in caplog.text
    assert caplog.records
    assert all(r.exc_info is None and r.exc_text is None for r in caplog.records)


def test_debug_stage_lookup_error_keeps_payload_private(caplog):
    class BrokenStore:
        def get(self, chat_id):
            raise RuntimeError(SENTINEL)

    @logged
    def handler(store, incoming):
        return "ok"

    with caplog.at_level(logging.DEBUG):
        assert handler(BrokenStore(), ChatInput(chat_id=8, kind="text", text=SENTINEL)) == "ok"
    assert "stage=None" in caplog.text
    assert SENTINEL not in caplog.text
    assert all(r.exc_info is None and r.exc_text is None for r in caplog.records)


def test_async_guard_propagates_cancellation_without_notice_or_log(caplog):
    @guard_errors
    async def cancelled(chat_id):
        raise asyncio.CancelledError

    with caplog.at_level(logging.DEBUG), pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled(9))
    assert not [r for r in caplog.records if r.name == "telegram_documentaries"]


@pytest.mark.parametrize("boundary", ["chat_id", "photo_size"])
def test_adapter_error_logs_do_not_retain_raw_input(caplog, boundary):
    from types import SimpleNamespace

    from app.telegram_adapter import extract_chat_input

    class BrokenValue:
        def __int__(self):
            raise ValueError(SENTINEL)

        def __gt__(self, other):
            raise TypeError(SENTINEL)

    message = SimpleNamespace(text=None, chat=SimpleNamespace(id=123))
    if boundary == "chat_id":
        message.chat.id = BrokenValue()
    else:
        message.photo = [
            SimpleNamespace(file_id="first", file_size=BrokenValue()),
            SimpleNamespace(file_id="last", file_size=BrokenValue()),
        ]
    with caplog.at_level(logging.DEBUG):
        result = extract_chat_input(SimpleNamespace(effective_message=message))
    if boundary == "chat_id":
        assert result is None
        assert "operation=extract_chat_id" in caplog.text
        assert "type=ValueError" in caplog.text
    else:
        assert result is not None and result.file_id == "last"
        assert "operation=select_photo" in caplog.text
        assert "type=TypeError" in caplog.text
    assert caplog.records
    assert SENTINEL not in caplog.text
    assert all(r.exc_info is None and r.exc_text is None for r in caplog.records)

