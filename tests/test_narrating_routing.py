"""Narration routes through the one existing per-chat task registry, not a new one.

Covers the bot-level ``narrator=`` dependency: the Scripter's automatic handoff
inside the same task, NARRATING-only retries, resets during generation,
encoding and voice delivery, duplicate updates, cross-chat independence,
shutdown, the real registered Telegram handlers with a mocked transport, and the
terminal DONE stage. All providers, codecs and Telegram calls are offline.
"""
import asyncio
import io
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from image_fixtures import PNG

from app import bot as bot_module
from app import orchestrator
from app.audio_format import NarrationAudio
from app.bot import build_application, dispatch, dispatch_safely
from app.config import Settings
from app.handlers import GREETING_TEXT, NARRATOR_FAILURE_TEXT
from app.logging_config import FRIENDLY_ERROR
from app.media import avatar_dest, photo_dest
from app.session_store import InMemorySessionStore
from app.states import InterviewState, Stage, TraitSummary

NARRATION = "Here we observe a quietly bold specimen, devoted entirely to its evening rituals."
RATE = 24000
PCM_MIME = "audio/L16;codec=pcm;rate=24000"
PCM = b"\x01\x02" * (RATE // 4)
OGG = b"OggS" + bytes(20) + b"OpusHead" + b"offline-encoded-voice"


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


def retry_args(kind):
    """The ``route`` arguments for one non-reset update of each supported kind."""
    if kind == "photo":
        return (None, "photo")
    return ("/wildlife" if kind == "command" else "try again", "text")


def script(text=NARRATION):
    from app.scripter import NarrationScript

    return NarrationScript.model_construct(text=text)


def audio():
    return NarrationAudio(data=PCM, mime_type=PCM_MIME)


def interview():
    return InterviewState(summary=TraitSummary(summary="bold", traits=["bold"]))


def at_narrating(store, chat=1, text=NARRATION):
    state = store.get(chat)
    state.stage = Stage.NARRATING
    state.interview = interview()
    state.narration_text = text
    return state


def at_scripting(store, chat=1):
    state = store.get(chat)
    state.stage = Stage.SCRIPTING
    state.interview = interview()
    return state


def at_converting(store, root, chat=1):
    state = store.get(chat)
    state.stage = Stage.CONVERTING
    state.interview = interview()
    portrait = photo_dest(chat, root=root)
    portrait.parent.mkdir(parents=True, exist_ok=True)
    portrait.write_bytes(b"portrait")
    return state


def instrument(tasks):
    """Record registry starts and signal each task's end, through the public API only."""
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


def text_sends(bot):
    return [call.kwargs.get("text") for call in bot.send_message.await_args_list]


def voice_paths(bot):
    return [call.kwargs.get("voice") for call in bot.send_voice.await_args_list]


async def encode_ok(data):
    """A codec fake that insists on being handed a correctly wrapped WAV."""
    with wave.open(io.BytesIO(data)) as source:
        assert (source.getnchannels(), source.getsampwidth(),
                source.getframerate()) == (1, 2, RATE)
    return OGG


def narrator_stub(render=None):
    narrator = AsyncMock()
    if render is None:
        narrator.render.return_value = audio()
    else:
        narrator.render.side_effect = render
    return narrator


def narration_kwargs(tmp_path, narrator, send_voice=None, encode=None, tasks=None,
                     validate=None):
    kwargs = {"narrator": narrator, "encode_audio": encode or encode_ok,
              "validate_audio": validate or AsyncMock(), "media_root": tmp_path}
    if send_voice is not None:
        kwargs["send_voice"] = send_voice
    if tasks is not None:
        kwargs["conversion_tasks"] = tasks
    return kwargs


def test_scripter_handoff_narrates_inside_the_same_task(tmp_path):
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

        async def write(summary):
            order.append("script")
            return script()

        writer = AsyncMock()
        writer.write_script.side_effect = write

        async def render(text):
            order.append("render")
            return audio()

        async def send_voice(chat, path):
            order.append("voice")
            assert path.suffix == ".ogg"
            assert path.read_bytes() == OGG
            assert store.get(chat).stage is Stage.NARRATING
            return True

        bot = AsyncMock()
        bot.send_message.side_effect = lambda **kwargs: order.append("narration")
        reply = await dispatch(store, text_update("go"), bot, converter=converter,
                               send_photo=send_photo, scripter=writer,
                               **narration_kwargs(tmp_path, narrator_stub(render),
                                                  send_voice=send_voice, tasks=tasks))
        await asyncio.wait_for(finished[1].wait(), 1)
        assert reply is None
        assert order == ["photo", "script", "narration", "render", "voice"]
        assert started == [(1, True)]  # One task carries all three stages.
        assert store.get(1).narration_text == NARRATION
        assert store.get(1).stage is Stage.DONE
        assert text_sends(bot) == [NARRATION]
        assert list((tmp_path / "1").glob("*.ogg")) == []
        await tasks.close()

    asyncio.run(scenario())


def test_direct_seam_narrates_without_a_registry(tmp_path):
    async def scenario():
        store = InMemorySessionStore()
        at_narrating(store)
        send_voice = AsyncMock(return_value=True)
        bot = AsyncMock()
        reply = await dispatch(store, text_update("go"), bot,
                               **narration_kwargs(tmp_path, narrator_stub(),
                                                  send_voice=send_voice))
        assert reply is None
        send_voice.assert_awaited_once()
        assert send_voice.await_args.args[0] == 1
        assert store.get(1).stage is Stage.DONE
        assert store.get(1).narration_text == NARRATION
        assert text_sends(bot) == []

    asyncio.run(scenario())


def test_direct_seam_without_a_narrator_stays_at_narrating(tmp_path):
    """Earlier-stage isolation tests may omit the Narrator; nothing may run."""
    async def scenario():
        store = InMemorySessionStore()
        at_narrating(store)
        bot = AsyncMock()
        converter, writer = AsyncMock(), AsyncMock()
        reply = await dispatch(store, text_update("go"), bot, converter=converter,
                               send_photo=AsyncMock(return_value=True), scripter=writer,
                               media_root=tmp_path)
        assert reply is None
        converter.generate_avatar.assert_not_awaited()
        writer.write_script.assert_not_awaited()
        bot.send_voice.assert_not_awaited()
        assert store.get(1).stage is Stage.NARRATING

    asyncio.run(scenario())


def test_live_wiring_uses_the_default_codec_seam(tmp_path, monkeypatch):
    """Without injected codec kwargs the module-level defaults are used."""
    async def scenario():
        store = InMemorySessionStore()
        at_narrating(store)
        seen = {}

        async def encode(data):
            seen["wav"] = data
            return OGG

        async def validate(data):
            seen["encoded"] = data

        monkeypatch.setattr(orchestrator, "encode_voice", encode)
        monkeypatch.setattr(orchestrator, "validate_voice", validate)
        send_voice = AsyncMock(return_value=True)
        await dispatch(store, text_update("go"), AsyncMock(), narrator=narrator_stub(),
                       send_voice=send_voice, media_root=tmp_path)
        assert seen["wav"].startswith(b"RIFF")
        assert seen["encoded"] == OGG
        assert store.get(1).stage is Stage.DONE

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["text", "photo", "command"])
def test_retry_at_narrating_never_rescripts_or_reconverts(tmp_path, kind):
    async def scenario():
        store = InMemorySessionStore()
        at_narrating(store)
        tasks = bot_module.ConversionTasks(store)
        converter, bouncer, writer = AsyncMock(), AsyncMock(), AsyncMock()
        narrator = narrator_stub()
        send_voice = AsyncMock(return_value=True)
        bot = AsyncMock()
        reply = await dispatch(store, make_update(kind), bot, converter=converter,
                               send_photo=AsyncMock(return_value=True), bouncer=bouncer,
                               interviewer=AsyncMock(), scripter=writer,
                               **narration_kwargs(tmp_path, narrator, send_voice=send_voice,
                                                  tasks=tasks))
        await asyncio.wait_for(tasks.wait(), 1)
        assert reply is None
        converter.generate_avatar.assert_not_awaited()
        writer.write_script.assert_not_awaited()
        bouncer.inspect.assert_not_awaited()
        bot.get_file.assert_not_awaited()  # A photo at NARRATING never downloads.
        narrator.render.assert_awaited_once_with(NARRATION)
        send_voice.assert_awaited_once()
        assert store.get(1).stage is Stage.DONE
        await tasks.close()

    asyncio.run(scenario())


