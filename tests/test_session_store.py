"""Tests for the per-chat in-memory session store."""

from __future__ import annotations

import pytest

from app.session_store import InMemorySessionStore
from app.states import IllegalTransitionError, Stage


def test_unknown_chat_is_created_in_greeting() -> None:
    store = InMemorySessionStore()
    assert store.get(100).stage is Stage.GREETING


def test_same_chat_returns_same_state() -> None:
    store = InMemorySessionStore()
    assert store.get(7) is store.get(7)


def test_reset_returns_greeting_from_any_stage() -> None:
    store = InMemorySessionStore()
    for stage in (Stage.AWAITING_PHOTO, Stage.INTERVIEW, Stage.SCRIPTING, Stage.DONE):
        store.get(1).stage = stage
        assert store.reset(1).stage is Stage.GREETING


def test_reset_is_idempotent() -> None:
    store = InMemorySessionStore()
    assert store.reset(3).stage is Stage.GREETING
    assert store.reset(3).stage is Stage.GREETING


def test_sessions_are_isolated_per_chat() -> None:
    store = InMemorySessionStore()
    store.get(1).stage = Stage.INTERVIEW
    assert store.get(2).stage is Stage.GREETING
    store.reset(2)
    assert store.get(1).stage is Stage.INTERVIEW


def test_set_stage_enforces_transition_rules() -> None:
    store = InMemorySessionStore()
    assert store.set_stage(1, Stage.AWAITING_PHOTO).stage is Stage.AWAITING_PHOTO
    with pytest.raises(IllegalTransitionError):
        store.set_stage(1, Stage.INTERVIEW)
