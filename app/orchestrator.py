"""The INTERVIEW, CONVERTING and SCRIPTING-stage orchestrator.

Owns the ``INTERVIEW``-stage routing and delegates to the typed
:class:`~app.interviewer.Interviewer` and :class:`~app.converter.Converter`.
The pure rules live here: one step of progression per valid answer,
commit-only-on-success, and a friendly retry when a model call fails. The
``CONVERTING``-stage step generates one hybrid-animal avatar, sends it, and
advances to ``SCRIPTING``. The scripting step sends validated narration and
advances to ``NARRATING``; no audio work runs yet.

Converter and Scripter errors log only metadata, never model payloads or raw
exceptions. Earlier Interviewer logging is unchanged and remains Feature 7 work.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import ValidationError

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
    SCRIPTER_FAILURE_TEXT,
    SCRIPTER_MISSING_SUMMARY_TEXT,
)
from app.image_format import image_suffix
from app.interviewer import TOTAL_QUESTIONS, Interviewer
from app.logging_config import guard_errors, logged
from app.media import MEDIA_ROOT, avatar_dest, photo_dest
from app.scripter import NarrationScript, Scripter
from app.session_store import InMemorySessionStore
from app.states import InterviewAnswer, InterviewState, Stage
from app.telegram_adapter import ChatInput

logger = logging.getLogger("telegram_documentaries")

# A capped, single-line message keeps secret/answer-bearing payloads out of logs;
# only used for non-validation failures (a ValidationError is logged by type).
_FAILURE_MESSAGE_MAX = 200

# The photo-send capability injected from bot.py (mirrors photo_flow's DownloadFn).
SendPhotoFn = Callable[[int, Path, str], Awaitable[bool]]


def _log_interviewer_failure(chat_id: int, exc: Exception) -> None:
    """Log an interviewer failure as type (+ short message) only.

    A full traceback can embed the request payload (history/answers), so we
    deliberately log no ``exc_info`` and never the raw answer or summary text.
    A ``ValidationError`` is logged as its type alone: its message embeds
    ``input_value`` -- the raw model output, which can carry answer/summary text.
    """
    if isinstance(exc, ValidationError):
        logger.error("Interviewer failed chat_id=%s: %s", chat_id, type(exc).__name__)
        return
    logger.error(
        "Interviewer failed chat_id=%s: %s: %s",
        chat_id,
        type(exc).__name__,
        str(exc)[:_FAILURE_MESSAGE_MAX],
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
