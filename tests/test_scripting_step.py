"""Scripting is a commit-after-delivery transaction; external calls are offline."""
import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from app import orchestrator
from app.session_store import InMemorySessionStore
from app.states import InterviewState, Stage, TraitSummary

TEXT = "Here we observe a quietly bold creature, remarkably devoted to its evening rituals."
SECRET = "SentinelPrivatePayload"


def prepared():
    store = InMemorySessionStore()
    state = store.get(1)
    state.stage = Stage.SCRIPTING
    state.interview = InterviewState(summary=TraitSummary(summary=SECRET, traits=["bold"]))
    return store


def candidate(text=TEXT):
    from app.scripter import NarrationScript
    return NarrationScript.model_construct(text=text)


def test_success_sends_before_commit_and_passes_traits():
    async def run():
        store = prepared()
        before = store.get(1).model_dump()
        writer = AsyncMock()
        writer.write_script.return_value = candidate()
        async def send(chat, text):
            assert chat == 1 and text == TEXT
            assert store.get(1).model_dump() == before
            return True
        sender = AsyncMock(side_effect=send)
        await orchestrator.handle_scripting_update(store, 1, scripter=writer, send_message=sender)
        writer.write_script.assert_awaited_once_with(store.get(1).interview.summary)
        sender.assert_awaited_once_with(1, TEXT)
        assert store.get(1).narration_text == TEXT
        assert store.get(1).stage is Stage.NARRATING
    asyncio.run(run())


@pytest.mark.parametrize("missing", ["interview", "summary"])
def test_missing_traits_prompts_restart_without_model(missing):
    async def run():
        store = prepared()
        if missing == "interview":
            store.get(1).interview = None
        else:
            store.get(1).interview.summary = None
        before = store.get(1).model_dump()
        writer, sender = AsyncMock(), AsyncMock(return_value=True)
        await orchestrator.handle_scripting_update(store, 1, scripter=writer, send_message=sender)
        writer.write_script.assert_not_awaited()
        assert sender.await_count == 1
        assert "/restart" in sender.await_args.args[1]
        assert store.get(1).model_dump() == before
    asyncio.run(run())


@pytest.mark.parametrize("bad", ["", "word " * 91, "two\nparagraphs", "**bold**", "[pause]", 42,
                                  {"wrong": "schema"}, None])
def test_invalid_injected_output_never_sent_or_saved(bad, caplog):
    async def run():
        store = prepared()
        before = store.get(1).model_dump()
        writer, sender = AsyncMock(), AsyncMock(return_value=True)
        writer.write_script.return_value = candidate(bad) if isinstance(bad, str) else bad
        await orchestrator.handle_scripting_update(store, 1, scripter=writer, send_message=sender)
        assert sender.await_count == 1
        assert "try again" in sender.await_args.args[1]
        assert store.get(1).model_dump() == before
    with caplog.at_level(logging.DEBUG):
        asyncio.run(run())
    assert SECRET not in caplog.text
    assert "input_value" not in caplog.text


@pytest.mark.parametrize("failure", ["model", "send_exception", "send_false", "retry_exception",
                                     "retry_false"])
def test_failure_preserves_state_and_logs_metadata_only(failure, caplog):
    async def run():
        store = prepared()
        before = store.get(1).model_dump()
        writer, sender = AsyncMock(), AsyncMock(return_value=True)
        writer.write_script.return_value = candidate(TEXT + " " + SECRET)
        if failure in ("model", "retry_exception", "retry_false"):
            writer.write_script.side_effect = RuntimeError(SECRET)
        if failure == "send_exception":
            sender.side_effect = [RuntimeError(SECRET), True]
        elif failure == "send_false":
            sender.side_effect = [False, True]
        elif failure == "retry_exception":
            sender.side_effect = RuntimeError(SECRET)
        elif failure == "retry_false":
            sender.return_value = False
        await orchestrator.handle_scripting_update(store, 1, scripter=writer, send_message=sender)
        assert store.get(1).model_dump() == before
        assert sender.await_count == (2 if failure.startswith("send") else 1)
        assert "try again" in sender.await_args.args[1]
    with caplog.at_level(logging.DEBUG):
        asyncio.run(run())
    assert SECRET not in caplog.text
    assert TEXT not in caplog.text
    assert "Traceback" not in caplog.text
    assert caplog.records


@pytest.mark.parametrize("phase", ["generation", "send", "retry"])
@pytest.mark.parametrize("swallow", [False, True])
def test_deadlines_even_when_provider_swallows_cancellation(phase, swallow, monkeypatch):
    async def run():
        store = prepared()
        before = store.get(1).model_dump()
        monkeypatch.setattr(orchestrator, "SCRIPTING_TIMEOUT_SECONDS", 0.001)
        monkeypatch.setattr(orchestrator, "SCRIPT_SEND_TIMEOUT_SECONDS", 0.001)
        async def block():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if not swallow:
                    raise
        async def write(summary):
            if phase == "generation":
                await block()
            if phase == "retry":
                raise RuntimeError(SECRET)
            return candidate()
        async def send(chat, text):
            if (phase == "send" and text == TEXT) or phase == "retry":
                await block()
            return True
        writer = AsyncMock()
        writer.write_script.side_effect = write
        sender = AsyncMock(side_effect=send)
        await asyncio.wait_for(orchestrator.handle_scripting_update(
            store, 1, scripter=writer, send_message=sender), 1)
        assert store.get(1).model_dump() == before
        if phase == "generation":
            assert all(call.args[1] != TEXT for call in sender.await_args_list)
        assert sender.await_count <= 2
    asyncio.run(run())


def test_stale_entry_and_cancelled_generation_propagate_without_notifications():
    async def run():
        store = prepared()
        writer, sender = AsyncMock(), AsyncMock()
        with pytest.raises(asyncio.CancelledError):
            await orchestrator.handle_scripting_update(
                store, 1, scripter=writer, send_message=sender, session_version=-1)
        writer.write_script.assert_not_awaited()
        writer.write_script.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await orchestrator.handle_scripting_update(store, 1, scripter=writer, send_message=sender)
        sender.assert_not_awaited()
        assert store.get(1).stage is Stage.SCRIPTING
    asyncio.run(run())


@pytest.mark.parametrize("phase", ["generation", "send", "notification"])
def test_session_invalidated_without_task_cancel_blocks_stale_results(phase):
    async def run():
        store = prepared()
        writer = AsyncMock()
        def reset_to_new_session():
            store.reset(1)
            store.get(1).stage = Stage.NARRATING
            store.get(1).narration_text = "New session narration."
        async def write(summary):
            if phase == "generation":
                reset_to_new_session()
            elif phase == "notification":
                raise RuntimeError(SECRET)
            return candidate()
        async def send(chat, text):
            reset_to_new_session()
            return True
        writer.write_script.side_effect = write
        sender = AsyncMock(side_effect=send)
        with pytest.raises(asyncio.CancelledError):
            await orchestrator.handle_scripting_update(store, 1, scripter=writer, send_message=sender)
        assert store.get(1).stage is Stage.NARRATING
        assert store.get(1).narration_text == "New session narration."
        assert sender.await_count == (0 if phase == "generation" else 1)
    asyncio.run(run())
