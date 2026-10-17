"""The existing application shutdown owns late codec cleanup too."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app import audio_encoding
from app import bot as wiring
from app.config import Settings


@pytest.mark.parametrize("fail", [False, True])
def test_post_stop_drains_codec_after_pipeline_tasks(monkeypatch, caplog, fail):
    async def scenario():
        order = []

        async def close():
            order.append("pipeline")

        async def drain():
            order.append("codec")
            if fail:
                raise audio_encoding.AudioEncodingError("KEY_SENTINEL AUDIO_SENTINEL")

        registry = AsyncMock()
        registry.close.side_effect = close
        monkeypatch.setattr(wiring, "ConversionTasks", lambda store: registry)
        monkeypatch.setattr(audio_encoding, "drain_codec_cleanup", drain)
        app = wiring.build_application(Settings(telegram_bot_token="123:dummy"))
        await app.post_stop(app)
        assert order == ["pipeline", "codec"]
        assert "KEY_SENTINEL" not in caplog.text
        assert "AUDIO_SENTINEL" not in caplog.text
        assert all(record.exc_info is None for record in caplog.records)
        if fail:
            assert "AudioEncodingError" in caplog.text

    asyncio.run(scenario())
