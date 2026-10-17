"""The INTERVIEW, CONVERTING, SCRIPTING and NARRATING-stage orchestrator.

Owns the ``INTERVIEW``-stage routing and delegates to the typed
:class:`~app.interviewer.Interviewer` and :class:`~app.converter.Converter`.
The pure rules live here: one step of progression per valid answer,
commit-only-on-success, and a friendly retry when a model call fails. The
``CONVERTING``-stage step generates one hybrid-animal avatar, sends it, and
advances to ``SCRIPTING``. The scripting step sends validated narration and
advances to ``NARRATING``; the Narrator delivers voice before terminal ``DONE``.

Converter, Scripter and Narrator errors log only metadata, never model payloads or raw
exceptions. Earlier Interviewer logging is unchanged and remains Feature 7 work.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.audio_encoding import ENCODING_TIMEOUT_SECONDS, encode_voice, validate_voice
from app.audio_format import MAX_AUDIO_BYTES, NarrationAudio, normalize_audio
from app.converter import Converter
from app.handlers import (
    CONVERTER_CAPTION,
    CONVERTER_FAILURE_TEXT,
    CONVERTER_MISSING_PORTRAIT_TEXT,
    INTERVIEW_COMPLETE_TEXT,
    INTERVIEW_FAILURE_TEXT,
    INTERVIEW_INTRO,
    INTERVIEW_OTHER_REDIRECT_TEXT,
    INTERVIEW_PHOTO_REDIRECT_TEXT,
    INTERVIEW_REPROMPT_TEXT,
    INTERVIEW_UNKNOWN_COMMAND_TEXT,
    NARRATOR_FAILURE_TEXT,
    NARRATOR_MISSING_TEXT,
    SCRIPTER_FAILURE_TEXT,
    SCRIPTER_MISSING_SUMMARY_TEXT,
)
from app.image_format import image_suffix
from app.interviewer import TOTAL_QUESTIONS, Interviewer
from app.logging_config import guard_errors, logged
from app.media import MEDIA_ROOT, avatar_dest, narration_dest, photo_dest
from app.narrator import Narrator
from app.scripter import NarrationScript, Scripter
from app.session_store import InMemorySessionStore
from app.states import InterviewAnswer, InterviewState, Stage
from app.telegram_adapter import ChatInput

logger = logging.getLogger("telegram_documentaries")

# The photo-send capability injected from bot.py (mirrors photo_flow's DownloadFn).
SendPhotoFn = Callable[[int, Path, str], Awaitable[bool]]


def _log_interviewer_failure(chat_id: int, exc: Exception) -> None:
    """Log an interviewer failure as safe metadata only.

    Neither a traceback nor the exception message is logged: a provider error
    can embed the request payload (the history, an answer) or the raw model
    output, so only the operation and the exception type are recorded.
    """
    logger.error(
        "Interviewer failed chat_id=%s operation=interviewer type=%s",
        chat_id,
        type(exc).__name__,
    )


def _log_converter_failure(chat_id: int, exc: Exception) -> None:
    """Log metadata only: exception messages may contain keys and model payloads."""
    logger.error("Converter failed chat_id=%s type=%s", chat_id, type(exc).__name__)


@logged
@guard_errors
async def start_interview(
    store: InMemorySessionStore, chat_id: int, *, interviewer: Interviewer
) -> str:
    """Set a fresh interview, ask question 1, and return the intro plus question."""
    state = store.get(chat_id)
    interview = InterviewState()
    state.interview = interview
    try:
        question = await interviewer.next_question([])
    except Exception as exc:  # noqa: BLE001 - a model error must not crash the bot
        _log_interviewer_failure(chat_id, exc)
        return INTERVIEW_FAILURE_TEXT
    interview.pending_question = question.text
    logger.info("interview started chat_id=%s answered=0/%d", chat_id, TOTAL_QUESTIONS)
    return f"{INTERVIEW_INTRO}\n\n{question.text}"


@logged
@guard_errors
async def handle_interview_update(
    store: InMemorySessionStore, incoming: ChatInput, *, interviewer: Interviewer
) -> str:
    """Route one update during ``INTERVIEW``: answer, redirect, or nudge.

    A photo gets a gentle redirect with no state change; an unknown command gets
    a nudge and stays on the current question; any other kind (sticker,
    document, unsupported media) gets a short redirect; blank text re-asks the
    current question. A valid answer only commits once the model call succeeds.
    """
    chat_id = incoming.chat_id
    state = store.get(chat_id)

    if incoming.kind == "photo":
        return INTERVIEW_PHOTO_REDIRECT_TEXT
    if incoming.kind == "command":
        # /start and /restart are handled before this point in the dispatcher.
        return INTERVIEW_UNKNOWN_COMMAND_TEXT
    if incoming.kind != "text":
        return INTERVIEW_OTHER_REDIRECT_TEXT

    interview = state.interview
    if interview is None or interview.pending_question is None:
        # Self-heal: a text at INTERVIEW with no live question asks question 1.
        return await start_interview(store, chat_id, interviewer=interviewer)

    text = incoming.text or ""
    if not text.strip():
        return INTERVIEW_REPROMPT_TEXT

    history = [
        *interview.answers,
        InterviewAnswer(question=interview.pending_question, answer=text),
    ]
    completing = len(history) >= TOTAL_QUESTIONS

    try:
        if completing:
            summary = await interviewer.summarise(history)
        else:
            next_question = await interviewer.next_question(history)
    except Exception as exc:  # noqa: BLE001 - a model error must not crash the bot
        _log_interviewer_failure(chat_id, exc)
        return INTERVIEW_FAILURE_TEXT

    # Commit only after the model call succeeded, so a failure leaves the
    # interview exactly as it was and the user can retry.
    interview.record(text)
    if completing:
        interview.summary = summary
        store.set_stage(chat_id, Stage.CONVERTING)
        logger.info(
            "interview complete chat_id=%s answered=%d/%d",
            chat_id,
            len(interview.answers),
            TOTAL_QUESTIONS,
        )
        return INTERVIEW_COMPLETE_TEXT

    interview.pending_question = next_question.text
    logger.info(
        "interview answered=%d/%d chat_id=%s",
        len(interview.answers),
        TOTAL_QUESTIONS,
        chat_id,
    )
    return next_question.text


def _delete_quietly(path: Path) -> None:
    """Remove a generated file (best effort) so a retry always starts clean."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not delete generated avatar path=%s", path.name)


