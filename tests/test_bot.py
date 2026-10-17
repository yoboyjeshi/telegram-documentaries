"""Tests for Telegram wiring: guarded sends, dispatch, and app construction."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram import Bot, Update, User
from telegram.error import TelegramError
from telegram.ext import Application, BaseHandler, CommandHandler, MessageHandler

from app import bot as bot_module
from app.bot import (
    build_application,
    dispatch,
    dispatch_safely,
    make_downloader,
    make_send_photo,
    safe_send_message,
    safe_send_photo,
)
from app.bouncer import BouncerVerdict
from app.config import Settings
from app.handlers import (
    AWAITING_PHOTO_PROMPT,
    CONVERTER_CAPTION,
    CONVERTER_FAILURE_TEXT,
    GREETING_TEXT,
    INTERVIEW_COMPLETE_TEXT,
    INTERVIEW_INTRO,
    INTERVIEW_OTHER_REDIRECT_TEXT,
    INTERVIEW_PHOTO_REDIRECT_TEXT,
    INTERVIEW_UNKNOWN_COMMAND_TEXT,
)
from app.interviewer import TOTAL_QUESTIONS, InterviewQuestion
from app.logging_config import FRIENDLY_ERROR
from app.media import avatar_dest, photo_dest
from app.session_store import InMemorySessionStore
from app.states import InterviewAnswer, InterviewState, Stage, TraitSummary


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
    assert store.get(1).stage is Stage.AWAITING_PHOTO


def test_dispatch_restart_resets_from_any_stage() -> None:
    store = InMemorySessionStore()
    store.get(1).stage = Stage.INTERVIEW
    bot = AsyncMock()
    reply = asyncio.run(dispatch(store, _update("/restart"), bot))
    assert reply == GREETING_TEXT
    assert store.get(1).stage is Stage.AWAITING_PHOTO


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


def _photo_update(chat_id: int = 1, file_id: str = "fid") -> SimpleNamespace:
    message = SimpleNamespace(
        text=None,
        photo=[SimpleNamespace(file_id=file_id, file_size=1)],
        chat=SimpleNamespace(id=chat_id),
    )
    return SimpleNamespace(effective_message=message, message=message)


class _FakeBouncer:
    def __init__(self, is_human: bool) -> None:
        self._is_human = is_human

    async def inspect(self, image_path: Path) -> BouncerVerdict:
        return BouncerVerdict(is_human=self._is_human)


class _FakeInterviewer:
    async def next_question(self, history: list[InterviewAnswer]) -> InterviewQuestion:
        return InterviewQuestion(text=f"Q{len(history) + 1}?")

    async def summarise(self, history: list[InterviewAnswer]) -> TraitSummary:
        return TraitSummary(summary="done")


def test_dispatch_routes_photo_to_flow(tmp_path: Path) -> None:
    store = InMemorySessionStore()
    store.set_stage(1, Stage.AWAITING_PHOTO)
    bot = AsyncMock()

    reply = asyncio.run(
        dispatch(
            store,
            _photo_update(),
            bot,
            bouncer=_FakeBouncer(True),
            interviewer=_FakeInterviewer(),
            media_root=tmp_path,
        )
    )

    assert INTERVIEW_INTRO in reply
    assert "Q1?" in reply
    assert store.get(1).stage is Stage.INTERVIEW


def test_dispatch_text_at_interview_records_answer_and_progresses() -> None:
    store = InMemorySessionStore()
    store.get(1).stage = Stage.INTERVIEW
    store.get(1).interview = InterviewState(pending_question="Q1?")
    bot = AsyncMock()

    reply = asyncio.run(
        dispatch(store, _update("my answer"), bot, interviewer=_FakeInterviewer())
    )

    assert reply == "Q2?"
    state = store.get(1)
    assert state.stage is Stage.INTERVIEW
    assert state.interview is not None
    assert state.interview.answers == [InterviewAnswer(question="Q1?", answer="my answer")]
    bot.send_message.assert_awaited_once()


def test_dispatch_photo_at_interview_redirects_without_download() -> None:
    store = InMemorySessionStore()
    store.get(1).stage = Stage.INTERVIEW
    store.get(1).interview = InterviewState(pending_question="Q1?")
    bot = AsyncMock()

    reply = asyncio.run(
        dispatch(
            store,
            _photo_update(),
            bot,
            bouncer=_FakeBouncer(True),
            interviewer=_FakeInterviewer(),
        )
    )

    state = store.get(1)
    assert reply == INTERVIEW_PHOTO_REDIRECT_TEXT
    assert state.stage is Stage.INTERVIEW
    assert state.interview is not None
    assert state.interview.pending_question == "Q1?"
    bot.get_file.assert_not_awaited()  # the photo never reaches the download path


def test_dispatch_unknown_command_at_interview_nudges_without_advancing() -> None:
    store = InMemorySessionStore()
    store.get(1).stage = Stage.INTERVIEW
    store.get(1).interview = InterviewState(pending_question="Q1?")
    bot = AsyncMock()

    reply = asyncio.run(
        dispatch(store, _update("/foo"), bot, interviewer=_FakeInterviewer())
    )

    state = store.get(1)
    assert reply == INTERVIEW_UNKNOWN_COMMAND_TEXT
    assert state.stage is Stage.INTERVIEW
    assert state.interview is not None
    assert state.interview.answers == []
    assert state.interview.pending_question == "Q1?"


def test_dispatch_start_at_interview_resets_and_clears_interview(tmp_path: Path) -> None:
    store = InMemorySessionStore()
    store.get(1).stage = Stage.INTERVIEW
    store.get(1).interview = InterviewState(pending_question="Q1?")
    bot = AsyncMock()

    reply = asyncio.run(
        dispatch(store, _update("/start"), bot, interviewer=_FakeInterviewer())
    )

    assert reply == GREETING_TEXT
    assert store.get(1).stage is Stage.AWAITING_PHOTO
    assert store.get(1).interview is None


def test_build_application_builds_a_lazy_interviewer(monkeypatch) -> None:
    created = {"n": 0}

    class _SpyLazyInterviewer:
        def __init__(self, key_provider) -> None:
            created["n"] += 1
            self._key_provider = key_provider

    monkeypatch.setattr(bot_module, "LazyInterviewer", _SpyLazyInterviewer)
    settings = Settings(telegram_bot_token="123:dummy")
    build_application(settings)

    assert created["n"] == 1


def test_dispatch_text_at_awaiting_photo_prompts() -> None:
    store = InMemorySessionStore()
    store.set_stage(1, Stage.AWAITING_PHOTO)
    bot = AsyncMock()
    assert asyncio.run(dispatch(store, _update("hi"), bot)) == AWAITING_PHOTO_PROMPT


def test_make_downloader_fetches_into_namespaced_path(tmp_path: Path) -> None:
    bot = AsyncMock()
    tg_file = AsyncMock()
    bot.get_file.return_value = tg_file
    dest = tmp_path / "1" / "photo.jpg"

    result = asyncio.run(make_downloader(bot)("fid", dest))

    assert result == dest
    bot.get_file.assert_awaited_once_with("fid")
    tg_file.download_to_drive.assert_awaited_once()
    assert dest.parent.exists()


def test_build_application_registers_photo_handler() -> None:
    settings = Settings(telegram_bot_token="123:dummy")
    application = build_application(settings)
    message_handlers = [h for h in application.handlers[0] if isinstance(h, MessageHandler)]
    assert len(message_handlers) >= 2


def test_on_error_logs_the_underlying_exception(caplog) -> None:
    error = RuntimeError("kaboom")
    context = SimpleNamespace(error=error)

    with caplog.at_level(logging.ERROR):
        asyncio.run(bot_module._on_error(None, context))

    records = [record for record in caplog.records if record.exc_info]
    assert records, "expected an ERROR record carrying the exception"
    assert records[0].exc_info[1] is error


def _bot_command_update(
    text: str, bot: Bot, *, update_id: int, chat_id: int = 1
) -> Update:
    """Build a real PTB ``Update`` carrying a ``BOT_COMMAND`` entity."""
    data = {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1760000000,
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
            "entities": [
                {"type": "bot_command", "offset": 0, "length": len(text.split()[0])}
            ],
        },
    }
    return Update.de_json(data, bot)


def _registry_application() -> tuple[Application, Bot]:
    """The real ``Application`` with an offline bot identity.

    PTB's ``CommandHandler.check_update`` needs ``bot.username``, normally set by
    ``Bot.get_me()`` during ``initialize()``; that would be a network call here.
    """
    application = build_application(Settings(telegram_bot_token="123:ABC"))
    # Private-API shim: PTB only sets ``_bot_user`` from ``get_me()`` during
    # ``initialize()`` (a network call), so we inject an offline identity to
    # build a real ``Application``/``Update`` pair here. Revisit on a PTB major
    # bump if this private attribute is renamed or made read-only.
    application.bot._bot_user = User(  # type: ignore[assignment]
        id=1, first_name="TestBot", is_bot=True, username="testbot"
    )
    return application, application.bot


def _first_matching_handler(
    handlers: Sequence[BaseHandler], update: Update
) -> BaseHandler | None:
    """Return the handler PTB would run for ``update`` (registry order wins)."""
    for handler in handlers:
        check = handler.check_update(update)
        if check is None or check is False:
            continue
        return handler
    return None


def test_unknown_command_is_matched_by_the_handler_registry() -> None:
    """Proves routing through PTB's registry: ``/foo`` must not be dropped."""
    application, bot = _registry_application()
    update = _bot_command_update("/foo", bot, update_id=1)

    matched = _first_matching_handler(application.handlers[0], update)

    assert matched is not None, "an unknown command must not be dropped by the registry"
    # The catch-all MessageHandler (not a specific CommandHandler) wins for /foo.
    assert isinstance(matched, MessageHandler)


