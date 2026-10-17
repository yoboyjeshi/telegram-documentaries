"""Tests for the Interviewer contract, ADK wiring, and lazy construction (no network)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

import pytest

from app.config import ConfigurationError
from app.interviewer import (
    INTERVIEWER_MODEL,
    TOTAL_QUESTIONS,
    AdkInterviewer,
    InterviewQuestion,
    LazyInterviewer,
)
from app.states import InterviewAnswer, TraitSummary


def _history() -> list[InterviewAnswer]:
    return [InterviewAnswer(question="q1", answer="a1")]


def test_interviewer_model_name() -> None:
    assert INTERVIEWER_MODEL == "gemini-3.1-flash-lite"


def test_total_questions_is_six_and_within_range() -> None:
    assert TOTAL_QUESTIONS == 6
    assert 5 <= TOTAL_QUESTIONS <= 7


def test_interview_question_schema() -> None:
    question = InterviewQuestion(text="What is your favourite snack?")
    assert question.text == "What is your favourite snack?"


def test_adk_interviewer_requires_a_key() -> None:
    with pytest.raises(ConfigurationError):
        AdkInterviewer("")


def test_adk_interviewer_never_logs_the_key(caplog, monkeypatch) -> None:
    """The real ADK path must never emit the key, even when it fails.

    The key-exposure step is monkeypatched to raise, so the genuine ``_run_adk``
    path runs and fails offline (no network); any code on that path that logged
    the key would land in ``caplog``.
    """
    secret = "SUPER_SECRET_GEMINI_KEY"
    seen: list[str] = []

    def explode(key: str) -> None:
        seen.append(key)  # proves the real path carried the actual key
        raise RuntimeError("GEMINI_API_KEY rejected")

    monkeypatch.setattr("app.interviewer.ensure_google_api_key", explode)
    interviewer = AdkInterviewer(secret)

    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError, match="rejected"):
        asyncio.run(interviewer.next_question(_history()))

    assert seen == [secret]
    assert secret not in caplog.text


def test_adk_interviewer_question_runner_delegates_and_passes_history() -> None:
    seen: list[Sequence[InterviewAnswer]] = []

    async def question_runner(history: Sequence[InterviewAnswer]) -> InterviewQuestion:
        seen.append(history)
        return InterviewQuestion(text="How do you start your day?")

    interviewer = AdkInterviewer("key", question_runner=question_runner)
    history = _history()
    result = asyncio.run(interviewer.next_question(history))

    assert result.text == "How do you start your day?"
    assert seen == [history]


def test_adk_interviewer_summary_runner_delegates_and_passes_history() -> None:
    async def summary_runner(history: Sequence[InterviewAnswer]) -> TraitSummary:
        return TraitSummary(summary="A curious night owl.", traits=["curious"])

    interviewer = AdkInterviewer("key", summary_runner=summary_runner)
    result = asyncio.run(interviewer.summarise(_history()))

    assert result.summary == "A curious night owl."
    assert result.traits == ["curious"]


def test_adk_interviewer_propagates_question_runner_errors() -> None:
    async def question_runner(history: Sequence[InterviewAnswer]) -> InterviewQuestion:
        raise RuntimeError("gemini down")

    interviewer = AdkInterviewer("key", question_runner=question_runner)
    with pytest.raises(RuntimeError):
        asyncio.run(interviewer.next_question(_history()))


def test_adk_interviewer_propagates_summary_runner_errors() -> None:
    async def summary_runner(history: Sequence[InterviewAnswer]) -> TraitSummary:
        raise RuntimeError("gemini down")

    interviewer = AdkInterviewer("key", summary_runner=summary_runner)
    with pytest.raises(RuntimeError):
        asyncio.run(interviewer.summarise(_history()))


def test_lazy_interviewer_is_key_free_at_construction() -> None:
    def provider() -> str:
        raise AssertionError("key must not be requested at construction")

    LazyInterviewer(provider)  # constructing must not fetch the key


def test_lazy_interviewer_builds_once_and_reuses() -> None:
    builds = {"n": 0}

    async def question_runner(history: Sequence[InterviewAnswer]) -> InterviewQuestion:
        return InterviewQuestion(text="Q?")

    async def summary_runner(history: Sequence[InterviewAnswer]) -> TraitSummary:
        return TraitSummary(summary="done")

    def builder(key: str) -> AdkInterviewer:
        builds["n"] += 1
        return AdkInterviewer(key, question_runner=question_runner, summary_runner=summary_runner)

    interviewer = LazyInterviewer(lambda: "key", builder=builder)
    asyncio.run(interviewer.next_question([]))
    asyncio.run(interviewer.summarise([]))
    assert builds["n"] == 1


def test_lazy_interviewer_propagates_missing_key() -> None:
    def provider() -> str:
        raise ConfigurationError("GEMINI_API_KEY is not set.")

    interviewer = LazyInterviewer(provider)
    with pytest.raises(ConfigurationError):
        asyncio.run(interviewer.next_question([]))
