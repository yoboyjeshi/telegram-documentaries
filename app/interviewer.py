"""The Interviewer agent: ask pointed questions, then synthesise the traits.

The decision contracts are typed Pydantic models, so the model's output is
validated at the boundary (a schema, not a regex). The Google ADK agent is
imported lazily so importing this module needs no model, network, or key, and
the API key is held privately and never logged. Tests inject fake runners, so
no network call is ever made there.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol, TypeVar

from pydantic import BaseModel

from app.config import ConfigurationError, ensure_google_api_key
from app.states import InterviewAnswer, TraitSummary

logger = logging.getLogger("telegram_documentaries")

INTERVIEWER_MODEL = "gemini-3.1-flash-lite"
INTERVIEWER_APP_NAME = "telegram_documentaries"

# Fixed interview length, inside the 5-7 question requirement.
TOTAL_QUESTIONS = 6

QUESTION_INSTRUCTION = (
    "You are the Interviewer for a wildlife-documentary bot. Ask the user ONE "
    "pointed, friendly question about their quirks, daily habits, or personality. "
    "Do not repeat or reword a question that has already been asked. Ask exactly "
    "one short question, ending with a question mark."
)

SUMMARY_INSTRUCTION = (
    "You are the Interviewer for a wildlife-documentary bot. Synthesise the "
    "user's answers into a concise trait summary. Put one or two warm, vivid "
    "sentences in 'summary' and a short list of key adjectives in 'traits'. "
    "Do not invent facts the answers do not support."
)


class InterviewQuestion(BaseModel):
    """Typed question from the Interviewer agent (validated at the boundary)."""

    text: str


class Interviewer(Protocol):
    """Anything that can ask the next question and summarise the answers."""

    async def next_question(self, history: Sequence[InterviewAnswer]) -> InterviewQuestion: ...

    async def summarise(self, history: Sequence[InterviewAnswer]) -> TraitSummary: ...


QuestionRunner = Callable[[Sequence[InterviewAnswer]], Awaitable[InterviewQuestion]]
SummaryRunner = Callable[[Sequence[InterviewAnswer]], Awaitable[TraitSummary]]

SchemaT = TypeVar("SchemaT", bound=BaseModel)


def _format_history(history: Sequence[InterviewAnswer]) -> str:
    """Render the Q/A history as plain text for the model (never logged)."""
    if not history:
        return "(no answers yet -- this is the first question)"
    return "\n".join(f"Q: {pair.question}\nA: {pair.answer}" for pair in history)


class AdkInterviewer:
    """Google ADK ``LlmAgent`` Interviewer (gemini-3.1-flash-lite).

    The API key is held privately and never logged. Explicit ``question_runner``
    and ``summary_runner`` seams can be injected in tests, so no network call is
    ever made there.
    """

    def __init__(
        self,
        api_key: str,
        model: str = INTERVIEWER_MODEL,
        question_runner: QuestionRunner | None = None,
        summary_runner: SummaryRunner | None = None,
    ) -> None:
        if not api_key:
            raise ConfigurationError(
                "GEMINI_API_KEY is required for the Interviewer agent. Add it to .env."
            )
        self._api_key = api_key
        self.model = model
        self._question_runner = question_runner
        self._summary_runner = summary_runner

    async def next_question(self, history: Sequence[InterviewAnswer]) -> InterviewQuestion:
        """Return the next question, delegating to an injected runner when present."""
        if self._question_runner is not None:
            return await self._question_runner(history)
        return await self._next_question_with_adk(history)

    async def summarise(self, history: Sequence[InterviewAnswer]) -> TraitSummary:
        """Return the trait summary, delegating to an injected runner when present."""
        if self._summary_runner is not None:
            return await self._summary_runner(history)
        return await self._summarise_with_adk(history)

    async def _next_question_with_adk(
        self, history: Sequence[InterviewAnswer]
    ) -> InterviewQuestion:
        prompt = (
            f"Here is the interview so far:\n{_format_history(history)}\n\n"
            "Ask the next question using the schema."
        )
        return await self._run_adk(
            agent_name="interviewer_question",
            instruction=QUESTION_INSTRUCTION,
            output_schema=InterviewQuestion,
            session_prefix="interview-question",
            prompt=prompt,
        )

    async def _summarise_with_adk(self, history: Sequence[InterviewAnswer]) -> TraitSummary:
        prompt = (
            f"Here are the user's interview answers:\n{_format_history(history)}\n\n"
            "Write the trait summary using the schema."
        )
        return await self._run_adk(
            agent_name="interviewer_summary",
            instruction=SUMMARY_INSTRUCTION,
            output_schema=TraitSummary,
            session_prefix="interview-summary",
            prompt=prompt,
        )

    async def _run_adk(
        self,
        *,
        agent_name: str,
        instruction: str,
        output_schema: type[SchemaT],
        session_prefix: str,
        prompt: str,
    ) -> SchemaT:
        # Lazy imports: the rest of the app imports without ADK/a model/a key.
        ensure_google_api_key(self._api_key)
        from google.adk.agents import LlmAgent
        from google.adk.runners import InMemoryRunner
        from google.genai import types

        agent = LlmAgent(
            name=agent_name,
            model=self.model,
            instruction=instruction,
            output_schema=output_schema,
        )
        runner = InMemoryRunner(agent=agent, app_name=INTERVIEWER_APP_NAME)
        user_id = "interviewer"
        # A fresh session id per call keeps each independent turn isolated.
        session_id = f"{session_prefix}-{uuid.uuid4().hex}"
        await runner.session_service.create_session(
            app_name=INTERVIEWER_APP_NAME, user_id=user_id, session_id=session_id
        )
        message = types.Content(role="user", parts=[types.Part(text=prompt)])

        raw = ""
        async for event in runner.run_async(
            user_id=user_id, session_id=session_id, new_message=message
        ):
            if not event.is_final_response():
                continue
            content = event.content
            if content is None or not content.parts:
                continue
            text = "".join(part.text or "" for part in content.parts if part.text)
            if text.strip():
                raw = text
        return output_schema.model_validate_json(raw)


class LazyInterviewer:
    """Builds an :class:`AdkInterviewer` on first use, so startup needs no key."""

    def __init__(
        self,
        key_provider: Callable[[], str],
        builder: Callable[[str], Interviewer] = AdkInterviewer,
    ) -> None:
        self._key_provider = key_provider
        self._builder = builder
        self._delegate: Interviewer | None = None

    def _get_delegate(self) -> Interviewer:
        if self._delegate is None:
            self._delegate = self._builder(self._key_provider())
        return self._delegate

    async def next_question(self, history: Sequence[InterviewAnswer]) -> InterviewQuestion:
        return await self._get_delegate().next_question(history)

    async def summarise(self, history: Sequence[InterviewAnswer]) -> TraitSummary:
        return await self._get_delegate().summarise(history)
