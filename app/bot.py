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

from app import orchestrator
from app.bouncer import Bouncer, LazyBouncer
from app.config import Settings
from app.conversion_tasks import ConversionTasks
from app.converter import Converter, LazyConverter
from app.handlers import handle_restart, handle_start, handle_text
from app.interviewer import Interviewer, LazyInterviewer
from app.logging_config import FRIENDLY_ERROR
from app.media import MEDIA_ROOT
from app.orchestrator import SendPhotoFn
from app.photo_flow import DownloadFn, handle_photo_update
from app.session_store import InMemorySessionStore
from app.states import Stage
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
    except Exception as exc:  # noqa: BLE001 - guarded sends must not crash the poller
        logger.error("Failed to send message chat_id=%s type=%s", chat_id, type(exc).__name__)
        return False


def make_downloader(bot: Bot) -> DownloadFn:
    """Build the Telegram photo downloader, writing to a chat-namespaced path."""

    async def download(file_id: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        telegram_file = await bot.get_file(file_id)
        await telegram_file.download_to_drive(custom_path=str(dest))
        return dest

    return download


async def safe_send_photo(bot: Bot, chat_id: int, path: Path, caption: str) -> bool:
    """Send a Telegram photo; a failure is logged and swallowed, never raised.

    Mirrors :func:`safe_send_message`: catches ``Exception`` (never
    ``BaseException``) so an unexpected send error cannot crash the poller
    while ``asyncio.CancelledError`` still propagates. Only the ``chat_id`` is
    logged -- never the caption, the file bytes, or the path contents.
    """
    try:
        await bot.send_photo(chat_id=chat_id, photo=path, caption=caption)
        return True
    except Exception as exc:  # noqa: BLE001 - guarded sends must not crash the poller
        logger.error("Failed to send photo chat_id=%s type=%s", chat_id, type(exc).__name__)
        return False


def make_send_photo(bot: Bot) -> SendPhotoFn:
    """Build the typed photo-send capability injected into the Converter step."""

    async def send_photo(chat_id: int, path: Path, caption: str) -> bool:
        return await safe_send_photo(bot, chat_id, path, caption)

    return send_photo


async def dispatch(
    store: InMemorySessionStore,
    update: object,
    bot: Bot,
    *,
    bouncer: Bouncer | None = None,
    interviewer: Interviewer | None = None,
    converter: Converter | None = None,
    send_photo: SendPhotoFn | None = None,
    media_root: Path | None = None,
    conversion_tasks: ConversionTasks | None = None,
) -> str | None:
    """Route an update to the right handler and send the reply.

    ``/start`` and ``/restart`` always win (they reset from any stage). An
    update arriving while the chat sits at ``CONVERTING`` re-runs the Converter
    step (the retry / self-heal path); when the interview completes in this
    turn, the Converter step runs right after the acknowledgement. The
    ``ran_converter`` guard ensures the step runs at most once per turn.
    """
    incoming = extract_chat_input(update)
    if incoming is None:
        logger.warning("Ignoring unsupported update")
        return None

    chat_id = incoming.chat_id
    stage = store.get(chat_id).stage
    ran_converter = False

    async def convert() -> str | None:
        assert converter is not None and send_photo is not None
        if conversion_tasks is None:
            # Explicit awaitable seam for callers/tests; the live app always supplies a registry.
            return await orchestrator.handle_converting_update(
                store, chat_id, converter=converter, send_photo=send_photo, media_root=media_root
            )

        async def work(version: int) -> None:
            result = await orchestrator.handle_converting_update(
                store, chat_id, converter=converter, send_photo=send_photo,
                media_root=media_root, session_version=version,
            )
            if result is not None and store.version(chat_id) == version:
                await safe_send_message(bot, chat_id, result)

        conversion_tasks.start(chat_id, work)
        return None

    if (incoming.kind == "command" and incoming.command in ("start", "restart")
            and conversion_tasks is not None):
        conversion_tasks.cancel(chat_id)

    if incoming.kind == "command" and incoming.command == "start":
        reply: str | None = handle_start(store, incoming, media_root=media_root)
    elif incoming.kind == "command" and incoming.command == "restart":
        reply = handle_restart(store, incoming, media_root=media_root)
    elif stage is Stage.CONVERTING:
        # Retry / self-heal: any update at CONVERTING re-runs the step.
        if converter is None or send_photo is None:
            logger.warning("Update at CONVERTING but no Converter is configured")
            return None
        reply = await convert()
        ran_converter = True
    elif stage is Stage.INTERVIEW and incoming.kind in (
        "text",
        "photo",
        "command",
        "other",
    ):
        # Photos at INTERVIEW must never reach the download path.
        if interviewer is None:
            logger.warning("Update at INTERVIEW but no Interviewer is configured")
            return None
        reply = await orchestrator.handle_interview_update(
            store, incoming, interviewer=interviewer
        )
    elif incoming.kind == "text":
        reply = handle_text(store, incoming)
    elif incoming.kind == "photo":
        if bouncer is None or interviewer is None:
            logger.warning("Photo received but a required dependency is not configured")
            return None
        reply = await handle_photo_update(
            store,
            incoming,
            bouncer=bouncer,
            download=make_downloader(bot),
            interviewer=interviewer,
            media_root=media_root,
        )
    else:
        logger.warning(
            "No handler for update kind=%s command=%s", incoming.kind, incoming.command
        )
        return None

    if reply is not None:
        await safe_send_message(bot, chat_id, reply)

    # Post-interview handoff: the acknowledgement went out and the chat now
    # sits at CONVERTING, so run the Converter step in the same turn -- unless
    # it already ran above, so it never runs twice in one turn.
    if not ran_converter and store.get(chat_id).stage is Stage.CONVERTING:
        if converter is None or send_photo is None:
            logger.warning("Chat at CONVERTING but no Converter is configured")
        else:
            follow_up = await convert()
            if follow_up is not None:
                await safe_send_message(bot, chat_id, follow_up)

    return reply


async def dispatch_safely(
    store: InMemorySessionStore,
    update: object,
    bot: Bot,
    *,
    bouncer: Bouncer | None = None,
    interviewer: Interviewer | None = None,
    converter: Converter | None = None,
    send_photo: SendPhotoFn | None = None,
    media_root: Path | None = None,
    conversion_tasks: ConversionTasks | None = None,
) -> str | None:
    """Run :func:`dispatch`, delivering a friendly message if it raises.

    The pure handlers guard themselves, so this is the backstop for any failure
    outside them: the user must never be left with silence.
    """
    try:
        return await dispatch(
            store,
            update,
            bot,
            bouncer=bouncer,
            interviewer=interviewer,
            converter=converter,
            send_photo=send_photo,
            media_root=media_root,
            conversion_tasks=conversion_tasks,
        )
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
    conversion_tasks = ConversionTasks(session_store)

    async def stop_conversions(application: Application) -> None:
        await conversion_tasks.close()

    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token.get_secret_value())
        .post_stop(stop_conversions)
        .build()
    )
    bouncer = LazyBouncer(settings.require_gemini_key)
    interviewer = LazyInterviewer(settings.require_gemini_key)
    converter = LazyConverter(settings.require_gemini_key)

    async def _handle(update: Update, context: CallbackContext) -> None:
        await dispatch_safely(
            session_store,
            update,
            context.bot,
            bouncer=bouncer,
            interviewer=interviewer,
            converter=converter,
            send_photo=make_send_photo(context.bot),
            media_root=MEDIA_ROOT,
            conversion_tasks=conversion_tasks,
        )

    application.add_handler(CommandHandler("start", _handle))
    application.add_handler(CommandHandler("restart", _handle))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle))
    application.add_handler(MessageHandler(filters.PHOTO, _handle))
    # Catch-all for unknown commands so they reach dispatch (e.g. /foo mid-interview).
    # Registered last so it never shadows /start or /restart.
    application.add_handler(MessageHandler(filters.COMMAND, _handle))
    application.add_error_handler(_on_error)
    return application
