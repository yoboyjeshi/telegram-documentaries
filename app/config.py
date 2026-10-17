"""Configuration loaded from ``.env`` and validated with Pydantic.

Secrets are held as :class:`pydantic.SecretStr` so they are masked in ``repr``
and can never be logged by accident.
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from pydantic import BaseModel, SecretStr

logger = logging.getLogger("telegram_documentaries")


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def ensure_google_api_key(api_key: str) -> None:
    """Expose the key to the ADK/Gemini clients without ever logging its value.

    A pre-existing ``GOOGLE_API_KEY`` wins (``setdefault`` semantics). Warn only
    when a *different* value is already set, so a misconfiguration is observable
    rather than silently masked; an identical value is a silent no-op. No key
    value (old or new) is ever logged.
    """
    existing = os.environ.get("GOOGLE_API_KEY")
    if existing:
        if existing != api_key:
            logger.warning(
                "GOOGLE_API_KEY is already set in the environment; "
                "the existing value takes precedence over the supplied agent key."
            )
        return
    os.environ["GOOGLE_API_KEY"] = api_key


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

    def require_gemini_key(self) -> str:
        """Return the Gemini API key as a string or raise if missing."""
        if self.gemini_api_key is None:
            raise ConfigurationError(
                "GEMINI_API_KEY is not set. Add it to your .env file."
            )
        return self.gemini_api_key.get_secret_value()
