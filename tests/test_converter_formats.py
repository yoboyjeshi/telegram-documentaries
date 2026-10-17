"""Reject non-images and preserve supported image formats at the boundary."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import orchestrator
from app.converter import _image_bytes_from_events
from app.handlers import CONVERTER_FAILURE_TEXT
from app.media import photo_dest
from app.session_store import InMemorySessionStore
from app.states import InterviewState, Stage, TraitSummary


@pytest.mark.parametrize('mime,data', [('audio/wav', b'audio'), ('image/gif', b'GIF89a'),
                                      ('image/png', b'not a png'), ('image/jpeg', b'\xff\xd8broken')])
def test_bad_inline_image_is_rejected(mime, data):
    event = SimpleNamespace(is_final_response=lambda: True, content=SimpleNamespace(parts=[
        SimpleNamespace(inline_data=SimpleNamespace(mime_type=mime, data=data))]))
    with pytest.raises(RuntimeError):
        _image_bytes_from_events([event])


def test_invalid_runner_bytes_never_reach_telegram(tmp_path):
    store = InMemorySessionStore()
    state = store.get(1)
    state.stage = Stage.CONVERTING
    state.interview = InterviewState(summary=TraitSummary(summary='bold'))
    p = photo_dest(1, root=tmp_path)
    p.parent.mkdir(parents=True)
    p.write_bytes(b'portrait')
    converter, send = AsyncMock(), AsyncMock(return_value=True)
    converter.generate_avatar.return_value = b'not an image'
    result = asyncio.run(orchestrator.handle_converting_update(
        store, 1, converter=converter, send_photo=send, media_root=tmp_path))
    assert result == CONVERTER_FAILURE_TEXT
    assert store.get(1).stage is Stage.CONVERTING
    send.assert_not_awaited()


@pytest.mark.parametrize('fmt,suffix', [('PNG', '.png'), ('JPEG', '.jpg')])
def test_real_image_saved_with_matching_extension(tmp_path, fmt, suffix):
    from image_fixtures import image_bytes
    store = InMemorySessionStore()
    state = store.get(1)
    state.stage = Stage.CONVERTING
    state.interview = InterviewState(summary=TraitSummary(summary='bold'))
    p = photo_dest(1, root=tmp_path)
    p.parent.mkdir(parents=True)
    p.write_bytes(b'portrait')
    data = image_bytes(fmt)
    converter = AsyncMock()
    converter.generate_avatar.return_value = data
    async def send(chat, path, caption):
        assert path.suffix == suffix
        assert path.read_bytes() == data
        return True
    result = asyncio.run(orchestrator.handle_converting_update(
        store, 1, converter=converter, send_photo=send, media_root=tmp_path))
    assert result is None
    assert store.get(1).stage is Stage.SCRIPTING


def test_real_adk_wiring_is_exercised_offline(tmp_path, monkeypatch):
    import sys

    from image_fixtures import JPEG

    import app.converter as module
    from app.converter import CONVERTER_MODEL, AdkConverter
    seen = {}
    class Agent:
        def __init__(self, **kwargs):
            seen['agent'] = kwargs
    class Runner:
        def __init__(self, **kwargs):
            self.session_service = SimpleNamespace(create_session=AsyncMock())
        async def run_async(self, **kwargs):
            seen['message'] = kwargs['new_message']
            yield SimpleNamespace(is_final_response=lambda: True, content=SimpleNamespace(parts=[
                SimpleNamespace(inline_data=SimpleNamespace(mime_type='image/jpeg', data=JPEG))]))
    class Part:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
        @staticmethod
        def from_bytes(**kwargs):
            return SimpleNamespace(**kwargs)
    types = SimpleNamespace(Part=Part, Content=lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setitem(sys.modules, 'google.adk.agents', SimpleNamespace(LlmAgent=Agent))
    monkeypatch.setitem(sys.modules, 'google.adk.runners', SimpleNamespace(InMemoryRunner=Runner))
    monkeypatch.setitem(sys.modules, 'google.genai', SimpleNamespace(types=types))
    monkeypatch.setattr(module, 'ensure_google_api_key', lambda key: None)
    path = tmp_path/'portrait.jpg'
    path.write_bytes(JPEG)
    summary = TraitSummary(summary='bold explorer', traits=['curious'])
    result = asyncio.run(AdkConverter('dummy').generate_avatar(path, summary))
    assert result == JPEG
    assert seen['agent']['model'] == CONVERTER_MODEL
    assert seen['message'].parts[0].data == JPEG
    assert 'curious' in seen['message'].parts[1].text


def test_declared_mime_must_match_decoded_format():
    from image_fixtures import JPEG
    event = SimpleNamespace(is_final_response=lambda: True, content=SimpleNamespace(parts=[
        SimpleNamespace(inline_data=SimpleNamespace(mime_type='image/png', data=JPEG))]))
    with pytest.raises(RuntimeError):
        _image_bytes_from_events([event])