def test_start_command_is_still_matched_by_the_start_command_handler() -> None:
    """The catch-all must not shadow ``/start``: order in the registry is kept."""
    application, bot = _registry_application()
    update = _bot_command_update("/start", bot, update_id=2)

    matched = _first_matching_handler(application.handlers[0], update)

    assert isinstance(matched, CommandHandler)
    assert matched.commands == {"start"}


def test_restart_command_is_still_matched_by_the_restart_command_handler() -> None:
    """The catch-all must not shadow ``/restart`` either: registry order wins."""
    application, bot = _registry_application()
    update = _bot_command_update("/restart", bot, update_id=3)

    matched = _first_matching_handler(application.handlers[0], update)

    assert isinstance(matched, CommandHandler)
    assert matched.commands == {"restart"}


def test_dispatch_restart_at_interview_resets_and_clears_interview() -> None:
    store = InMemorySessionStore()
    state = store.get(1)
    state.stage = Stage.INTERVIEW
    state.interview = InterviewState(
        answers=[InterviewAnswer(question="Q1?", answer="answer-1")],
        pending_question="Q2?",
    )
    bot = AsyncMock()

    reply = asyncio.run(
        dispatch(store, _update("/restart"), bot, interviewer=_FakeInterviewer())
    )

    assert reply == GREETING_TEXT
    assert store.get(1).stage is Stage.AWAITING_PHOTO
    assert store.get(1).interview is None


