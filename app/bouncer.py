"""The Bouncer agent: decide whether a photo shows a human face.

The decision contract is a typed Pydantic model, so the multimodal model's
answer is validated at the boundary (a schema, not a regex). Refusals are
canned, deterministic lines -- no extra API call. The Google ADK agent is
imported lazily so importing this module needs no model, network, or key.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from app.config import ConfigurationError

logger = logging.getLogger("telegram_documentaries")

BOUNCER_MODEL = "gemini-3.1-flash-lite"
BOUNCER_APP_NAME = "telegram_documentaries"

BOUNCER_INSTRUCTION = (
    "You are the Bouncer for a wildlife-documentary bot. Inspect the image and "
    "decide whether it clearly shows a human face. Set is_human=true only when a "
    "human face is present; otherwise set it false. Keep 'reason' to a few words."
)

BOUNCER_PROMPT = "Does this image clearly show a human face? Answer using the schema."

REFUSAL_LINES: tuple[str, ...] = (
    (
        "Ah, a magnificent specimen -- but my field notes only cover humans. "
        "Send me a portrait photo of yourself and I'll raise my binoculars."
    ),
    (
        "Splendid creature! Alas, this expedition is strictly for humans. "
        "Send a photo of your face and we'll begin."
    ),
    (
        "My documentary crew specialises in people. Send me a portrait photo and "
        "I'll turn you into wildlife."
    ),
)


def refusal_text(chat_id: int) -> str:
    """A deterministic, warm refusal drawn from :data:`REFUSAL_LINES`."""
    return REFUSAL_LINES[chat_id % len(REFUSAL_LINES)]


class BouncerVerdict(BaseModel):
    """Typed decision from the Bouncer agent (validated at the boundary)."""

    is_human: bool
    reason: str | None = None


class Bouncer(Protocol):
    """Anything that can inspect an image and return a :class:`BouncerVerdict`."""

    async def inspect(self, image_path: Path) -> BouncerVerdict: ...


RunnerFn = Callable[[Path], Awaitable[BouncerVerdict]]


class AdkBouncer:
    """Google ADK ``LlmAgent`` (gemini-3.1-flash-lite, multimodal) Bouncer.

    The API key is held privately and never logged. An explicit ``runner`` can be
    injected in tests, so no network call is ever made there.
    """

    def __init__(
        self,
        api_key: str,
        model: str = BOUNCER_MODEL,
        runner: RunnerFn | None = None,
    ) -> None:
        if not api_key:
            raise ConfigurationError(
                "GEMINI_API_KEY is required for the Bouncer agent. Add it to .env."
            )
        self._api_key = api_key
        self.model = model
        self._runner = runner

    async def inspect(self, image_path: Path) -> BouncerVerdict:
        """Return the verdict, delegating to an injected runner when present."""
        if self._runner is not None:
            return await self._runner(image_path)
        return await self._inspect_with_adk(image_path)

    async def _inspect_with_adk(self, image_path: Path) -> BouncerVerdict:
        # Lazy imports: the rest of the app imports without ADK/a model/a key.
        os.environ.setdefault("GOOGLE_API_KEY", self._api_key)
        from google.adk.agents import LlmAgent
        from google.adk.runners import InMemoryRunner
        from google.genai import types

        agent = LlmAgent(
            name="bouncer",
            model=self.model,
            instruction=BOUNCER_INSTRUCTION,
            output_schema=BouncerVerdict,
        )
        runner = InMemoryRunner(agent=agent, app_name=BOUNCER_APP_NAME)
        user_id = "bouncer"
        session_id = "bouncer"
        await runner.session_service.create_session(
            app_name=BOUNCER_APP_NAME, user_id=user_id, session_id=session_id
        )
        image_part = types.Part.from_bytes(data=image_path.read_bytes(), mime_type="image/jpeg")
        message = types.Content(role="user", parts=[image_part, types.Part(text=BOUNCER_PROMPT)])

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
        return BouncerVerdict.model_validate_json(raw)


class LazyBouncer:
    """Builds an :class:`AdkBouncer` on first use, so startup needs no API key."""

    def __init__(
        self,
        key_provider: Callable[[], str],
        builder: Callable[[str], Bouncer] = AdkBouncer,
    ) -> None:
        self._key_provider = key_provider
        self._builder = builder
        self._delegate: Bouncer | None = None

    async def inspect(self, image_path: Path) -> BouncerVerdict:
        if self._delegate is None:
            self._delegate = self._builder(self._key_provider())
        return await self._delegate.inspect(image_path)
