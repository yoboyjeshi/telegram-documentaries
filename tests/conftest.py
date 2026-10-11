"""Shared test fixtures."""

from __future__ import annotations

import pytest

from app.session_store import InMemorySessionStore


@pytest.fixture
def store() -> InMemorySessionStore:
    """A fresh in-memory session store."""
    return InMemorySessionStore()
