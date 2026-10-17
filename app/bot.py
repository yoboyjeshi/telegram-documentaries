"""Telegram wiring: guarded sends, update dispatch, and the long-polling app."""

from __future__ import annotations

import logging
from pathlib import Path

from telegram import Bot, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    MessageHandler,
    filters,
)

from app.bouncer import Bouncer, LazyBouncer
from app.config import Settings
from app.handlers import handle_restart, handle_start, handle_text
from app.logging_config import FRIENDLY_ERROR
from app.media import MEDIA_ROOT
from app.photo_flow import DownloadFn, handle_photo_update
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


def make_downloader(bot: Bot) -> DownloadFn:
    """Build the Telegram photo downloader, writing to a chat-namespaced path."""

    async def download(file_id: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        telegram_file = await bot.get_file(file_id)
        await telegram_file.download_to_drive(custom_path=str(dest))
        return dest

    return download


async def dispatch(
    store: InMemorySessionStore,
    update: object,
    bot: Bot,
    *,
    bouncer: Bouncer | None = None,
    media_root: Path | None = None,
) -> str | None:
    """Route an update to the right handler and send the reply."""
    incoming = extract_chat_input(update)
    if incoming is None:
        logger.warning("Ignoring unsupported update")
        return None

    if incoming.kind == "command" and incoming.command == "start":
        reply = handle_start(store, incoming, media_root=media_root)
    elif incoming.kind == "command" and incoming.command == "restart":
        reply = handle_restart(store, incoming, media_root=media_root)
    elif incoming.kind == "text":
        reply = handle_text(store, incoming)
    elif incoming.kind == "photo":
        if bouncer is None:
            logger.warning("Photo received but no Bouncer is configured")
            return None
        reply = await handle_photo_update(
            store,
            incoming,
            bouncer=bouncer,
            download=make_downloader(bot),
            media_root=media_root,
        )
    else:
        logger.warning(
            "No handler for update kind=%s command=%s", incoming.kind, incoming.command
        )
        return None

    await safe_send_message(bot, incoming.chat_id, reply)
    return reply


async def dispatch_safely(
    store: InMemorySessionStore,
    update: object,
    bot: Bot,
    *,
    bouncer: Bouncer | None = None,
    media_root: Path | None = None,
) -> str | None:
    """Run :func:`dispatch`, delivering a friendly message if it raises.

    The pure handlers guard themselves, so this is the backstop for any failure
    outside them: the user must never be left with silence.
    """
    try:
        return await dispatch(store, update, bot, bouncer=bouncer, media_root=media_root)
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
    # PTB passes the exception on ``context.error``; log it explicitly with its
    # traceback (``exc_info=<exception>``) rather than relying on ambient
    # exception state, which is not active inside this error handler.
    logger.error(
        "Unhandled error while processing update",
        exc_info=context.error,
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
    bouncer = LazyBouncer(settings.require_gemini_key)

    async def _handle(update: Update, context: CallbackContext) -> None:
        await dispatch_safely(
            session_store,
            update,
            context.bot,
            bouncer=bouncer,
            media_root=MEDIA_ROOT,
        )

    application.add_handler(CommandHandler("start", _handle))
    application.add_handler(CommandHandler("restart", _handle))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle))
    application.add_handler(MessageHandler(filters.PHOTO, _handle))
    application.add_error_handler(_on_error)
    return application
