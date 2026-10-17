"""Two complete runs through actual registered handlers, entirely offline."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from image_fixtures import PNG
from telegram import Update, User

from app import bot as wiring
from app import orchestrator
from app.bouncer import BouncerVerdict
from app.config import Settings
from app.conversion_tasks import ConversionTasks
from app.interviewer import TOTAL_QUESTIONS, InterviewQuestion
from app.scripter import NarrationScript
from app.session_store import InMemorySessionStore
from app.states import Stage, TraitSummary


def test_two_registered_handler_pipelines_to_done(tmp_path, monkeypatch):
    async def scenario():
        from app.narrator import NarrationAudio
        store = InMemorySessionStore()
        registry = ConversionTasks(store)
        monkeypatch.setattr(wiring, "ConversionTasks", lambda _: registry)
        monkeypatch.setattr(wiring, "MEDIA_ROOT", tmp_path)
        transport = AsyncMock()
        bouncer, interviewer, converter, scripter, narrator = [AsyncMock() for _ in range(5)]
        bouncer.inspect.return_value = BouncerVerdict(is_human=True)
        interviewer.next_question.return_value = InterviewQuestion(text="What drives you?")
        interviewer.summarise.return_value = TraitSummary(summary="A patient night owl.")
        converter.generate_avatar.return_value = PNG
        narration = "A patient creature greets the evening with quiet confidence."
        scripter.write_script.return_value = NarrationScript(text=narration)
        narrator.render.return_value = NarrationAudio(
            data=b"\x01\x00" * 2400, mime_type="audio/L16;codec=pcm;rate=24000")
        for name, provider in (("LazyBouncer", bouncer), ("LazyInterviewer", interviewer),
                               ("LazyConverter", converter), ("LazyScripter", scripter),
                               ("LazyNarrator", narrator)):
            monkeypatch.setattr(wiring, name, lambda key, obj=provider: obj)
        monkeypatch.setattr(orchestrator, "encode_voice", AsyncMock(return_value=b"fake-ogg"))
        monkeypatch.setattr(orchestrator, "validate_voice", AsyncMock(return_value=None))
        order = []

        async def download(custom_path):
            Path(custom_path).write_bytes(b"offline portrait")

        transport.get_file.return_value.download_to_drive.side_effect = download

        async def photo(**kwargs):
            assert kwargs["photo"].read_bytes() == PNG
            order.append("photo")

        async def message(**kwargs):
            if kwargs["text"] == narration:
                order.append("script")

        async def voice(**kwargs):
            assert kwargs["voice"].read_bytes() == b"fake-ogg"
            assert kwargs["voice"].suffix == ".ogg"
            assert store.get(1).stage is Stage.NARRATING
            order.append("voice")

        transport.send_photo.side_effect = photo
        transport.send_message.side_effect = message
        transport.send_voice.side_effect = voice
        app = wiring.build_application(Settings(telegram_bot_token="123:dummy"), store)
        app.bot._bot_user = User(id=1, first_name="Offline", is_bot=True, username="testbot")
        update_id = 0

        async def route(text=None, photo=False):
            nonlocal update_id
            update_id += 1
            msg = {"message_id": update_id, "date": 1760000000,
                   "chat": {"id": 1, "type": "private"}}
            if photo:
                msg["photo"] = [{"file_id": "portrait", "file_unique_id": "one",
                                 "width": 100, "height": 100, "file_size": 100}]
            else:
                msg["text"] = text
                if text.startswith("/"):
                    msg["entities"] = [{"type": "bot_command", "offset": 0,
                                        "length": len(text)}]
            update = Update.de_json({"update_id": update_id, "message": msg}, app.bot)
            for handler in app.handlers[0]:
                matched = handler.check_update(update)
                if matched is not None and matched is not False:
                    await handler.callback(update, SimpleNamespace(bot=transport))
                    return
            raise AssertionError("No registered handler")

        for reset in ("/start", "/restart"):
            await route(reset)
            assert store.get(1).stage is Stage.AWAITING_PHOTO
            assert store.get(1).narration_text is None
            assert not (tmp_path / "1").exists()
            await route(photo=True)
            assert store.get(1).stage is Stage.INTERVIEW
            for _ in range(TOTAL_QUESTIONS):
                await route("An offline answer")
            await asyncio.wait_for(registry.wait(), 2)
            assert store.get(1).stage is Stage.DONE
            assert store.get(1).narration_text == narration
            assert not list(tmp_path.rglob("*.ogg"))
            calls = narrator.render.await_count
            for text in ("again", "/unknown"):
                await route(text)
            await route(photo=True)
            assert narrator.render.await_count == calls
            assert not registry.active(1)
        assert order == ["photo", "script", "voice"] * 2
        for provider in (bouncer.inspect, converter.generate_avatar,
                         scripter.write_script, narrator.render):
            assert provider.await_count == 2
        assert transport.get_file.await_count == 2
        await app.post_stop(app)
        assert not registry.active(1)
    asyncio.run(scenario())