CONVERSION_TIMEOUT_SECONDS = 120
PHOTO_SEND_TIMEOUT_SECONDS = 30


@logged
async def handle_converting_update(
    store: InMemorySessionStore,
    chat_id: int,
    *,
    converter: Converter,
    send_photo: SendPhotoFn,
    media_root: Path | None = None,
    session_version: int | None = None,
) -> str | None:
    """Send one validated image; all exits clean only the owning session's files.

    Cancellation is deliberately not caught. Reset invalidates ownership before
    cancellation, so a delayed old task cannot send, commit, or delete new media.
    """
    version = store.version(chat_id) if session_version is None else session_version
    root = media_root or MEDIA_ROOT

    def check_current() -> None:
        task = asyncio.current_task()
        if (store.version(chat_id) != version
                or store.get(chat_id).stage is not Stage.CONVERTING
                or (task is not None and task.cancelling())):
            raise asyncio.CancelledError

    check_current()
    try:
        state = store.get(chat_id)
        summary = state.interview.summary if state.interview is not None else None
        portrait = photo_dest(chat_id, root=root)
        if summary is None or not portrait.is_file():
            logger.warning("Converter missing portrait or summary chat_id=%s", chat_id)
            return CONVERTER_MISSING_PORTRAIT_TEXT

        async with asyncio.timeout(CONVERSION_TIMEOUT_SECONDS) as deadline:
            data = await converter.generate_avatar(portrait, summary)
        check_current()
        if deadline.expired():
            raise TimeoutError
        avatar = avatar_dest(chat_id, root=root, suffix=image_suffix(data))
        avatar.write_bytes(data)
        async with asyncio.timeout(PHOTO_SEND_TIMEOUT_SECONDS) as send_deadline:
            delivered = await send_photo(chat_id, avatar, CONVERTER_CAPTION)
        check_current()
        if send_deadline.expired():
            raise TimeoutError
        if not delivered:
            logger.error("Converter photo send not delivered chat_id=%s", chat_id)
            return CONVERTER_FAILURE_TEXT

        store.set_stage(chat_id, Stage.SCRIPTING)
        logger.info("converter done chat_id=%s bytes=%d", chat_id, len(data))
        return None
    except Exception as exc:  # noqa: BLE001 - external and filesystem failures stay retryable
        check_current()
        _log_converter_failure(chat_id, exc)
        return CONVERTER_FAILURE_TEXT
    finally:
        # After reset the command owns cleanup. Never unlink a new session's avatar.
        if store.version(chat_id) == version:
            for suffix in (".png", ".jpg"):
                _delete_quietly(avatar_dest(chat_id, root=root, suffix=suffix))


