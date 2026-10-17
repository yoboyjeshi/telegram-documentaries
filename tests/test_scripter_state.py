"""Narration storage belongs to one resettable session."""
import pytest

from app.handlers import handle_restart, handle_start
from app.session_store import InMemorySessionStore
from app.states import Stage
from app.telegram_adapter import ChatInput


def test_new_session_has_no_narration():
    assert InMemorySessionStore().get(1).narration_text is None


@pytest.mark.parametrize("handler", [handle_start, handle_restart])
def test_reset_clears_narration_and_preserves_other_chat(handler, tmp_path):
    store = InMemorySessionStore()
    state = store.get(1)
    state.stage = Stage.NARRATING
    state.narration_text = "A bold creature emerges."
    store.get(2).narration_text = "Another creature waits."
    handler(store, ChatInput(chat_id=1, kind="command", command="restart"),
            media_root=tmp_path)
    assert state.narration_text is None
    assert state.stage is Stage.AWAITING_PHOTO
    assert store.get(2).narration_text == "Another creature waits."
