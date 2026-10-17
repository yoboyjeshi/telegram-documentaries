"""Early-stage workers: bounded photo intake and ownership-guarded interview turns.

The sequential update handlers in :mod:`app.bot` only schedule these coroutines
on the single :class:`~app.conversion_tasks.ConversionTasks` registry, so a
reset never waits on a model call. Every worker re-checks ownership (session
version, expected stage and its own cancellation state) after each external
await and immediately before any send, filesystem publication or state commit.

Error logging here is metadata only: operation, chat id, stage and exception
type. Raw exception text, tracebacks, portraits, questions and answers are
never logged, because provider errors can embed the request payload.
"""

from __future__ import annotations

import asyncio
import logging
import warnings
from collections.abc import Awaitable, Callable
from io import BytesIO
from pathlib import Path
from typing import TypeVar

from PIL import Image

from app.bouncer import Bouncer, refusal_text
from app.handlers import (
    BOUNCER_FAILURE_TEXT,
    INTERVIEW_COMPLETE_TEXT,
    INTERVIEW_FAILURE_TEXT,
    INTERVIEW_INTRO,
    INTERVIEW_REPROMPT_TEXT,
    PHOTO_DOWNLOAD_FAILURE_TEXT,
)
from app.interviewer import TOTAL_QUESTIONS, Interviewer
from app.media import MEDIA_ROOT, photo_dest
from app.session_store import InMemorySessionStore
from app.states import InterviewAnswer, InterviewState, Stage
from app.telegram_adapter import ChatInput

logger = logging.getLogger("telegram_documentaries")

# Application-level budgets, not SDK retry loops. The user's thinking time is
# never bounded: these only cover one external operation each.
PHOTO_ACQUISITION_TIMEOUT_SECONDS = 30
BOUNCER_TIMEOUT_SECONDS = 60
INTERVIEW_TIMEOUT_SECONDS = 60
EARLY_SEND_TIMEOUT_SECONDS = 30

MAX_PORTRAIT_BYTES = 10 * 1024 * 1024
MAX_PORTRAIT_PIXELS = 20_000_000

T = TypeVar("T")

DownloadBytesFn = Callable[[str], Awaitable[bytes]]
SendMessageFn = Callable[[int, str], Awaitable[bool]]
ContinueFn = Callable[[int], Awaitable[None]]


class PortraitRejected(Exception):
    """An intake attempt's bytes are not an admissible portrait.

    Carries a short, fixed reason code only -- never the rejected bytes.
    """


def validate_portrait_bytes(data: bytes) -> bytes:
    """Admit only non-empty, capped, genuinely decodable JPEG bytes.

    The admission cap is applied to the actual received length, so advertised
    metadata that is missing or lying cannot widen it. Downstream agents are
    told ``image/jpeg``, so the format is verified here rather than assumed.
    """
    if not data:
        raise PortraitRejected("empty")
    if len(data) > MAX_PORTRAIT_BYTES:
        raise PortraitRejected("oversize")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.format != "JPEG":
                    raise PortraitRejected("format")
                if image.width * image.height > MAX_PORTRAIT_PIXELS:
                    raise PortraitRejected("dimensions")
                image.verify()
            with Image.open(BytesIO(data)) as image:
                image.load()
    except PortraitRejected:
        raise
    except Exception:  # noqa: BLE001 - untrusted decoder errors become a safe reason code
        raise PortraitRejected("undecodable") from None
    return data


class _Ownership:
    """Session-version, stage and cancellation ownership for one worker."""

    def __init__(
        self, store: InMemorySessionStore, chat_id: int, version: int, *stages: Stage
    ) -> None:
        self.store = store
        self.chat_id = chat_id
        self.version = version
        self.stages: tuple[Stage, ...] = stages

    def expect(self, *stages: Stage) -> None:
        self.stages = stages

    def owned(self) -> bool:
        task = asyncio.current_task()
        return (
            self.store.version(self.chat_id) == self.version
            and self.store.get(self.chat_id).stage in self.stages
            and not (task is not None and task.cancelling())
        )

    def check(self) -> None:
        """Abandon retired work: a late result must never act on a new session."""
        if not self.owned():
            raise asyncio.CancelledError


async def _bounded(awaitable: Awaitable[T], budget: float) -> T:
    """Await one external operation under an explicit, checked deadline."""
    async with asyncio.timeout(budget) as deadline:
        result = await awaitable
    if deadline.expired():
        raise TimeoutError
    return result


