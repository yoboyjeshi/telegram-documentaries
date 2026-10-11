"""Tests for configuration loading and secret hygiene."""

from __future__ import annotations

import pytest

from app import config
from app.config import ConfigurationError, Settings


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
