"""Tests for the async photo flow: gating, verdict handling, resilience."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.bouncer import REFUSAL_LINES, BouncerVerdict
from app.handlers import (
    BOUNCER_FAILURE_TEXT,
    INTERVIEW_READY_TEXT,
    PHOTO_DOWNLOAD_FAILURE_TEXT,
    PHOTO_REDIRECT_TEXT,
)
from app.photo_flow import handle_photo_update
from app.session_store import InMemorySessionStore
from app.states import Stage
from app.telegram_adapter import ChatInput

# A long, unlikely-to-collide sentinel for the fake image bytes, so the
# "image bytes never reach the log" assertions cannot pass by coincidence.
IMAGE_SENTINEL = "IMG_BYTES_SENTINEL_9f3a"


def _photo(chat_id: int = 1, file_id: str | None = "fid") -> ChatInput:
    return ChatInput(chat_id=chat_id, kind="photo", file_id=file_id)


class FakeBouncer:
    def __init__(self, verdict: BouncerVerdict | None = None, exc: Exception | None = None) -> None:
        self._verdict = verdict
        self._exc = exc
        self.calls = 0

    async def inspect(self, image_path: Path) -> BouncerVerdict:
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        assert self._verdict is not None
        return self._verdict


async def _ok_download(file_id: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(IMAGE_SENTINEL.encode())
    return dest


def _awaiting(chat_id: int = 1) -> InMemorySessionStore:
    store = InMemorySessionStore()
    store.set_stage(chat_id, Stage.AWAITING_PHOTO)
    return store


def test_human_photo_advances_to_interview(tmp_path: Path) -> None:
    store = _awaiting()
    bouncer = FakeBouncer(BouncerVerdict(is_human=True))

    reply = asyncio.run(
        handle_photo_update(
            store, _photo(), bouncer=bouncer, download=_ok_download, media_root=tmp_path
        )
    )

    assert reply == INTERVIEW_READY_TEXT
    assert store.get(1).stage is Stage.INTERVIEW
    assert bouncer.calls == 1


def test_non_human_photo_refuses_and_stays_awaiting(tmp_path: Path) -> None:
    store = _awaiting()
    bouncer = FakeBouncer(BouncerVerdict(is_human=False))

    reply = asyncio.run(
        handle_photo_update(
            store, _photo(), bouncer=bouncer, download=_ok_download, media_root=tmp_path
        )
    )

    assert reply in REFUSAL_LINES
    assert store.get(1).stage is Stage.AWAITING_PHOTO


def test_photo_at_wrong_stage_redirects_without_download(tmp_path: Path) -> None:
    store = InMemorySessionStore()  # GREETING
    bouncer = FakeBouncer(BouncerVerdict(is_human=True))
    downloads = {"n": 0}

    async def download(file_id: str, dest: Path) -> Path:
        downloads["n"] += 1
        return dest

    reply = asyncio.run(
        handle_photo_update(
            store, _photo(), bouncer=bouncer, download=download, media_root=tmp_path
        )
    )

    assert reply == PHOTO_REDIRECT_TEXT
    assert downloads["n"] == 0
    assert bouncer.calls == 0
    assert store.get(1).stage is Stage.GREETING


def test_download_failure_is_notified_and_stage_recovered(tmp_path: Path) -> None:
    store = _awaiting()
    bouncer = FakeBouncer(BouncerVerdict(is_human=True))

    async def boom(file_id: str, dest: Path) -> Path:
        raise RuntimeError("network down")

    reply = asyncio.run(
        handle_photo_update(
            store, _photo(), bouncer=bouncer, download=boom, media_root=tmp_path
        )
    )

    assert reply == PHOTO_DOWNLOAD_FAILURE_TEXT
    assert store.get(1).stage is Stage.AWAITING_PHOTO
    assert bouncer.calls == 0


def test_bouncer_failure_is_notified_and_stage_recovered(tmp_path: Path) -> None:
    store = _awaiting()
    bouncer = FakeBouncer(exc=RuntimeError("gemini timeout"))

    reply = asyncio.run(
        handle_photo_update(
            store, _photo(), bouncer=bouncer, download=_ok_download, media_root=tmp_path
        )
    )

    assert reply == BOUNCER_FAILURE_TEXT
    assert store.get(1).stage is Stage.AWAITING_PHOTO


def test_missing_file_id_is_handled_without_crash(tmp_path: Path) -> None:
    store = _awaiting()
    bouncer = FakeBouncer(BouncerVerdict(is_human=True))

    reply = asyncio.run(
        handle_photo_update(
            store, _photo(file_id=None), bouncer=bouncer, download=_ok_download, media_root=tmp_path
        )
    )

    assert reply == PHOTO_DOWNLOAD_FAILURE_TEXT
    assert store.get(1).stage is Stage.AWAITING_PHOTO


def test_flow_logs_verdict_not_image_bytes(tmp_path: Path, caplog) -> None:
    store = _awaiting()
    bouncer = FakeBouncer(BouncerVerdict(is_human=True, reason="face"))

    with caplog.at_level(logging.INFO):
        asyncio.run(
            handle_photo_update(
                store, _photo(), bouncer=bouncer, download=_ok_download, media_root=tmp_path
            )
        )

    assert "is_human=True" in caplog.text
    assert IMAGE_SENTINEL not in caplog.text


def test_bouncer_failure_logs_no_image_bytes(tmp_path: Path, caplog) -> None:
    """A bouncer error whose cause embeds the payload must not leak image bytes."""

    class PayloadError(Exception):
        pass

    class PayloadBouncer:
        async def inspect(self, image_path: Path) -> BouncerVerdict:
            raise RuntimeError("gemini timeout") from PayloadError(image_path.read_bytes())

    store = _awaiting()

    with caplog.at_level(logging.ERROR):
        reply = asyncio.run(
            handle_photo_update(
                store,
                _photo(),
                bouncer=PayloadBouncer(),
                download=_ok_download,
                media_root=tmp_path,
            )
        )

    assert reply == BOUNCER_FAILURE_TEXT  # friendly retry behaviour unchanged
    assert store.get(1).stage is Stage.AWAITING_PHOTO
    # Fail loud in logs: the failure is observable, without the payload.
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors
    assert any(
        "RuntimeError" in r.getMessage() and "gemini timeout" in r.getMessage() for r in errors
    )
    assert IMAGE_SENTINEL not in caplog.text
