"""Guarded voice sending never logs exception payloads or swallows cancellation."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app import bot as wiring


def test_voice_send_success(tmp_path):
    async def scenario():
        path = tmp_path / "narration.ogg"
        path.write_bytes(b"offline-test")
        bot = AsyncMock()
        assert await wiring.safe_send_voice(bot, 7, path) is True
        bot.send_voice.assert_awaited_once_with(chat_id=7, voice=path)
    asyncio.run(scenario())


def test_voice_send_failure_is_private(caplog):
    async def scenario():
        bot = AsyncMock()
        bot.send_voice.side_effect = RuntimeError("KEY_SENTINEL NARRATION_SENTINEL")
        assert await wiring.safe_send_voice(bot, 7, Path("private.ogg")) is False
        assert "KEY_SENTINEL" not in caplog.text
        assert "NARRATION_SENTINEL" not in caplog.text
        assert "RuntimeError" in caplog.text
        assert all(record.exc_info is None for record in caplog.records)
    asyncio.run(scenario())


def test_voice_send_cancellation_propagates():
    async def scenario():
        bot = AsyncMock()
        bot.send_voice.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await wiring.safe_send_voice(bot, 7, Path("private.ogg"))
    asyncio.run(scenario())


def test_send_voice_capability(tmp_path):
    async def scenario():
        bot = AsyncMock()
        path = tmp_path / "narration.ogg"
        assert await wiring.make_send_voice(bot)(7, path) is True
        bot.send_voice.assert_awaited_once_with(chat_id=7, voice=path)
    asyncio.run(scenario())
