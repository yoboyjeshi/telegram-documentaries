"""Scripting routes through the one existing per-chat task registry, not a new one.

Covers the bot-level ``scripter=`` dependency: the Converter's automatic
handoff inside the same task, scripting-only retries, resets, duplicate
updates, cross-chat independence, shutdown, and the real registered Telegram
handlers with a mocked transport. All providers and Telegram calls are offline.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from image_fixtures import PNG

from app import bot as bot_module
from app.bot import build_application, dispatch
from app.config import Settings
from app.handlers import GREETING_TEXT
from app.media import avatar_dest, photo_dest
from app.session_store import InMemorySessionStore
from app.states import InterviewState, Stage, TraitSummary

NARRATION = "Here we observe a quietly bold specimen, devoted entirely to its evening rituals."
AUDIO_CALLS = ("send_audio", "send_voice", "send_video", "send_document")


def text_update(text, chat=1):
    return SimpleNamespace(
        effective_message=SimpleNamespace(text=text, chat=SimpleNamespace(id=chat))
    )


def photo_update(chat=1):
    photo = [SimpleNamespace(file_id="file-1", file_size=10)]
    return SimpleNamespace(
        effective_message=SimpleNamespace(photo=photo, chat=SimpleNamespace(id=chat))
    )


def make_update(kind, chat=1):
    if kind == "photo":
        return photo_update(chat)
    return text_update("/wildlife" if kind == "command" else "again", chat)


def script(text=NARRATION):
    from app.scripter import NarrationScript

    return NarrationScript.model_construct(text=text)


def at_scripting(store, chat=1):
    state = store.get(chat)
    state.stage = Stage.SCRIPTING
    state.interview = InterviewState(summary=TraitSummary(summary="bold", traits=["bold"]))
    return state


def at_converting(store, root, chat=1):
    state = store.get(chat)
    state.stage = Stage.CONVERTING
    state.interview = InterviewState(summary=TraitSummary(summary="bold", traits=["bold"]))
    portrait = photo_dest(chat, root=root)
    portrait.parent.mkdir(parents=True, exist_ok=True)
    portrait.write_bytes(b"portrait")
    return state


def instrument(tasks):
    """Record registry starts and signal each task's end, through the public API only.

    ``started`` proves a handoff never smuggles in a second task; ``finished``
    gives every test a deterministic completion Event instead of polling, and
    is set on a cancelled task too because the wrapper uses ``finally``.
    """
    started = []
    finished = {}
    original = tasks.start

    def start(chat_id, work):
        done = asyncio.Event()

        async def wrapped(version):
            try:
                await work(version)
            finally:
                done.set()

        accepted = original(chat_id, wrapped)
        started.append((chat_id, accepted))
        if accepted:
            finished[chat_id] = done
        return accepted

    tasks.start = start
    return started, finished


def capture_registry(monkeypatch):
    """Expose the registry ``build_application`` creates, without touching production."""
    created = []
    real = bot_module.ConversionTasks

    class Recording(real):
        def __init__(self, store):
            super().__init__(store)
            created.append(self)

    monkeypatch.setattr(bot_module, "ConversionTasks", Recording)
    return created


def narration_sends(bot):
    return [call.kwargs.get("text") for call in bot.send_message.await_args_list]


def assert_no_audio(bot):
    for name in AUDIO_CALLS:
        getattr(bot, name).assert_not_awaited()


def test_converter_handoff_scripts_inside_the_same_task(tmp_path):
    async def scenario():
        store = InMemorySessionStore()
        at_converting(store, tmp_path)
        tasks = bot_module.ConversionTasks(store)
        started, finished = instrument(tasks)
        order = []
        converter = AsyncMock()
        converter.generate_avatar.return_value = PNG

        async def send_photo(chat, path, caption):
            order.append("photo")
            return True

        writer = AsyncMock()

        async def write(summary):
            order.append("script")
            return script()

        writer.write_script.side_effect = write
        bot = AsyncMock()
        bot.send_message.side_effect = lambda **kwargs: order.append("narration")
        reply = await dispatch(store, text_update("go"), bot, converter=converter,
                               send_photo=send_photo, scripter=writer, media_root=tmp_path,
                               conversion_tasks=tasks)
        await asyncio.wait_for(finished[1].wait(), 1)
        assert reply is None
        assert order == ["photo", "script", "narration"]
        assert started == [(1, True)]  # One task carries both stages.
        writer.write_script.assert_awaited_once_with(store.get(1).interview.summary)
        assert narration_sends(bot) == [NARRATION]
        assert store.get(1).narration_text == NARRATION
        assert store.get(1).stage is Stage.NARRATING
        assert_no_audio(bot)
        await tasks.close()

    asyncio.run(scenario())


def test_direct_seam_scripts_without_a_registry():
    async def scenario():
        store = InMemorySessionStore()
        at_scripting(store)
        writer = AsyncMock()
        writer.write_script.return_value = script()
        bot = AsyncMock()
        reply = await dispatch(store, text_update("go"), bot, scripter=writer)
        assert reply is None
        writer.write_script.assert_awaited_once_with(store.get(1).interview.summary)
        assert narration_sends(bot) == [NARRATION]
        assert store.get(1).narration_text == NARRATION
        assert store.get(1).stage is Stage.NARRATING
        assert_no_audio(bot)

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["text", "photo", "command"])
def test_retry_at_scripting_never_reruns_conversion(kind, tmp_path):
    async def scenario():
        store = InMemorySessionStore()
        at_scripting(store)
        tasks = bot_module.ConversionTasks(store)
        converter, bouncer = AsyncMock(), AsyncMock()
        writer = AsyncMock()
        writer.write_script.return_value = script()
        bot = AsyncMock()
        reply = await dispatch(store, make_update(kind), bot, converter=converter,
                               send_photo=AsyncMock(return_value=True), bouncer=bouncer,
                               interviewer=AsyncMock(), scripter=writer, media_root=tmp_path,
                               conversion_tasks=tasks)
        await asyncio.wait_for(tasks.wait(), 1)
        assert reply is None
        converter.generate_avatar.assert_not_awaited()
        bouncer.inspect.assert_not_awaited()
        bot.get_file.assert_not_awaited()  # A photo at SCRIPTING never downloads.
        writer.write_script.assert_awaited_once()
        assert narration_sends(bot) == [NARRATION]
        assert store.get(1).stage is Stage.NARRATING
        await tasks.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("command", ["/start", "/restart"])
@pytest.mark.parametrize("phase", ["generation", "send"])
@pytest.mark.parametrize("late_result", [False, True])
def test_reset_during_scripting_cannot_commit_or_touch_a_new_session(
    tmp_path, command, phase, late_result
):
    async def scenario():
        store = InMemorySessionStore()
        at_scripting(store)
        tasks = bot_module.ConversionTasks(store)
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def block():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                if not late_result:
                    raise
                await release.wait()

        writer = AsyncMock()

        async def write(summary):
            if phase == "generation":
                await block()
            return script()

        writer.write_script.side_effect = write
        bot = AsyncMock()

        async def send_message(**kwargs):
            if phase == "send" and kwargs.get("text") == NARRATION:
                await block()
            return SimpleNamespace(message_id=1)

        bot.send_message.side_effect = send_message
        await dispatch(store, text_update("go"), bot, scripter=writer, media_root=tmp_path,
                       conversion_tasks=tasks)
        await asyncio.wait_for(entered.wait(), 1)
        assert tasks.active(1)
        await dispatch(store, text_update(command), bot, media_root=tmp_path,
                       conversion_tasks=tasks)
        await asyncio.wait_for(cancelled.wait(), 1)
        assert store.get(1).stage is Stage.AWAITING_PHOTO
        assert store.get(1).narration_text is None

        # A new session may reuse the same chat: the old task owns none of it.
        at_scripting(store)
        store.get(1).narration_text = "A newer creature stirs."
        portrait = photo_dest(1, root=tmp_path)
        portrait.parent.mkdir(parents=True, exist_ok=True)
        portrait.write_bytes(b"new portrait")
        avatar = avatar_dest(1, root=tmp_path)
        avatar.write_bytes(PNG)
        release.set()
        await asyncio.wait_for(tasks.wait(), 1)
        assert store.get(1).stage is Stage.SCRIPTING
        assert store.get(1).narration_text == "A newer creature stirs."
        assert portrait.read_bytes() == b"new portrait"
        assert avatar.read_bytes() == PNG
        assert narration_sends(bot).count(NARRATION) <= (1 if phase == "send" else 0)
        assert sum(text is not None and "having a moment" in text
                   for text in narration_sends(bot)) == 0
        await tasks.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("command", ["/start", "/restart"])
def test_reset_at_the_converter_handoff_stops_before_narration(tmp_path, command):
    async def scenario():
        store = InMemorySessionStore()
        at_converting(store, tmp_path)
        tasks = bot_module.ConversionTasks(store)
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        converter = AsyncMock()
        converter.generate_avatar.return_value = PNG
        writer = AsyncMock()

        async def write(summary):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()  # Resistant provider returns after the reset.
            return script()

        writer.write_script.side_effect = write
        bot = AsyncMock()
        send_photo = AsyncMock(return_value=True)
        await dispatch(store, text_update("go"), bot, converter=converter,
                       send_photo=send_photo, scripter=writer, media_root=tmp_path,
                       conversion_tasks=tasks)
        await asyncio.wait_for(entered.wait(), 1)
        send_photo.assert_awaited_once()  # The avatar went out before the handoff.
        await dispatch(store, text_update(command), bot, media_root=tmp_path,
                       conversion_tasks=tasks)
        await asyncio.wait_for(cancelled.wait(), 1)
        at_scripting(store)
        store.get(1).narration_text = "A newer creature stirs."
        release.set()
        await asyncio.wait_for(tasks.wait(), 1)
        assert store.get(1).stage is Stage.SCRIPTING
        assert store.get(1).narration_text == "A newer creature stirs."
        assert NARRATION not in narration_sends(bot)
        await tasks.close()

    asyncio.run(scenario())


def test_duplicate_updates_do_not_overlap_scripting(tmp_path):
    async def scenario():
        store = InMemorySessionStore()
        at_scripting(store)
        tasks = bot_module.ConversionTasks(store)
        entered = asyncio.Event()
        calls = []
        writer = AsyncMock()

        async def write(summary):
            calls.append(summary)
            entered.set()
            await asyncio.Event().wait()

        writer.write_script.side_effect = write
        bot = AsyncMock()
        kwargs = {"scripter": writer, "media_root": tmp_path, "conversion_tasks": tasks}
        await dispatch(store, text_update("go"), bot, **kwargs)
        await asyncio.wait_for(entered.wait(), 1)
        await dispatch(store, text_update("again"), bot, **kwargs)
        await dispatch(store, text_update("and again"), bot, **kwargs)
        assert len(calls) == 1
        assert tasks.active(1)
        assert store.get(1).stage is Stage.SCRIPTING
        await tasks.close()
        assert not tasks.active(1)

    asyncio.run(scenario())


def test_scripting_chats_stay_independent(tmp_path):
    async def scenario():
        store = InMemorySessionStore()
        at_scripting(store, 1)
        at_scripting(store, 2)
        tasks = bot_module.ConversionTasks(store)
        started, finished = instrument(tasks)
        entered = asyncio.Event()
        writer = AsyncMock()

        async def write(summary):
            if summary is store.get(1).interview.summary:
                entered.set()
                await asyncio.Event().wait()
            return script()

        writer.write_script.side_effect = write
        bot = AsyncMock()
        kwargs = {"scripter": writer, "media_root": tmp_path, "conversion_tasks": tasks}
        await dispatch(store, text_update("go", 1), bot, **kwargs)
        await asyncio.wait_for(entered.wait(), 1)
        await dispatch(store, text_update("go", 2), bot, **kwargs)
        await asyncio.wait_for(finished[2].wait(), 1)
        assert started == [(1, True), (2, True)]  # One task per chat, both accepted.
        assert store.get(2).narration_text == NARRATION
        assert store.get(2).stage is Stage.NARRATING
        assert store.get(1).stage is Stage.SCRIPTING
        assert store.get(1).narration_text is None
        assert tasks.active(1)
        await tasks.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["generation", "send"])
@pytest.mark.parametrize("swallow", [False, True])
def test_shutdown_cancels_scripting_in_both_phases(tmp_path, phase, swallow):
    async def scenario():
        store = InMemorySessionStore()
        at_scripting(store)
        tasks = bot_module.ConversionTasks(store)
        entered = asyncio.Event()

        async def block():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if not swallow:
                    raise

        writer = AsyncMock()

        async def write(summary):
            if phase == "generation":
                await block()
            return script()

        writer.write_script.side_effect = write
        bot = AsyncMock()

        async def send_message(**kwargs):
            if phase == "send" and kwargs.get("text") == NARRATION:
                await block()
            return SimpleNamespace(message_id=1)

        bot.send_message.side_effect = send_message
        await dispatch(store, text_update("go"), bot, scripter=writer, media_root=tmp_path,
                       conversion_tasks=tasks)
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(tasks.close(), 2)
        assert store.get(1).stage is Stage.SCRIPTING
        assert store.get(1).narration_text is None
        assert sum(text is not None and "having a moment" in text
                   for text in narration_sends(bot)) == 0
        if phase == "generation":
            assert NARRATION not in narration_sends(bot)

    asyncio.run(scenario())


def routed_app(monkeypatch, store, root, bot, write):
    """Build the production application with mocked providers and transport.

    Returns the app plus a ``route`` that pushes a real ``Update`` through the
    registered handlers, and the instrumented registry the app itself created.
    """
    from telegram import Update, User

    keys = []

    class StubScripter:
        def __init__(self, key):
            keys.append(key)

        async def write_script(self, summary):
            return await write(summary)

    class UnusedConverter:
        def __init__(self, key):
            pass

        async def generate_avatar(self, path, summary):
            raise AssertionError("Conversion must not rerun at SCRIPTING")

    monkeypatch.setattr(bot_module, "LazyScripter", StubScripter)
    monkeypatch.setattr(bot_module, "LazyConverter", UnusedConverter)
    monkeypatch.setattr(bot_module, "MEDIA_ROOT", root)
    registries = capture_registry(monkeypatch)
    app = build_application(Settings(telegram_bot_token="123:dummy"), store)
    assert keys  # The production app wires a lazily-built Scripter.
    assert len(registries) == 1  # Both stages share the one registry.
    started, finished = instrument(registries[0])
    app.bot._bot_user = User(id=1, first_name="Test", is_bot=True, username="testbot")

    async def route(text):
        message = {"message_id": 1, "date": 1760000000,
                   "chat": {"id": 1, "type": "private"}, "text": text}
        if text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text)}]
        incoming = Update.de_json({"update_id": 1, "message": message}, app.bot)
        for handler in app.handlers[0]:
            matched = handler.check_update(incoming)
            if matched is not None and matched is not False:
                await handler.callback(incoming, SimpleNamespace(bot=bot))
                return
        raise AssertionError("No matching handler")

    return app, route, started, finished


def test_registered_handlers_script_with_mocked_transport(tmp_path, monkeypatch):
    async def scenario():
        store = InMemorySessionStore()
        at_scripting(store)
        bot = AsyncMock()

        async def write(summary):
            return script()

        app, route, started, finished = routed_app(monkeypatch, store, tmp_path, bot, write)
        assert app.concurrent_updates == 1
        assert all(handler.block for handler in app.handlers[0])
        await asyncio.wait_for(route("try again"), 1)
        await asyncio.wait_for(finished[1].wait(), 1)
        assert started == [(1, True)]
        assert store.get(1).narration_text == NARRATION
        assert store.get(1).stage is Stage.NARRATING
        assert narration_sends(bot) == [NARRATION]

        # NARRATING is terminal for Feature 5: no audio and no new generation.
        finished.clear()
        await asyncio.wait_for(route("anything else"), 1)
        assert not finished  # No second registry task once narration is committed.
        assert started == [(1, True)]
        assert store.get(1).stage is Stage.NARRATING
        assert store.get(1).narration_text == NARRATION
        assert narration_sends(bot).count(NARRATION) == 1
        assert_no_audio(bot)
        assert app.post_stop is not None
        await app.post_stop(app)

    asyncio.run(scenario())


@pytest.mark.parametrize("command", ["/start", "/restart"])
@pytest.mark.parametrize("phase", ["generation", "send"])
def test_registered_handlers_reset_during_scripting(tmp_path, monkeypatch, command, phase):
    """A reset routed through the real handlers must strand a resistant provider."""
    async def scenario():
        store = InMemorySessionStore()
        at_scripting(store)
        store.get(1).narration_text = None
        portrait = photo_dest(1, root=tmp_path)
        portrait.parent.mkdir(parents=True, exist_ok=True)
        portrait.write_bytes(b"portrait")
        avatar_dest(1, root=tmp_path).write_bytes(PNG)
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def block():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()  # Resistant provider returns after the reset.

        async def write(summary):
            if phase == "generation":
                await block()
            return script()

        bot = AsyncMock()

        async def send_message(**kwargs):
            if phase == "send" and kwargs.get("text") == NARRATION:
                await block()
            return SimpleNamespace(message_id=1)

        bot.send_message.side_effect = send_message
        app, route, started, finished = routed_app(monkeypatch, store, tmp_path, bot, write)
        await asyncio.wait_for(route("try again"), 1)
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(route(command), 1)
        await asyncio.wait_for(cancelled.wait(), 1)
        assert store.get(1).stage is Stage.AWAITING_PHOTO
        assert store.get(1).narration_text is None
        assert store.get(1).interview is None
        assert not portrait.parent.exists()  # The reset purged this chat's media.
        assert GREETING_TEXT in narration_sends(bot)

        # The same chat starts again while the old task is still suspended.
        at_scripting(store)
        store.get(1).narration_text = "A newer creature stirs."
        portrait.parent.mkdir(parents=True, exist_ok=True)
        portrait.write_bytes(b"new portrait")
        new_avatar = avatar_dest(1, root=tmp_path)
        new_avatar.write_bytes(PNG)
        release.set()
        await asyncio.wait_for(finished[1].wait(), 1)
        assert store.get(1).stage is Stage.SCRIPTING
        assert store.get(1).narration_text == "A newer creature stirs."
        assert portrait.read_bytes() == b"new portrait"
        assert new_avatar.read_bytes() == PNG
        assert narration_sends(bot).count(NARRATION) <= (1 if phase == "send" else 0)
        assert sum(text is not None and "having a moment" in text
                   for text in narration_sends(bot)) == 0
        assert started == [(1, True)]  # The reset started no replacement task.
        assert app.post_stop is not None
        await app.post_stop(app)

    asyncio.run(scenario())


def test_narrating_starts_no_further_scripting_work(tmp_path):
    async def scenario():
        store = InMemorySessionStore()
        at_scripting(store)
        tasks = bot_module.ConversionTasks(store)
        started, finished = instrument(tasks)
        writer = AsyncMock()
        writer.write_script.return_value = script()
        bot = AsyncMock()
        converter = AsyncMock()
        kwargs = {"scripter": writer, "converter": converter,
                  "send_photo": AsyncMock(return_value=True), "media_root": tmp_path,
                  "conversion_tasks": tasks}
        await dispatch(store, text_update("go"), bot, **kwargs)
        await asyncio.wait_for(finished[1].wait(), 1)
        assert store.get(1).stage is Stage.NARRATING
        finished.clear()
        await dispatch(store, text_update("more please"), bot, **kwargs)
        assert not finished  # Nothing is scheduled once narration is committed.
        assert writer.write_script.await_count == 1
        assert started == [(1, True)]
        converter.generate_avatar.assert_not_awaited()
        assert store.get(1).stage is Stage.NARRATING
        assert store.get(1).narration_text == NARRATION
        assert_no_audio(bot)
        await tasks.close()

    asyncio.run(scenario())
