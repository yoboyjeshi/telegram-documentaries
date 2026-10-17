"""Application stop preserves cancellation from either cleanup boundary."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import audio_encoding
from app import bot as wiring
from app.config import Settings


@pytest.mark.parametrize("boundary", ["pipeline", "codec"])
def test_post_stop_preserves_cancellation(monkeypatch, caplog, boundary):
    async def scenario():
        close = AsyncMock()
        drain = AsyncMock()
        (close if boundary == "pipeline" else drain).side_effect = asyncio.CancelledError
        monkeypatch.setattr(wiring, "ConversionTasks", lambda store: SimpleNamespace(close=close))
        monkeypatch.setattr(audio_encoding, "drain_codec_cleanup", drain)
        app = wiring.build_application(Settings(telegram_bot_token="123:dummy"))
        with pytest.raises(asyncio.CancelledError):
            await app.post_stop(app)
        close.assert_awaited_once()
        assert drain.await_count == (0 if boundary == "pipeline" else 1)
        assert not caplog.records

    asyncio.run(scenario())
