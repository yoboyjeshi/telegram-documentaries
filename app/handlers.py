"""Pure, framework-free handlers.

Each handler takes the session store and a normalised :class:`ChatInput` and
returns the reply text. Photo intake that needs I/O lives in
:mod:`app.photo_flow`; the pure decisions live here.
"""

from __future__ import annotations

from pathlib import Path

from app.bouncer import BouncerVerdict, refusal_text
from app.logging_config import guard_errors, logged
from app.media import MEDIA_ROOT, purge_chat_media
from app.session_store import InMemorySessionStore
from app.states import Stage
from app.telegram_adapter import ChatInput

GREETING_TEXT = (
    "Welcome to The Telegram Documentaries! "
    "Send me a portrait photo and I'll turn you into a wildlife documentary."
)

AWAITING_PHOTO_PROMPT = "I'm ready when you are -- send me a portrait photo and let's begin!"

PHOTO_REDIRECT_TEXT = (
    "Let's take this one step at a time -- send /start when you're ready to begin."
)

INTERVIEW_READY_TEXT = (
    "Splendid -- you're exactly the sort of specimen we're looking for! "
    "The interview is coming up next."
)

PHOTO_DOWNLOAD_FAILURE_TEXT = "I couldn't fetch that photo just now. Please send it again."

BOUNCER_FAILURE_TEXT = (
    "My image analyst is unavailable right now. Please send your photo again."
)


@logged
@guard_errors
def handle_start(
    store: InMemorySessionStore, incoming: ChatInput, media_root: Path | None = None
) -> str:
    """Greet, reset this chat, purge its temp media, and await a photo."""
    store.reset(incoming.chat_id)
    purge_chat_media(incoming.chat_id, root=media_root or MEDIA_ROOT)
    store.set_stage(incoming.chat_id, Stage.AWAITING_PHOTO)
    return GREETING_TEXT


@logged
@guard_errors
def handle_restart(
    store: InMemorySessionStore, incoming: ChatInput, media_root: Path | None = None
) -> str:
    """Same as ``/start``: reset, purge temp media, and await a photo."""
    store.reset(incoming.chat_id)
    purge_chat_media(incoming.chat_id, root=media_root or MEDIA_ROOT)
    store.set_stage(incoming.chat_id, Stage.AWAITING_PHOTO)
    return GREETING_TEXT


@logged
@guard_errors
def handle_text(store: InMemorySessionStore, incoming: ChatInput) -> str:
    """Prompt for a photo while awaiting one; otherwise echo the text verbatim."""
    if store.get(incoming.chat_id).stage is Stage.AWAITING_PHOTO:
        return AWAITING_PHOTO_PROMPT
    return incoming.text or ""


@logged
@guard_errors
def handle_photo_wrong_stage(store: InMemorySessionStore, incoming: ChatInput) -> str:
    """Gently redirect a photo that arrived outside ``AWAITING_PHOTO``."""
    return PHOTO_REDIRECT_TEXT


@logged
@guard_errors
def apply_bouncer_verdict(
    store: InMemorySessionStore, chat_id: int, verdict: BouncerVerdict
) -> str:
    """Advance to ``INTERVIEW`` on a human; otherwise refuse and await a photo."""
    if verdict.is_human:
        store.set_stage(chat_id, Stage.INTERVIEW)
        return INTERVIEW_READY_TEXT
    store.set_stage(chat_id, Stage.AWAITING_PHOTO)
    return refusal_text(chat_id)
