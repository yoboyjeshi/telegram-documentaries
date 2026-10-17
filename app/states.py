"""Pipeline stages, per-chat session state, and transition rules.

The stage machine is the backbone every later feature advances. Reset to
``GREETING`` is always legal; every other move must be the next ordered step.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Stage(str, Enum):
    """The pipeline stages a chat moves through, in order."""

    GREETING = "GREETING"
    AWAITING_PHOTO = "AWAITING_PHOTO"
    BOUNCER_CHECK = "BOUNCER_CHECK"
    INTERVIEW = "INTERVIEW"
    CONVERTING = "CONVERTING"
    SCRIPTING = "SCRIPTING"
    NARRATING = "NARRATING"
    DONE = "DONE"


PIPELINE_ORDER: tuple[Stage, ...] = (
    Stage.GREETING,
    Stage.AWAITING_PHOTO,
    Stage.BOUNCER_CHECK,
    Stage.INTERVIEW,
    Stage.CONVERTING,
    Stage.SCRIPTING,
    Stage.NARRATING,
    Stage.DONE,
)


class IllegalTransitionError(Exception):
    """Raised when an ordered stage transition is not permitted."""


# Explicit retry edges: the ONLY non-ordered moves allowed besides reset.
# The Bouncer rejecting a photo returns the chat to AWAITING_PHOTO.
RETRY_TRANSITIONS: frozenset[tuple[Stage, Stage]] = frozenset(
    {(Stage.BOUNCER_CHECK, Stage.AWAITING_PHOTO)}
)


def next_stage(current: Stage) -> Stage | None:
    """Return the successor stage, or ``None`` if ``current`` is the last stage."""
    index = PIPELINE_ORDER.index(current)
    if index + 1 >= len(PIPELINE_ORDER):
        return None
    return PIPELINE_ORDER[index + 1]


def can_transition(src: Stage, dst: Stage) -> bool:
    """Reset to ``GREETING``, the ordered step, or an explicit retry edge is legal."""
    if dst is Stage.GREETING:
        return True
    if (src, dst) in RETRY_TRANSITIONS:
        return True
    return next_stage(src) is dst


def assert_transition(src: Stage, dst: Stage) -> None:
    """Raise :class:`IllegalTransitionError` unless ``src -> dst`` is permitted."""
    if not can_transition(src, dst):
        raise IllegalTransitionError(f"Illegal transition: {src.value} -> {dst.value}")


class InterviewAnswer(BaseModel):
    """One completed interview Q/A pair."""

    question: str
    answer: str


class TraitSummary(BaseModel):
    """Concise synthesis of the collected answers (stored for later stages)."""

    summary: str
    traits: list[str] = Field(default_factory=list)


class InterviewState(BaseModel):
    """Progress of one interview: collected Q/A pairs, the pending question, and
    (once complete) the trait summary. One interview per chat."""

    answers: list[InterviewAnswer] = Field(default_factory=list)
    pending_question: str | None = None
    summary: TraitSummary | None = None

    def record(self, answer: str) -> None:
        """Pair the pending question with ``answer``, then clear the pending slot.

        Fails loud if there is no pending question to pair with: recording an
        orphan answer would silently corrupt the interview history.
        """
        if self.pending_question is None:
            raise RuntimeError("Cannot record an interview answer: no pending question.")
        self.answers.append(InterviewAnswer(question=self.pending_question, answer=answer))
        self.pending_question = None


class SessionState(BaseModel):
    """Per-chat session state. One instance per chat id."""

    chat_id: int
    stage: Stage = Stage.GREETING
    interview: InterviewState | None = None
    narration_text: str | None = None
