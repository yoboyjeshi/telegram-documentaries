"""Telegram wiring: guarded sends, update dispatch, and the long-polling app."""

from __future__ import annotations

import logging
from typing import Any

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)

from app.config import Settings
from app.handlers import handle_restart, handle_start, handle_text
from app.logging_config import guard_errors
from app.session_store import InMemorySessionStore
from app.telegram_adapter import extract_chat_input

logger = logging.getLogger("telegram_documentaries")


async def safe_send_message(bot: Any, chat_id: int, text: str) -> bool:
    """Send a Telegram message; a failure is logged and swallowed, never raised."""
    try:
        await bot.send_message(chat_id=chat_id, text=text)
        return True
    except TelegramError:
        logger.exception("Failed to send message to chat_id=%s", chat_id)
        return False


async def dispatch(store: InMemorySessionStore, update: object, bot: Any) -> str | None:
    """Route an update to the right pure handler and send the reply."""
    incoming = extract_chat_input(update)
    if incoming is None:
        logger.warning("Ignoring unsupported update")
        return None

    if incoming.kind == "command" and incoming.command == "start":
        reply = handle_start(store, incoming)
    elif incoming.kind == "command" and incoming.command == "restart":
        reply = handle_restart(store, incoming)
    elif incoming.kind == "text":
        reply = handle_text(store, incoming)
    else:
        logger.info("No handler for update kind=%s command=%s", incoming.kind, incoming.command)
        return None

    await safe_send_message(bot, incoming.chat_id, reply)
    return reply


async def _on_error(update: object, context: Any) -> None:
    """Global backstop: log unhandled errors instead of crashing the poller."""
    error = getattr(context, "error", None)
    if error is not None:
        logger.error("Unhandled error while processing update", exc_info=error)
    else:
        logger.error("Unhandled error while processing update")


def build_application(
    settings: Settings, store: InMemorySessionStore | None = None
) -> Application:
    """Build the long-polling ``Application`` with handlers and an error backstop."""
    session_store = store or InMemorySessionStore()
    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token.get_secret_value())
        .build()
    )

    @guard_errors
    async def _on_start(update: Update, context: Any) -> None:
        await dispatch(session_store, update, context.bot)

    @guard_errors
    async def _on_restart(update: Update, context: Any) -> None:
        await dispatch(session_store, update, context.bot)

    @guard_errors
    async def _on_text(update: Update, context: Any) -> None:
        await dispatch(session_store, update, context.bot)

    application.add_handler(CommandHandler("start", _on_start))
    application.add_handler(CommandHandler("restart", _on_restart))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))
    application.add_error_handler(_on_error)
    return application