def test_scripting_retry_finishes_the_pipeline_in_one_task(tmp_path):
    async def scenario():
        store = InMemorySessionStore()
        at_scripting(store)
        tasks = bot_module.ConversionTasks(store)
        started, finished = instrument(tasks)
        writer = AsyncMock()
        writer.write_script.return_value = script()
        narrator = narrator_stub()
        send_voice = AsyncMock(return_value=True)
        bot = AsyncMock()
        await dispatch(store, text_update("try again"), bot, scripter=writer,
                       **narration_kwargs(tmp_path, narrator, send_voice=send_voice,
                                          tasks=tasks))
        await asyncio.wait_for(finished[1].wait(), 1)
        assert started == [(1, True)]
        writer.write_script.assert_awaited_once()
        narrator.render.assert_awaited_once_with(NARRATION)
        assert store.get(1).stage is Stage.DONE
        assert text_sends(bot) == [NARRATION]
        await tasks.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("command", ["/start", "/restart"])
@pytest.mark.parametrize("phase", ["generation", "encode", "validate", "send"])
@pytest.mark.parametrize("late_result", [False, True])
def test_reset_during_narration_cannot_commit_or_touch_a_new_session(
    tmp_path, command, phase, late_result
):
    async def scenario():
        store = InMemorySessionStore()
        at_narrating(store)
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

        async def render(text):
            if phase == "generation":
                await block()
            return audio()

        async def encode(data):
            if phase == "encode":
                await block()
            return await encode_ok(data)

        async def validate(data):
            if phase == "validate":
                await block()

        async def send_voice(chat, path):
            if phase == "send":
                await block()
            return True

        sender = AsyncMock(side_effect=send_voice)
        bot = AsyncMock()
        await dispatch(store, text_update("go"), bot,
                       **narration_kwargs(tmp_path, narrator_stub(render), send_voice=sender,
                                          encode=encode, tasks=tasks, validate=validate))
        await asyncio.wait_for(entered.wait(), 1)
        assert tasks.active(1)
        await dispatch(store, text_update(command), bot, media_root=tmp_path,
                       conversion_tasks=tasks)
        await asyncio.wait_for(cancelled.wait(), 1)
        assert store.get(1).stage is Stage.AWAITING_PHOTO
        assert store.get(1).narration_text is None

        # A new session may reuse the same chat: the old attempt owns none of it.
        at_narrating(store, text="A newer creature stirs.")
        portrait = photo_dest(1, root=tmp_path)
        portrait.parent.mkdir(parents=True, exist_ok=True)
        portrait.write_bytes(b"new portrait")
        avatar = avatar_dest(1, root=tmp_path)
        avatar.write_bytes(PNG)
        replacement = tmp_path / "1" / "narration-9-replacement.ogg"
        replacement.write_bytes(OGG)
        release.set()
        await asyncio.wait_for(tasks.wait(), 1)
        assert store.get(1).stage is Stage.NARRATING
        assert store.get(1).narration_text == "A newer creature stirs."
        assert portrait.read_bytes() == b"new portrait"
        assert avatar.read_bytes() == PNG
        assert replacement.read_bytes() == OGG
        assert sender.await_count == (1 if phase == "send" else 0)
        assert sum(text is not None and "having a moment" in text
                   for text in text_sends(bot)) == 0
        await tasks.close()

    asyncio.run(scenario())