async def _deliver(
    guard: _Ownership, send_message: SendMessageFn, chat_id: int, text: str
) -> None:
    """Send one bounded, ownership-guarded message or raise."""
    guard.check()
    async with asyncio.timeout(EARLY_SEND_TIMEOUT_SECONDS) as deadline:
        delivered = await send_message(chat_id, text)
    guard.check()
    if deadline.expired():
        raise TimeoutError
    if not delivered:
        raise RuntimeError("Message not delivered")


async def _notify(
    guard: _Ownership, send_message: SendMessageFn, chat_id: int, text: str, operation: str
) -> None:
    """One bounded failure notice; never a retry loop, never for cancellation."""
    try:
        await _deliver(guard, send_message, chat_id, text)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a failed notice must not mask the failure
        logger.error(
            "operation=%s_notice chat_id=%s stage=%s type=%s",
            operation, chat_id, _stage_name(guard), type(exc).__name__,
        )


def _stage_name(guard: _Ownership) -> str:
    return guard.store.get(guard.chat_id).stage.value


def _discard_owned_portrait(guard: _Ownership, dest: Path) -> None:
    """Remove this attempt's portrait, never a replacement session's file."""
    if guard.store.version(guard.chat_id) != guard.version:
        return
    try:
        dest.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "operation=portrait_cleanup chat_id=%s type=%s", guard.chat_id, type(exc).__name__
        )


async def _recover_to_awaiting_photo(
    guard: _Ownership, send_message: SendMessageFn, text: str, operation: str
) -> None:
    """Return the owning session to photo intake and send one bounded notice."""
    if not guard.owned():
        return
    guard.store.set_stage(guard.chat_id, Stage.AWAITING_PHOTO)
    guard.expect(Stage.AWAITING_PHOTO)
    await _notify(guard, send_message, guard.chat_id, text, operation)


async def _deliver_first_question(
    guard: _Ownership,
    *,
    interviewer: Interviewer,
    send_message: SendMessageFn,
) -> bool:
    """Ask question 1 and only then record it, so a failed send stays retryable."""
    chat_id = guard.chat_id
    try:
        question = await _bounded(interviewer.next_question([]), INTERVIEW_TIMEOUT_SECONDS)
        guard.check()
        await _deliver(guard, send_message, chat_id, f"{INTERVIEW_INTRO}\n\n{question.text}")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the user keeps a retryable interview
        logger.error(
            "operation=interview_question chat_id=%s stage=INTERVIEW type=%s",
            chat_id, type(exc).__name__,
        )
        await _notify(guard, send_message, chat_id, INTERVIEW_FAILURE_TEXT, "interview_question")
        return False
    # Confirmed delivery: commit with no further await.
    guard.check()
    guard.store.get(chat_id).interview = InterviewState(pending_question=question.text)
    logger.info("operation=interview_started chat_id=%s answered=0/%d", chat_id, TOTAL_QUESTIONS)
    return True


async def run_photo_intake(
    store: InMemorySessionStore,
    incoming: ChatInput,
    *,
    bouncer: Bouncer,
    download_bytes: DownloadBytesFn,
    interviewer: Interviewer,
    send_message: SendMessageFn,
    media_root: Path | None = None,
    session_version: int | None = None,
) -> None:
    """Acquire, admit and publish one portrait, then run the Bouncer and question 1.

    The downloader is handed a ``file_id`` only: it owns memory, never the
    shared ``photo.jpg`` path, so a cancellation-resistant download cannot
    recreate media after a reset. Photo acceptance and question 1 stay in this
    same worker.
    """
    chat_id = incoming.chat_id
    version = store.version(chat_id) if session_version is None else session_version
    root = media_root or MEDIA_ROOT
    dest = photo_dest(chat_id, root=root)
    guard = _Ownership(store, chat_id, version, Stage.AWAITING_PHOTO)

    published = False

    guard.check()
    store.set_stage(chat_id, Stage.BOUNCER_CHECK)
    guard.expect(Stage.BOUNCER_CHECK)

    if not incoming.file_id:
        logger.warning("operation=photo_acquisition chat_id=%s reason=missing_file_id", chat_id)
        await _recover_to_awaiting_photo(
            guard, send_message, PHOTO_DOWNLOAD_FAILURE_TEXT, "photo_acquisition"
        )
        return

    try:
        data = await _bounded(
            download_bytes(incoming.file_id), PHOTO_ACQUISITION_TIMEOUT_SECONDS
        )
        guard.check()
        portrait = validate_portrait_bytes(data if isinstance(data, bytes) else bytes(data))
        # Last ownership check, then publication: deliberately no await between them.
        guard.check()
        published = True
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(portrait)
    except asyncio.CancelledError:
        if published:
            _discard_owned_portrait(guard, dest)
        raise
    except Exception as exc:  # noqa: BLE001 - rejected intake stays a friendly retry
        logger.error(
            "operation=photo_acquisition chat_id=%s stage=BOUNCER_CHECK type=%s",
            chat_id, type(exc).__name__,
        )
        if published:
            # A partially written portrait must never reach the Bouncer or media.
            _discard_owned_portrait(guard, dest)
        await _recover_to_awaiting_photo(
            guard, send_message, PHOTO_DOWNLOAD_FAILURE_TEXT, "photo_acquisition"
        )
        return

    accepted = False
    try:
        try:
            verdict = await _bounded(bouncer.inspect(dest), BOUNCER_TIMEOUT_SECONDS)
            guard.check()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any Bouncer error is a friendly retry
            logger.error(
                "operation=bouncer chat_id=%s stage=BOUNCER_CHECK type=%s",
                chat_id, type(exc).__name__,
            )
            await _recover_to_awaiting_photo(guard, send_message, BOUNCER_FAILURE_TEXT, "bouncer")
            return

        logger.info("operation=bouncer chat_id=%s is_human=%s", chat_id, verdict.is_human)
        if not verdict.is_human:
            await _recover_to_awaiting_photo(
                guard, send_message, refusal_text(chat_id), "bouncer_refusal"
            )
            return

        guard.check()
        accepted = True
        store.set_stage(chat_id, Stage.INTERVIEW)
        guard.expect(Stage.INTERVIEW)
        await _deliver_first_question(guard, interviewer=interviewer, send_message=send_message)
    finally:
        if not accepted:
            # Covers refusal, Bouncer failure and shutdown cancellation alike;
            # the version check inside keeps a replacement session's file.
            _discard_owned_portrait(guard, dest)


