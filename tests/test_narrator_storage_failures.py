"""Narration filesystem failures are retryable and metadata-only."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import orchestrator
from app.session_store import InMemorySessionStore
from app.states import Stage


@pytest.mark.parametrize("operation", ["mkdir", "write_bytes", "unlink"])
def test_filesystem_fault_does_not_escape_or_leak(tmp_path, monkeypatch, caplog, operation):
    async def scenario():
        from app.narrator import NarrationAudio
        store = InMemorySessionStore()
        state = store.get(1)
        state.stage = Stage.NARRATING
        state.narration_text = "A patient creature waits."
        narrator = SimpleNamespace(render=AsyncMock(return_value=NarrationAudio(
            data=b"\x01\x00" * 2400, mime_type="audio/L16;codec=pcm;rate=24000")))
        original = getattr(Path, operation)

        def fail(path, *args, **kwargs):
            if path == tmp_path / "1" or path.parent == tmp_path / "1":
                raise OSError("KEY_SENTINEL NARRATION_SENTINEL")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, operation, fail)
        send = AsyncMock(return_value=True)
        notify = AsyncMock(return_value=True)
        await orchestrator.handle_narrating_update(
            store, 1, narrator=narrator, send_voice=send, send_message=notify,
            media_root=tmp_path, encode_audio=AsyncMock(return_value=b"fake-ogg"),
            validate_audio=AsyncMock(return_value=None))
        assert state.stage is (Stage.DONE if operation == "unlink" else Stage.NARRATING)
        assert send.await_count == (1 if operation == "unlink" else 0)
        assert notify.await_count == (0 if operation == "unlink" else 1)
        assert "KEY_SENTINEL" not in caplog.text
        assert "NARRATION_SENTINEL" not in caplog.text
        assert "OSError" in caplog.text
        assert all(r.exc_info is None for r in caplog.records)
    asyncio.run(scenario())
