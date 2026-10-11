"""Per-chat, in-memory session store keyed by ``chat_id`` (always an ``int``)."""

from __future__ import annotations

from app.states import SessionState, Stage, assert_transition


class InMemorySessionStore:
    """Holds one :class:`SessionState` per chat id.

    The store is intentionally in-memory only; persistence is out of scope for
    Feature 1 and no database is used (see ``SPECS/TECH.md``).
    """

    def __init__(self) -> None:
        self._sessions: dict[int, SessionState] = {}

    def get(self, chat_id: int) -> SessionState:
        """Return the state for ``chat_id``, creating a fresh ``GREETING`` state."""
        if chat_id not in self._sessions:
            self._sessions[chat_id] = SessionState(chat_id=chat_id)
        return self._sessions[chat_id]

    def reset(self, chat_id: int) -> SessionState:
        """Return ``chat_id`` to ``GREETING``. Legal from any stage."""
        state = self.get(chat_id)
        state.stage = Stage.GREETING
        return state

    def set_stage(self, chat_id: int, stage: Stage) -> SessionState:
        """Advance ``chat_id`` to ``stage``, enforcing the transition rules."""
        state = self.get(chat_id)
        assert_transition(state.stage, stage)
        state.stage = stage
        return state
