"""The INTERVIEW-stage orchestrator.

Owns the ``INTERVIEW``-stage routing and delegates to the typed
:class:`~app.interviewer.Interviewer`. The pure rules live here: one step of
progression per valid answer, commit-only-on-success, and a friendly retry when
a model call fails. Later features (Converter, Scripter, Narrator) extend this
module; nothing happens after ``CONVERTING`` yet.

Logging stays out of the business logic where possible (``@logged`` /
``@guard_errors``); the only direct log lines are counts and failure types --
never answer text, summary text, image bytes, or keys.
"""

from __future__ import annotations

import logging

from app.handlers import (
    INTERVIEW_COMPLETE_TEXT,
    INTERVIEW_FAILURE_TEXT,
    INTERVIEW_INTRO,
    INTERVIEW_PHOTO_REDIRECT_TEXT,
    INTERVIEW_REPROMPT_TEXT,
    INTERVIEW_UNKNOWN_COMMAND_TEXT,
)
from app.interviewer import TOTAL_QUESTIONS, Interviewer
from app.logging_config import guard_errors, logged
from app.session_store import InMemorySessionStore
from app.states import InterviewAnswer, InterviewState, Stage
from app.telegram_adapter import ChatInput

logger = logging.getLogger("telegram_documentaries")


def _log_interviewer_failure(chat_id: int, exc: Exception) -> None:
    """Log an interviewer failure as type + message only.

    A full traceback can embed the request payload (history/answers), so we
    deliberately log no ``exc_info`` and never the raw answer or summary text.
    """
    logger.error(
        "Interviewer failed chat_id=%s: %s: %s", chat_id, type(exc).__name__, exc
    )


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
    a nudge and stays on the current question; blank text re-asks the current
    question. A valid answer only commits once the model call has succeeded.
    """
    chat_id = incoming.chat_id
    state = store.get(chat_id)

    if incoming.kind == "photo":
        return INTERVIEW_PHOTO_REDIRECT_TEXT
    if incoming.kind != "text":
        # Any non-text, non-photo update (including an unknown command) is nudged;
        # /start and /restart are handled before this point in the dispatcher.
        return INTERVIEW_UNKNOWN_COMMAND_TEXT

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
