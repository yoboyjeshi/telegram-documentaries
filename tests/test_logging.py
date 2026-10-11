"""Tests that logging never leaks secrets or message bodies."""

from __future__ import annotations

import logging

from app.config import Settings
from app.logging_config import FRIENDLY_ERROR, guard_errors, logged
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
    def my_handler(store, incoming):  # type: ignore[no-untyped-def]
        return incoming.text

    incoming = ChatInput(chat_id=42, kind="text", text="TOPSECRETBODY")
    with caplog.at_level(logging.INFO):
        result = my_handler(None, incoming)

    assert result == "TOPSECRETBODY"
    assert "handler=my_handler" in caplog.text
    assert "42" in caplog.text
    assert "TOPSECRETBODY" not in caplog.text


def test_guard_errors_logs_and_returns_fallback(caplog) -> None:
    @guard_errors
    def boom(chat_id: int) -> str:
        raise RuntimeError("kaboom")

    with caplog.at_level(logging.ERROR):
        result = boom(5)

    assert result == FRIENDLY_ERROR
    assert any(record.levelno == logging.ERROR for record in caplog.records)
    assert any(record.exc_info for record in caplog.records)