def test_dispatch_other_kind_at_interview_redirects_instead_of_silence() -> None:
    store = InMemorySessionStore()
    store.get(1).stage = Stage.INTERVIEW
    store.get(1).interview = InterviewState(pending_question="Q1?")
    bot = AsyncMock()

    reply = asyncio.run(
        dispatch(store, _update(None), bot, interviewer=_FakeInterviewer())
    )

    assert reply == INTERVIEW_OTHER_REDIRECT_TEXT
    bot.send_message.assert_awaited_once()


# --- Converter wiring: safe_send_photo, handoff, CONVERTING retry -------------

CAPTION_SENTINEL = "CAPTION_SENTINEL_documentary_line"
from image_fixtures import PNG

IMAGE_SENTINEL = PNG


def test_safe_send_photo_sends_photo_with_caption() -> None:
    bot = AsyncMock()
    photo = Path("avatar.png")

    assert asyncio.run(safe_send_photo(bot, 1, photo, CAPTION_SENTINEL)) is True

    bot.send_photo.assert_awaited_once_with(chat_id=1, photo=photo, caption=CAPTION_SENTINEL)


def test_safe_send_photo_swallows_telegram_error() -> None:
    bot = AsyncMock()
    bot.send_photo.side_effect = TelegramError("boom")
    assert asyncio.run(safe_send_photo(bot, 1, Path("a.png"), "cap")) is False


