"""Tests for the Bouncer contract, refusals, and ADK wiring (no network)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.bouncer import (
    BOUNCER_MODEL,
    REFUSAL_LINES,
    AdkBouncer,
    BouncerVerdict,
    LazyBouncer,
    refusal_text,
)
from app.config import ConfigurationError


def test_verdict_defaults_reason_to_none() -> None:
    verdict = BouncerVerdict(is_human=True)
    assert verdict.is_human is True
    assert verdict.reason is None


def test_refusal_pool_is_non_empty_and_asks_for_a_photo() -> None:
    assert REFUSAL_LINES
    assert len(REFUSAL_LINES) >= 2
    for line in REFUSAL_LINES:
        assert line.strip()
        lowered = line.lower()
        assert any(word in lowered for word in ("photo", "portrait", "picture"))


def test_refusal_text_is_deterministic_and_from_the_pool() -> None:
    assert refusal_text(42) == refusal_text(42)
    assert refusal_text(42) in REFUSAL_LINES
    assert all(refusal_text(chat) in REFUSAL_LINES for chat in range(len(REFUSAL_LINES) * 2))


def test_bouncer_model_name() -> None:
    assert BOUNCER_MODEL == "gemini-3.1-flash-lite"


def test_adkbouncer_requires_a_key() -> None:
    with pytest.raises(ConfigurationError):
        AdkBouncer("")


def test_adkbouncer_never_logs_the_key(caplog) -> None:
    secret = "SUPER_SECRET_GEMINI_KEY"
    AdkBouncer(secret)
    assert secret not in caplog.text


def test_adkbouncer_delegates_to_injected_runner(tmp_path: Path) -> None:
    async def runner(image_path: Path) -> BouncerVerdict:
        return BouncerVerdict(is_human=True, reason="a face")

    bouncer = AdkBouncer("key", runner=runner)
    verdict = asyncio.run(bouncer.inspect(tmp_path / "photo.jpg"))
    assert verdict.is_human is True
    assert verdict.reason == "a face"


def test_adkbouncer_propagates_runner_errors(tmp_path: Path) -> None:
    async def runner(image_path: Path) -> BouncerVerdict:
        raise RuntimeError("gemini down")

    bouncer = AdkBouncer("key", runner=runner)
    with pytest.raises(RuntimeError):
        asyncio.run(bouncer.inspect(tmp_path / "photo.jpg"))


def test_lazy_bouncer_builds_once_and_reuses(tmp_path: Path) -> None:
    builds = {"n": 0}

    async def runner(image_path: Path) -> BouncerVerdict:
        return BouncerVerdict(is_human=True)

    def builder(key: str) -> AdkBouncer:
        builds["n"] += 1
        return AdkBouncer(key, runner=runner)

    bouncer = LazyBouncer(lambda: "key", builder=builder)
    asyncio.run(bouncer.inspect(tmp_path / "a.jpg"))
    asyncio.run(bouncer.inspect(tmp_path / "b.jpg"))
    assert builds["n"] == 1


def test_lazy_bouncer_propagates_missing_key(tmp_path: Path) -> None:
    def provider() -> str:
        raise ConfigurationError("GEMINI_API_KEY is not set.")

    bouncer = LazyBouncer(provider)
    with pytest.raises(ConfigurationError):
        asyncio.run(bouncer.inspect(tmp_path / "a.jpg"))