async def run_interview_turn(
    store: InMemorySessionStore,
    incoming: ChatInput,
    *,
    interviewer: Interviewer,
    send_message: SendMessageFn,
    session_version: int | None = None,
    on_complete: ContinueFn | None = None,
) -> None:
    """Run one interview turn transactionally: generate, send, then commit.

    The candidate answer is staged locally, so an unsuccessful question or
    acknowledgement delivery leaves the previous accepted conversation exactly
    as it was and the user can retry without double-recording an answer. The
    sixth answer continues into ``on_complete`` inside this same worker.
    """
    chat_id = incoming.chat_id
    version = store.version(chat_id) if session_version is None else session_version
    guard = _Ownership(store, chat_id, version, Stage.INTERVIEW)

    guard.check()
    interview = store.get(chat_id).interview
    if interview is None or interview.pending_question is None:
        # Self-heal: no live question (for example a failed question 1) asks again.
        await _deliver_first_question(guard, interviewer=interviewer, send_message=send_message)
        return

    text = incoming.text or ""
    if not text.strip():
        await _notify(guard, send_message, chat_id, INTERVIEW_REPROMPT_TEXT, "interview_reprompt")
        return

    pending = interview.pending_question
    history = [*interview.answers, InterviewAnswer(question=pending, answer=text)]
    completing = len(history) >= TOTAL_QUESTIONS
    operation = "interview_summary" if completing else "interview_question"
    summary = None
    question = None

    try:
        if completing:
            summary = await _bounded(interviewer.summarise(history), INTERVIEW_TIMEOUT_SECONDS)
        else:
            question = await _bounded(
                interviewer.next_question(history), INTERVIEW_TIMEOUT_SECONDS
            )
        guard.check()
        await _deliver(
            guard, send_message, chat_id,
            INTERVIEW_COMPLETE_TEXT if completing else question.text,  # type: ignore[union-attr]
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the pending question and answers survive
        logger.error(
            "operation=%s chat_id=%s stage=INTERVIEW type=%s",
            operation, chat_id, type(exc).__name__,
        )
        await _notify(guard, send_message, chat_id, INTERVIEW_FAILURE_TEXT, operation)
        return

    # Confirmed delivery: commit the staged turn with no further await.
    guard.check()
    live = store.get(chat_id).interview
    if live is not interview or live.pending_question != pending:
        logger.warning("operation=%s chat_id=%s reason=stale_turn", operation, chat_id)
        return

    interview.record(text)
    if completing:
        interview.summary = summary
        store.set_stage(chat_id, Stage.CONVERTING)
        logger.info(
            "operation=interview_complete chat_id=%s answered=%d/%d",
            chat_id, len(interview.answers), TOTAL_QUESTIONS,
        )
        if on_complete is not None:
            # Same worker and session version through every downstream stage.
            await on_complete(version)
        return

    interview.pending_question = question.text  # type: ignore[union-attr]
    logger.info(
        "operation=interview_answered chat_id=%s answered=%d/%d",
        chat_id, len(interview.answers), TOTAL_QUESTIONS,
    )