def test_duplicate_updates_do_not_overlap_narration(tmp_path):
    async def scenario():
        store = InMemorySessionStore()
        at_narrating(store)
        tasks = bot_module.ConversionTasks(store)
        entered = asyncio.Event()
        calls = []

        async def render(text):
            calls.append(text)
            entered.set()
            await asyncio.Event().wait()

        bot = AsyncMock()
        kwargs = narration_kwargs(tmp_path, narrator_stub(render),
                                  send_voice=AsyncMock(return_value=True), tasks=tasks)
        await dispatch(store, text_update("go"), bot, **kwargs)
        await asyncio.wait_for(entered.wait(), 1)
        await dispatch(store, text_update("again"), bot, **kwargs)
        await dispatch(store, text_update("and again"), bot, **kwargs)
        assert calls == [NARRATION]
        assert tasks.active(1)
        assert store.get(1).stage is Stage.NARRATING
        await tasks.close()
        assert not tasks.active(1)

    asyncio.run(scenario())


def test_narrating_chats_stay_independent(tmp_path):
    async def scenario():
        store = InMemorySessionStore()
        at_narrating(store, 1)
        at_narrating(store, 2, text="A second specimen takes the stage this evening.")
        tasks = bot_module.ConversionTasks(store)
        started, finished = instrument(tasks)
        entered = asyncio.Event()

        async def render(text):
            if text == NARRATION:
                entered.set()
                await asyncio.Event().wait()
            return audio()

        sent = []

        async def send_voice(chat, path):
            sent.append(chat)
            return True

        bot = AsyncMock()
        kwargs = narration_kwargs(tmp_path, narrator_stub(render),
                                  send_voice=AsyncMock(side_effect=send_voice), tasks=tasks)
        await dispatch(store, text_update("go", 1), bot, **kwargs)
        await asyncio.wait_for(entered.wait(), 1)
        await dispatch(store, text_update("go", 2), bot, **kwargs)
        await asyncio.wait_for(finished[2].wait(), 1)
        assert started == [(1, True), (2, True)]
        assert sent == [2]
        assert store.get(2).stage is Stage.DONE
        assert store.get(1).stage is Stage.NARRATING
        assert tasks.active(1)
        tasks.cancel(2)
        assert tasks.active(1)
        await tasks.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["generation", "encode", "validate", "send"])
