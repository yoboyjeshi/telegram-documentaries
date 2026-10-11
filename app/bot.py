"""Telegram wiring: guarded sends, update dispatch, and the long-polling app."""

from __future__ import annotations

import logging

from telegram import Bot, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    MessageHandler,
    filters,
)

from app.config import Settings
from app.handlers import handle_restart, handle_start, handle_text
from app.logging_config import FRIENDLY_ERROR
from app.session_store import InMemorySessionStore
from app.telegram_adapter import extract_chat_input

logger = logging.getLogger("telegram_documentaries")


async def safe_send_message(bot: Bot, chat_id: int, text: str) -> bool:
    """Send a Telegram message; a failure is logged and swallowed, never raised.

    Catches ``Exception`` (never ``BaseException``) so an unexpected send error
    cannot crash the poller while ``asyncio.CancelledError`` still propagates.
    """
    try:
        await bot.send_message(chat_id=chat_id, text=text)
        return True
    except Exception:
        logger.exception("Failed to send message to chat_id=%s", chat_id)
        return False


async def dispatch(store: InMemorySessionStore, update: object, bot: Bot) -> str | None:
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
        logger.warning(
            "No handler for update kind=%s command=%s", incoming.kind, incoming.command
        )
        return None

    await safe_send_message(bot, incoming.chat_id, reply)
    return reply


async def dispatch_safely(
    store: InMemorySessionStore, update: object, bot: Bot
) -> str | None:
    """Run :func:`dispatch`, delivering a friendly message if it raises.

    The pure handlers guard themselves, so this is the backstop for any failure
    outside them: the user must never be left with silence.
    """
    try:
        return await dispatch(store, update, bot)
    except Exception:
        logger.exception("Unhandled error while handling update")
        try:
            incoming = extract_chat_input(update)
        except Exception:
            logger.debug("Could not resolve chat for friendly error", exc_info=True)
            incoming = None
        if incoming is not None:
            await safe_send_message(bot, incoming.chat_id, FRIENDLY_ERROR)
        return None


async def _on_error(update: object, context: CallbackContext) -> None:
    """Global backstop: log unhandled errors instead of crashing the poller."""
    # PTB invokes this handler from within its error path; logging at ERROR with
    # a traceback is the conventional form (``exc_info=True``), even though the
    # static check cannot see the surrounding exception context.
    logger.error(
        "Unhandled error while processing update",
        exc_info=True,  # noqa: LOG014
    )


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

    async def _on_start(update: Update, context: CallbackContext) -> None:
        await dispatch_safely(session_store, update, context.bot)

    async def _on_restart(update: Update, context: CallbackContext) -> None:
        await dispatch_safely(session_store, update, context.bot)

    async def _on_text(update: Update, context: CallbackContext) -> None:
        await dispatch_safely(session_store, update, context.bot)

    application.add_handler(CommandHandler("start", _on_start))
    application.add_handler(CommandHandler("restart", _on_restart))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))
    application.add_error_handler(_on_error)
    return application
