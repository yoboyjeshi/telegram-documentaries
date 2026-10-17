"""Slice A: bounded, attempt-owned portrait intake and guarded publication.

All coordination uses ``asyncio.Event`` signals -- never sleeps or polling --
including the cancellation-resistant providers that return after their budget
has expired.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from image_fixtures import JPEG, PNG

from app import early_flow
from app.bot import dispatch, make_memory_downloader
from app.bouncer import REFUSAL_LINES, BouncerVerdict
from app.conversion_tasks import ConversionTasks
from app.early_flow import MAX_PORTRAIT_BYTES, PortraitRejected, validate_portrait_bytes
from app.handlers import (
    BOUNCER_FAILURE_TEXT,
    INTERVIEW_INTRO,
    PHOTO_DOWNLOAD_FAILURE_TEXT,
)
from app.interviewer import InterviewQuestion
from app.media import photo_dest
from app.session_store import InMemorySessionStore
from app.states import InterviewAnswer, Stage, TraitSummary

# Sentinels that must never reach a log line.
FILE_ID_SENTINEL = "FILE_ID_SENTINEL_7b1e"
PROVIDER_SENTINEL = "PROVIDER_MESSAGE_SENTINEL_4c2d"


class FakeInterviewer:
    def __init__(self) -> None:
        self.calls = 0

    async def next_question(self, history: list[InterviewAnswer]) -> InterviewQuestion:
        self.calls += 1
        return InterviewQuestion(text=f"Q{len(history) + 1}?")

    async def summarise(self, history: list[InterviewAnswer]) -> TraitSummary:
        return TraitSummary(summary="done")


class FakeBouncer:
    def __init__(self, verdict: BouncerVerdict | None = None,
                 exc: Exception | None = None) -> None:
        self._verdict = verdict
        self._exc = exc
        self.seen: list[bytes] = []

    async def inspect(self, image_path: Path) -> BouncerVerdict:
        self.seen.append(image_path.read_bytes())
        if self._exc is not None:
            raise self._exc
        assert self._verdict is not None
        return self._verdict


def _photo_update(chat_id: int = 1, file_id: str = FILE_ID_SENTINEL) -> SimpleNamespace:
    message = SimpleNamespace(text=None, chat=SimpleNamespace(id=chat_id),
                              photo=[SimpleNamespace(file_id=file_id, file_size=len(JPEG))])
    return SimpleNamespace(effective_message=message, message=message)


def _text_update(text: str, chat_id: int = 1) -> SimpleNamespace:
    message = SimpleNamespace(text=text, chat=SimpleNamespace(id=chat_id))
    return SimpleNamespace(effective_message=message, message=message)


def _awaiting(chat_id: int = 1) -> InMemorySessionStore:
    store = InMemorySessionStore()
    store.set_stage(chat_id, Stage.AWAITING_PHOTO)
    return store


def _sent(bot) -> list[str]:
    return [call.kwargs["text"] for call in bot.send_message.await_args_list]


# --- admission control -------------------------------------------------------

@pytest.mark.parametrize("data", [b"", b"\xff\xd8\xff" + b"0" * 32, PNG, b"not an image"])
def test_invalid_portrait_bytes_are_rejected(data) -> None:
    with pytest.raises(PortraitRejected):
        validate_portrait_bytes(data)


def test_oversized_portrait_is_rejected_by_actual_length() -> None:
    with pytest.raises(PortraitRejected):
        validate_portrait_bytes(JPEG + b"\x00" * MAX_PORTRAIT_BYTES)


def test_valid_jpeg_is_admitted_unchanged() -> None:
    assert validate_portrait_bytes(JPEG) == JPEG


def test_memory_downloader_never_receives_a_filesystem_path(tmp_path: Path) -> None:
    bot = AsyncMock()
    telegram_file = AsyncMock()
    telegram_file.file_size = len(JPEG)

    async def write(sink) -> None:
        sink.write(JPEG)

    telegram_file.download_to_memory.side_effect = write
    bot.get_file.return_value = telegram_file

    data = asyncio.run(make_memory_downloader(bot)(FILE_ID_SENTINEL))

    assert data == JPEG
    bot.get_file.assert_awaited_once_with(FILE_ID_SENTINEL)
    telegram_file.download_to_memory.assert_awaited_once()
    telegram_file.download_to_drive.assert_not_awaited()
    assert not list(tmp_path.iterdir())


def test_memory_downloader_prechecks_advertised_size() -> None:
    bot = AsyncMock()
    telegram_file = AsyncMock()
    telegram_file.file_size = MAX_PORTRAIT_BYTES + 1
    bot.get_file.return_value = telegram_file

    with pytest.raises(PortraitRejected):
        asyncio.run(make_memory_downloader(bot)("fid"))

    telegram_file.download_to_memory.assert_not_awaited()


@pytest.mark.parametrize("advertised", [None, 1])
def test_memory_downloader_rechecks_actual_length_when_metadata_lies(advertised) -> None:
    bot = AsyncMock()
    telegram_file = AsyncMock()
    telegram_file.file_size = advertised

    async def write(sink) -> None:
        sink.write(b"\x00" * (MAX_PORTRAIT_BYTES + 1))

    telegram_file.download_to_memory.side_effect = write
    bot.get_file.return_value = telegram_file

    with pytest.raises(PortraitRejected):
        asyncio.run(make_memory_downloader(bot)("fid"))


# --- guarded publication through the live dispatch path ----------------------

def _kwargs(tmp_path, bouncer, interviewer, download, tasks):
    return {"bouncer": bouncer, "interviewer": interviewer, "download_bytes": download,
            "media_root": tmp_path, "conversion_tasks": tasks}


def test_accepted_portrait_is_published_once_and_starts_the_interview(tmp_path: Path) -> None:
    async def scenario():
        store = _awaiting()
        tasks = ConversionTasks(store)
        bouncer = FakeBouncer(BouncerVerdict(is_human=True))
        interviewer = FakeInterviewer()
        bot = AsyncMock()

        async def download(file_id: str) -> bytes:
            return JPEG

        reply = await dispatch(store, _photo_update(), bot,
                               **_kwargs(tmp_path, bouncer, interviewer, download, tasks))
        await asyncio.wait_for(tasks.wait(), 1)

        assert reply is None  # the sequential handler scheduled the work
        assert photo_dest(1, root=tmp_path).read_bytes() == JPEG
        assert bouncer.seen == [JPEG]
        assert store.get(1).stage is Stage.INTERVIEW
        assert store.get(1).interview.pending_question == "Q1?"
        sent = _sent(bot)
        assert len(sent) == 1
        assert INTERVIEW_INTRO in sent[0] and "Q1?" in sent[0]
        await tasks.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("data", [b"", PNG, b"junk"])
def test_rejected_bytes_never_reach_the_filesystem_or_the_bouncer(tmp_path, data) -> None:
    async def scenario():
        store = _awaiting()
        tasks = ConversionTasks(store)
        bouncer = FakeBouncer(BouncerVerdict(is_human=True))
        bot = AsyncMock()

        async def download(file_id: str) -> bytes:
            return data

        await dispatch(store, _photo_update(), bot,
                       **_kwargs(tmp_path, bouncer, FakeInterviewer(), download, tasks))
        await asyncio.wait_for(tasks.wait(), 1)

        assert bouncer.seen == []
        assert not photo_dest(1, root=tmp_path).exists()
        assert store.get(1).stage is Stage.AWAITING_PHOTO
        bot.send_message.assert_awaited_once_with(chat_id=1, text=PHOTO_DOWNLOAD_FAILURE_TEXT)
        await tasks.close()

    asyncio.run(scenario())


def test_publication_failure_recovers_without_leaving_a_partial_portrait(tmp_path) -> None:
    async def scenario():
        store = _awaiting()
        tasks = ConversionTasks(store)
        bouncer = FakeBouncer(BouncerVerdict(is_human=True))
        bot = AsyncMock()
        # A file where the chat's media directory belongs: mkdir must fail.
        blocked = tmp_path / "1"
        blocked.write_bytes(b"not a directory")

        async def download(file_id: str) -> bytes:
            return JPEG

        await dispatch(store, _photo_update(), bot,
                       **_kwargs(tmp_path, bouncer, FakeInterviewer(), download, tasks))
        await asyncio.wait_for(tasks.wait(), 1)

        assert bouncer.seen == []
        assert blocked.read_bytes() == b"not a directory"
        assert store.get(1).stage is Stage.AWAITING_PHOTO
        bot.send_message.assert_awaited_once_with(chat_id=1, text=PHOTO_DOWNLOAD_FAILURE_TEXT)
        await tasks.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("command", ["/start", "/restart"])
def test_cancellation_resistant_download_cannot_publish_after_reset(tmp_path, command) -> None:
    async def scenario():
        store = _awaiting()
        store.set_stage(2, Stage.AWAITING_PHOTO)
        tasks = ConversionTasks(store)
        bouncer = FakeBouncer(BouncerVerdict(is_human=True))
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        bot = AsyncMock()

        async def download(file_id: str) -> bytes:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()  # a provider that ignores cancellation
            return JPEG

        kwargs = _kwargs(tmp_path, bouncer, FakeInterviewer(), download, tasks)
        await dispatch(store, _photo_update(), bot, **kwargs)
        await asyncio.wait_for(entered.wait(), 1)
        assert tasks.active(1)

        await dispatch(store, _text_update(command), bot, **kwargs)
        await asyncio.wait_for(cancelled.wait(), 1)
        assert store.get(1).stage is Stage.AWAITING_PHOTO
        assert not photo_dest(1, root=tmp_path).parent.exists()

        # A replacement session, and another chat, own their own media.
        replacement = photo_dest(1, root=tmp_path)
        replacement.parent.mkdir(parents=True, exist_ok=True)
        replacement.write_bytes(b"replacement-portrait")
        other = photo_dest(2, root=tmp_path)
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_bytes(b"other-chat-portrait")

        release.set()
        await asyncio.wait_for(tasks.wait(), 1)

        assert replacement.read_bytes() == b"replacement-portrait"
        assert other.read_bytes() == b"other-chat-portrait"
        assert bouncer.seen == []
        assert store.get(1).stage is Stage.AWAITING_PHOTO
        # Only the reset greeting went out: cancellation is never an error notice.
        assert bot.send_message.await_count == 1
        await tasks.close()

    asyncio.run(scenario())


class TrackingTasks(ConversionTasks):
    """The same registry; every scheduled worker also exposes a done Event.

    ``wait()`` drains *every* tracked task, including a retired worker whose
    dependency ignores cancellation, so a test that must wait for one specific
    worker waits on its own Event instead.
    """

    def __init__(self, store) -> None:
        super().__init__(store)
        self.done: list[asyncio.Event] = []

    def start(self, chat_id, work):
        finished = asyncio.Event()

        async def tracked(version: int) -> None:
            try:
                await work(version)
            finally:
                finished.set()

        accepted = super().start(chat_id, tracked)
        if accepted:
            self.done.append(finished)
        return accepted


def test_replacement_intake_survives_a_retired_download_completing_late(tmp_path) -> None:
    async def scenario():
        store = _awaiting()
        tasks = TrackingTasks(store)
        bouncer = FakeBouncer(BouncerVerdict(is_human=True))
        interviewer = FakeInterviewer()
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        bot = AsyncMock()
        attempts = {"n": 0}

        async def download(file_id: str) -> bytes:
            attempts["n"] += 1
            if attempts["n"] == 1:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
                return b"retired-attempt-bytes"
            return JPEG

        kwargs = _kwargs(tmp_path, bouncer, interviewer, download, tasks)
        await dispatch(store, _photo_update(), bot, **kwargs)
        await asyncio.wait_for(entered.wait(), 1)
        await dispatch(store, _text_update("/restart"), bot, **kwargs)
        await asyncio.wait_for(cancelled.wait(), 1)

        # A brand-new intake runs to completion while the old download lingers.
        await dispatch(store, _photo_update(), bot, **kwargs)
        await asyncio.wait_for(tasks.done[-1].wait(), 1)
        assert store.get(1).stage is Stage.INTERVIEW
        assert photo_dest(1, root=tmp_path).read_bytes() == JPEG

        release.set()
        await asyncio.wait_for(tasks.wait(), 1)

        assert photo_dest(1, root=tmp_path).read_bytes() == JPEG
        assert store.get(1).stage is Stage.INTERVIEW
        assert store.get(1).interview.pending_question == "Q1?"
        assert bouncer.seen == [JPEG]
        await tasks.close()

    asyncio.run(scenario())


def test_bouncer_failure_removes_only_the_failed_attempt_portrait(tmp_path: Path) -> None:
    async def scenario():
        store = _awaiting()
        tasks = ConversionTasks(store)
        bouncer = FakeBouncer(exc=RuntimeError(PROVIDER_SENTINEL))
        bot = AsyncMock()

        async def download(file_id: str) -> bytes:
            return JPEG

        await dispatch(store, _photo_update(), bot,
                       **_kwargs(tmp_path, bouncer, FakeInterviewer(), download, tasks))
        await asyncio.wait_for(tasks.wait(), 1)

        assert bouncer.seen == [JPEG]
        assert not photo_dest(1, root=tmp_path).exists()
        assert store.get(1).stage is Stage.AWAITING_PHOTO
        bot.send_message.assert_awaited_once_with(chat_id=1, text=BOUNCER_FAILURE_TEXT)
        await tasks.close()

    asyncio.run(scenario())


def test_shutdown_during_the_bouncer_removes_the_unaccepted_portrait(tmp_path) -> None:
    async def scenario():
        store = _awaiting()
        tasks = ConversionTasks(store)
        entered, release = asyncio.Event(), asyncio.Event()
        bot = AsyncMock()

        class Blocking:
            async def inspect(self, image_path: Path) -> BouncerVerdict:
                entered.set()
                await release.wait()
                return BouncerVerdict(is_human=True)

        async def download(file_id: str) -> bytes:
            return JPEG

        await dispatch(store, _photo_update(), bot,
                       **_kwargs(tmp_path, Blocking(), FakeInterviewer(), download, tasks))
        await asyncio.wait_for(entered.wait(), 1)
        assert photo_dest(1, root=tmp_path).exists()

        await asyncio.wait_for(tasks.close(), 2)

        # Shutdown is not a reset: nobody else purges, so the worker must.
        assert not photo_dest(1, root=tmp_path).exists()
        assert store.get(1).stage is Stage.BOUNCER_CHECK
        bot.send_message.assert_not_awaited()
        release.set()

    asyncio.run(scenario())


def test_refused_portrait_is_removed_and_refusal_is_sent(tmp_path: Path) -> None:
    async def scenario():
        store = _awaiting()
        tasks = ConversionTasks(store)
        bouncer = FakeBouncer(BouncerVerdict(is_human=False))
        bot = AsyncMock()

        async def download(file_id: str) -> bytes:
            return JPEG

        await dispatch(store, _photo_update(), bot,
                       **_kwargs(tmp_path, bouncer, FakeInterviewer(), download, tasks))
        await asyncio.wait_for(tasks.wait(), 1)

        assert not photo_dest(1, root=tmp_path).exists()
        assert store.get(1).stage is Stage.AWAITING_PHOTO
        assert bot.send_message.await_args.kwargs["text"] in REFUSAL_LINES
        await tasks.close()

    asyncio.run(scenario())


def test_acquisition_budget_expiry_recovers_and_ignores_a_late_download(
    tmp_path, monkeypatch
) -> None:
    async def scenario():
        monkeypatch.setattr(early_flow, "PHOTO_ACQUISITION_TIMEOUT_SECONDS", 0.01)
        store = _awaiting()
        tasks = ConversionTasks(store)
        bouncer = FakeBouncer(BouncerVerdict(is_human=True))
        entered, expired, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        bot = AsyncMock()

        async def download(file_id: str) -> bytes:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # The budget, not a reset, cancelled this download.
                expired.set()
                await release.wait()
            return JPEG

        await dispatch(store, _photo_update(), bot,
                       **_kwargs(tmp_path, bouncer, FakeInterviewer(), download, tasks))
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(expired.wait(), 1)
        release.set()
        await asyncio.wait_for(tasks.wait(), 1)

        assert bouncer.seen == []
        assert not photo_dest(1, root=tmp_path).exists()
        assert store.get(1).stage is Stage.AWAITING_PHOTO
        bot.send_message.assert_awaited_once_with(chat_id=1, text=PHOTO_DOWNLOAD_FAILURE_TEXT)
        await tasks.close()

    asyncio.run(scenario())


def test_intake_errors_log_metadata_only(tmp_path, caplog) -> None:
    async def scenario():
        store = _awaiting()
        tasks = ConversionTasks(store)

        class PayloadError(Exception):
            pass

        class PayloadBouncer:
            async def inspect(self, image_path: Path) -> BouncerVerdict:
                raise RuntimeError(PROVIDER_SENTINEL) from PayloadError(image_path.read_bytes())

        async def download(file_id: str) -> bytes:
            return JPEG

        bot = AsyncMock()
        await dispatch(store, _photo_update(), bot,
                       **_kwargs(tmp_path, PayloadBouncer(), FakeInterviewer(), download, tasks))
        await asyncio.wait_for(tasks.wait(), 1)
        await tasks.close()

    with caplog.at_level("DEBUG"):
        asyncio.run(scenario())

    errors = [r for r in caplog.records if r.levelno >= 40]
    assert any("RuntimeError" in r.getMessage() and "operation=bouncer" in r.getMessage()
               for r in errors)
    assert PROVIDER_SENTINEL not in caplog.text
    assert FILE_ID_SENTINEL not in caplog.text
    assert all(record.exc_info is None for record in errors)
