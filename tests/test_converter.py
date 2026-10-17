"""Tests for the Converter contract, ADK wiring, and lazy build (no network)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from image_fixtures import PNG

from app.config import ConfigurationError
from app.converter import (
    CONVERTER_MODEL,
    AdkConverter,
    LazyConverter,
    _image_bytes_from_events,
)
from app.states import TraitSummary

IMAGE_SENTINEL = PNG
SUMMARY_SENTINEL = "SUMMARY_SENTINEL_bold_explorer"


def _summary() -> TraitSummary:
    return TraitSummary(summary=SUMMARY_SENTINEL, traits=["bold", "sly"])


def _final_event(*parts: object) -> SimpleNamespace:
    return SimpleNamespace(
        is_final_response=lambda: True,
        content=SimpleNamespace(parts=list(parts)),
    )


def _non_final_event(*parts: object) -> SimpleNamespace:
    return SimpleNamespace(
        is_final_response=lambda: False,
        content=SimpleNamespace(parts=list(parts)),
    )


def _text_part(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text, inline_data=None)


def _image_part(data: bytes) -> SimpleNamespace:
    return SimpleNamespace(
        text=None, inline_data=SimpleNamespace(data=data, mime_type="image/png")
    )


def test_converter_model_name() -> None:
    assert CONVERTER_MODEL == "gemini-3.1-flash-image"


def test_adkconverter_requires_a_key() -> None:
    with pytest.raises(ConfigurationError):
        AdkConverter("")


def test_adkconverter_never_logs_the_key(tmp_path: Path, caplog) -> None:
    secret = "SUPER_SECRET_GEMINI_KEY"

    async def failing_runner(portrait_path: Path, summary: TraitSummary) -> bytes:
        logging.getLogger("telegram_documentaries").error(
            "Converter runner failed for %s", portrait_path.name
        )
        raise RuntimeError("gemini down")

    converter = AdkConverter(secret, runner=failing_runner)
    portrait = tmp_path / "photo.jpg"
    portrait.write_bytes(b"portrait")
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError):
        asyncio.run(converter.generate_avatar(portrait, _summary()))

    assert "Converter runner failed" in caplog.text  # a real logging path ran
    assert secret not in caplog.text


def test_adkconverter_delegates_to_injected_runner(tmp_path: Path) -> None:
    seen: list[tuple[Path, TraitSummary]] = []

    async def runner(portrait_path: Path, summary: TraitSummary) -> bytes:
        seen.append((portrait_path, summary))
        return IMAGE_SENTINEL

    portrait = tmp_path / "photo.jpg"
    converter = AdkConverter("key", runner=runner)
    data = asyncio.run(converter.generate_avatar(portrait, _summary()))

    assert data == IMAGE_SENTINEL
    assert seen == [(portrait, _summary())]


def test_adkconverter_propagates_runner_errors(tmp_path: Path) -> None:
    async def runner(portrait_path: Path, summary: TraitSummary) -> bytes:
        raise RuntimeError("gemini down")

    converter = AdkConverter("key", runner=runner)
    with pytest.raises(RuntimeError):
        asyncio.run(converter.generate_avatar(tmp_path / "photo.jpg", _summary()))


def test_adkconverter_runner_bytes_are_never_logged(tmp_path: Path, caplog) -> None:
    async def runner(portrait_path: Path, summary: TraitSummary) -> bytes:
        return IMAGE_SENTINEL

    converter = AdkConverter("key", runner=runner)
    with caplog.at_level(logging.DEBUG):
        asyncio.run(converter.generate_avatar(tmp_path / "photo.jpg", _summary()))

    assert repr(IMAGE_SENTINEL) not in caplog.text
    assert SUMMARY_SENTINEL not in caplog.text


def test_lazy_converter_builds_once_and_reuses(tmp_path: Path) -> None:
    builds = {"n": 0}

    async def runner(portrait_path: Path, summary: TraitSummary) -> bytes:
        return IMAGE_SENTINEL

    def builder(key: str) -> AdkConverter:
        builds["n"] += 1
        return AdkConverter(key, runner=runner)

    converter = LazyConverter(lambda: "key", builder=builder)
    asyncio.run(converter.generate_avatar(tmp_path / "a.jpg", _summary()))
    asyncio.run(converter.generate_avatar(tmp_path / "b.jpg", _summary()))
    assert builds["n"] == 1


def test_lazy_converter_is_key_free_at_construction() -> None:
    calls = {"n": 0}

    def provider() -> str:
        calls["n"] += 1
        return "key"

    LazyConverter(provider)  # constructing must not touch the key provider
    assert calls["n"] == 0


def test_lazy_converter_propagates_missing_key(tmp_path: Path) -> None:
    def provider() -> str:
        raise ConfigurationError("GEMINI_API_KEY is not set.")

    converter = LazyConverter(provider)
    with pytest.raises(ConfigurationError):
        asyncio.run(converter.generate_avatar(tmp_path / "a.jpg", _summary()))


def test_image_bytes_extracted_from_final_event_stream() -> None:
    events = [
        _final_event(_text_part("thinking...")),
        _final_event(_text_part("here it is"), _image_part(IMAGE_SENTINEL)),
    ]

    assert _image_bytes_from_events(events) == IMAGE_SENTINEL


def test_image_bytes_extracted_even_when_only_the_image_part_is_present() -> None:
    events = [_final_event(_image_part(IMAGE_SENTINEL))]

    assert _image_bytes_from_events(events) == IMAGE_SENTINEL


def test_first_inline_image_wins() -> None:
    from image_fixtures import image_bytes
    events = [_final_event(_image_part(IMAGE_SENTINEL), _image_part(image_bytes(color="blue")))]

    assert _image_bytes_from_events(events) == IMAGE_SENTINEL


def test_non_final_events_are_ignored() -> None:
    events = [
        _non_final_event(_image_part(b"partial-progress")),
        _final_event(_image_part(IMAGE_SENTINEL)),
    ]

    assert _image_bytes_from_events(events) == IMAGE_SENTINEL


def test_empty_event_stream_raises() -> None:
    with pytest.raises(RuntimeError):
        _image_bytes_from_events([])


def test_text_only_event_stream_raises() -> None:
    events = [_final_event(_text_part("no image here, sorry"))]

    with pytest.raises(RuntimeError):
        _image_bytes_from_events(events)


def test_event_without_content_is_skipped_and_raises_when_no_image() -> None:
    events = [
        SimpleNamespace(is_final_response=lambda: True, content=None),
        _final_event(_text_part("still no image")),
    ]

    with pytest.raises(RuntimeError):
        _image_bytes_from_events(events)
