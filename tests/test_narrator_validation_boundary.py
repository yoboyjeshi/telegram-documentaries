"""Injected encoders cannot bypass the production send-boundary validator."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import audio_encoding, orchestrator
from app.audio_format import NarrationAudio
from app.handlers import NARRATOR_FAILURE_TEXT
from app.session_store import InMemorySessionStore
from app.states import Stage


@pytest.mark.parametrize("payload", [b"not-ogg", b"OggS", b"RIFF" + bytes(40)],
                         ids=["garbage", "truncated-ogg", "wav-disguised-as-ogg"])
def test_default_validator_blocks_bad_injected_encoding(tmp_path, monkeypatch, payload):
    async def scenario():
        store = InMemorySessionStore()
        state = store.get(1)
        state.stage = Stage.NARRATING
        state.narration_text = "A patient creature waits."
        narrator = SimpleNamespace(render=AsyncMock(return_value=NarrationAudio(
            data=bytes([1, 0]) * 2400, mime_type="audio/L16;rate=24000")))
        encode = AsyncMock(return_value=payload)
        send = AsyncMock(return_value=True)
        notify = AsyncMock(return_value=True)
        spawn = AsyncMock(side_effect=AssertionError("Malformed container reached decoder"))
        monkeypatch.setattr(audio_encoding, "_spawn_ffmpeg", spawn)
        await orchestrator.handle_narrating_update(
            store, 1, narrator=narrator, send_voice=send, send_message=notify,
            media_root=tmp_path, encode_audio=encode)
        encode.assert_awaited_once()
        spawn.assert_not_awaited()
        send.assert_not_awaited()
        notify.assert_awaited_once_with(1, NARRATOR_FAILURE_TEXT)
        assert state.stage is Stage.NARRATING
        assert state.narration_text == "A patient creature waits."
        assert not (tmp_path / "1").exists()

    asyncio.run(scenario())


def test_real_codec_pipeline_delivers_only_valid_ogg_then_cleans(tmp_path):
    """Mandatory local FFmpeg integration, with only model and Telegram mocked."""
    async def scenario():
        store = InMemorySessionStore()
        state = store.get(1)
        state.stage = Stage.NARRATING
        state.narration_text = "A patient creature waits."
        narrator = SimpleNamespace(render=AsyncMock(return_value=NarrationAudio(
            data=bytes([1, 0]) * 12000, mime_type="audio/L16;rate=24000")))
        sent = []

        async def deliver(chat_id, path):
            assert chat_id == 1
            assert state.stage is Stage.NARRATING
            assert path.suffix == ".ogg"
            assert path.read_bytes().startswith(b"OggS")
            assert 0 < audio_encoding._inspect_ogg_opus(path.read_bytes()) <= 120
            sent.append(path)
            return True

        notify = AsyncMock(return_value=True)
        await orchestrator.handle_narrating_update(
            store, 1, narrator=narrator, send_voice=deliver, send_message=notify,
            media_root=tmp_path)
        assert state.stage is Stage.DONE, (
            "Real codec pipeline failed; FFmpeg with libopus and OGG support is mandatory")
        assert len(sent) == 1 and not sent[0].exists()
        assert not list(tmp_path.rglob("*.ogg"))
        narrator.render.assert_awaited_once_with(state.narration_text)
        notify.assert_not_awaited()

    asyncio.run(scenario())
