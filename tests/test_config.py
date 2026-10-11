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
