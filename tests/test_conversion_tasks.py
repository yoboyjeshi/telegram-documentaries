"""Reset must interrupt conversion without enabling concurrent update handlers."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from image_fixtures import PNG

from app import bot as bot_module
from app.bot import build_application, dispatch
from app.config import Settings
from app.media import photo_dest
from app.session_store import InMemorySessionStore
from app.states import InterviewState, Stage, TraitSummary


def update(text, chat=1):
    return SimpleNamespace(effective_message=SimpleNamespace(text=text, chat=SimpleNamespace(id=chat)))


def prepare(store, root, chat=1):
    state = store.get(chat)
    state.stage = Stage.CONVERTING
    state.interview = InterviewState(summary=TraitSummary(summary='bold'))
    portrait = photo_dest(chat, root=root)
    portrait.parent.mkdir(parents=True, exist_ok=True)
    portrait.write_bytes(b'portrait')


@pytest.mark.parametrize('command', ['/start', '/restart'])
@pytest.mark.parametrize('late_result', [False, True])
def test_reset_interrupts_generation_and_blocks_late_result(tmp_path, command, late_result):
    async def scenario():
        store = InMemorySessionStore()
        prepare(store, tmp_path)
        tasks = bot_module.ConversionTasks(store)
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()
        class Slow:
            async def generate_avatar(self, path, summary):
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    if not late_result:
                        raise
                    await release.wait()
                return PNG
        bot, send = AsyncMock(), AsyncMock(return_value=True)
        await dispatch(store, update('go'), bot, converter=Slow(), send_photo=send,
                       media_root=tmp_path, conversion_tasks=tasks)
        await asyncio.wait_for(entered.wait(), 1)
        assert tasks.active(1)
        await dispatch(store, update(command), bot, media_root=tmp_path, conversion_tasks=tasks)
        await asyncio.wait_for(cancelled.wait(), 1)
        assert store.get(1).stage is Stage.AWAITING_PHOTO
        assert not photo_dest(1, root=tmp_path).parent.exists()
        prepare(store, tmp_path)  # A new session may reuse the same chat directory.
        release.set()
        await asyncio.wait_for(tasks.wait(), 1)
        await tasks.close()
        send.assert_not_awaited()
        assert store.get(1).stage is Stage.CONVERTING
        assert photo_dest(1, root=tmp_path).exists()
        assert bot.send_message.await_count == 1  # Reset reply, no stale retry.
    asyncio.run(scenario())


def test_one_active_conversion_other_chat_and_shutdown(tmp_path):
    async def scenario():
        store = InMemorySessionStore()
        prepare(store, tmp_path)
        tasks = bot_module.ConversionTasks(store)
        entered = asyncio.Event()
        calls = []
        class Slow:
            async def generate_avatar(self, path, summary):
                calls.append(path)
                entered.set()
                await asyncio.Event().wait()
        bot, send = AsyncMock(), AsyncMock()
        kwargs = {"converter": Slow(), "send_photo": send, "media_root": tmp_path,
                  "conversion_tasks": tasks}
        await dispatch(store, update('go'), bot, **kwargs)
        await asyncio.wait_for(entered.wait(), 1)
        await dispatch(store, update('again'), bot, **kwargs)
        await dispatch(store, update('/start', 2), bot, **kwargs)
        assert len(calls) == 1
        assert store.get(2).stage is Stage.AWAITING_PHOTO
        assert tasks.active(1)
        await tasks.close()
        assert not tasks.active(1)
        send.assert_not_awaited()
    asyncio.run(scenario())


def test_application_uses_registry_and_keeps_sequential_handlers(tmp_path, monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        class Slow:
            def __init__(self, key):
                pass
            async def generate_avatar(self, path, summary):
                entered.set()
                await asyncio.Event().wait()
        monkeypatch.setattr(bot_module, 'LazyConverter', Slow)
        monkeypatch.setattr(bot_module, 'MEDIA_ROOT', tmp_path)
        store = InMemorySessionStore()
        prepare(store, tmp_path)
        app = build_application(Settings(telegram_bot_token='123:dummy'), store)
        assert app.concurrent_updates == 1
        assert all(handler.block for handler in app.handlers[0])
        from telegram import Update, User
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
        bot = AsyncMock()
        await asyncio.wait_for(route('go'), 1)
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(route('/restart'), 1)
        assert store.get(1).stage is Stage.AWAITING_PHOTO
        assert app.post_stop is not None
        await app.post_stop(app)
        bot.send_photo.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize('command', ['/start', '/restart'])
def test_reset_during_send_does_not_advance_or_delete_new_media(tmp_path, command):
    from image_fixtures import PNG

    from app.media import avatar_dest
    async def scenario():
        store = InMemorySessionStore()
        prepare(store, tmp_path)
        tasks = bot_module.ConversionTasks(store)
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        converter = AsyncMock()
        converter.generate_avatar.return_value = PNG
        async def send(chat, path, caption):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()  # Simulate an already-started send finishing late.
            return True
        bot = AsyncMock()
        await dispatch(store, update('go'), bot, converter=converter, send_photo=send,
                       media_root=tmp_path, conversion_tasks=tasks)
        await asyncio.wait_for(entered.wait(), 1)
        await dispatch(store, update(command), bot, media_root=tmp_path, conversion_tasks=tasks)
        await asyncio.wait_for(cancelled.wait(), 1)
        assert not avatar_dest(1, root=tmp_path).exists()
        prepare(store, tmp_path)
        avatar_dest(1, root=tmp_path).write_bytes(PNG)
        release.set()
        await asyncio.wait_for(tasks.wait(), 1)
        assert store.get(1).stage is Stage.CONVERTING
        assert avatar_dest(1, root=tmp_path).read_bytes() == PNG
        assert bot.send_message.await_count == 1
        await tasks.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('swallow', [False, True])
@pytest.mark.parametrize('phase', ['generation', 'sending'])
def test_close_cleans_generated_file_without_late_send_or_commit(tmp_path, swallow, phase):
    from image_fixtures import PNG

    from app.media import avatar_dest
    async def scenario():
        store = InMemorySessionStore()
        prepare(store, tmp_path)
        tasks = bot_module.ConversionTasks(store)
        entered = asyncio.Event()
        async def blocked():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if not swallow:
                    raise
        async def generate(path, summary):
            if phase == 'generation':
                await blocked()
            return PNG
        converter = AsyncMock()
        converter.generate_avatar.side_effect = generate
        async def send(chat, path, caption):
            if phase == 'sending':
                await blocked()
            return True
        send_mock = AsyncMock(side_effect=send)
        await dispatch(store, update('go'), AsyncMock(), converter=converter,
                       send_photo=send_mock, media_root=tmp_path, conversion_tasks=tasks)
        await asyncio.wait_for(entered.wait(), 1)
        await tasks.close()
        assert store.get(1).stage is Stage.CONVERTING
        assert not avatar_dest(1, root=tmp_path).exists()
        if phase == 'generation':
            send_mock.assert_not_awaited()
    asyncio.run(scenario())


def test_background_failure_retry_then_success(tmp_path):
    from image_fixtures import PNG

    from app.handlers import CONVERTER_FAILURE_TEXT
    async def scenario():
        store = InMemorySessionStore()
        prepare(store, tmp_path)
        tasks = bot_module.ConversionTasks(store)
        converter = AsyncMock()
        converter.generate_avatar.side_effect = [RuntimeError('failed'), PNG]
        bot, send = AsyncMock(), AsyncMock(return_value=True)
        for _ in range(2):
            await dispatch(store, update('retry'), bot, converter=converter, send_photo=send,
                           media_root=tmp_path, conversion_tasks=tasks)
            await asyncio.wait_for(tasks.wait(), 1)
        assert store.get(1).stage is Stage.SCRIPTING
        assert send.await_count == 1
        bot.send_message.assert_awaited_once_with(chat_id=1, text=CONVERTER_FAILURE_TEXT)
        await tasks.close()
    asyncio.run(scenario())
