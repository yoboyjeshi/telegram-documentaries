"""Tests for configuration loading and secret hygiene."""

from __future__ import annotations

import logging
import os

import pytest

from app import config
from app.config import ConfigurationError, Settings, ensure_google_api_key


def test_missing_token_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ConfigurationError):
        Settings.from_env()


def test_token_loads_and_is_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok-123")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    settings = Settings.from_env()

    assert settings.telegram_bot_token.get_secret_value() == "tok-123"
    assert "tok-123" not in repr(settings)


def test_require_gemini_key_raises_when_missing() -> None:
    settings = Settings(telegram_bot_token="tok", gemini_api_key=None)
    with pytest.raises(ConfigurationError):
        settings.require_gemini_key()


def test_require_gemini_key_returns_value() -> None:
    settings = Settings(telegram_bot_token="tok", gemini_api_key="the-key")
    assert settings.require_gemini_key() == "the-key"


def test_ensure_google_api_key_sets_when_absent_without_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with caplog.at_level(logging.WARNING):
        ensure_google_api_key("SUPPLIED_KEY")
    assert os.environ["GOOGLE_API_KEY"] == "SUPPLIED_KEY"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_ensure_google_api_key_keeps_different_existing_value_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "PRESET_ENV_KEY")
    with caplog.at_level(logging.WARNING):
        ensure_google_api_key("SUPPLIED_KEY")
    # The pre-existing env value wins; the anomaly is logged without any key value.
    assert os.environ["GOOGLE_API_KEY"] == "PRESET_ENV_KEY"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings
    assert any("GOOGLE_API_KEY" in r.getMessage() for r in warnings)
    assert "SUPPLIED_KEY" not in caplog.text
    assert "PRESET_ENV_KEY" not in caplog.text


def test_ensure_google_api_key_identical_existing_value_is_silent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "SAME_KEY")
    with caplog.at_level(logging.WARNING):
        ensure_google_api_key("SAME_KEY")
    assert os.environ["GOOGLE_API_KEY"] == "SAME_KEY"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "SAME_KEY" not in caplog.text
