"""Exercise real ADK and SDK serialization with only HTTP replaced offline."""
import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from app.scripter import SCRIPTER_MODEL, AdkScripter, NarrationScript
from app.states import TraitSummary

TEXT = "Here we observe a quietly determined creature surveying its domain."


def test_real_adk_request_uses_supported_gemini_schema(monkeypatch):
    from google import genai
    from google.adk.models.google_llm import Gemini
    from google.genai import types

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"candidates": [{
            "content": {"role": "model", "parts": [{"text": json.dumps({"text": TEXT})}]},
            "finishReason": "STOP",
        }]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = genai.Client(api_key="offline-key", vertexai=False, http_options=types.HttpOptions(
                httpx_async_client=http, retry_options=types.HttpRetryOptions(attempts=1)))
            monkeypatch.setattr(Gemini, "api_client", property(lambda self: client))
            monkeypatch.setattr("app.scripter.ensure_google_api_key", lambda key: None)
            try:
                return await AdkScripter("offline-key").write_script(
                    TraitSummary(summary="determined", traits=["curious"]))
            finally:
                await client.aio.aclose()
                client.close()

    assert asyncio.run(run()).text == TEXT
    assert len(requests) == 1
    assert SCRIPTER_MODEL in str(requests[0].url)
    config = json.loads(requests[0].content)["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    schema = config["responseSchema"]
    assert schema["type"].upper() == "OBJECT"
    assert schema["required"] == ["text"]
    assert set(schema["properties"]) == {"text"}
    assert schema["properties"]["text"]["type"].upper() == "STRING"
    assert "additional_properties" not in schema
    assert "additionalProperties" not in schema


@pytest.mark.parametrize("payload", [
    {"text": TEXT, "unexpected": "must reject"},
    {"text": 123},
    {"text": "word " * 91},
    {"text": "First line.\nSecond line."},
])
def test_wire_schema_compatibility_does_not_relax_local_validation(payload):
    with pytest.raises(ValidationError):
        NarrationScript.model_validate(payload)
