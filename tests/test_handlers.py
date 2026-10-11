"""Tests for the pure handlers: greeting, reset, and echo."""

from __future__ import annotations

from app.handlers import GREETING_TEXT, handle_restart, handle_start, handle_text
from app.logging_config import FRIENDLY_ERROR
from app.session_store import InMemorySessionStore
from app.states import Stage
from app.telegram_adapter import ChatInput


def _input(
    chat_id: int = 1,
    kind: str = "text",
    text: str | None = "hello",
    command: str | None = None,
) -> ChatInput:
    return ChatInput(chat_id=chat_id, kind=kind, text=text, command=command)


def test_start_resets_and_greets() -> None:
    store = InMemorySessionStore()
    store.get(1).stage = Stage.INTERVIEW

    reply = handle_start(store, _input(kind="command", text="/start", command="start"))

    assert reply == GREETING_TEXT
    assert store.get(1).stage is Stage.GREETING


def test_restart_resets_from_any_stage() -> None:
    store = InMemorySessionStore()
    for stage in (Stage.AWAITING_PHOTO, Stage.SCRIPTING, Stage.DONE):
        store.get(5).stage = stage
        reply = handle_restart(
            store, _input(chat_id=5, kind="command", text="/restart", command="restart")
        )
        assert reply == GREETING_TEXT
        assert store.get(5).stage is Stage.GREETING


def test_echo_returns_text_verbatim() -> None:
    store = InMemorySessionStore()
    assert handle_text(store, _input(text="quack quack")) == "quack quack"


def test_handler_never_raises_even_if_store_fails() -> None:
    class BoomStore:
        def reset(self, chat_id: int) -> None:
            raise RuntimeError("boom")

    reply = handle_start(BoomStore(), _input(kind="command", text="/start", command="start"))

    assert reply == FRIENDLY_ERROR
