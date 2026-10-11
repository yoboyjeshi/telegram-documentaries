"""Tests that logging never leaks secrets or message bodies."""

from __future__ import annotations

import logging

from app.config import Settings
from app.logging_config import FRIENDLY_ERROR, configure_logging, guard_errors, logged
from app.session_store import InMemorySessionStore
from app.states import Stage
from app.telegram_adapter import ChatInput


def test_secret_value_never_logged(caplog) -> None:
    secret = "SUPER_SECRET_TOKEN_VALUE"
    settings = Settings(telegram_bot_token=secret)
    log = logging.getLogger("telegram_documentaries")
    with caplog.at_level(logging.INFO):
        log.info("settings=%s", settings)
        log.info("token=%s", settings.telegram_bot_token)
    assert secret not in caplog.text


def test_logged_reports_chat_id_but_not_body(caplog) -> None:
    @logged
    def my_handler(store: object, incoming: ChatInput) -> str | None:
        return incoming.text

    incoming = ChatInput(chat_id=42, kind="text", text="TOPSECRETBODY")
    with caplog.at_level(logging.INFO):
        result = my_handler(None, incoming)

    assert result == "TOPSECRETBODY"
    assert "handler=my_handler" in caplog.text
    assert "42" in caplog.text
    assert "TOPSECRETBODY" not in caplog.text


def test_logged_includes_current_stage_from_session_store(caplog) -> None:
    store = InMemorySessionStore()
    store.get(7).stage = Stage.INTERVIEW

    @logged
    def my_handler(store: InMemorySessionStore, incoming: ChatInput) -> str:
        return "ok"

    with caplog.at_level(logging.INFO):
        my_handler(store, ChatInput(chat_id=7, kind="text", text="hi"))

    assert "handler=my_handler" in caplog.text
    assert "chat_id=7" in caplog.text
    assert "stage=INTERVIEW" in caplog.text


def test_logged_logs_greeting_stage_for_new_chat(caplog) -> None:
    store = InMemorySessionStore()

    @logged
    def my_handler(store: InMemorySessionStore, incoming: ChatInput) -> str:
        return "ok"

    with caplog.at_level(logging.INFO):
        my_handler(store, ChatInput(chat_id=9, kind="text", text="hi"))

    assert "stage=GREETING" in caplog.text


def test_logged_reports_stage_before_and_after(caplog) -> None:
    store = InMemorySessionStore()
    store.get(3).stage = Stage.INTERVIEW

    @logged
    def reset_handler(store: InMemorySessionStore, incoming: ChatInput) -> str:
        store.reset(incoming.chat_id)
        return "done"

    with caplog.at_level(logging.INFO):
        reset_handler(
            store,
            ChatInput(chat_id=3, kind="command", text="/start", command="start"),
        )

    assert "stage=INTERVIEW" in caplog.text
    assert "stage=GREETING" in caplog.text


def test_logged_never_raises_when_stage_cannot_be_resolved(caplog) -> None:
    """A missing/exploding store must not turn logging into a failure."""

    class ExplodingStore:
        def get(self, chat_id: int) -> object:
            raise RuntimeError("boom")

    @logged
    def my_handler(store: object, incoming: ChatInput) -> str | None:
        return incoming.text

    with caplog.at_level(logging.INFO):
        result = my_handler(ExplodingStore(), ChatInput(chat_id=1, kind="text", text="hi"))

    assert result == "hi"
    assert "stage=None" in caplog.text


def test_guard_errors_logs_and_returns_fallback(caplog) -> None:
    @guard_errors
    def boom(chat_id: int) -> str:
        raise RuntimeError("kaboom")

    with caplog.at_level(logging.ERROR):
        result = boom(5)

    assert result == FRIENDLY_ERROR
    assert any(record.levelno == logging.ERROR for record in caplog.records)
    assert any(record.exc_info for record in caplog.records)


def test_http_request_urls_are_not_logged(caplog) -> None:
    """The HTTP client logs full request URLs at INFO; Telegram embeds the token."""
    configure_logging(level=logging.INFO)
    token = "123456789:SUPER_SECRET_BOT_TOKEN"

    with caplog.at_level(logging.INFO):
        logging.getLogger("httpx").info(
            "HTTP Request: POST https://api.telegram.org/bot%s/getMe", token
        )

    assert token not in caplog.text
    assert "api.telegram.org" not in caplog.text


def test_http_client_cannot_emit_info_records() -> None:
    """httpx must be quiet at INFO so request URLs never reach the logs."""
    configure_logging(level=logging.INFO)
    root = logging.getLogger()
    previous = root.level
    try:
        root.setLevel(logging.INFO)
        assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
    finally:
        root.setLevel(previous)
