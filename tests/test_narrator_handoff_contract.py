"""Tests-first contract for the terminal narration step and media naming."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import bot, media, orchestrator
from app.session_store import InMemorySessionStore
from app.states import Stage


def test_narrating_step_exists():
    assert callable(getattr(orchestrator, "handle_narrating_update", None))


def test_attempt_paths_are_namespaced_and_unique(tmp_path):
    first = media.narration_dest(1, 2, root=tmp_path)
    second = media.narration_dest(1, 2, root=tmp_path)
    assert first != second
    assert first.parent == tmp_path / "1"
    assert first.suffix == ".ogg"
    assert first.name.startswith("narration-2-")


def test_done_is_terminal_not_echoed():
    async def scenario():
        store = InMemorySessionStore()
        store.get(1).stage = Stage.DONE
        transport = AsyncMock()
        incoming = SimpleNamespace(effective_message=SimpleNamespace(
            text="again", chat=SimpleNamespace(id=1)))
        await bot.dispatch(store, incoming, transport)
        assert store.get(1).stage is Stage.DONE
        transport.send_message.assert_not_awaited()
        transport.send_voice.assert_not_awaited()
        transport.get_file.assert_not_awaited()
    asyncio.run(scenario())
