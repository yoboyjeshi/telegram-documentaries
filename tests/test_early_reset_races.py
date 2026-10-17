"""An expired budget and a concurrent reset must leave no stale effect at all.

The matrix in ``test_early_workers`` covers each on its own. Here both happen to
the same worker: its external call absorbs the budget's cancellation, then the
reset's cancellation, and finally succeeds far too late. That late success must
never reach the replacement session -- no state, no send, no media -- and must
not surface as an unhandled task failure either.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from image_fixtures import JPEG

from app import early_flow
from app.bot import dispatch
from app.bouncer import BouncerVerdict
from app.conversion_tasks import ConversionTasks
from app.handlers import (
    BOUNCER_FAILURE_TEXT,
    GREETING_TEXT,
    INTERVIEW_FAILURE_TEXT,
    PHOTO_DOWNLOAD_FAILURE_TEXT,
)
from app.interviewer import TOTAL_QUESTIONS, InterviewQuestion
from app.media import photo_dest
from app.session_store import InMemorySessionStore
from app.states import InterviewAnswer, InterviewState, Stage, TraitSummary

LATE_SENTINEL = "LATE_RESULT_SENTINEL_never_use_me"
ANSWER_SENTINEL = "ANSWER_SENTINEL_quietly_determined"
BUDGET = 0.01


class ResistantCall:
    """One external call that absorbs every cancellation until it is released.

    The first cancellation is the application budget; the second is the reset.
    Both are swallowed, so ownership cannot be inferred from a raised
    ``CancelledError`` -- only from the session version, stage and the worker's
    own cancellation state.
    """

    def __init__(self, result: object) -> None:
        self.result = result
        self.entered = asyncio.Event()
        self.budget_expired = asyncio.Event()
        self.reset_seen = asyncio.Event()
        self.release = asyncio.Event()
        self.cancellations = 0

    async def __call__(self) -> object:
        self.entered.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancellations += 1
                target = self.budget_expired if self.cancellations == 1 else self.reset_seen
                target.set()
        return self.result


def _photo_update(chat_id: int = 1) -> SimpleNamespace:
    message = SimpleNamespace(text=None, chat=SimpleNamespace(id=chat_id),
                              photo=[SimpleNamespace(file_id="fid", file_size=len(JPEG))])
    return SimpleNamespace(effective_message=message, message=message)


def _text(text: str, chat_id: int = 1) -> SimpleNamespace:
    message = SimpleNamespace(text=text, chat=SimpleNamespace(id=chat_id))
    return SimpleNamespace(effective_message=message, message=message)


async def _jpeg(file_id: str) -> bytes:
    return JPEG


def _at_interview(store, *, answered: int, pending: str) -> None:
    state = store.get(1)
    state.stage = Stage.INTERVIEW
    state.interview = InterviewState(
        answers=[InterviewAnswer(question=f"Q{i + 1}?", answer=f"a{i + 1}")
                 for i in range(answered)],
        pending_question=pending,
    )


def _sent(bot) -> list[str]:
    return [call.kwargs["text"] for call in bot.send_message.await_args_list]


@pytest.mark.parametrize("command", ["/start", "/restart"])
@pytest.mark.parametrize("phase", ["bouncer", "question", "summary"])
def test_expired_budget_plus_a_reset_leaves_no_stale_state_send_or_media(
    tmp_path: Path, monkeypatch, caplog, command: str, phase: str
) -> None:
    async def scenario():
        budget = ("BOUNCER_TIMEOUT_SECONDS" if phase == "bouncer"
                  else "INTERVIEW_TIMEOUT_SECONDS")
        monkeypatch.setattr(early_flow, budget, BUDGET)
        store = InMemorySessionStore()
        tasks = ConversionTasks(store)
        bot = AsyncMock()
        call = ResistantCall({
            "bouncer": BouncerVerdict(is_human=True),
            "question": InterviewQuestion(text=LATE_SENTINEL),
            "summary": TraitSummary(summary=LATE_SENTINEL),
        }[phase])

        class Bouncer:
            async def inspect(self, image_path: Path) -> BouncerVerdict:
                if phase == "bouncer":
                    return await call()
                return BouncerVerdict(is_human=True)

        class Interviewer:
            async def next_question(self, history) -> InterviewQuestion:
                if phase == "question":
                    return await call()
                return InterviewQuestion(text=f"Q{len(history) + 1}?")

            async def summarise(self, history) -> TraitSummary:
                if phase == "summary":
                    return await call()
                return TraitSummary(summary="an unused summary")

        kwargs = {"bouncer": Bouncer(), "interviewer": Interviewer(),
                  "download_bytes": _jpeg, "media_root": tmp_path,
                  "conversion_tasks": tasks}
        try:
            if phase == "bouncer":
                store.set_stage(1, Stage.AWAITING_PHOTO)
                await dispatch(store, _photo_update(), bot, **kwargs)
            elif phase == "question":
                _at_interview(store, answered=0, pending="Q1?")
                await dispatch(store, _text(ANSWER_SENTINEL), bot, **kwargs)
            else:
                _at_interview(store, answered=TOTAL_QUESTIONS - 1, pending="Q6?")
                await dispatch(store, _text(ANSWER_SENTINEL), bot, **kwargs)

            await asyncio.wait_for(call.entered.wait(), 1)
            if phase == "bouncer":
                # The portrait is published before the Bouncer runs.
                assert photo_dest(1, root=tmp_path).exists()
            await asyncio.wait_for(call.budget_expired.wait(), 1)

            # The reset lands while the expired call is still running.
            await dispatch(store, _text(command), bot, **kwargs)
            await asyncio.wait_for(call.reset_seen.wait(), 1)
            assert store.get(1).stage is Stage.AWAITING_PHOTO
            assert store.get(1).interview is None
            assert not photo_dest(1, root=tmp_path).parent.exists()
        finally:
            call.release.set()
            await asyncio.wait_for(tasks.wait(), 1)

        assert call.cancellations == 2  # the budget, then the reset: both absorbed
        state = store.get(1)
        assert state.stage is Stage.AWAITING_PHOTO
        assert state.interview is None
        assert state.narration_text is None
        assert not photo_dest(1, root=tmp_path).parent.exists()
        # Exactly one message: the reset greeting. No stale reply, no notice.
        assert _sent(bot) == [GREETING_TEXT]
        assert all(text not in _sent(bot) for text in
                   (BOUNCER_FAILURE_TEXT, INTERVIEW_FAILURE_TEXT, PHOTO_DOWNLOAD_FAILURE_TEXT))
        assert not tasks.active(1)
        await tasks.close()

    with caplog.at_level("DEBUG"):
        asyncio.run(scenario())

    assert LATE_SENTINEL not in caplog.text
    assert ANSWER_SENTINEL not in caplog.text
    assert not [r for r in caplog.records if "Pipeline task failed" in r.getMessage()]
