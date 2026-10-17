"""Public-wiring regressions runnable unchanged against the merged baseline."""
from __future__ import annotations

import asyncio
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from telegram import Update, User

from app.bot import build_application, dispatch
from app.bouncer import BouncerVerdict
from app.config import Settings
from app.conversion_tasks import ConversionTasks
from app.interviewer import InterviewQuestion
from app.session_store import InMemorySessionStore
from app.states import Stage


def test_public_photo_callback_returns_before_model_finishes(tmp_path):
    """The sequential callback cannot wait for Bouncer or resets queue behind it."""
    async def scenario():
        store = InMemorySessionStore()
        store.set_stage(1, Stage.AWAITING_PHOTO)
        registry = ConversionTasks(store)
        transport, bouncer, interviewer = AsyncMock(), AsyncMock(), AsyncMock()
        image = BytesIO()
        Image.new("RGB", (2, 2), "white").save(image, format="JPEG")
        data = image.getvalue()
        telegram_file = transport.get_file.return_value
        telegram_file.file_size = len(data)

        async def memory(sink):
            sink.write(data)

        async def drive(custom_path):
            from pathlib import Path
            Path(custom_path).write_bytes(data)

        # Both SDK seams are mocked only so this regression runs against old/new wiring.
        telegram_file.download_to_memory.side_effect = memory
        telegram_file.download_to_drive.side_effect = drive
        entered, release = asyncio.Event(), asyncio.Event()

        async def inspect(path):
            entered.set()
            await release.wait()
            return BouncerVerdict(is_human=True)

        bouncer.inspect.side_effect = inspect
        interviewer.next_question.return_value = InterviewQuestion(text="What drives you?")
        message = SimpleNamespace(text=None, chat=SimpleNamespace(id=1),
                                  photo=[SimpleNamespace(file_id="synthetic", file_size=len(data))])
        try:
            async with asyncio.timeout(0.25):
                await dispatch(store, SimpleNamespace(effective_message=message), transport,
                               bouncer=bouncer, interviewer=interviewer,
                               conversion_tasks=registry, media_root=tmp_path)
            async with asyncio.timeout(1):
                await entered.wait()
            assert registry.active(1)
            assert store.get(1).stage is Stage.BOUNCER_CHECK
        finally:
            release.set()
            await registry.wait()
            await registry.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["document", "voice"])
def test_public_application_has_a_handler_for_unsupported_uploads(kind):
    app = build_application(Settings(telegram_bot_token="123:dummy"))
    app.bot._bot_user = User(id=1, first_name="Offline", is_bot=True, username="bot")
    payload = {"file_id": "synthetic", "file_unique_id": "unique"}
    if kind == "voice":
        payload["duration"] = 1
    update = Update.de_json({
        "update_id": 1,
        "message": {"message_id": 1, "date": 1760000000,
                    "chat": {"id": 1, "type": "private"}, kind: payload},
    }, app.bot)
    matches = [handler for handler in app.handlers[0]
               if handler.check_update(update) not in (None, False)]
    assert matches, "Unsupported message must reach registered guidance, not disappear"
