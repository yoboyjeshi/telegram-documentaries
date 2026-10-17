"""An unknown command is user-supplied text and must not be logged verbatim.

The dispatcher's no-handler branch is reachable at GREETING, AWAITING_PHOTO and
BOUNCER_CHECK, where a stray ``/anything`` is still arbitrary input (it can
carry a pasted secret). The branch must stay observable as metadata.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import dispatch
from app.session_store import InMemorySessionStore
from app.states import Stage

COMMAND_SENTINEL = "COMMAND_SENTINEL_hunter2"


def _command_update(text: str, chat_id: int = 1) -> SimpleNamespace:
    message = SimpleNamespace(text=text, chat=SimpleNamespace(id=chat_id))
    return SimpleNamespace(effective_message=message, message=message)


@pytest.mark.parametrize(
    "stage", [Stage.GREETING, Stage.AWAITING_PHOTO, Stage.BOUNCER_CHECK]
)
def test_unknown_command_is_never_logged_verbatim(stage, caplog) -> None:
    store = InMemorySessionStore()
    store.get(1).stage = stage
    bot = AsyncMock()

    with caplog.at_level("DEBUG"):
        reply = asyncio.run(dispatch(store, _command_update(f"/{COMMAND_SENTINEL}"), bot))

    assert reply is None
    bot.send_message.assert_not_awaited()
    assert store.get(1).stage is stage
    assert COMMAND_SENTINEL not in caplog.text
    # Still observable: kind, chat and stage say what was ignored and where.
    assert any(
        "kind=command" in record.getMessage() and f"stage={stage.value}" in record.getMessage()
        for record in caplog.records
    )