SCRIPTING_TIMEOUT_SECONDS = 120
SCRIPT_SEND_TIMEOUT_SECONDS = 30
SendMessageFn = Callable[[int, str], Awaitable[bool]]


async def handle_scripting_update(
    store: InMemorySessionStore,
    chat_id: int,
    *,
    scripter: Scripter,
    send_message: SendMessageFn,
    session_version: int | None = None,
) -> None:
    """Validate, deliver, then commit narration for the owning SCRIPTING session.

    Notifications are bounded and ownership-guarded too. CancelledError passes
    through unchanged; logging deliberately never includes exception payloads.
    """
    version = store.version(chat_id) if session_version is None else session_version

    def check_current() -> None:
        task = asyncio.current_task()
        if (store.version(chat_id) != version
                or store.get(chat_id).stage is not Stage.SCRIPTING
                or (task is not None and task.cancelling())):
            raise asyncio.CancelledError

    async def deliver(text: str) -> None:
        check_current()
        async with asyncio.timeout(SCRIPT_SEND_TIMEOUT_SECONDS) as deadline:
            delivered = await send_message(chat_id, text)
        check_current()
        if deadline.expired():
            raise TimeoutError
        if not delivered:
            raise RuntimeError("Message not delivered")

    async def notify(text: str) -> None:
        try:
            await deliver(text)
        except Exception as exc:  # noqa: BLE001 - one bounded notification, never a retry loop
            check_current()
            logger.error("Scripter notification failed chat_id=%s stage=SCRIPTING type=%s",
                         chat_id, type(exc).__name__)

    check_current()
    state = store.get(chat_id)
    summary = state.interview.summary if state.interview is not None else None
    if summary is None:
        logger.warning("Scripter missing summary chat_id=%s stage=SCRIPTING", chat_id)
        await notify(SCRIPTER_MISSING_SUMMARY_TEXT)
        return

    try:
        async with asyncio.timeout(SCRIPTING_TIMEOUT_SECONDS) as deadline:
            result = await scripter.write_script(summary)
        check_current()
        if deadline.expired():
            raise TimeoutError
        # Revalidate even injected/model_construct results at the send boundary.
        script = NarrationScript.model_validate(result)
        await deliver(script.text)
        check_current()
        state.narration_text = script.text
        store.set_stage(chat_id, Stage.NARRATING)
        logger.info("Scripter done chat_id=%s stage=NARRATING", chat_id)
    except Exception as exc:  # noqa: BLE001 - model/validation/send errors stay retryable
        check_current()
        logger.error("Scripter failed chat_id=%s stage=SCRIPTING type=%s",
                     chat_id, type(exc).__name__)
        await notify(SCRIPTER_FAILURE_TEXT)


