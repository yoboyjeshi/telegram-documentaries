"""Tests for the INTERVIEW-stage orchestrator (progression, resilience, routing)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from app import orchestrator
from app.handlers import (
    INTERVIEW_COMPLETE_TEXT,
    INTERVIEW_FAILURE_TEXT,
    INTERVIEW_INTRO,
    INTERVIEW_PHOTO_REDIRECT_TEXT,
    INTERVIEW_REPROMPT_TEXT,
    INTERVIEW_UNKNOWN_COMMAND_TEXT,
)
from app.interviewer import TOTAL_QUESTIONS, InterviewQuestion
from app.session_store import InMemorySessionStore
from app.states import InterviewAnswer, InterviewState, Stage, TraitSummary
from app.telegram_adapter import ChatInput

ANSWER_SENTINEL = "ANSWER_SENTINEL_cheese"
SUMMARY_SENTINEL = "SUMMARY_SENTINEL_bold_explorer"


class FakeInterviewer:
    """Deterministic interviewer double: no network, records what it was asked."""

    def __init__(
        self,
        question_exc: Exception | None = None,
        summary_exc: Exception | None = None,
    ) -> None:
        self._question_exc = question_exc
        self._summary_exc = summary_exc
        self.question_histories: list[list[InterviewAnswer]] = []
        self.summary_histories: list[list[InterviewAnswer]] = []

    async def next_question(self, history: Sequence[InterviewAnswer]) -> InterviewQuestion:
        self.question_histories.append(list(history))
        if self._question_exc is not None:
            raise self._question_exc
        return InterviewQuestion(text=f"Q{len(history) + 1}?")

    async def summarise(self, history: Sequence[InterviewAnswer]) -> TraitSummary:
        self.summary_histories.append(list(history))
        if self._summary_exc is not None:
            raise self._summary_exc
        return TraitSummary(summary=SUMMARY_SENTINEL, traits=["bold"])


def _text(text: str, chat_id: int = 1) -> ChatInput:
    return ChatInput(chat_id=chat_id, kind="text", text=text)


def _photo(chat_id: int = 1) -> ChatInput:
    return ChatInput(chat_id=chat_id, kind="photo", file_id="fid")


def _command(command: str, chat_id: int = 1) -> ChatInput:
    return ChatInput(chat_id=chat_id, kind="command", text=f"/{command}", command=command)


def _at_interview(
    store: InMemorySessionStore, pending: str | None = "Q1?", chat_id: int = 1
) -> None:
    state = store.get(chat_id)
    state.stage = Stage.INTERVIEW
    state.interview = InterviewState(pending_question=pending)


def test_start_interview_sets_fresh_state_asks_q1_and_keeps_stage() -> None:
    store = InMemorySessionStore()
    store.get(1).stage = Stage.INTERVIEW
    interviewer = FakeInterviewer()

    reply = asyncio.run(orchestrator.start_interview(store, 1, interviewer=interviewer))

    state = store.get(1)
    assert state.stage is Stage.INTERVIEW
    assert state.interview is not None
    assert state.interview.answers == []
    assert state.interview.pending_question == "Q1?"
    assert INTERVIEW_INTRO in reply
    assert "Q1?" in reply
    assert interviewer.question_histories == [[]]


def test_start_interview_failure_returns_failure_and_leaves_no_pending() -> None:
    store = InMemorySessionStore()
    store.get(1).stage = Stage.INTERVIEW
    interviewer = FakeInterviewer(question_exc=RuntimeError("gemini down"))

    reply = asyncio.run(orchestrator.start_interview(store, 1, interviewer=interviewer))

    assert reply == INTERVIEW_FAILURE_TEXT
    assert store.get(1).stage is Stage.INTERVIEW
    assert store.get(1).interview is not None
    assert store.get(1).interview.pending_question is None


def test_answering_advances_one_step_and_records_the_pair() -> None:
    store = InMemorySessionStore()
    _at_interview(store, pending="Q1?")
    interviewer = FakeInterviewer()

    reply = asyncio.run(orchestrator.handle_interview_update(store, _text(ANSWER_SENTINEL), interviewer=interviewer))

    state = store.get(1)
    assert reply == "Q2?"
    assert state.interview is not None
    assert state.interview.answers == [
        InterviewAnswer(question="Q1?", answer=ANSWER_SENTINEL)
    ]
    assert state.interview.pending_question == "Q2?"
    assert state.stage is Stage.INTERVIEW
    # The interviewer saw the updated history (previous answers + the new pair).
    assert interviewer.question_histories == [
        [InterviewAnswer(question="Q1?", answer=ANSWER_SENTINEL)]
    ]


def test_progression_through_all_questions_completes_and_advances() -> None:
    store = InMemorySessionStore()
    store.get(1).stage = Stage.INTERVIEW
    interviewer = FakeInterviewer()

    reply = asyncio.run(orchestrator.start_interview(store, 1, interviewer=interviewer))
    assert "Q1?" in reply

    for index in range(1, TOTAL_QUESTIONS + 1):
        reply = asyncio.run(
            orchestrator.handle_interview_update(
                store, _text(f"answer-{index}"), interviewer=interviewer
            )
        )

    state = store.get(1)
    assert state.stage is Stage.CONVERTING
    assert state.interview is not None
    assert state.interview.pending_question is None
    assert state.interview.summary is not None
    assert state.interview.summary.summary == SUMMARY_SENTINEL
    assert len(state.interview.answers) == TOTAL_QUESTIONS
    assert reply == INTERVIEW_COMPLETE_TEXT
    assert interviewer.summary_histories and len(interviewer.summary_histories[-1]) == TOTAL_QUESTIONS


def test_blank_answer_reasks_without_recording_or_advancing() -> None:
    store = InMemorySessionStore()
    _at_interview(store, pending="Q1?")
    interviewer = FakeInterviewer()

    reply = asyncio.run(orchestrator.handle_interview_update(store, _text("   "), interviewer=interviewer))

    state = store.get(1)
    assert reply == INTERVIEW_REPROMPT_TEXT
    assert state.interview is not None
    assert state.interview.answers == []
    assert state.interview.pending_question == "Q1?"
    assert state.stage is Stage.INTERVIEW
    assert interviewer.question_histories == []


def test_photo_mid_interview_redirects_without_state_change() -> None:
    store = InMemorySessionStore()
    _at_interview(store, pending="Q1?")
    interviewer = FakeInterviewer()

    reply = asyncio.run(
        orchestrator.handle_interview_update(store, _photo(), interviewer=interviewer)
    )

    state = store.get(1)
    assert reply == INTERVIEW_PHOTO_REDIRECT_TEXT
    assert state.interview is not None
    assert state.interview.answers == []
    assert state.interview.pending_question == "Q1?"
    assert state.stage is Stage.INTERVIEW
    assert interviewer.question_histories == []


def test_unknown_command_mid_interview_nudges_without_recording() -> None:
    store = InMemorySessionStore()
    _at_interview(store, pending="Q1?")
    interviewer = FakeInterviewer()

    reply = asyncio.run(
        orchestrator.handle_interview_update(store, _command("foo"), interviewer=interviewer)
    )

    state = store.get(1)
    assert reply == INTERVIEW_UNKNOWN_COMMAND_TEXT
    assert state.interview is not None
    assert state.interview.answers == []
    assert state.interview.pending_question == "Q1?"
    assert state.stage is Stage.INTERVIEW
    assert interviewer.question_histories == []


def test_self_heal_text_without_interview_asks_q1() -> None:
    store = InMemorySessionStore()
    store.get(1).stage = Stage.INTERVIEW  # no interview state at all
    interviewer = FakeInterviewer()

    reply = asyncio.run(orchestrator.handle_interview_update(store, _text("hello"), interviewer=interviewer))

    state = store.get(1)
    assert INTERVIEW_INTRO in reply
    assert "Q1?" in reply
    assert state.interview is not None
    assert state.interview.pending_question == "Q1?"
    assert state.stage is Stage.INTERVIEW
    assert interviewer.question_histories == [[]]


def test_next_question_failure_returns_failure_and_leaves_state_unchanged() -> None:
    store = InMemorySessionStore()
    _at_interview(store, pending="Q1?")
    interviewer = FakeInterviewer(question_exc=RuntimeError("gemini down"))

    reply = asyncio.run(orchestrator.handle_interview_update(store, _text(ANSWER_SENTINEL), interviewer=interviewer))

    state = store.get(1)
    assert reply == INTERVIEW_FAILURE_TEXT
    assert state.interview is not None
    assert state.interview.answers == []
    assert state.interview.pending_question == "Q1?"
    assert state.stage is Stage.INTERVIEW


def test_summarise_failure_returns_failure_and_does_not_advance() -> None:
    store = InMemorySessionStore()
    state = store.get(1)
    state.stage = Stage.INTERVIEW
    state.interview = InterviewState(
        answers=[InterviewAnswer(question=f"Q{i}?", answer=f"a{i}") for i in range(1, TOTAL_QUESTIONS)],
        pending_question="Q6?",
    )
    interviewer = FakeInterviewer(summary_exc=RuntimeError("gemini down"))

    reply = asyncio.run(orchestrator.handle_interview_update(store, _text(ANSWER_SENTINEL), interviewer=interviewer))

    assert reply == INTERVIEW_FAILURE_TEXT
    assert state.stage is Stage.INTERVIEW
    assert state.interview is not None
    assert len(state.interview.answers) == TOTAL_QUESTIONS - 1
    assert state.interview.pending_question == "Q6?"
    assert state.interview.summary is None


def test_failure_logs_type_and_message_but_not_answer_text(caplog) -> None:
    store = InMemorySessionStore()
    _at_interview(store, pending="Q1?")
    interviewer = FakeInterviewer(question_exc=RuntimeError("gemini timeout"))

    with caplog.at_level(logging.ERROR):
        asyncio.run(
            orchestrator.handle_interview_update(
                store, _text(ANSWER_SENTINEL), interviewer=interviewer
            )
        )

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors
    assert any("RuntimeError" in r.getMessage() and "gemini timeout" in r.getMessage() for r in errors)
    assert ANSWER_SENTINEL not in caplog.text
    # No traceback: a full traceback can embed the request payload.
    assert all(r.exc_info is None for r in errors)


def test_progress_is_logged_as_counts_not_answer_text(caplog) -> None:
    store = InMemorySessionStore()
    _at_interview(store, pending="Q1?")
    interviewer = FakeInterviewer()

    with caplog.at_level(logging.INFO):
        asyncio.run(
            orchestrator.handle_interview_update(
                store, _text(ANSWER_SENTINEL), interviewer=interviewer
            )
        )

    assert "answered=1/6" in caplog.text
    assert ANSWER_SENTINEL not in caplog.text


def test_summary_text_is_never_logged(caplog) -> None:
    store = InMemorySessionStore()
    state = store.get(1)
    state.stage = Stage.INTERVIEW
    state.interview = InterviewState(
        answers=[InterviewAnswer(question=f"Q{i}?", answer=f"a{i}") for i in range(1, TOTAL_QUESTIONS)],
        pending_question="Q6?",
    )
    interviewer = FakeInterviewer()

    with caplog.at_level(logging.INFO):
        asyncio.run(
            orchestrator.handle_interview_update(store, _text("final"), interviewer=interviewer)
        )

    assert SUMMARY_SENTINEL not in caplog.text
    assert "final" not in caplog.text
