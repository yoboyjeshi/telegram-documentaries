"""Filesystem-failure recovery and metadata-only cleanup logging."""
from __future__ import annotations

import asyncio
import logging
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from app import early_flow, handlers
from app.handlers import GREETING_TEXT, PHOTO_DOWNLOAD_FAILURE_TEXT
from app.media import photo_dest
from app.session_store import InMemorySessionStore
from app.states import InterviewState, Stage
from app.telegram_adapter import ChatInput

SENTINEL = "PRIVATE_FILESYSTEM_PORTRAIT_TOKEN_8fda"


def _assert_private(caplog, operation):
    records = [r for r in caplog.records if r.name == "telegram_documentaries"]
    assert any(operation in r.getMessage() for r in records)
    assert SENTINEL not in caplog.text
    assert all(SENTINEL not in repr(r.args) for r in records)
    assert all(r.exc_info is None and r.exc_text is None for r in records)


@pytest.mark.parametrize("command", ["start", "restart"])
def test_reset_purge_failure_still_clears_state_and_keeps_error_private(
    monkeypatch, tmp_path, caplog, command
):
    store = InMemorySessionStore()
    state = store.get(1)
    state.stage = Stage.NARRATING
    state.interview = InterviewState(pending_question=SENTINEL)
    state.narration_text = SENTINEL
    old_version = store.version(1)
    portrait = photo_dest(1, root=tmp_path)
    portrait.parent.mkdir(parents=True)
    portrait.write_bytes(b"existing portrait")

    def fail_purge(chat_id, root):
        assert store.get(chat_id).stage is Stage.AWAITING_PHOTO
        assert store.get(chat_id).interview is None
        raise OSError(SENTINEL)

    monkeypatch.setattr(handlers, "purge_chat_media", fail_purge)
    with caplog.at_level(logging.DEBUG):
        reply = getattr(handlers, f"handle_{command}")(
            store, ChatInput(chat_id=1, kind="command", command=command),
            media_root=tmp_path,
        )
    assert reply == GREETING_TEXT
    assert store.version(1) > old_version
    assert store.get(1).stage is Stage.AWAITING_PHOTO
    assert store.get(1).interview is None
    assert store.get(1).narration_text is None
    assert portrait.exists()  # Purge failed; never claim successful deletion.
    _assert_private(caplog, "operation=media_purge")


def test_partial_portrait_write_is_removed_and_never_reaches_bouncer(
    monkeypatch, tmp_path, caplog
):
    buffer = BytesIO()
    Image.new("RGB", (2, 2), "blue").save(buffer, format="JPEG")
    jpeg = buffer.getvalue()
    store = InMemorySessionStore()
    store.set_stage(1, Stage.AWAITING_PHOTO)
    dest = photo_dest(1, root=tmp_path)
    original = Path.write_bytes

    def partial_write(path, data):
        if path == dest:
            original(path, data[:20])
            raise OSError(SENTINEL)
        return original(path, data)

    monkeypatch.setattr(Path, "write_bytes", partial_write)
    bouncer, interviewer = AsyncMock(), AsyncMock()
    send = AsyncMock(return_value=True)
    with caplog.at_level(logging.DEBUG):
        asyncio.run(early_flow.run_photo_intake(
            store, ChatInput(chat_id=1, kind="photo", file_id="synthetic"),
            bouncer=bouncer, interviewer=interviewer,
            download_bytes=AsyncMock(return_value=jpeg), send_message=send,
            media_root=tmp_path,
        ))
    assert not dest.exists()
    assert store.get(1).stage is Stage.AWAITING_PHOTO
    bouncer.inspect.assert_not_awaited()
    interviewer.next_question.assert_not_awaited()
    send.assert_awaited_once_with(1, PHOTO_DOWNLOAD_FAILURE_TEXT)
    _assert_private(caplog, "operation=photo_acquisition")


def test_portrait_unlink_failure_is_metadata_only(monkeypatch, tmp_path, caplog):
    store = InMemorySessionStore()
    store.set_stage(1, Stage.AWAITING_PHOTO)
    store.set_stage(1, Stage.BOUNCER_CHECK)
    dest = photo_dest(1, root=tmp_path)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"existing portrait")
    guard = early_flow._Ownership(store, 1, store.version(1), Stage.BOUNCER_CHECK)
    original = Path.unlink

    def fail_unlink(path, *args, **kwargs):
        if path == dest:
            raise OSError(SENTINEL)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with caplog.at_level(logging.DEBUG):
        early_flow._discard_owned_portrait(guard, dest)
    assert dest.exists()
    assert store.get(1).stage is Stage.BOUNCER_CHECK
    _assert_private(caplog, "operation=portrait_cleanup")
