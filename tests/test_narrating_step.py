"""Narration is a deliver-then-commit transaction; provider and codec are offline.

Mirrors ``test_scripting_step.py`` one stage later: the stored paragraph is the
only input, the encoder only ever sees a correctly wrapped WAV, the ``.ogg``
artifact exists exactly while the voice upload is in flight, and ``DONE`` is
committed only after a confirmed delivery. No network, no real FFmpeg, no sleeps.
"""
import asyncio
import io
import logging
import re
import wave
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app import orchestrator
from app.audio_format import NarrationAudio
from app.handlers import NARRATOR_FAILURE_TEXT, NARRATOR_MISSING_TEXT
from app.session_store import InMemorySessionStore
from app.states import InterviewState, Stage, TraitSummary

NARRATION = "Here we observe a quietly bold creature, remarkably devoted to its evening rituals."
SECRET = "SentinelPrivatePayload"

RATE = 24000
PCM_MIME = "audio/L16;codec=pcm;rate=24000"
PCM = b"\x01\x02" * (RATE // 4)  # 0.25s of 16-bit mono little-endian samples
OGG = b"OggS" + bytes(20) + b"OpusHead" + b"offline-encoded-voice"
ARTIFACT_NAME = re.compile(r"narration-0-[0-9a-f]+\.ogg")


def wav(frames=PCM):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(frames)
    return buffer.getvalue()


WAV = wav()


def prepared(text=NARRATION):
    store = InMemorySessionStore()
    state = store.get(1)
    state.stage = Stage.NARRATING
    state.interview = InterviewState(summary=TraitSummary(summary=SECRET, traits=["bold"]))
    state.narration_text = text
    return store


def audio(data=PCM, mime=PCM_MIME):
    """Injected provider output; ``model_construct`` skips validation on purpose."""
    return NarrationAudio.model_construct(data=data, mime_type=mime)


def run_step(store, **kwargs):
    defaults = {"send_message": AsyncMock(return_value=True), "encode_audio": None,
                "validate_audio": None, "media_root": None}
    defaults.update(kwargs)
    return orchestrator.handle_narrating_update(store, 1, **defaults)


def artifacts(root):
    return sorted(path.name for path in (root / "1").glob("*") if path.is_file())


def test_success_delivers_voice_then_commits_done(tmp_path):
    async def run():
        store = prepared()
        narrator = AsyncMock()
        narrator.render.return_value = audio()
        seen = {}

        async def encode(data):
            seen["wav"] = data
            return OGG

        validate = AsyncMock()

        async def send(chat, path):
            assert chat == 1
            assert ARTIFACT_NAME.fullmatch(path.name), path.name
            assert path.read_bytes() == OGG  # never raw PCM under another extension
            assert store.get(1).stage is Stage.NARRATING  # commit happens after delivery
            seen["path"] = path
            return True

        sender = AsyncMock(side_effect=send)
        message = AsyncMock(return_value=True)
        await run_step(store, narrator=narrator, send_voice=sender, send_message=message,
                       media_root=tmp_path, encode_audio=encode, validate_audio=validate)
        narrator.render.assert_awaited_once_with(NARRATION)
        with wave.open(io.BytesIO(seen["wav"])) as source:
            assert (source.getnchannels(), source.getsampwidth(),
                    source.getframerate()) == (1, 2, RATE)
            assert source.readframes(source.getnframes()) == PCM
        validate.assert_awaited_once_with(OGG)
        sender.assert_awaited_once()
        assert store.get(1).stage is Stage.DONE
        assert store.get(1).narration_text == NARRATION
        assert not seen["path"].exists()  # the attempt cleans its own artifact
        assert artifacts(tmp_path) == []
        message.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("mime", ["audio/wav", "audio/x-wav", "AUDIO/WAV"])
def test_wav_provider_output_is_never_double_wrapped(tmp_path, mime):
    async def run():
        store = prepared()
        narrator = AsyncMock()
        narrator.render.return_value = audio(WAV, mime)
        seen = {}

        async def encode(data):
            seen["wav"] = data
            return OGG

        await run_step(store, narrator=narrator, send_voice=AsyncMock(return_value=True),
                       media_root=tmp_path, encode_audio=encode, validate_audio=AsyncMock())
        assert seen["wav"].count(b"RIFF") == 1
        with wave.open(io.BytesIO(seen["wav"])) as source:
            assert source.readframes(source.getnframes()) == PCM
        assert store.get(1).stage is Stage.DONE

    asyncio.run(run())


@pytest.mark.parametrize("text", [None, "", "   \n\t "])
def test_missing_narration_prompts_restart_without_a_provider_call(tmp_path, text):
    async def run():
        store = prepared(text)
        before = store.get(1).model_dump()
        narrator, sender, message = AsyncMock(), AsyncMock(), AsyncMock(return_value=True)
        encode, validate = AsyncMock(), AsyncMock()
        await run_step(store, narrator=narrator, send_voice=sender, send_message=message,
                       media_root=tmp_path, encode_audio=encode, validate_audio=validate)
        narrator.render.assert_not_awaited()
        encode.assert_not_awaited()
        validate.assert_not_awaited()
        sender.assert_not_awaited()
        message.assert_awaited_once_with(1, NARRATOR_MISSING_TEXT)
        assert store.get(1).model_dump() == before
        assert store.get(1).stage is Stage.NARRATING
        assert not (tmp_path / "1").exists()

    asyncio.run(run())


BAD_PROVIDER_AUDIO = [
    ("empty", audio(b"")),
    ("not-bytes", audio("still text")),
    ("odd-length-pcm", audio(PCM + b"\x00")),
    ("missing-mime", audio(PCM, "")),
    ("wrong-mime", audio(PCM, "audio/mpeg")),
    ("contradictory-rate", audio(PCM, "audio/L16;codec=pcm;rate=48000")),
    ("contradictory-codec", audio(PCM, "audio/L16;codec=opus;rate=24000")),
    ("contradictory-channels", audio(PCM, "audio/L16;codec=pcm;rate=24000;channels=2")),
    ("truncated-wav", audio(WAV[:40], "audio/wav")),
    ("pcm-labelled-wav", audio(PCM, "audio/wav")),
    ("oversized", audio(bytes(10 * 1024 * 1024 + 2))),
    ("over-duration", audio(bytes(2 * RATE * 121))),
    ("wrong-schema", {"wrong": "schema"}),
    ("none", None),
    ("number", 42),
]


@pytest.mark.parametrize("case,result", BAD_PROVIDER_AUDIO, ids=[c for c, _ in BAD_PROVIDER_AUDIO])
def test_invalid_provider_output_is_never_encoded_saved_or_sent(tmp_path, case, result, caplog):
    async def run():
        store = prepared()
        before = store.get(1).model_dump()
        narrator = AsyncMock()
        narrator.render.return_value = result
        sender, message = AsyncMock(), AsyncMock(return_value=True)
        encode, validate = AsyncMock(return_value=OGG), AsyncMock()
        await run_step(store, narrator=narrator, send_voice=sender, send_message=message,
                       media_root=tmp_path, encode_audio=encode, validate_audio=validate)
        encode.assert_not_awaited()
        validate.assert_not_awaited()
        sender.assert_not_awaited()
        message.assert_awaited_once_with(1, NARRATOR_FAILURE_TEXT)
        assert store.get(1).model_dump() == before
        assert artifacts(tmp_path) == []

    with caplog.at_level(logging.DEBUG):
        asyncio.run(run())
    assert SECRET not in caplog.text
    assert NARRATION not in caplog.text
    assert "input_value" not in caplog.text
    assert "Traceback" not in caplog.text


@pytest.mark.parametrize("encoded", [b"", "not bytes", None, bytes(10 * 1024 * 1024 + 1)],
                         ids=["empty", "str", "none", "oversize"])
def test_invalid_encoded_artifact_is_never_sent(tmp_path, encoded):
    async def run():
        store = prepared()
        before = store.get(1).model_dump()
        narrator = AsyncMock()
        narrator.render.return_value = audio()
        sender, message = AsyncMock(), AsyncMock(return_value=True)
        await run_step(store, narrator=narrator, send_voice=sender, send_message=message,
                       media_root=tmp_path, encode_audio=AsyncMock(return_value=encoded),
                       validate_audio=AsyncMock())
        sender.assert_not_awaited()
        message.assert_awaited_once_with(1, NARRATOR_FAILURE_TEXT)
        assert store.get(1).model_dump() == before
        assert artifacts(tmp_path) == []

    asyncio.run(run())


# Filesystem failures/privacy are covered by tests/test_narrator_storage_failures.py.
FAILURES = ["provider", "encode", "validate", "send_exception", "send_false",
            "notify_exception", "notify_false"]


@pytest.mark.parametrize("failure", FAILURES)
def test_failures_keep_state_and_log_metadata_only(tmp_path, failure, caplog, monkeypatch):
    async def run():
        store = prepared()
        before = store.get(1).model_dump()
        narrator = AsyncMock()
        narrator.render.return_value = audio()
        encode = AsyncMock(return_value=OGG)
        validate = AsyncMock()
        sender = AsyncMock(return_value=True)
        message = AsyncMock(return_value=True)
        if failure in ("provider", "notify_exception", "notify_false"):
            narrator.render.side_effect = RuntimeError(f"{SECRET} {NARRATION}")
        if failure == "encode":
            encode.side_effect = RuntimeError(f"{SECRET} {NARRATION}")
        elif failure == "validate":
            validate.side_effect = RuntimeError(f"{SECRET} {NARRATION}")
        elif failure == "send_exception":
            sender.side_effect = RuntimeError(f"{SECRET} {NARRATION}")
        elif failure == "send_false":
            sender.return_value = False
        elif failure == "notify_exception":
            message.side_effect = RuntimeError(f"{SECRET} {NARRATION}")
        elif failure == "notify_false":
            message.return_value = False
        await run_step(store, narrator=narrator, send_voice=sender, send_message=message,
                       media_root=tmp_path, encode_audio=encode, validate_audio=validate)
        assert store.get(1).model_dump() == before
        assert store.get(1).stage is Stage.NARRATING
        assert message.await_count == 1  # one bounded notice, never a loop
        if failure not in ("notify_exception", "notify_false"):
            assert message.await_args.args == (1, NARRATOR_FAILURE_TEXT)
        assert artifacts(tmp_path) == []

    with caplog.at_level(logging.DEBUG):
        asyncio.run(run())
    assert SECRET not in caplog.text
    assert NARRATION not in caplog.text
    assert "Traceback" not in caplog.text
    assert caplog.records
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("phase", ["generation", "encode", "validate", "send", "notification"])
@pytest.mark.parametrize("swallow", [False, True])
def test_deadlines_even_when_a_provider_swallows_cancellation(tmp_path, phase, swallow,
                                                              monkeypatch):
    async def run():
        store = prepared()
        before = store.get(1).model_dump()
        monkeypatch.setattr(orchestrator, "NARRATING_TIMEOUT_SECONDS", 0.001)
        monkeypatch.setattr(orchestrator, "ENCODING_TIMEOUT_SECONDS", 0.001)
        monkeypatch.setattr(orchestrator, "VOICE_SEND_TIMEOUT_SECONDS", 0.001)

        async def block():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if not swallow:
                    raise

        async def render(text):
            if phase == "generation":
                await block()
            if phase == "notification":
                raise RuntimeError(SECRET)
            return audio()

        async def encode(data):
            if phase == "encode":
                await block()
            return OGG

        async def validate(data):
            if phase == "validate":
                await block()

        async def send(chat, path):
            if phase == "send":
                await block()
            return True

        async def notify(chat, text):
            if phase == "notification":
                await block()
            return True

        narrator = AsyncMock()
        narrator.render.side_effect = render
        sender = AsyncMock(side_effect=send)
        message = AsyncMock(side_effect=notify)
        await asyncio.wait_for(run_step(
            store, narrator=narrator, send_voice=sender, send_message=message,
            media_root=tmp_path, encode_audio=encode, validate_audio=validate), 1)
        assert store.get(1).model_dump() == before
        assert store.get(1).stage is Stage.NARRATING  # a late result is never success
        assert sender.await_count == (1 if phase == "send" else 0)
        assert message.await_count <= 1
        assert artifacts(tmp_path) == []

    asyncio.run(run())


def test_stale_entry_and_cancelled_provider_propagate_without_notifications(tmp_path):
    async def run():
        store = prepared()
        narrator, sender, message = AsyncMock(), AsyncMock(), AsyncMock()
        with pytest.raises(asyncio.CancelledError):
            await run_step(store, narrator=narrator, send_voice=sender, send_message=message,
                           media_root=tmp_path, session_version=-1)
        narrator.render.assert_not_awaited()
        narrator.render.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await run_step(store, narrator=narrator, send_voice=sender, send_message=message,
                           media_root=tmp_path)
        sender.assert_not_awaited()
        message.assert_not_awaited()
        assert store.get(1).stage is Stage.NARRATING
        assert not (tmp_path / "1").exists()

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["generation", "encode", "validate", "send", "notification"])
def test_session_invalidated_without_task_cancel_blocks_stale_results(tmp_path, phase):
    async def run():
        store = prepared()

        def replace_session():
            store.reset(1)
            store.get(1).stage = Stage.NARRATING
            store.get(1).narration_text = "A newer creature stirs."

        async def render(text):
            if phase == "generation":
                replace_session()
            elif phase == "notification":
                raise RuntimeError(SECRET)
            return audio()

        async def encode(data):
            if phase == "encode":
                replace_session()
            return OGG

        async def validate(data):
            if phase == "validate":
                replace_session()

        async def send(chat, path):
            replace_session()
            return True

        async def notify(chat, text):
            replace_session()
            return True

        narrator = AsyncMock()
        narrator.render.side_effect = render
        sender = AsyncMock(side_effect=send)
        message = AsyncMock(side_effect=notify)
        with pytest.raises(asyncio.CancelledError):
            await run_step(store, narrator=narrator, send_voice=sender, send_message=message,
                           media_root=tmp_path, encode_audio=encode, validate_audio=validate)
        assert store.get(1).stage is Stage.NARRATING
        assert store.get(1).narration_text == "A newer creature stirs."
        assert sender.await_count == (1 if phase == "send" else 0)
        assert message.await_count == (1 if phase == "notification" else 0)

    asyncio.run(run())


