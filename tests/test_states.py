"""Tests for the stage enum, session model, and transition rules."""

from __future__ import annotations

import pytest

from app.states import (
    PIPELINE_ORDER,
    IllegalTransitionError,
    SessionState,
    Stage,
    assert_transition,
    can_transition,
    next_stage,
)


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
