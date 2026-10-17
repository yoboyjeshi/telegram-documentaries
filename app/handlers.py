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

INTERVIEW_INTRO = (
    "Splendid -- you're exactly the sort of specimen we're looking for! "
    "Let's begin the interview."
)

INTERVIEW_PHOTO_REDIRECT_TEXT = (
    "I appreciate the enthusiasm, but let's keep to words for now -- "
    "please answer the question in a message."
)

INTERVIEW_REPROMPT_TEXT = (
    "I didn't quite catch that -- please type your answer to the question."
)

INTERVIEW_COMPLETE_TEXT = (
    "Marvellous -- that's the full interview and I have everything I need. "
    "Our crew is preparing your documentary next."
)

INTERVIEW_FAILURE_TEXT = (
    "My interviewer is having a moment -- please send your answer again."
)

INTERVIEW_UNKNOWN_COMMAND_TEXT = (
    "That isn't a command I recognise -- just answer the question, "
    "or use /restart to start over."
)

INTERVIEW_OTHER_REDIRECT_TEXT = (
    "How intriguing, but this interview is word-based -- "
    "please answer the question in a message."
)

PHOTO_DOWNLOAD_FAILURE_TEXT = "I couldn't fetch that photo just now. Please send it again."

BOUNCER_FAILURE_TEXT = (
    "My image analyst is unavailable right now. Please send your photo again."
)

CONVERTER_CAPTION = (
    "And so our subject steps into the wild -- one face, two natures, entirely at home."
)

CONVERTER_FAILURE_TEXT = (
    "My portrait artist is having a moment -- send any message and I'll try again."
)

CONVERTER_MISSING_PORTRAIT_TEXT = (
    "I seem to have lost your portrait along the way -- send /restart and we'll begin again."
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
) -> str | None:
    """Advance to ``INTERVIEW`` on a human; otherwise refuse and await a photo.

    A human verdict returns ``None``: the caller starts the interview (the entry
    reply is the intro plus question 1, which needs the interviewer).
    """
    if verdict.is_human:
        store.set_stage(chat_id, Stage.INTERVIEW)
        return None
    store.set_stage(chat_id, Stage.AWAITING_PHOTO)
    return refusal_text(chat_id)


SCRIPTER_FAILURE_TEXT = (
    "My documentary writer is having a moment. Send a text message to try again, "
    "or /restart to start over."
)
SCRIPTER_MISSING_SUMMARY_TEXT = (
    "I seem to have lost your interview notes. Send /restart and we'll begin again."
)