def test_retired_attempt_never_removes_replacement_session_media(tmp_path):
    """The old attempt's cleanup runs after a reset; new-session files must survive."""
    async def run():
        store = prepared()
        released = asyncio.Event()

        async def send(chat, path):
            store.reset(1)
            store.get(1).stage = Stage.NARRATING
            store.get(1).narration_text = "A newer creature stirs."
            replacement = (tmp_path / "1" / "narration-1-replacement.ogg")
            replacement.parent.mkdir(parents=True, exist_ok=True)
            replacement.write_bytes(OGG)
            released.set()
            return True

        narrator = AsyncMock()
        narrator.render.return_value = audio()
        with pytest.raises(asyncio.CancelledError):
            await run_step(store, narrator=narrator, send_voice=AsyncMock(side_effect=send),
                           send_message=AsyncMock(return_value=True), media_root=tmp_path,
                           encode_audio=AsyncMock(return_value=OGG), validate_audio=AsyncMock())
        assert released.is_set()
        replacement = tmp_path / "1" / "narration-1-replacement.ogg"
        assert replacement.read_bytes() == OGG
        assert store.get(1).stage is Stage.NARRATING

    asyncio.run(run())


def test_cleanup_failure_after_delivery_keeps_done_without_resending(tmp_path, caplog,
                                                                    monkeypatch):
    async def run():
        store = prepared()
        narrator = AsyncMock()
        narrator.render.return_value = audio()
        sender = AsyncMock(return_value=True)

        def boom(self, missing_ok=False):
            raise OSError(f"{SECRET} {NARRATION}")

        monkeypatch.setattr(Path, "unlink", boom)
        await run_step(store, narrator=narrator, send_voice=sender,
                       send_message=AsyncMock(return_value=True), media_root=tmp_path,
                       encode_audio=AsyncMock(return_value=OGG), validate_audio=AsyncMock())
        sender.assert_awaited_once()
        assert store.get(1).stage is Stage.DONE

    with caplog.at_level(logging.DEBUG):
        asyncio.run(run())
    assert SECRET not in caplog.text
    assert NARRATION not in caplog.text
    assert "Traceback" not in caplog.text
