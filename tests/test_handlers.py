"""Tests for the pure handlers: greeting/reset, echo, and photo prompts."""

from __future__ import annotations

from pathlib import Path

from app import media
from app.bouncer import REFUSAL_LINES, BouncerVerdict
from app.handlers import (
    AWAITING_PHOTO_PROMPT,
    GREETING_TEXT,
    INTERVIEW_COMPLETE_TEXT,
    INTERVIEW_FAILURE_TEXT,
    INTERVIEW_INTRO,
    INTERVIEW_OTHER_REDIRECT_TEXT,
    INTERVIEW_PHOTO_REDIRECT_TEXT,
    INTERVIEW_REPROMPT_TEXT,
    INTERVIEW_UNKNOWN_COMMAND_TEXT,
    PHOTO_REDIRECT_TEXT,
    apply_bouncer_verdict,
    handle_photo_wrong_stage,
    handle_restart,
    handle_start,
    handle_text,
)
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


def test_start_resets_and_awaits_a_photo(tmp_path: Path) -> None:
    store = InMemorySessionStore()
    store.get(1).stage = Stage.INTERVIEW

    reply = handle_start(
        store, _input(kind="command", text="/start", command="start"), media_root=tmp_path
    )

    assert reply == GREETING_TEXT
    assert store.get(1).stage is Stage.AWAITING_PHOTO


def test_restart_resets_from_any_stage_and_awaits_a_photo(tmp_path: Path) -> None:
    store = InMemorySessionStore()
    for stage in (Stage.AWAITING_PHOTO, Stage.SCRIPTING, Stage.DONE):
        store.get(5).stage = stage
        reply = handle_restart(
            store,
            _input(chat_id=5, kind="command", text="/restart", command="restart"),
            media_root=tmp_path,
        )
        assert reply == GREETING_TEXT
        assert store.get(5).stage is Stage.AWAITING_PHOTO


def test_start_purges_that_chat_media(tmp_path: Path) -> None:
    store = InMemorySessionStore()
    chat_dir = media.chat_media_dir(1, root=tmp_path)
    chat_dir.mkdir(parents=True, exist_ok=True)
    (chat_dir / "photo.jpg").write_text("old")

    handle_start(
        store, _input(kind="command", text="/start", command="start"), media_root=tmp_path
    )

    assert not chat_dir.exists()


def test_restart_purges_only_its_own_chat(tmp_path: Path) -> None:
    store = InMemorySessionStore()
    dir_5 = media.chat_media_dir(5, root=tmp_path)
    dir_6 = media.chat_media_dir(6, root=tmp_path)
    dir_5.mkdir(parents=True, exist_ok=True)
    dir_6.mkdir(parents=True, exist_ok=True)
    (dir_5 / "photo.jpg").write_text("5")
    (dir_6 / "photo.jpg").write_text("6")

    handle_restart(
        store,
        _input(chat_id=5, kind="command", text="/restart", command="restart"),
        media_root=tmp_path,
    )

    assert not dir_5.exists()
    assert (dir_6 / "photo.jpg").exists()


def test_text_at_awaiting_photo_prompts_for_a_photo() -> None:
    store = InMemorySessionStore()
    store.set_stage(1, Stage.AWAITING_PHOTO)
    assert handle_text(store, _input(text="hello?")) == AWAITING_PHOTO_PROMPT


def test_text_echoes_when_not_awaiting_photo() -> None:
    store = InMemorySessionStore()  # GREETING
    assert handle_text(store, _input(text="quack quack")) == "quack quack"


def test_apply_human_verdict_advances_to_interview() -> None:
    store = InMemorySessionStore()
    store.set_stage(1, Stage.AWAITING_PHOTO)
    store.set_stage(1, Stage.BOUNCER_CHECK)

    reply = apply_bouncer_verdict(store, 1, BouncerVerdict(is_human=True))

    # A human verdict advances the stage and hands off to the caller (no reply
    # text of its own); the caller starts the interview.
    assert reply is None
    assert store.get(1).stage is Stage.INTERVIEW


def test_apply_non_human_verdict_refuses_and_awaits_again() -> None:
    store = InMemorySessionStore()
    store.set_stage(1, Stage.AWAITING_PHOTO)
    store.set_stage(1, Stage.BOUNCER_CHECK)

    reply = apply_bouncer_verdict(store, 1, BouncerVerdict(is_human=False))

    assert reply in REFUSAL_LINES
    assert store.get(1).stage is Stage.AWAITING_PHOTO


def test_photo_wrong_stage_redirects_without_changing_stage() -> None:
    store = InMemorySessionStore()

    reply = handle_photo_wrong_stage(store, _input(kind="photo"))

    assert reply == PHOTO_REDIRECT_TEXT
    assert store.get(1).stage is Stage.GREETING


def test_interview_texts_are_non_empty_strings() -> None:
    texts = (
        INTERVIEW_INTRO,
        INTERVIEW_PHOTO_REDIRECT_TEXT,
        INTERVIEW_REPROMPT_TEXT,
        INTERVIEW_COMPLETE_TEXT,
        INTERVIEW_FAILURE_TEXT,
        INTERVIEW_UNKNOWN_COMMAND_TEXT,
        INTERVIEW_OTHER_REDIRECT_TEXT,
    )
    assert all(isinstance(text, str) and text.strip() for text in texts)


def test_interview_photo_redirect_asks_for_a_written_answer() -> None:
    lowered = INTERVIEW_PHOTO_REDIRECT_TEXT.lower()
    assert "answer" in lowered or "message" in lowered or "words" in lowered


def test_interview_reask_asks_for_a_written_answer() -> None:
    lowered = INTERVIEW_REPROMPT_TEXT.lower()
    assert "answer" in lowered or "question" in lowered


def test_interview_other_kind_redirect_asks_for_a_written_answer() -> None:
    lowered = INTERVIEW_OTHER_REDIRECT_TEXT.lower()
    assert "answer" in lowered or "message" in lowered or "words" in lowered


def test_interview_unknown_command_nudges_to_answer_or_restart() -> None:
    lowered = INTERVIEW_UNKNOWN_COMMAND_TEXT.lower()
    assert "answer" in lowered
    assert "/restart" in lowered


def test_interview_failure_text_offers_a_retry() -> None:
    lowered = INTERVIEW_FAILURE_TEXT.lower()
    assert "again" in lowered or "retry" in lowered


def test_handler_never_raises_even_if_store_fails() -> None:
    class BoomStore:
        def reset(self, chat_id: int) -> None:
            raise RuntimeError("boom")

    reply = handle_start(BoomStore(), _input(kind="command", text="/start", command="start"))

    assert reply == FRIENDLY_ERROR
