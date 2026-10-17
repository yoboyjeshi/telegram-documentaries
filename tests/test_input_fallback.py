"""Slice C: unsupported uploads must reach a registered fallback, never a model.

These tests drive the real ``Application`` handler registry (document, sticker
and voice updates), not the normalised-input helpers, so handler priority is
part of the contract under test.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update, User
from telegram.ext import CommandHandler, MessageHandler

from app import bot as wiring
from app.config import Settings
from app.conversion_tasks import ConversionTasks
from app.handlers import (
    GENERATION_UNSUPPORTED_TEXT,
    GREETING_TEXT,
    INTERVIEW_OTHER_REDIRECT_TEXT,
    INTERVIEW_PHOTO_REDIRECT_TEXT,
    INTERVIEW_UNKNOWN_COMMAND_TEXT,
    UNSUPPORTED_UPLOAD_PHOTO_TEXT,
)
from app.session_store import InMemorySessionStore
from app.states import InterviewState, Stage, TraitSummary

UNSUPPORTED = {
    "document": {"file_id": "doc", "file_unique_id": "u1", "file_name": "me.jpg",
                 "mime_type": "image/jpeg", "file_size": 10},
    "sticker": {"file_id": "stk", "file_unique_id": "u2", "width": 1, "height": 1,
                "is_animated": False, "is_video": False, "type": "regular"},
    "voice": {"file_id": "voi", "file_unique_id": "u3", "duration": 1},
}


def _app(monkeypatch, tmp_path, store):
    """A real Application with every provider replaced by a recording mock."""
    registry = ConversionTasks(store)
    providers = {name: AsyncMock() for name in
                 ("LazyBouncer", "LazyInterviewer", "LazyConverter", "LazyScripter",
                  "LazyNarrator")}
    for name, provider in providers.items():
        monkeypatch.setattr(wiring, name, lambda key, obj=provider: obj)
    monkeypatch.setattr(wiring, "ConversionTasks", lambda _: registry)
    monkeypatch.setattr(wiring, "MEDIA_ROOT", tmp_path)
    app = wiring.build_application(Settings(telegram_bot_token="123:dummy"), store)
    app.bot._bot_user = User(id=1, first_name="Offline", is_bot=True, username="bot")
    return app, registry, providers


def _update(app, payload: dict | None = None, *, text: str | None = None,
            photo: bool = False, update_id: int = 1) -> Update:
    message: dict = {"message_id": update_id, "date": 1760000000,
                     "chat": {"id": 1, "type": "private"}}
    if payload is not None:
        message.update(payload)
    if photo:
        message["photo"] = [{"file_id": "p", "file_unique_id": "pu", "width": 1,
                             "height": 1, "file_size": 1}]
    if text is not None:
        message["text"] = text
        if text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text)}]
    return Update.de_json({"update_id": update_id, "message": message}, app.bot)


async def _route(app, update, transport) -> bool:
    for handler in app.handlers[0]:
        matched = handler.check_update(update)
        if matched is not None and matched is not False:
            await handler.callback(update, SimpleNamespace(bot=transport))
            return True
    return False


def _sent(transport) -> list[str]:
    return [call.kwargs["text"] for call in transport.send_message.await_args_list]


@pytest.mark.parametrize("kind", sorted(UNSUPPORTED))
@pytest.mark.parametrize("stage,expected", [
    (Stage.AWAITING_PHOTO, UNSUPPORTED_UPLOAD_PHOTO_TEXT),
    (Stage.BOUNCER_CHECK, GENERATION_UNSUPPORTED_TEXT),
    (Stage.INTERVIEW, INTERVIEW_OTHER_REDIRECT_TEXT),
    (Stage.CONVERTING, GENERATION_UNSUPPORTED_TEXT),
    (Stage.SCRIPTING, GENERATION_UNSUPPORTED_TEXT),
    (Stage.NARRATING, GENERATION_UNSUPPORTED_TEXT),
])
def test_unsupported_upload_is_guided_without_media_or_model_work(
    monkeypatch, tmp_path, kind, stage, expected
):
    async def scenario():
        store = InMemorySessionStore()
        state = store.get(1)
        state.stage = stage
        state.interview = InterviewState(pending_question="Q1?",
                                         summary=TraitSummary(summary="bold"))
        state.narration_text = "A bold creature greets the evening."
        app, registry, providers = _app(monkeypatch, tmp_path, store)
        transport = AsyncMock()

        assert await _route(app, _update(app, UNSUPPORTED[kind]), transport), (
            "an unsupported upload must match a registered handler"
        )
        await asyncio.wait_for(registry.wait(), 1)

        assert _sent(transport) == [expected]
        assert store.get(1).stage is stage
        transport.get_file.assert_not_awaited()
        transport.send_photo.assert_not_awaited()
        transport.send_voice.assert_not_awaited()
        for provider in providers.values():
            assert provider.method_calls == []
        assert not registry.active(1)
        await registry.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", sorted(UNSUPPORTED))
def test_greeting_routes_unsupported_uploads_to_a_handler_without_any_work(
    monkeypatch, tmp_path, kind, caplog
):
    """Before /start the baseline warns and ignores; it must still be routed."""
    async def scenario():
        store = InMemorySessionStore()
        assert store.get(1).stage is Stage.GREETING
        app, registry, providers = _app(monkeypatch, tmp_path, store)
        transport = AsyncMock()

        assert await _route(app, _update(app, UNSUPPORTED[kind]), transport), (
            "an unsupported upload at GREETING must still match a handler"
        )
        await asyncio.wait_for(registry.wait(), 1)

        transport.send_message.assert_not_awaited()
        transport.get_file.assert_not_awaited()
        assert store.get(1).stage is Stage.GREETING
        assert not list(tmp_path.iterdir())
        for provider in providers.values():
            assert provider.method_calls == []
        await registry.close()

    with caplog.at_level("WARNING"):
        asyncio.run(scenario())

    assert any("other" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("kind", sorted(UNSUPPORTED))
def test_done_ignores_unsupported_uploads(monkeypatch, tmp_path, kind):
    async def scenario():
        store = InMemorySessionStore()
        store.get(1).stage = Stage.DONE
        app, registry, providers = _app(monkeypatch, tmp_path, store)
        transport = AsyncMock()

        assert await _route(app, _update(app, UNSUPPORTED[kind]), transport)
        await asyncio.wait_for(registry.wait(), 1)

        transport.send_message.assert_not_awaited()
        transport.get_file.assert_not_awaited()
        assert store.get(1).stage is Stage.DONE
        for provider in providers.values():
            assert provider.method_calls == []
        await registry.close()

    asyncio.run(scenario())


def test_reset_still_wins_over_the_fallback(monkeypatch, tmp_path):
    async def scenario():
        store = InMemorySessionStore()
        store.get(1).stage = Stage.NARRATING
        app, registry, _ = _app(monkeypatch, tmp_path, store)
        transport = AsyncMock()

        for command in ("/start", "/restart"):
            await _route(app, _update(app, text=command), transport)
            assert store.get(1).stage is Stage.AWAITING_PHOTO

        assert _sent(transport) == [GREETING_TEXT, GREETING_TEXT]
        await registry.close()

    asyncio.run(scenario())


def test_fallback_is_registered_last_and_shadows_nothing(monkeypatch, tmp_path):
    store = InMemorySessionStore()
    app, _, _ = _app(monkeypatch, tmp_path, store)
    handlers = app.handlers[0]

    def matched(update):
        for handler in handlers:
            check = handler.check_update(update)
            if check is not None and check is not False:
                return handler
        return None

    assert isinstance(matched(_update(app, text="/start")), CommandHandler)
    assert isinstance(matched(_update(app, text="/restart")), CommandHandler)
    assert matched(_update(app, text="hello")) is handlers[2]
    assert matched(_update(app, photo=True)) is handlers[3]
    assert matched(_update(app, text="/foo")) is handlers[4]
    # The unsupported-message fallback is the last registered handler.
    assert matched(_update(app, UNSUPPORTED["document"])) is handlers[-1]
    assert isinstance(handlers[-1], MessageHandler)


def test_text_at_photo_intake_and_wrong_input_at_interview(monkeypatch, tmp_path):
    async def scenario():
        store = InMemorySessionStore()
        store.set_stage(1, Stage.AWAITING_PHOTO)
        app, registry, providers = _app(monkeypatch, tmp_path, store)
        transport = AsyncMock()

        await _route(app, _update(app, text="hello there"), transport)
        assert store.get(1).stage is Stage.AWAITING_PHOTO

        store.get(1).stage = Stage.INTERVIEW
        store.get(1).interview = InterviewState(pending_question="Q1?")
        await _route(app, _update(app, photo=True, update_id=2), transport)
        await _route(app, _update(app, text="/foo", update_id=3), transport)
        await asyncio.wait_for(registry.wait(), 1)

        assert _sent(transport)[1:] == [INTERVIEW_PHOTO_REDIRECT_TEXT,
                                        INTERVIEW_UNKNOWN_COMMAND_TEXT]
        assert store.get(1).interview.answers == []
        assert store.get(1).interview.pending_question == "Q1?"
        transport.get_file.assert_not_awaited()
        for provider in providers.values():
            assert provider.method_calls == []
        await registry.close()

    asyncio.run(scenario())
