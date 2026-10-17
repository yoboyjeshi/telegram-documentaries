"""Exceptions may carry keys or user text; never format arbitrary payloads."""
import asyncio
import logging
from unittest.mock import AsyncMock

from app import orchestrator
from app.bot import safe_send_message, safe_send_photo
from app.media import photo_dest
from app.session_store import InMemorySessionStore
from app.states import InterviewState, Stage, TraitSummary

SECRET = 'PRIVATE_KEY_AND_TRAITS_SENTINEL'


def test_converter_failure_payload_is_not_logged(tmp_path, caplog):
    store = InMemorySessionStore()
    state = store.get(1)
    state.stage = Stage.CONVERTING
    state.interview = InterviewState(summary=TraitSummary(summary='private'))
    portrait = photo_dest(1, root=tmp_path)
    portrait.parent.mkdir(parents=True)
    portrait.write_bytes(b'portrait')
    converter = AsyncMock()
    converter.generate_avatar.side_effect = RuntimeError(SECRET)
    with caplog.at_level(logging.ERROR):
        asyncio.run(orchestrator.handle_converting_update(
            store, 1, converter=converter, send_photo=AsyncMock(), media_root=tmp_path))
    assert SECRET not in caplog.text
    assert 'RuntimeError' in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_telegram_photo_and_retry_failures_do_not_log_payload(tmp_path, caplog):
    bot = AsyncMock()
    bot.send_photo.side_effect = RuntimeError(SECRET)
    bot.send_message.side_effect = RuntimeError(SECRET)
    with caplog.at_level(logging.ERROR):
        assert asyncio.run(safe_send_photo(bot, 1, tmp_path/'avatar.png', 'caption')) is False
        assert asyncio.run(safe_send_message(bot, 1, 'retry')) is False
    assert SECRET not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