@pytest.mark.parametrize("swallow", [False, True])
def test_shutdown_cancels_narration_in_every_phase(tmp_path, phase, swallow):
    async def scenario():
        store = InMemorySessionStore()
        at_narrating(store)
        tasks = bot_module.ConversionTasks(store)
        entered = asyncio.Event()

        async def block():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if not swallow:
                    raise

        async def render(text):
            if phase == "generation":
                await block()
            return audio()

        async def encode(data):
            if phase == "encode":
                await block()
            return await encode_ok(data)

        async def validate(data):
            if phase == "validate":
                await block()

        async def send_voice(chat, path):
            if phase == "send":
                await block()
            return True

        sender = AsyncMock(side_effect=send_voice)
        bot = AsyncMock()
        await dispatch(store, text_update("go"), bot,
                       **narration_kwargs(tmp_path, narrator_stub(render), send_voice=sender,
                                          encode=encode, tasks=tasks, validate=validate))
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(tasks.close(), 2)
        assert store.get(1).stage is Stage.NARRATING  # no post-shutdown commit
        assert store.get(1).narration_text == NARRATION
        assert sender.await_count == (1 if phase == "send" else 0)
        assert sum(text is not None and "having a moment" in text
                   for text in text_sends(bot)) == 0

    asyncio.run(scenario())


def test_dispatch_safely_reports_a_friendly_error_if_narration_routing_raises(tmp_path,
                                                                             monkeypatch):
    async def scenario():
        store = InMemorySessionStore()
        at_narrating(store)

        async def boom(*args, **kwargs):
            raise RuntimeError("SentinelRoutingFailure")

        monkeypatch.setattr(orchestrator, "handle_narrating_update", boom)
        bot = AsyncMock()
        reply = await dispatch_safely(store, text_update("go"), bot,
                                      **narration_kwargs(tmp_path, narrator_stub(),
                                                         send_voice=AsyncMock(return_value=True)))
        assert reply is None
        assert text_sends(bot) == [FRIENDLY_ERROR]
        assert store.get(1).stage is Stage.NARRATING

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["text", "photo", "command"])
def test_done_is_terminal_for_every_non_reset_update(tmp_path, kind):
    async def scenario():
        store = InMemorySessionStore()
        at_narrating(store)
        store.set_stage(1, Stage.DONE)
        tasks = bot_module.ConversionTasks(store)
        started, finished = instrument(tasks)
        converter, writer, bouncer = AsyncMock(), AsyncMock(), AsyncMock()
        narrator = narrator_stub()
        send_voice = AsyncMock(return_value=True)
        bot = AsyncMock()
        kwargs = {"converter": converter, "send_photo": AsyncMock(return_value=True),
                  "bouncer": bouncer, "interviewer": AsyncMock(), "scripter": writer,
                  **narration_kwargs(tmp_path, narrator, send_voice=send_voice, tasks=tasks)}
        await dispatch(store, make_update(kind), bot, **kwargs)
        await asyncio.wait_for(tasks.wait(), 1)
        assert started == []  # DONE starts no pipeline task at all.
        assert not finished
        narrator.render.assert_not_awaited()
        writer.write_script.assert_not_awaited()
        converter.generate_avatar.assert_not_awaited()
        bouncer.inspect.assert_not_awaited()
        bot.get_file.assert_not_awaited()
        send_voice.assert_not_awaited()
        assert store.get(1).stage is Stage.DONE
        assert store.get(1).narration_text == NARRATION

        # Reset commands still win from the terminal stage.
        await dispatch(store, text_update("/restart"), bot, **kwargs)
        assert store.get(1).stage is Stage.AWAITING_PHOTO
        assert store.get(1).narration_text is None
        assert GREETING_TEXT in text_sends(bot)
        await tasks.close()

    asyncio.run(scenario())


