"""Configuration loaded from ``.env`` and validated with Pydantic.

Secrets are held as :class:`pydantic.SecretStr` so they are masked in ``repr``
and can never be logged by accident.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from pydantic import BaseModel, SecretStr


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


class Settings(BaseModel):
    """Validated application settings."""

    telegram_bot_token: SecretStr
    gemini_api_key: SecretStr | None = None

    @classmethod
    def from_env(cls) -> Settings:
        """Load ``.env`` and build validated settings.

        Raises :class:`ConfigurationError` (with a clear, secret-free message)
        if ``TELEGRAM_BOT_TOKEN`` is missing.
        """
        load_dotenv()
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise ConfigurationError(
                "TELEGRAM_BOT_TOKEN is not set. Add it to your .env file "
                "(see .env.example)."
            )
        gemini_key = os.environ.get("GEMINI_API_KEY")
        return cls(
            telegram_bot_token=SecretStr(token),
            gemini_api_key=SecretStr(gemini_key) if gemini_key else None,
        )
