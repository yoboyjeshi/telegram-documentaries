"""Exercise the actual guarded Telegram send, not only the orchestration seam."""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import dispatch_safely
from app.conversion_tasks import ConversionTasks
from app.session_store import InMemorySessionStore
from app.states import InterviewState, Stage, TraitSummary


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("retry_fails", [False, True])
def test_real_send_wrapper_never_leaks_payload_and_stays_retryable(background, retry_fails, caplog):
    async def run():
        from app.scripter import NarrationScript
        secret = "PrivateKeyTraitScriptSentinel"
        text = "A creature emerges with quiet confidence. " + secret
        store = InMemorySessionStore()
        state = store.get(1)
        state.stage = Stage.SCRIPTING
        state.interview = InterviewState(summary=TraitSummary(summary=secret))
        before = state.model_dump()
        writer = AsyncMock()
        writer.write_script.return_value = NarrationScript(text=text)
        bot = AsyncMock()
        bot.send_message.side_effect = [RuntimeError(secret + text),
                                       RuntimeError(secret) if retry_fails else True]
        tasks = ConversionTasks(store) if background else None
        incoming = SimpleNamespace(effective_message=SimpleNamespace(
            text="retry", chat=SimpleNamespace(id=1)))
        await dispatch_safely(store, incoming, bot, scripter=writer, conversion_tasks=tasks)
        if tasks:
            await asyncio.wait_for(tasks.wait(), 1)
        assert state.model_dump() == before
        assert bot.send_message.await_count == 2
        assert "try again" in bot.send_message.await_args.kwargs['text']
        assert secret not in caplog.text
        assert text not in caplog.text
        assert "Traceback" not in caplog.text
        assert "RuntimeError" in caplog.text
        if tasks:
            await tasks.close()
    with caplog.at_level(logging.DEBUG):
        asyncio.run(run())
