"""Check the installed ADK accepts the real schema without contacting Gemini."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.scripter import SCRIPTER_MODEL, AdkScripter, NarrationScript
from app.states import TraitSummary


def test_installed_adk_agent_and_genai_content_are_compatible(monkeypatch):
    import google.adk.runners
    from google.adk.agents import LlmAgent
    from google.genai import types

    text = "Here we observe a quietly determined creature, ready for the evening."
    captured = []
    class OfflineRunner:
        def __init__(self, *, agent, app_name):
            assert isinstance(agent, LlmAgent)
            assert agent.model == SCRIPTER_MODEL
            assert agent.output_schema is NarrationScript
            assert agent.tools == []
            captured.append(agent)
            self.session_service = SimpleNamespace(create_session=AsyncMock())
        async def run_async(self, *, user_id, session_id, new_message):
            assert isinstance(new_message, types.Content)
            assert new_message.parts[0].text
            assert "UNTRUSTED" in new_message.parts[0].text
            yield SimpleNamespace(is_final_response=lambda: True,
                                  content=types.Content(parts=[types.Part(
                                      text=json.dumps({"text": text}))]))
    monkeypatch.setattr(google.adk.runners, "InMemoryRunner", OfflineRunner)
    monkeypatch.setattr("app.scripter.ensure_google_api_key", lambda key: None)
    result = asyncio.run(AdkScripter("offline").write_script(TraitSummary(summary="determined")))
    assert result.text == text
    assert len(captured) == 1