def test_safe_send_photo_swallows_unexpected_exception_and_never_logs_caption(caplog) -> None:
    """A send failure that is not a TelegramError must still never crash the bot."""
    bot = AsyncMock()
    bot.send_photo.side_effect = RuntimeError("unexpected network failure")

    with caplog.at_level(logging.ERROR):
        result = asyncio.run(safe_send_photo(bot, 1, Path("a.png"), CAPTION_SENTINEL))

    assert result is False
    assert CAPTION_SENTINEL not in caplog.text


def test_make_send_photo_wraps_safe_send_photo() -> None:
    bot = AsyncMock()

    result = asyncio.run(make_send_photo(bot)(1, Path("a.png"), "cap"))

    assert result is True
    bot.send_photo.assert_awaited_once()


def test_build_application_builds_a_lazy_converter(monkeypatch) -> None:
    created = {"n": 0}

    class _SpyLazyConverter:
        def __init__(self, key_provider) -> None:
            created["n"] += 1
            self._key_provider = key_provider

    monkeypatch.setattr(bot_module, "LazyConverter", _SpyLazyConverter)
    settings = Settings(telegram_bot_token="123:dummy")
    build_application(settings)

    assert created["n"] == 1


class _FakeConverter:
    """Deterministic converter double: records calls, optionally fails."""

    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc
        self.calls: list[tuple[Path, TraitSummary]] = []

    async def generate_avatar(self, portrait_path: Path, summary: TraitSummary) -> bytes:
        self.calls.append((portrait_path, summary))
        if self._exc is not None:
            raise self._exc
        return IMAGE_SENTINEL


def _send_recorder(delivered: bool = True):
    calls: list[tuple[int, Path, str]] = []

    async def send_photo(chat_id: int, photo: Path, caption: str) -> bool:
        calls.append((chat_id, photo, caption))
        return delivered

    return send_photo, calls


def _at_converting(store: InMemorySessionStore, tmp_path: Path, chat_id: int = 1) -> None:
    state = store.get(chat_id)
    state.stage = Stage.CONVERTING
    state.interview = InterviewState(
        answers=[InterviewAnswer(question=f"Q{i}?", answer=f"a{i}") for i in range(1, TOTAL_QUESTIONS)],
        summary=TraitSummary(summary="bold explorer", traits=["bold"]),
    )
    portrait = photo_dest(chat_id, root=tmp_path)
    portrait.parent.mkdir(parents=True, exist_ok=True)
    portrait.write_bytes(b"portrait-bytes")


def _at_last_interview_answer(store: InMemorySessionStore) -> None:
    state = store.get(1)
    state.stage = Stage.INTERVIEW
    state.interview = InterviewState(
        answers=[InterviewAnswer(question=f"Q{i}?", answer=f"a{i}") for i in range(1, TOTAL_QUESTIONS)],
        pending_question="Q6?",
    )


def test_interview_completion_runs_converter_in_same_turn(tmp_path: Path) -> None:
    store = InMemorySessionStore()
    _at_last_interview_answer(store)
    portrait = photo_dest(1, root=tmp_path)
    portrait.parent.mkdir(parents=True, exist_ok=True)
    portrait.write_bytes(b"portrait-bytes")
    bot = AsyncMock()
    converter = _FakeConverter()
    send_photo, sent = _send_recorder()

    reply = asyncio.run(
        dispatch(
            store,
            _update("final answer"),
            bot,
            interviewer=_FakeInterviewer(),
            converter=converter,
            send_photo=send_photo,
            media_root=tmp_path,
        )
    )

    assert reply == INTERVIEW_COMPLETE_TEXT
    assert store.get(1).stage is Stage.SCRIPTING
    # The acknowledgement text and the avatar photo both went out.
    bot.send_message.assert_awaited_once_with(chat_id=1, text=INTERVIEW_COMPLETE_TEXT)
    assert sent == [(1, avatar_dest(1, root=tmp_path), CONVERTER_CAPTION)]
    assert converter.calls  # the converter ran in the same turn
    assert not avatar_dest(1, root=tmp_path).exists()  # deleted after the send


