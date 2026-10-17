"""Slice B: early-stage registry ownership, budgets and turn integrity.

Every scenario drives the production ``dispatch`` wiring with the existing
single ``ConversionTasks`` registry, so these are live-wiring tests rather than
direct helper calls. Coordination is event-based: no sleeps and no polling.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from image_fixtures import JPEG, PNG
from telegram.error import TelegramError

from app import conversion_tasks, early_flow, orchestrator
from app.bot import dispatch
from app.bouncer import BouncerVerdict
from app.conversion_tasks import ConversionTasks
from app.handlers import (
    BOUNCER_FAILURE_TEXT,
    INTERVIEW_COMPLETE_TEXT,
    INTERVIEW_FAILURE_TEXT,
    INTERVIEW_INTRO,
    PHOTO_DOWNLOAD_FAILURE_TEXT,
)
from app.interviewer import TOTAL_QUESTIONS, InterviewQuestion
from app.media import photo_dest
from app.scripter import NarrationScript
from app.session_store import InMemorySessionStore
from app.states import InterviewAnswer, InterviewState, Stage, TraitSummary

ANSWER_SENTINEL = "ANSWER_SENTINEL_quietly_determined"


class CountingTasks(ConversionTasks):
    """The same registry, instrumented to count scheduling attempts."""

    def __init__(self, store) -> None:
        super().__init__(store)
        self.started: list[int] = []

    def start(self, chat_id, work):
        accepted = super().start(chat_id, work)
        if accepted:
            self.started.append(chat_id)
        return accepted


def _photo_update(chat_id: int = 1) -> SimpleNamespace:
    message = SimpleNamespace(text=None, chat=SimpleNamespace(id=chat_id),
                             photo=[SimpleNamespace(file_id="fid", file_size=len(JPEG))])
    return SimpleNamespace(effective_message=message, message=message)


def _text(text: str, chat_id: int = 1) -> SimpleNamespace:
    message = SimpleNamespace(text=text, chat=SimpleNamespace(id=chat_id))
    return SimpleNamespace(effective_message=message, message=message)


async def _jpeg(file_id: str) -> bytes:
    return JPEG


def _awaiting(chat_id: int = 1) -> InMemorySessionStore:
    store = InMemorySessionStore()
    store.set_stage(chat_id, Stage.AWAITING_PHOTO)
    return store


def _at_interview(store, *, answered: int = 0, pending: str = "Q1?") -> None:
    state = store.get(1)
    state.stage = Stage.INTERVIEW
    state.interview = InterviewState(
        answers=[InterviewAnswer(question=f"Q{i + 1}?", answer=f"a{i + 1}")
                 for i in range(answered)],
        pending_question=pending,
    )


def _sent(bot) -> list[str]:
    return [call.kwargs["text"] for call in bot.send_message.await_args_list]


class ScriptedInterviewer:
    """Deterministic interviewer with event gates and failure injection."""

    def __init__(self, *, question_exc: Exception | None = None,
                 summary_exc: Exception | None = None,
                 entered: asyncio.Event | None = None,
                 gate: asyncio.Event | None = None) -> None:
        self.question_exc = question_exc
        self.summary_exc = summary_exc
        self.entered = entered
        self.gate = gate
        self.question_histories: list[list[InterviewAnswer]] = []
        self.summary_histories: list[list[InterviewAnswer]] = []
        self.tasks: list[asyncio.Task] = []

    async def _wait(self) -> None:
        if self.entered is not None:
            self.entered.set()
        if self.gate is not None:
            await self.gate.wait()

    async def next_question(self, history) -> InterviewQuestion:
        self.question_histories.append(list(history))
        await self._wait()
        if self.question_exc is not None:
            raise self.question_exc
        return InterviewQuestion(text=f"Q{len(history) + 1}?")

    async def summarise(self, history) -> TraitSummary:
        self.summary_histories.append(list(history))
        self.tasks.append(asyncio.current_task())
        await self._wait()
        if self.summary_exc is not None:
            raise self.summary_exc
        return TraitSummary(summary="A quietly determined creature.", traits=["determined"])


class GatedBouncer:
    def __init__(self, entered: asyncio.Event | None = None,
                 gate: asyncio.Event | None = None, *, is_human: bool = True,
                 exc: Exception | None = None) -> None:
        self.entered = entered
        self.gate = gate
        self.is_human = is_human
        self.exc = exc
        self.calls = 0

    async def inspect(self, image_path: Path) -> BouncerVerdict:
        self.calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.exc is not None:
            raise self.exc
        return BouncerVerdict(is_human=self.is_human)


class StallingSender:
    """Sends that outlive their budget's cancellation and land far too late.

    The first ``stalls`` sends never finish on their own: the send budget
    cancels them, and each swallows that cancellation and returns anyway -- the
    worst case for ownership, since the message really was delivered. Later
    sends succeed immediately.
    """

    def __init__(self, stalls: int = 1) -> None:
        self.stalls = stalls
        self.entered = asyncio.Event()
        self.expired = asyncio.Event()
        self.texts: list[str] = []
        self.cancellations = 0
        self._never = asyncio.Event()

    async def send(self, **kwargs) -> None:
        # A bound async method, so AsyncMock awaits it as a side effect.
        self.texts.append(kwargs["text"])
        if len(self.texts) > self.stalls:
            return
        self.entered.set()
        try:
            await self._never.wait()
        except asyncio.CancelledError:
            self.cancellations += 1
            if self.cancellations >= self.stalls:
                self.expired.set()


# --- scheduling and deduplication -------------------------------------------

def test_photo_handler_schedules_one_worker_and_returns_promptly(tmp_path: Path) -> None:
    async def scenario():
        store = _awaiting()
        tasks = CountingTasks(store)
        entered, gate = asyncio.Event(), asyncio.Event()
        bouncer = GatedBouncer(entered, gate)
        bot = AsyncMock()
        kwargs = {"bouncer": bouncer, "interviewer": ScriptedInterviewer(),
                  "download_bytes": _jpeg, "media_root": tmp_path, "conversion_tasks": tasks}

        reply = await dispatch(store, _photo_update(), bot, **kwargs)
        assert reply is None
        assert tasks.active(1)
        await asyncio.wait_for(entered.wait(), 1)
        assert store.get(1).stage is Stage.BOUNCER_CHECK

        # A duplicate active photo must not create a second worker or download.
        await dispatch(store, _photo_update(), bot, **kwargs)
        assert tasks.started == [1]

        gate.set()
        await asyncio.wait_for(tasks.wait(), 1)
        assert bouncer.calls == 1
        assert store.get(1).stage is Stage.INTERVIEW
        await tasks.close()

    asyncio.run(scenario())


def test_duplicate_answer_while_a_turn_is_active_is_not_queued(tmp_path: Path) -> None:
    async def scenario():
        store = InMemorySessionStore()
        _at_interview(store)
        tasks = CountingTasks(store)
        entered, gate = asyncio.Event(), asyncio.Event()
        interviewer = ScriptedInterviewer(entered=entered, gate=gate)
        bot = AsyncMock()
        kwargs = {"interviewer": interviewer, "media_root": tmp_path,
                  "conversion_tasks": tasks}

        await dispatch(store, _text(ANSWER_SENTINEL), bot, **kwargs)
        await asyncio.wait_for(entered.wait(), 1)
        await dispatch(store, _text("a second, impatient answer"), bot, **kwargs)
        assert tasks.started == [1]
        assert len(interviewer.question_histories) == 1

        gate.set()
        await asyncio.wait_for(tasks.wait(), 1)
        state = store.get(1)
        assert [pair.answer for pair in state.interview.answers] == [ANSWER_SENTINEL]
        assert state.interview.pending_question == "Q2?"
        assert _sent(bot) == ["Q2?"]
        await tasks.close()

    asyncio.run(scenario())


# --- reset matrix ------------------------------------------------------------

@pytest.mark.parametrize("command", ["/start", "/restart"])
@pytest.mark.parametrize("phase", ["download", "bouncer", "first_question",
                                   "next_question", "summary", "send"])
def test_reset_during_early_work_recovers_and_blocks_late_results(
    tmp_path: Path, command: str, phase: str
) -> None:
    async def scenario():
        store = InMemorySessionStore()
        tasks = CountingTasks(store)
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        bot = AsyncMock()

        async def blocked():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()  # a dependency that ignores cancellation

        if phase == "send":
            async def send_message(**kwargs):
                if kwargs["text"].startswith("Q") or "interview" in kwargs["text"]:
                    await blocked()

            bot.send_message.side_effect = send_message

        async def download(file_id: str) -> bytes:
            if phase == "download":
                await blocked()
            return JPEG

        class Bouncer:
            async def inspect(self, image_path: Path) -> BouncerVerdict:
                if phase == "bouncer":
                    await blocked()
                return BouncerVerdict(is_human=True)

        class Interviewer:
            async def next_question(self, history) -> InterviewQuestion:
                if phase == "first_question" or (phase == "next_question" and history):
                    await blocked()
                return InterviewQuestion(text=f"Q{len(history) + 1}?")

            async def summarise(self, history) -> TraitSummary:
                if phase == "summary":
                    await blocked()
                return TraitSummary(summary="late summary")

        kwargs = {"bouncer": Bouncer(), "interviewer": Interviewer(),
                  "download_bytes": download, "media_root": tmp_path,
                  "conversion_tasks": tasks}

        if phase in ("download", "bouncer", "first_question"):
            store.set_stage(1, Stage.AWAITING_PHOTO)
            await dispatch(store, _photo_update(), bot, **kwargs)
        elif phase in ("next_question", "send"):
            _at_interview(store)
            await dispatch(store, _text(ANSWER_SENTINEL), bot, **kwargs)
        else:
            _at_interview(store, answered=TOTAL_QUESTIONS - 1, pending="Q6?")
            await dispatch(store, _text(ANSWER_SENTINEL), bot, **kwargs)

        await asyncio.wait_for(entered.wait(), 1)
        sends_before_reset = bot.send_message.await_count

        await dispatch(store, _text(command), bot, **kwargs)
        await asyncio.wait_for(cancelled.wait(), 1)

        assert store.get(1).stage is Stage.AWAITING_PHOTO
        assert store.get(1).interview is None
        assert not photo_dest(1, root=tmp_path).parent.exists()

        release.set()
        await asyncio.wait_for(tasks.wait(), 1)

        assert store.get(1).stage is Stage.AWAITING_PHOTO
        assert store.get(1).interview is None
        assert not photo_dest(1, root=tmp_path).exists()
        # Exactly one extra message: the reset prompt. No stale reply, no notice.
        assert bot.send_message.await_count == sends_before_reset + 1
        assert _sent(bot)[-1] not in (INTERVIEW_FAILURE_TEXT, BOUNCER_FAILURE_TEXT,
                                      PHOTO_DOWNLOAD_FAILURE_TEXT)
        await tasks.close()

    asyncio.run(scenario())


# --- budgets -----------------------------------------------------------------

@pytest.mark.parametrize("phase,budget,expected", [
    ("bouncer", "BOUNCER_TIMEOUT_SECONDS", BOUNCER_FAILURE_TEXT),
    ("question", "INTERVIEW_TIMEOUT_SECONDS", INTERVIEW_FAILURE_TEXT),
    ("summary", "INTERVIEW_TIMEOUT_SECONDS", INTERVIEW_FAILURE_TEXT),
])
def test_expired_budget_notifies_once_and_ignores_the_late_result(
    tmp_path: Path, monkeypatch, phase: str, budget: str, expected: str
) -> None:
    async def scenario():
        monkeypatch.setattr(early_flow, budget, 0.01)
        store = InMemorySessionStore()
        tasks = CountingTasks(store)
        entered, expired, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        bot = AsyncMock()

        async def blocked():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                expired.set()  # the budget, not a reset, cancelled this call
                await release.wait()

        class Bouncer:
            async def inspect(self, image_path: Path) -> BouncerVerdict:
                if phase == "bouncer":
                    await blocked()
                return BouncerVerdict(is_human=True)

        class Interviewer:
            async def next_question(self, history) -> InterviewQuestion:
                if phase == "question":
                    await blocked()
                return InterviewQuestion(text=f"Q{len(history) + 1}?")

            async def summarise(self, history) -> TraitSummary:
                if phase == "summary":
                    await blocked()
                return TraitSummary(summary="late summary")

        kwargs = {"bouncer": Bouncer(), "interviewer": Interviewer(),
                  "download_bytes": _jpeg, "media_root": tmp_path,
                  "conversion_tasks": tasks}
        if phase == "bouncer":
            store.set_stage(1, Stage.AWAITING_PHOTO)
            await dispatch(store, _photo_update(), bot, **kwargs)
        elif phase == "question":
            _at_interview(store)
            await dispatch(store, _text(ANSWER_SENTINEL), bot, **kwargs)
        else:
            _at_interview(store, answered=TOTAL_QUESTIONS - 1, pending="Q6?")
            await dispatch(store, _text(ANSWER_SENTINEL), bot, **kwargs)

        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(expired.wait(), 1)
        release.set()
        await asyncio.wait_for(tasks.wait(), 1)

        assert _sent(bot) == [expected]
        if phase == "bouncer":
            assert store.get(1).stage is Stage.AWAITING_PHOTO
            assert not photo_dest(1, root=tmp_path).exists()
        else:
            state = store.get(1)
            assert state.stage is Stage.INTERVIEW
            assert state.interview.pending_question == ("Q1?" if phase == "question"
                                                        else "Q6?")
            assert all(pair.answer != ANSWER_SENTINEL for pair in state.interview.answers)
        await tasks.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("answered,pending", [(0, "Q1?"), (TOTAL_QUESTIONS - 1, "Q6?")])
def test_send_budget_expiry_blocks_the_commit_and_notifies_once(
    tmp_path, monkeypatch, answered, pending
) -> None:
    async def scenario():
        monkeypatch.setattr(early_flow, "EARLY_SEND_TIMEOUT_SECONDS", 0.01)
        store = InMemorySessionStore()
        _at_interview(store, answered=answered, pending=pending)
        tasks = CountingTasks(store)
        sender = StallingSender()
        bot = AsyncMock()
        bot.send_message.side_effect = sender.send
        converter = AsyncMock()

        await dispatch(store, _text(ANSWER_SENTINEL), bot,
                       interviewer=ScriptedInterviewer(), converter=converter,
                       send_photo=AsyncMock(return_value=True), media_root=tmp_path,
                       conversion_tasks=tasks)
        await asyncio.wait_for(sender.entered.wait(), 1)
        await asyncio.wait_for(sender.expired.wait(), 1)
        await asyncio.wait_for(tasks.wait(), 1)

        state = store.get(1)
        assert state.stage is Stage.INTERVIEW  # no commit behind an expired send
        assert len(state.interview.answers) == answered
        assert state.interview.pending_question == pending
        assert state.interview.summary is None
        converter.generate_avatar.assert_not_awaited()
        # One bounded failure notice, never a retry loop.
        assert sender.texts[1:] == [INTERVIEW_FAILURE_TEXT]
        await tasks.close()

    asyncio.run(scenario())


def test_expired_failure_notice_is_logged_without_retrying(tmp_path, monkeypatch, caplog):
    async def scenario():
        monkeypatch.setattr(early_flow, "EARLY_SEND_TIMEOUT_SECONDS", 0.01)
        store = InMemorySessionStore()
        _at_interview(store)
        tasks = CountingTasks(store)
        sender = StallingSender(stalls=2)  # the question AND the notice both stall
        bot = AsyncMock()
        bot.send_message.side_effect = sender.send

        await dispatch(store, _text(ANSWER_SENTINEL), bot,
                       interviewer=ScriptedInterviewer(), media_root=tmp_path,
                       conversion_tasks=tasks)
        await asyncio.wait_for(sender.entered.wait(), 1)
        await asyncio.wait_for(sender.expired.wait(), 1)
        await asyncio.wait_for(tasks.wait(), 1)

        state = store.get(1)
        assert state.interview.answers == []
        assert state.interview.pending_question == "Q1?"
        # The next question went out, stalled past its budget, and was followed
        # by exactly one failure notice -- never a retry loop.
        assert sender.texts == ["Q2?", INTERVIEW_FAILURE_TEXT]
        await tasks.close()

    with caplog.at_level("ERROR"):
        asyncio.run(scenario())

    assert any("_notice" in record.getMessage() and "TimeoutError" in record.getMessage()
               for record in caplog.records)


# --- turn integrity ----------------------------------------------------------

def test_failed_question_send_leaves_the_turn_retryable_and_records_once(tmp_path) -> None:
    async def scenario():
        store = InMemorySessionStore()
        _at_interview(store)
        tasks = CountingTasks(store)
        interviewer = ScriptedInterviewer()
        bot = AsyncMock()
        bot.send_message.side_effect = [TelegramError("send failed"), None, None]
        kwargs = {"interviewer": interviewer, "media_root": tmp_path,
                  "conversion_tasks": tasks}

        await dispatch(store, _text(ANSWER_SENTINEL), bot, **kwargs)
        await asyncio.wait_for(tasks.wait(), 1)

        state = store.get(1)
        assert state.interview.answers == []  # nothing recorded behind a failed send
        assert state.interview.pending_question == "Q1?"
        assert state.stage is Stage.INTERVIEW

        await dispatch(store, _text(ANSWER_SENTINEL), bot, **kwargs)
        await asyncio.wait_for(tasks.wait(), 1)

        assert [pair.answer for pair in state.interview.answers] == [ANSWER_SENTINEL]
        assert state.interview.pending_question == "Q2?"
        await tasks.close()

    asyncio.run(scenario())


def test_failed_first_question_retries_without_rerunning_the_bouncer(tmp_path) -> None:
    async def scenario():
        store = _awaiting()
        tasks = CountingTasks(store)
        bouncer = GatedBouncer()
        interviewer = ScriptedInterviewer()
        bot = AsyncMock()
        bot.send_message.side_effect = [TelegramError("send failed"), None, None]
        kwargs = {"bouncer": bouncer, "interviewer": interviewer, "download_bytes": _jpeg,
                  "media_root": tmp_path, "conversion_tasks": tasks}

        await dispatch(store, _photo_update(), bot, **kwargs)
        await asyncio.wait_for(tasks.wait(), 1)
        assert store.get(1).stage is Stage.INTERVIEW
        interview = store.get(1).interview
        assert interview is None or not interview.answers
        assert photo_dest(1, root=tmp_path).read_bytes() == JPEG  # portrait retained

        await dispatch(store, _text("are you still there?"), bot, **kwargs)
        await asyncio.wait_for(tasks.wait(), 1)

        state = store.get(1)
        assert bouncer.calls == 1  # a successful Bouncer is never re-run
        assert state.interview.answers == []  # the nudge is not an answer
        assert state.interview.pending_question == "Q1?"
        assert INTERVIEW_INTRO in _sent(bot)[-1] and "Q1?" in _sent(bot)[-1]
        await tasks.close()

    asyncio.run(scenario())


def test_failed_summary_preserves_the_pending_question_and_prior_answers(tmp_path) -> None:
    async def scenario():
        store = InMemorySessionStore()
        _at_interview(store, answered=TOTAL_QUESTIONS - 1, pending="Q6?")
        tasks = CountingTasks(store)
        interviewer = ScriptedInterviewer(summary_exc=RuntimeError("provider down"))
        bot = AsyncMock()

        await dispatch(store, _text(ANSWER_SENTINEL), bot,
                       interviewer=interviewer, media_root=tmp_path, conversion_tasks=tasks)
        await asyncio.wait_for(tasks.wait(), 1)

        state = store.get(1)
        assert state.stage is Stage.INTERVIEW
        assert len(state.interview.answers) == TOTAL_QUESTIONS - 1
        assert state.interview.pending_question == "Q6?"
        assert state.interview.summary is None
        assert _sent(bot) == [INTERVIEW_FAILURE_TEXT]
        await tasks.close()

    asyncio.run(scenario())


def test_completion_acknowledgement_is_sent_before_the_converting_commit(tmp_path) -> None:
    async def scenario():
        store = InMemorySessionStore()
        _at_interview(store, answered=TOTAL_QUESTIONS - 1, pending="Q6?")
        tasks = CountingTasks(store)
        observed: list[tuple[Stage, int]] = []

        async def send_message(**kwargs):
            state = store.get(1)
            observed.append((state.stage, len(state.interview.answers)))

        bot = AsyncMock()
        bot.send_message.side_effect = send_message

        await dispatch(store, _text(ANSWER_SENTINEL), bot,
                       interviewer=ScriptedInterviewer(), media_root=tmp_path,
                       conversion_tasks=tasks)
        await asyncio.wait_for(tasks.wait(), 1)

        # The acknowledgement goes out while the turn is still uncommitted.
        assert observed[0] == (Stage.INTERVIEW, TOTAL_QUESTIONS - 1)
        assert _sent(bot)[0] == INTERVIEW_COMPLETE_TEXT
        state = store.get(1)
        assert len(state.interview.answers) == TOTAL_QUESTIONS
        assert state.interview.summary is not None
        assert state.stage is Stage.CONVERTING
        await tasks.close()

    asyncio.run(scenario())


def test_sixth_answer_uses_one_worker_and_one_task_through_every_stage(tmp_path, monkeypatch):
    async def scenario():
        from app.narrator import NarrationAudio
        store = InMemorySessionStore()
        _at_interview(store, answered=TOTAL_QUESTIONS - 1, pending="Q6?")
        portrait = photo_dest(1, root=tmp_path)
        portrait.parent.mkdir(parents=True, exist_ok=True)
        portrait.write_bytes(JPEG)
        tasks = CountingTasks(store)
        interviewer = ScriptedInterviewer()
        seen: dict[str, asyncio.Task] = {}

        class Converter:
            async def generate_avatar(self, path: Path, summary: TraitSummary) -> bytes:
                seen["converter"] = asyncio.current_task()
                return PNG

        narration = "A quietly determined creature greets the evening."

        class Scripter:
            async def write_script(self, summary: TraitSummary) -> NarrationScript:
                seen["scripter"] = asyncio.current_task()
                return NarrationScript(text=narration)

        class Narrator:
            async def render(self, text: str) -> NarrationAudio:
                seen["narrator"] = asyncio.current_task()
                return NarrationAudio(data=b"\x01\x00" * 2400,
                                      mime_type="audio/L16;codec=pcm;rate=24000")

        monkeypatch.setattr(orchestrator, "encode_voice", AsyncMock(return_value=b"fake-ogg"))
        monkeypatch.setattr(orchestrator, "validate_voice", AsyncMock(return_value=None))
        bot = AsyncMock()

        await dispatch(store, _text(ANSWER_SENTINEL), bot, interviewer=interviewer,
                       converter=Converter(), scripter=Scripter(), narrator=Narrator(),
                       send_photo=AsyncMock(return_value=True),
                       send_voice=AsyncMock(return_value=True),
                       media_root=tmp_path, conversion_tasks=tasks)
        await asyncio.wait_for(tasks.wait(), 2)

        assert tasks.started == [1]  # exactly one registry.start for this turn
        assert store.get(1).stage is Stage.DONE
        worker = interviewer.tasks[0]
        assert worker is not None
        assert seen["converter"] is worker
        assert seen["scripter"] is worker
        assert seen["narrator"] is worker
        await tasks.close()

    asyncio.run(scenario())


def test_shutdown_reports_pending_work_without_unhandled_errors(tmp_path, caplog, monkeypatch):
    async def scenario():
        monkeypatch.setattr(conversion_tasks, "SHUTDOWN_WAIT_SECONDS", 0.05)
        store = _awaiting()
        tasks = CountingTasks(store)
        release = asyncio.Event()
        entered = asyncio.Event()

        async def download(file_id: str) -> bytes:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()  # a dependency that outlives cancellation
            return JPEG

        bot = AsyncMock()
        kwargs = {"bouncer": GatedBouncer(), "interviewer": ScriptedInterviewer(),
                  "download_bytes": download, "media_root": tmp_path,
                  "conversion_tasks": tasks}
        await dispatch(store, _photo_update(), bot, **kwargs)
        await asyncio.wait_for(entered.wait(), 1)
        with caplog.at_level("WARNING"):
            await asyncio.wait_for(tasks.close(), 2)
        assert not tasks.active(1)
        # Reset stays usable even though the old dependency is still running.
        await dispatch(store, _text("/restart"), bot, **kwargs)
        assert store.get(1).stage is Stage.AWAITING_PHOTO
        release.set()
        return [r.getMessage() for r in caplog.records if r.levelno >= 30]

    messages = asyncio.run(scenario())
    assert any("pending" in message for message in messages)
    assert all("Traceback" not in message for message in messages)