def routed_app(monkeypatch, store, root, bot, render, encode=None, validate=None):
    """Build the production application with mocked providers, codec and transport."""
    from telegram import Update, User

    keys = []

    class StubNarrator:
        def __init__(self, key):
            keys.append(key)

        async def render(self, text):
            return await render(text)

    class UnusedScripter:
        def __init__(self, key):
            pass

        async def write_script(self, summary):
            raise AssertionError("Scripting must not rerun at NARRATING")

    class UnusedConverter:
        def __init__(self, key):
            pass

        async def generate_avatar(self, path, summary):
            raise AssertionError("Conversion must not rerun at NARRATING")

    async def validate_ok(data):
        assert data == OGG

    monkeypatch.setattr(bot_module, "LazyNarrator", StubNarrator)
    monkeypatch.setattr(bot_module, "LazyScripter", UnusedScripter)
    monkeypatch.setattr(bot_module, "LazyConverter", UnusedConverter)
    monkeypatch.setattr(bot_module, "MEDIA_ROOT", root)
    monkeypatch.setattr(orchestrator, "encode_voice", encode or encode_ok)
    monkeypatch.setattr(orchestrator, "validate_voice", validate or validate_ok)
    registries = capture_registry(monkeypatch)
    app = build_application(Settings(telegram_bot_token="123:dummy"), store)
    assert keys  # The production app wires a lazily-built Narrator.
    assert len(registries) == 1  # Every stage shares the one registry.
    started, finished = instrument(registries[0])
    app.bot._bot_user = User(id=1, first_name="Test", is_bot=True, username="testbot")

    async def route(text, kind="text"):
        message = {"message_id": 1, "date": 1760000000, "chat": {"id": 1, "type": "private"}}
        if kind == "photo":
            message["photo"] = [{"file_id": "file-1", "file_unique_id": "unique-1",
                                 "width": 2, "height": 2, "file_size": 10}]
        else:
            message["text"] = text
            if text.startswith("/"):
                message["entities"] = [{"type": "bot_command", "offset": 0,
                                        "length": len(text)}]
        incoming = Update.de_json({"update_id": 1, "message": message}, app.bot)
        for handler in app.handlers[0]:
            matched = handler.check_update(incoming)
            if matched is not None and matched is not False:
                await handler.callback(incoming, SimpleNamespace(bot=bot))
                return
        raise AssertionError("No matching handler")

    return app, route, started, finished


