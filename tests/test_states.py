"""Tests for the stage enum, session model, and transition rules."""

from __future__ import annotations

import pytest

from app.states import (
    PIPELINE_ORDER,
    IllegalTransitionError,
    InterviewAnswer,
    InterviewState,
    SessionState,
    Stage,
    TraitSummary,
    assert_transition,
    can_transition,
    next_stage,
)


def test_stage_enum_has_exactly_eight_members() -> None:
    assert len(list(Stage)) == 8
    assert set(Stage) == set(PIPELINE_ORDER)


def test_stage_members_in_pipeline_order() -> None:
    assert [stage.value for stage in PIPELINE_ORDER] == [
        "GREETING",
        "AWAITING_PHOTO",
        "BOUNCER_CHECK",
        "INTERVIEW",
        "CONVERTING",
        "SCRIPTING",
        "NARRATING",
        "DONE",
    ]
    assert len(PIPELINE_ORDER) == 8


def test_next_stage_walks_the_pipeline() -> None:
    assert next_stage(Stage.GREETING) is Stage.AWAITING_PHOTO
    assert next_stage(Stage.INTERVIEW) is Stage.CONVERTING
    assert next_stage(Stage.NARRATING) is Stage.DONE


def test_next_stage_is_none_at_the_end() -> None:
    assert next_stage(Stage.DONE) is None


def test_reset_to_greeting_is_always_allowed() -> None:
    for stage in PIPELINE_ORDER:
        assert can_transition(stage, Stage.GREETING)


def test_ordered_step_is_allowed() -> None:
    assert can_transition(Stage.GREETING, Stage.AWAITING_PHOTO)
    assert can_transition(Stage.NARRATING, Stage.DONE)


def test_illegal_jump_raises() -> None:
    with pytest.raises(IllegalTransitionError):
        assert_transition(Stage.GREETING, Stage.INTERVIEW)


def test_session_state_defaults_to_greeting() -> None:
    state = SessionState(chat_id=42)
    assert state.chat_id == 42
    assert state.stage is Stage.GREETING


def test_bouncer_flow_transitions_are_allowed() -> None:
    assert can_transition(Stage.AWAITING_PHOTO, Stage.BOUNCER_CHECK)
    assert can_transition(Stage.BOUNCER_CHECK, Stage.INTERVIEW)
    # The single Bouncer rejection retry edge:
    assert can_transition(Stage.BOUNCER_CHECK, Stage.AWAITING_PHOTO)


def test_unrelated_backward_moves_stay_illegal() -> None:
    with pytest.raises(IllegalTransitionError):
        assert_transition(Stage.INTERVIEW, Stage.AWAITING_PHOTO)
    with pytest.raises(IllegalTransitionError):
        assert_transition(Stage.CONVERTING, Stage.BOUNCER_CHECK)


def test_trait_summary_schema() -> None:
    summary = TraitSummary(summary="A bold explorer.", traits=["brave", "curious"])
    assert summary.summary == "A bold explorer."
    assert summary.traits == ["brave", "curious"]


def test_trait_summary_defaults_to_empty_traits() -> None:
    summary = TraitSummary(summary="A bold explorer.")
    assert summary.traits == []


def test_interview_answer_pairs_question_and_answer() -> None:
    pair = InterviewAnswer(question="Favourite snack?", answer="Cheese")
    assert pair.question == "Favourite snack?"
    assert pair.answer == "Cheese"


def test_interview_state_defaults() -> None:
    state = InterviewState()
    assert state.answers == []
    assert state.pending_question is None
    assert state.summary is None


def test_interview_state_instances_do_not_share_defaults() -> None:
    a, b = InterviewState(), InterviewState()
    a.answers.append(InterviewAnswer(question="q", answer="a"))
    a.pending_question = "q"
    a.summary = TraitSummary(summary="s")
    assert b.answers == []
    assert b.pending_question is None
    assert b.summary is None


def test_interview_state_record_pairs_pending_question_and_clears_pending() -> None:
    state = InterviewState(pending_question="Favourite snack?")
    state.record("Cheese")
    assert len(state.answers) == 1
    assert state.answers[0].question == "Favourite snack?"
    assert state.answers[0].answer == "Cheese"
    assert state.pending_question is None


def test_interview_state_record_fails_loud_without_pending_question() -> None:
    state = InterviewState()
    with pytest.raises(RuntimeError):
        state.record("orphan answer")


def test_session_state_interview_defaults_to_none() -> None:
    state = SessionState(chat_id=1)
    assert state.interview is None
