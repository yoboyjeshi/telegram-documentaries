"""The final interview answer must automatically deliver photo then narration."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from image_fixtures import JPEG, PNG

from app.bot import dispatch
from app.conversion_tasks import ConversionTasks
from app.interviewer import TOTAL_QUESTIONS
from app.media import photo_dest
from app.session_store import InMemorySessionStore
from app.states import InterviewAnswer, InterviewState, Stage, TraitSummary


@pytest.mark.parametrize("background", [False, True])
def test_final_answer_flows_to_narrating_once_without_audio(tmp_path, background):
    async def run():
        from app.scripter import NarrationScript
        store = InMemorySessionStore()
        state = store.get(1)
        state.stage = Stage.INTERVIEW
        state.interview = InterviewState(
            answers=[InterviewAnswer(question=f"Q{i}", answer="bold")
                     for i in range(TOTAL_QUESTIONS - 1)], pending_question="Last question?")
        portrait = photo_dest(1, root=tmp_path)
        portrait.parent.mkdir(parents=True)
        portrait.write_bytes(b"portrait")
        bot, interviewer, converter, scripter = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
        interviewer.summarise.return_value = TraitSummary(summary="A bold night owl.")
        converter.generate_avatar.return_value = PNG
        text = "Here we observe a bold creature, greeting the evening with quiet confidence."
        scripter.write_script.return_value = NarrationScript(text=text)
        order = []
        async def message(**kwargs):
            order.append("script" if kwargs['text'] == text else "ack")
        async def photo(*args):
            order.append("photo")
            return True
        bot.send_message.side_effect = message
        registry = ConversionTasks(store) if background else None
        incoming = SimpleNamespace(effective_message=SimpleNamespace(
            text="The final answer", chat=SimpleNamespace(id=1)))
        await dispatch(store, incoming, bot, interviewer=interviewer, converter=converter,
                       scripter=scripter, send_photo=photo, media_root=tmp_path,
                       conversion_tasks=registry)
        if registry:
            await asyncio.wait_for(registry.wait(), 1)
        assert order == ["ack", "photo", "script"]
        assert state.stage is Stage.NARRATING and state.narration_text == text
        converter.generate_avatar.assert_awaited_once()
        scripter.write_script.assert_awaited_once_with(state.interview.summary)
        await dispatch(store, incoming, bot, scripter=scripter, converter=converter,
                       conversion_tasks=registry)
        assert scripter.write_script.await_count == 1
        bot.send_audio.assert_not_awaited()
        bot.send_voice.assert_not_awaited()
        if registry:
            await registry.close()
    asyncio.run(run())


def test_two_complete_offline_runs_from_portrait_without_reboot(tmp_path):
    async def run():
        from app.bouncer import BouncerVerdict
        from app.interviewer import InterviewQuestion
        from app.scripter import NarrationScript
        store = InMemorySessionStore()
        registry = ConversionTasks(store)
        bot, bouncer, interviewer, converter, scripter = [AsyncMock() for _ in range(5)]
        bouncer.inspect.return_value = BouncerVerdict(is_human=True)
        interviewer.next_question.return_value = InterviewQuestion(text="What drives you?")
        interviewer.summarise.return_value = TraitSummary(summary="A quietly determined creature.")
        converter.generate_avatar.return_value = PNG
        narration = "A quietly determined creature greets the evening, ready for another adventure."
        scripter.write_script.return_value = NarrationScript(text=narration)
        async def download(sink):
            # Intake fetches into memory and admits decodable JPEG bytes only.
            sink.write(JPEG)
        telegram_file = AsyncMock()
        telegram_file.download_to_memory.side_effect = download
        bot.get_file.return_value = telegram_file
        order = []
        async def photo(chat, path, caption):
            assert path.read_bytes() == PNG
            order.append("photo")
            return True
        async def message(**kwargs):
            if kwargs['text'] == narration:
                order.append("narration")
        bot.send_message.side_effect = message
        kwargs = {"bouncer": bouncer, "interviewer": interviewer, "converter": converter,
                  "scripter": scripter, "send_photo": photo, "media_root": tmp_path,
                  "conversion_tasks": registry}
        def update(text=None, photo=None):
            return SimpleNamespace(effective_message=SimpleNamespace(
                text=text, photo=photo, chat=SimpleNamespace(id=1)))
        for command in ("/start", "/restart"):
            await dispatch(store, update(command), bot, **kwargs)
            assert store.get(1).narration_text is None
            await dispatch(store, update(photo=[SimpleNamespace(file_id="portrait", file_size=100)]),
                           bot, **kwargs)
            # Intake is a tracked worker: drain it before asserting its commit.
            await asyncio.wait_for(registry.wait(), 1)
            assert store.get(1).stage is Stage.INTERVIEW
            for _ in range(TOTAL_QUESTIONS):
                await dispatch(store, update("An offline answer"), bot, **kwargs)
                # Each interview turn is a tracked worker: drain it before the
                # next answer, exactly as the sequential poller would.
                await asyncio.wait_for(registry.wait(), 1)
            await asyncio.wait_for(registry.wait(), 1)
            assert store.get(1).stage is Stage.NARRATING
            assert store.get(1).narration_text == narration
        assert order == ["photo", "narration", "photo", "narration"]
        assert bouncer.inspect.await_count == 2
        assert converter.generate_avatar.await_count == 2
        assert scripter.write_script.await_count == 2
        bot.send_voice.assert_not_awaited()
        bot.send_audio.assert_not_awaited()
        await registry.close()
    asyncio.run(run())
