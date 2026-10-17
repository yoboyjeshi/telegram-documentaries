"""Payload-bearing failures at shared handler, worker and send boundaries stay private."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import bot as wiring
from app.conversion_tasks import ConversionTasks
from app.handlers import PHOTO_DOWNLOAD_FAILURE_TEXT
from app.logging_config import FRIENDLY_ERROR
from app.photo_flow import handle_photo_update
from app.session_store import InMemorySessionStore
from app.states import Stage
from app.telegram_adapter import ChatInput

SENTINEL = "PRIVATE_TOKEN_IMAGE_QUESTION_ANSWER_SUMMARY_4ff7"


def _private(caplog):
    records = [r for r in caplog.records if r.name == "telegram_documentaries"]
    assert records
    assert SENTINEL not in caplog.text
    assert all(r.exc_info is None and r.exc_text is None for r in records)
    assert all(SENTINEL not in repr(r.args) for r in records)


def test_global_error_handler_does_not_retain_chained_payload(caplog):
    try:
        try:
            raise ValueError(SENTINEL)
        except ValueError as cause:
            raise RuntimeError(SENTINEL) from cause
    except RuntimeError as error:
        with caplog.at_level(logging.ERROR):
            asyncio.run(wiring._on_error(None, SimpleNamespace(error=error)))
    _private(caplog)
    assert "RuntimeError" in caplog.text


@pytest.mark.parametrize("broken_adapter", [False, True])
def test_dispatch_backstop_and_chat_resolution_keep_errors_private(monkeypatch, caplog,
                                                                  broken_adapter):
    async def failure(*args, **kwargs):
        raise RuntimeError(SENTINEL)

    def bad_adapter(update):
        raise ValueError(SENTINEL)

    monkeypatch.setattr(wiring, "dispatch", failure)
    if broken_adapter:
        monkeypatch.setattr(wiring, "extract_chat_input", bad_adapter)
    bot = AsyncMock()
    update = SimpleNamespace(effective_message=SimpleNamespace(
        text=SENTINEL, chat=SimpleNamespace(id=1)))
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(wiring.dispatch_safely(InMemorySessionStore(), update, bot)) is None
    if broken_adapter:
        bot.send_message.assert_not_awaited()
    else:
        bot.send_message.assert_awaited_once_with(chat_id=1, text=FRIENDLY_ERROR)
    _private(caplog)


def test_registry_retrieves_worker_failure_without_payload_or_loop_error(caplog):
    async def scenario():
        tasks = ConversionTasks(InMemorySessionStore())
        errors = []
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: errors.append(context))

        async def failure(version):
            raise RuntimeError(SENTINEL)

        assert tasks.start(1, failure)
        await tasks.wait()
        assert not tasks.active(1)
        assert errors == []
        await tasks.close()

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    _private(caplog)
    assert "RuntimeError" in caplog.text


@pytest.mark.parametrize("kind", ["message", "photo", "voice"])
def test_send_adapters_preserve_cancellation_and_redact_failures(caplog, tmp_path, kind):
    async def scenario():
        bot = AsyncMock()
        sender = getattr(bot, f"send_{kind}")
        arguments = (SENTINEL,) if kind == "message" else (
            (tmp_path / "synthetic.jpg", SENTINEL) if kind == "photo"
            else (tmp_path / "synthetic.ogg",)
        )
        safe_send = getattr(wiring, f"safe_send_{kind}")
        sender.side_effect = RuntimeError(SENTINEL)
        assert await safe_send(bot, 1, *arguments) is False
        sender.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await safe_send(bot, 1, *arguments)

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    _private(caplog)


def test_legacy_photo_download_failure_keeps_functional_retry_without_payload(tmp_path, caplog):
    store = InMemorySessionStore()
    store.set_stage(1, Stage.AWAITING_PHOTO)
    bouncer, interviewer = AsyncMock(), AsyncMock()

    async def download(file_id: str, path: Path):
        raise RuntimeError(SENTINEL)

    with caplog.at_level(logging.ERROR):
        result = asyncio.run(handle_photo_update(
            store, ChatInput(chat_id=1, kind="photo", file_id=SENTINEL),
            bouncer=bouncer, download=download, interviewer=interviewer, media_root=tmp_path))
    assert result == PHOTO_DOWNLOAD_FAILURE_TEXT
    assert store.get(1).stage is Stage.AWAITING_PHOTO
    bouncer.inspect.assert_not_awaited()
    interviewer.next_question.assert_not_awaited()
    _private(caplog)
