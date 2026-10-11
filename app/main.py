"""Entry point: ``python -m app.main``.

Loads settings from ``.env`` and starts the bot with long polling. Importing
this module has no side effects (no token is read at import time).
"""

from __future__ import annotations

import logging

from app.bot import build_application
from app.config import Settings
from app.logging_config import configure_logging

logger = logging.getLogger("telegram_documentaries")


def main() -> None:
    """Configure logging, load settings, and run the long-polling bot."""
    configure_logging()
    settings = Settings.from_env()
    application = build_application(settings)
    logger.info("Starting Telegram long polling")
    application.run_polling()


if __name__ == "__main__":
    main()