@pytest.mark.parametrize("kind", ["text", "photo", "command"])
def test_registered_handlers_narrate_with_mocked_transport(tmp_path, monkeypatch, kind):
    async def scenario():
        store = InMemorySessionStore()
        at_narrating(store)
        delivered = []
        bot = AsyncMock()

        async def send_voice(chat_id, voice):
            assert isinstance(voice, Path)
            assert voice.suffix == ".ogg"
            assert voice.read_bytes() == OGG
            assert store.get(1).stage is Stage.NARRATING
            delivered.append(voice)
            return SimpleNamespace(message_id=2)

        bot.send_voice.side_effect = send_voice

        async def render(text):
            return audio()

        app, route, started, finished = routed_app(monkeypatch, store, tmp_path, bot, render)
        assert app.concurrent_updates == 1
        assert all(handler.block for handler in app.handlers[0])
        await asyncio.wait_for(route(*retry_args(kind)), 1)
        await asyncio.wait_for(finished[1].wait(), 1)
        assert started == [(1, True)]
        bot.get_file.assert_not_awaited()  # A photo at NARRATING never downloads.
        assert len(delivered) == 1
        assert voice_paths(bot) == delivered
        assert not delivered[0].exists()  # cleaned up after a confirmed delivery
        assert store.get(1).stage is Stage.DONE
        assert store.get(1).narration_text == NARRATION
        assert text_sends(bot) == []

        # DONE is terminal: no extra task, generation, download or delivery.
        finished.clear()
        await asyncio.wait_for(route(*retry_args(kind)), 1)
        assert not finished
        assert started == [(1, True)]
        assert bot.send_voice.await_count == 1
        bot.get_file.assert_not_awaited()
        assert store.get(1).stage is Stage.DONE
        assert app.post_stop is not None
        await app.post_stop(app)

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["text", "photo", "command"])
def test_registered_handlers_retry_after_a_narrator_failure(tmp_path, monkeypatch, kind):
    async def scenario():
        store = InMemorySessionStore()
        at_narrating(store)
        bot = AsyncMock()
        attempts = []

        async def render(text):
            attempts.append(text)
            if len(attempts) == 1:
                raise RuntimeError("SentinelProviderFailure")
            return audio()

        app, route, started, finished = routed_app(monkeypatch, store, tmp_path, bot, render)
        await asyncio.wait_for(route(*retry_args(kind)), 1)
        await asyncio.wait_for(finished[1].wait(), 1)
        assert store.get(1).stage is Stage.NARRATING
        assert store.get(1).narration_text == NARRATION
        assert text_sends(bot) == [NARRATOR_FAILURE_TEXT]
        bot.send_voice.assert_not_awaited()
        bot.get_file.assert_not_awaited()  # No download, script or conversion on retry.
        assert list((tmp_path / "1").glob("*.ogg")) == []

        finished.clear()
        await asyncio.wait_for(route(*retry_args(kind)), 1)
        await asyncio.wait_for(finished[1].wait(), 1)
        assert attempts == [NARRATION, NARRATION]
        assert started == [(1, True), (1, True)]
        assert bot.send_voice.await_count == 1
        assert store.get(1).stage is Stage.DONE
        assert app.post_stop is not None
        await app.post_stop(app)

    asyncio.run(scenario())


@pytest.mark.parametrize("command", ["/start", "/restart"])
@pytest.mark.parametrize("phase", ["generation", "encode", "validate", "send"])
def test_registered_handlers_reset_during_narration(tmp_path, monkeypatch, command, phase):
    """A reset routed through the real handlers must strand a resistant provider."""
    async def scenario():
        store = InMemorySessionStore()
        at_narrating(store)
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

        async def render(text):
            if phase == "generation":
                await block()
            return audio()

        async def encode(data):
            if phase == "encode":
                await block()
            return await encode_ok(data)

        async def validate(data):
            if phase == "validate":
                await block()

        bot = AsyncMock()

        async def send_voice(chat_id, voice):
            if phase == "send":
                await block()
            return SimpleNamespace(message_id=2)

        bot.send_voice.side_effect = send_voice
        app, route, started, finished = routed_app(monkeypatch, store, tmp_path, bot, render,
                                                   encode=encode, validate=validate)
        await asyncio.wait_for(route("try again"), 1)
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(route(command), 1)
        await asyncio.wait_for(cancelled.wait(), 1)
        assert store.get(1).stage is Stage.AWAITING_PHOTO
        assert store.get(1).narration_text is None
        assert store.get(1).interview is None
        assert not portrait.parent.exists()  # The reset purged this chat's media.
        assert GREETING_TEXT in text_sends(bot)

        # The same chat starts again while the old attempt is still suspended.
        at_narrating(store, text="A newer creature stirs.")
        portrait.parent.mkdir(parents=True, exist_ok=True)
        portrait.write_bytes(b"new portrait")
        new_avatar = avatar_dest(1, root=tmp_path)
        new_avatar.write_bytes(PNG)
        replacement = tmp_path / "1" / "narration-9-replacement.ogg"
        replacement.write_bytes(OGG)
        release.set()
        await asyncio.wait_for(finished[1].wait(), 1)
        assert store.get(1).stage is Stage.NARRATING  # never a stale DONE
        assert store.get(1).narration_text == "A newer creature stirs."
        assert portrait.read_bytes() == b"new portrait"
        assert new_avatar.read_bytes() == PNG
        assert replacement.read_bytes() == OGG
        assert bot.send_voice.await_count == (1 if phase == "send" else 0)
        assert sum(text is not None and "having a moment" in text
                   for text in text_sends(bot)) == 0
        assert started == [(1, True)]  # The reset started no replacement task.
        assert app.post_stop is not None
        await app.post_stop(app)

    asyncio.run(scenario())
