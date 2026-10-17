"""Async photo intake: gate on stage, download, run the Bouncer, apply verdict.

Every external step (the Telegram download and the Gemini call) is wrapped: a
failure notifies the user, recovers the chat to ``AWAITING_PHOTO``, and never
propagates into the polling loop.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.bouncer import Bouncer
from app.handlers import (
    BOUNCER_FAILURE_TEXT,
    PHOTO_DOWNLOAD_FAILURE_TEXT,
    apply_bouncer_verdict,
    handle_photo_wrong_stage,
)
from app.media import MEDIA_ROOT, photo_dest
from app.session_store import InMemorySessionStore
from app.states import Stage
from app.telegram_adapter import ChatInput

logger = logging.getLogger("telegram_documentaries")

DownloadFn = Callable[[str, Path], Awaitable[Path]]


async def handle_photo_update(
    store: InMemorySessionStore,
    incoming: ChatInput,
    *,
    bouncer: Bouncer,
    download: DownloadFn,
    media_root: Path | None = None,
) -> str:
    """Handle a photo update: verify it, then advance or refuse. Never raises."""
    chat_id = incoming.chat_id
    if store.get(chat_id).stage is not Stage.AWAITING_PHOTO:
        return handle_photo_wrong_stage(store, incoming)

    store.set_stage(chat_id, Stage.BOUNCER_CHECK)

    if not incoming.file_id:
        logger.warning("Photo update without a file_id chat_id=%s", chat_id)
        store.set_stage(chat_id, Stage.AWAITING_PHOTO)
        return PHOTO_DOWNLOAD_FAILURE_TEXT

    dest = photo_dest(chat_id, root=media_root or MEDIA_ROOT)
    try:
        await download(incoming.file_id, dest)
    except Exception:
        logger.exception("Failed to download photo chat_id=%s", chat_id)
        store.set_stage(chat_id, Stage.AWAITING_PHOTO)
        return PHOTO_DOWNLOAD_FAILURE_TEXT

    try:
        verdict = await bouncer.inspect(dest)
    except Exception as exc:
        # Any bouncer error must be handled here so the polling loop never crashes.
        # Log type + message only: a full traceback can embed the request
        # payload (image bytes) from the ADK/Gemini client.
        logger.error("Bouncer failed chat_id=%s: %s: %s", chat_id, type(exc).__name__, exc)
        store.set_stage(chat_id, Stage.AWAITING_PHOTO)
        return BOUNCER_FAILURE_TEXT

    logger.info("Bouncer verdict chat_id=%s is_human=%s", chat_id, verdict.is_human)
    return apply_bouncer_verdict(store, chat_id, verdict)
