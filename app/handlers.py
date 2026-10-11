"""Pure, framework-free handlers.

Each handler takes the session store and a normalised :class:`ChatInput` and
returns the reply text. Keeping them free of Telegram objects makes them
trivial to unit test (no network, no mocks of the framework needed).
"""

from __future__ import annotations

from app.logging_config import guard_errors, logged
from app.session_store import InMemorySessionStore
from app.telegram_adapter import ChatInput

GREETING_TEXT = (
    "Welcome to The Telegram Documentaries! "
    "Send me a portrait photo and I'll turn you into a wildlife documentary."
)


@logged
@guard_errors
def handle_start(store: InMemorySessionStore, incoming: ChatInput) -> str:
    """Greet the user and reset this chat's session to ``GREETING``."""
    store.reset(incoming.chat_id)
    return GREETING_TEXT


@logged
@guard_errors
def handle_restart(store: InMemorySessionStore, incoming: ChatInput) -> str:
    """Same effect as ``/start``: greet and reset this chat to ``GREETING``."""
    store.reset(incoming.chat_id)
    return GREETING_TEXT


@logged
@guard_errors
def handle_text(store: InMemorySessionStore, incoming: ChatInput) -> str:
    """Echo the received text back, so the pipe can be confirmed on a phone."""
    return incoming.text or ""
