"""Turn untrusted Telegram updates into a typed, normalised input contract.

All external input is treated as arbitrary: the adapter validates structure and
normalises ``chat_id`` to an ``int`` so it is safe to use as a session key.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel

logger = logging.getLogger("telegram_documentaries")


class ChatInput(BaseModel):
    """A validated, framework-free view of an inbound Telegram message."""

    chat_id: int
    kind: Literal["command", "text", "other"]
    text: str | None = None
    command: str | None = None


def extract_chat_input(update: object) -> ChatInput | None:
    """Build a :class:`ChatInput` from a Telegram update, or ``None`` if unusable."""
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    if message is None:
        return None

    chat = getattr(message, "chat", None)
    if chat is None:
        return None
    try:
        chat_id = int(chat.id)
    except (AttributeError, TypeError, ValueError):
        return None

    text = getattr(message, "text", None)
    if not isinstance(text, str):
        # e.g. a photo/voice message. Not handled yet, so warn rather than ignore.
        logger.warning("Ignoring unsupported message (no text) chat_id=%s", chat_id)
        return ChatInput(chat_id=chat_id, kind="other")

    if text.startswith("/"):
        command = text.split()[0][1:].split("@", 1)[0]
        return ChatInput(chat_id=chat_id, kind="command", text=text, command=command)

    return ChatInput(chat_id=chat_id, kind="text", text=text)