def test_update_at_converting_reruns_converter(tmp_path: Path) -> None:
    store = InMemorySessionStore()
    _at_converting(store, tmp_path)
    bot = AsyncMock()
    converter = _FakeConverter()
    send_photo, sent = _send_recorder()

    reply = asyncio.run(
        dispatch(
            store,
            _update("try again"),
            bot,
            converter=converter,
            send_photo=send_photo,
            media_root=tmp_path,
        )
    )

    assert reply is None  # nothing extra beyond the photo
    assert store.get(1).stage is Stage.SCRIPTING
    assert sent == [(1, avatar_dest(1, root=tmp_path), CONVERTER_CAPTION)]
    bot.send_message.assert_not_awaited()


def test_update_at_converting_sends_retry_text_when_converter_fails(tmp_path: Path) -> None:
    store = InMemorySessionStore()
    _at_converting(store, tmp_path)
    bot = AsyncMock()
    converter = _FakeConverter(exc=RuntimeError("gemini down"))
    send_photo, sent = _send_recorder()

    reply = asyncio.run(
        dispatch(
            store,
            _update("try again"),
            bot,
            converter=converter,
            send_photo=send_photo,
            media_root=tmp_path,
        )
    )

    assert reply == CONVERTER_FAILURE_TEXT
    assert store.get(1).stage is Stage.CONVERTING
    assert sent == []
    bot.send_message.assert_awaited_once_with(chat_id=1, text=CONVERTER_FAILURE_TEXT)


def test_interview_handoff_runs_converter_exactly_once_when_it_fails(tmp_path: Path) -> None:
    """The step must not run twice in one turn (handoff + CONVERTING branch)."""
    store = InMemorySessionStore()
    _at_last_interview_answer(store)
    portrait = photo_dest(1, root=tmp_path)
    portrait.parent.mkdir(parents=True, exist_ok=True)
    portrait.write_bytes(b"portrait-bytes")
    bot = AsyncMock()
    converter = _FakeConverter(exc=RuntimeError("gemini down"))
    send_photo, sent = _send_recorder()

    reply = asyncio.run(
        dispatch(
            store,
            _update("final answer"),
            bot,
            interviewer=_FakeInterviewer(),
            converter=converter,
            send_photo=send_photo,
            media_root=tmp_path,
        )
    )

    assert reply == INTERVIEW_COMPLETE_TEXT
    assert len(converter.calls) == 1  # exactly one converter run this turn
    assert store.get(1).stage is Stage.CONVERTING
    assert sent == []
    # Acknowledgement first, then the friendly retry.
    assert bot.send_message.await_count == 2
    assert bot.send_message.await_args_list[1].kwargs["text"] == CONVERTER_FAILURE_TEXT


def test_interview_handoff_without_converter_configured_still_sends_reply(tmp_path: Path) -> None:
    store = InMemorySessionStore()
    _at_last_interview_answer(store)
    bot = AsyncMock()

    reply = asyncio.run(
        dispatch(store, _update("final answer"), bot, interviewer=_FakeInterviewer())
    )

    assert reply == INTERVIEW_COMPLETE_TEXT
    assert store.get(1).stage is Stage.CONVERTING
    bot.send_message.assert_awaited_once()


def test_update_at_converting_without_converter_configured_warns(tmp_path: Path, caplog) -> None:
    store = InMemorySessionStore()
    _at_converting(store, tmp_path)
    bot = AsyncMock()

    with caplog.at_level(logging.WARNING):
        reply = asyncio.run(dispatch(store, _update("hi"), bot, media_root=tmp_path))

    assert reply is None
    assert store.get(1).stage is Stage.CONVERTING
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("CONVERTING" in r.getMessage() for r in warnings)


def test_dispatch_restart_at_converting_purges_avatar_and_portrait(tmp_path: Path) -> None:
    store = InMemorySessionStore()
    _at_converting(store, tmp_path)
    avatar = avatar_dest(1, root=tmp_path)
    avatar.write_bytes(b"stale-avatar")
    bot = AsyncMock()

    reply = asyncio.run(
        dispatch(
            store,
            _update("/restart"),
            bot,
            converter=_FakeConverter(),
            send_photo=AsyncMock(),
            media_root=tmp_path,
        )
    )

    assert reply == GREETING_TEXT
    assert store.get(1).stage is Stage.AWAITING_PHOTO
    assert not avatar.exists()
    assert not photo_dest(1, root=tmp_path).exists()
