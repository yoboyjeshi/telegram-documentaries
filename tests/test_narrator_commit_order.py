"""Own stage only after a successful, current voice delivery."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import orchestrator
from app.session_store import InMemorySessionStore
from app.states import Stage


@pytest.mark.parametrize("delivered", [True, False])
def test_state_commits_after_voice_send_only(tmp_path, delivered):
    async def scenario():
        from app.narrator import NarrationAudio
        store = InMemorySessionStore()
        state = store.get(1)
        state.stage = Stage.NARRATING
        state.narration_text = "A creature waits."
        narrator = SimpleNamespace(render=AsyncMock(return_value=NarrationAudio(
            data=b"\x01\x00" * 2400, mime_type="audio/L16;codec=pcm;rate=24000")))
        paths = []

        async def send_voice(chat_id, path):
            assert chat_id == 1
            assert state.stage is Stage.NARRATING
            assert path.read_bytes() == b"verified-fake-ogg"
            paths.append(path)
            return delivered

        notify = AsyncMock(return_value=True)
        await orchestrator.handle_narrating_update(
            store, 1, narrator=narrator, send_voice=send_voice, send_message=notify,
            media_root=tmp_path,
            encode_audio=AsyncMock(return_value=b"verified-fake-ogg"),
            validate_audio=AsyncMock(return_value=None))
        assert state.stage is (Stage.DONE if delivered else Stage.NARRATING)
        assert state.narration_text == "A creature waits."
        assert len(paths) == 1 and not paths[0].exists()
        assert notify.await_count == (0 if delivered else 1)
        narrator.render.assert_awaited_once_with("A creature waits.")
    asyncio.run(scenario())