NARRATING_TIMEOUT_SECONDS = 120
VOICE_SEND_TIMEOUT_SECONDS = 30
SendVoiceFn = Callable[[int, Path], Awaitable[bool]]
EncodeAudioFn = Callable[[bytes], Awaitable[bytes]]
ValidateAudioFn = Callable[[bytes], Awaitable[None]]


async def handle_narrating_update(
    store: InMemorySessionStore,
    chat_id: int,
    *,
    narrator: Narrator,
    send_voice: SendVoiceFn,
    send_message: SendMessageFn,
    media_root: Path | None = None,
    session_version: int | None = None,
    encode_audio: EncodeAudioFn | None = None,
    validate_audio: ValidateAudioFn | None = None,
) -> None:
    """Render, validate, deliver and only then commit DONE for the owning session.

    Reset owns directory purging. Retired work never writes or removes new files;
    cancellation is never translated into a retry notification.
    """
    version = store.version(chat_id) if session_version is None else session_version
    root = media_root or MEDIA_ROOT
    artifact: Path | None = None

    def check_current() -> None:
        task = asyncio.current_task()
        if (store.version(chat_id) != version
                or store.get(chat_id).stage is not Stage.NARRATING
                or (task is not None and task.cancelling())):
            raise asyncio.CancelledError

    async def notify(text: str) -> None:
        try:
            check_current()
            async with asyncio.timeout(VOICE_SEND_TIMEOUT_SECONDS) as deadline:
                delivered = await send_message(chat_id, text)
            check_current()
            if deadline.expired():
                raise TimeoutError
            if not delivered:
                raise RuntimeError("Notification not delivered")
        except Exception as exc:  # noqa: BLE001 - one bounded notification, no loop
            check_current()
            logger.error("Narrator notification failed chat_id=%s stage=NARRATING type=%s",
                         chat_id, type(exc).__name__)

    check_current()
    text = store.get(chat_id).narration_text
    if text is None or not text.strip():
        logger.warning("Narrator missing narration chat_id=%s stage=NARRATING", chat_id)
        await notify(NARRATOR_MISSING_TEXT)
        return

    try:
        async with asyncio.timeout(NARRATING_TIMEOUT_SECONDS) as deadline:
            result = await narrator.render(text)
            check_current()
            if deadline.expired():
                raise TimeoutError
            audio = NarrationAudio.model_validate(result)
            wav_data = normalize_audio(audio)
            async with asyncio.timeout(ENCODING_TIMEOUT_SECONDS) as codec_deadline:
                data = await (encode_audio or encode_voice)(wav_data)
                check_current()
                if codec_deadline.expired() or deadline.expired():
                    raise TimeoutError
                if type(data) is not bytes or not 0 < len(data) <= MAX_AUDIO_BYTES:
                    raise ValueError("Invalid encoded audio")
                await (validate_audio or validate_voice)(data)
                check_current()
            if codec_deadline.expired():
                raise TimeoutError
        check_current()
        if deadline.expired():
            raise TimeoutError
        artifact = narration_dest(chat_id, version, root=root)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(data)
        check_current()
        async with asyncio.timeout(VOICE_SEND_TIMEOUT_SECONDS) as send_deadline:
            delivered = await send_voice(chat_id, artifact)
        check_current()
        if send_deadline.expired():
            raise TimeoutError
        if not delivered:
            raise RuntimeError("Voice not delivered")
        store.set_stage(chat_id, Stage.DONE)
        logger.info("Narrator done chat_id=%s stage=DONE", chat_id)
    except Exception as exc:  # noqa: BLE001 - all new boundaries log metadata only
        check_current()
        logger.error("Narrator failed chat_id=%s stage=NARRATING type=%s",
                     chat_id, type(exc).__name__)
        await notify(NARRATOR_FAILURE_TEXT)
    finally:
        # A reset purges the directory. Never touch replacement-session media.
        if artifact is not None and store.version(chat_id) == version:
            try:
                artifact.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Narrator cleanup failed chat_id=%s type=%s",
                               chat_id, type(exc).__name__)
