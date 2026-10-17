"""All exits clean owned artifacts without deleting a new session's image."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from image_fixtures import PNG

from app import orchestrator
from app.handlers import CONVERTER_FAILURE_TEXT
from app.media import avatar_dest, photo_dest
from app.session_store import InMemorySessionStore
from app.states import InterviewState, Stage, TraitSummary


def prepare(root):
    store = InMemorySessionStore()
    store.get(1).stage = Stage.CONVERTING
    store.get(1).interview = InterviewState(summary=TraitSummary(summary='bold'))
    p = photo_dest(1, root=root)
    p.parent.mkdir(parents=True)
    p.write_bytes(b'portrait')
    converter = AsyncMock()
    converter.generate_avatar.return_value = PNG
    return store, converter


def test_partial_write_is_cleaned_with_safe_retry(tmp_path, monkeypatch, caplog):
    store, converter = prepare(tmp_path)
    original = Path.write_bytes
    def partial(path, data):
        original(path, b'partial')
        raise OSError('PRIVATE_DISK_PAYLOAD')
    monkeypatch.setattr(Path, 'write_bytes', partial)
    send = AsyncMock()
    result = asyncio.run(orchestrator.handle_converting_update(
        store, 1, converter=converter, send_photo=send, media_root=tmp_path))
    assert result == CONVERTER_FAILURE_TEXT
    assert store.get(1).stage is Stage.CONVERTING
    assert not avatar_dest(1, root=tmp_path).exists()
    assert 'PRIVATE_DISK_PAYLOAD' not in caplog.text
    send.assert_not_awaited()


def test_cancel_during_send_cleans_and_propagates(tmp_path):
    async def scenario():
        store, converter = prepare(tmp_path)
        entered = asyncio.Event()
        async def send(chat, path, caption):
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(orchestrator.handle_converting_update(
            store, 1, converter=converter, send_photo=send, media_root=tmp_path))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not avatar_dest(1, root=tmp_path).exists()
        assert store.get(1).stage is Stage.CONVERTING
    asyncio.run(scenario())


def test_cleanup_failure_does_not_roll_back_delivered_image(tmp_path, monkeypatch, caplog):
    store, converter = prepare(tmp_path)
    def denied(path, **kwargs):
        raise PermissionError('PRIVATE_FILE_PAYLOAD')
    monkeypatch.setattr(Path, 'unlink', denied)
    send = AsyncMock(return_value=True)
    result = asyncio.run(orchestrator.handle_converting_update(
        store, 1, converter=converter, send_photo=send, media_root=tmp_path))
    assert result is None
    assert store.get(1).stage is Stage.SCRIPTING
    assert send.await_count == 1
    assert 'PRIVATE_FILE_PAYLOAD' not in caplog.text
    assert 'delete' in caplog.text.lower() or 'cleanup' in caplog.text.lower()


def test_conversion_timeout_returns_retry(tmp_path, monkeypatch):
    async def scenario():
        store, converter = prepare(tmp_path)
        async def hang(*args):
            await asyncio.Event().wait()
        converter.generate_avatar.side_effect = hang
        monkeypatch.setattr(orchestrator, 'CONVERSION_TIMEOUT_SECONDS', 0.01, raising=False)
        send = AsyncMock()
        result = await asyncio.wait_for(orchestrator.handle_converting_update(
            store, 1, converter=converter, send_photo=send, media_root=tmp_path), 1)
        assert result == CONVERTER_FAILURE_TEXT
        assert store.get(1).stage is Stage.CONVERTING
        send.assert_not_awaited()
    asyncio.run(scenario())
