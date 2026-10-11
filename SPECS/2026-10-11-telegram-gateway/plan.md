# Plan — Feature 1: Telegram Gateway

**Method:** Red/Green TDD. For each task group: write the failing test first (Red), implement the minimum to pass (Green), then tidy. Run the dev scripts **`scripts/test`** (pytest) and **`scripts/hooks`** (ruff + mypy) after each group. Keep production code free of logging/business-logic mixing (use decorators).

**Target layout**
```
app/
  __init__.py
  main.py            # entrypoint: load settings, build app, run long polling
  config.py          # python-dotenv + Pydantic Settings
  logging_config.py  # logging setup + @logged + @guard_errors decorators
  states.py          # Stage enum, SessionState, transition helpers
  session_store.py   # InMemorySessionStore (keyed by int chat_id)
  telegram_adapter.py# extract_chat_input(update) -> ChatInput
  handlers.py        # pure handlers: handle_start / handle_restart / handle_text
  bot.py             # PTB wiring: safe_send_message, handler registration, error handler
tests/
  conftest.py
  test_config.py
  test_states.py
  test_session_store.py
  test_handlers.py
  test_adapter.py
scripts/
  test               # runs pytest
  hooks              # runs ruff + mypy
pyproject.toml
requirements.txt
```

---

## Task group 1 — Scaffolding & tooling
1.1 Create `app/`, `tests/`, `scripts/`, `__init__.py` files.
1.2 `requirements.txt`: pin `python-telegram-bot`, `pydantic`, `python-dotenv`, `pytest`, `pytest-asyncio`, `ruff`, `mypy`.
1.3 `pyproject.toml`: ruff + mypy config; pytest config.
1.4 `scripts/test` → runs pytest; `scripts/hooks` → runs ruff + mypy. Make executable.
1.5 Confirm `.gitignore` already covers `.venv/`, `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`.
1.6 Run `scripts/test` (expect "no tests collected" / pass) and `scripts/hooks`.

## Task group 2 — Config + logging foundation (Red/Green)
2.1 **Red:** `tests/test_config.py` — missing `TELEGRAM_BOT_TOKEN` raises a clear error; a present token loads and its value is masked in `repr`/logs (`SecretStr`).
2.2 **Green:** `app/config.py` — `Settings(BaseModel)` with `from_env()` that calls `load_dotenv()` and validates; fail loud with a message that never contains the value.
2.3 **Red:** test that `@logged` emits a record containing handler name and `chat_id` but not the secret.
2.4 **Green:** `app/logging_config.py` — `configure_logging()`, `@logged`, `@guard_errors` (logs traceback, returns fallback message, swallows).
2.5 Run `scripts/test` + `scripts/hooks`.

## Task group 3 — State model & transitions (Red/Green)
3.1 **Red:** `tests/test_states.py` — `Stage` has the eight members in the documented order; `next_stage()` returns the successor; the last stage has no successor; `can_transition`/`assert_transition` allow only the ordered step and reset-to-`GREETING`; an illegal jump raises `IllegalTransitionError`.
3.2 **Green:** `app/states.py` — `Stage(str, Enum)`, `PIPELINE_ORDER`, `next_stage()`, `assert_transition()`, `IllegalTransitionError`, `SessionState(BaseModel)` (`chat_id: int`, `stage: Stage = GREETING`).
3.3 Run `scripts/test` + `scripts/hooks`.

## Task group 4 — Session store (Red/Green)
4.1 **Red:** `tests/test_session_store.py` — `get()` on unknown chat creates `GREETING`; `reset()` returns `GREETING` from any stage; two chat ids stay independent; keys are `int`; reset is idempotent.
4.2 **Green:** `app/session_store.py` — `InMemorySessionStore` with `get`, `reset`, `set_stage` (uses `assert_transition`).
4.3 Run `scripts/test` + `scripts/hooks`.

## Task group 5 — Pure handlers (Red/Green)
5.1 **Red:** `tests/test_handlers.py` — `handle_start` returns `GREETING_TEXT` and sets the chat to `GREETING`; `handle_restart` resets from a non-`GREETING` stage and returns `GREETING_TEXT`; `handle_text` returns the input text verbatim; handlers are pure (take a store + `ChatInput`, return a string, no Telegram objects).
5.2 **Green:** `app/handlers.py` — `GREETING_TEXT`, `handle_start`, `handle_restart`, `handle_text`.
5.3 Run `scripts/test` + `scripts/hooks`.

## Task group 6 — Telegram adapter + bot wiring (Red/Green)
6.1 **Red:** `tests/test_adapter.py` — `extract_chat_input` maps a command update to `kind="command"` with the command name; a plain text to `kind="text"`; an unsupported update to `None`; `chat_id` is always `int`.
6.2 **Green:** `app/telegram_adapter.py` — Pydantic `ChatInput` + `extract_chat_input(update)`.
6.3 **Red:** `tests/test_bot.py` — `safe_send_message` swallows a raised `TelegramError`, logs it, and does not propagate; handler wiring dispatches `/start`, `/restart`, and text to the pure handlers and sends the returned string.
6.4 **Green:** `app/bot.py` — `build_application()`, command/text handler registration, `safe_send_message`, and a global `Application` error handler.
6.5 Run `scripts/test` + `scripts/hooks`.

## Task group 7 — Entrypoint
7.1 `app/main.py` — configure logging, load `Settings`, `build_application()`, `run_polling()`.
7.2 Smoke test: `python -c "import app.main"` imports cleanly without a token (no side effects at import).
7.3 Run `scripts/test` + `scripts/hooks`.

## Task group 8 — Docs & spec reconciliation
8.1 Update `README.md`: `scripts/test` / `scripts/hooks`, dependency install, and run instructions.
8.2 During validation, update `SPECS/TECH.md` to name `python-telegram-bot` and the Pydantic contract approach (subject to user approval).

---

## Guardrails
- Scope guard: **no** Gemini / ADK / Bouncer / media code in this feature.
- Secrets: never logged, never in tests, never committed (`.env` stays ignored).
- Keep it simple and general; do not add config toggles or speculative abstractions.
