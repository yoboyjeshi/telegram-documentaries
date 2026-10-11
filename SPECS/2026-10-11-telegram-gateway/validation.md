# Validation — Feature 1: Telegram Gateway

How we know Feature 1 is correct and can be merged.

Verification date: 2026-10-11. Branch: `feature/2026-10-11-telegram-gateway`
(commit `e6d04fe`). Verified by inspecting the code, tests, and dev scripts and
running both gates. Deviations from the plan are recorded in `plan.md`
("Implementation deviations"). Items that need a human/phone or a PR are left
unchecked and flagged.

## 1. Automated gates
- [x] `scripts/test` passes (all unit tests green, no network access).
      Evidence: `48 passed in 2.18s` (pytest 9.1.1, Python 3.11).
- [x] `scripts/hooks` passes (`ruff` clean, `mypy` clean).
      Evidence: `ruff check app tests` → "All checks passed!";
      `mypy app` → "Success: no issues found in 9 source files".
- [x] Tests were written first (Red) and demonstrably cover:
      (Red-first is the documented TDD process; git history squashes the red
      step into the feature commit, so coverage below is the verifiable part.)
  - [x] `Stage` enum: eight members in pipeline order. (`tests/test_states.py`)
  - [x] Transition helpers: ordered step allowed, reset-to-`GREETING` allowed,
        illegal jump raises `IllegalTransitionError`. (`tests/test_states.py`,
        `tests/test_session_store.py`)
  - [x] Session init: unknown `chat_id` → `GREETING`. (`tests/test_session_store.py`)
  - [x] `/start` and `/restart` reset the chat to `GREETING` (from any stage).
        (`tests/test_handlers.py`, `tests/test_bot.py`)
  - [x] Per-chat isolation: resetting chat A does not affect chat B.
        (`tests/test_session_store.py::test_sessions_are_isolated_per_chat`)
  - [x] `handle_text` returns the text verbatim. (`tests/test_handlers.py`)
  - [x] `extract_chat_input`: command vs text vs unsupported; `chat_id` is `int`.
        (`tests/test_adapter.py`) Note: a message that exists but has no text
        (e.g. a photo) yields `kind="other"` + a WARNING rather than `None`;
        only truly foreign updates (no message) yield `None`.
  - [x] `safe_send_message` swallows a `TelegramError` and does not propagate.
        (`tests/test_bot.py`; an unexpected non-`TelegramError` is also covered.)

## 2. Manual phone test (long polling)
With a real bot token in `.env`, run `python -m app.main`, then from a phone:
- [ ] Send `/start` → the bot replies with the greeting.
- [ ] Send any plain text → the bot echoes it back.
- [ ] Send `/restart` → the bot greets again / resets to `GREETING`.
- [ ] Repeat `/start`, text, `/restart` **multiple times in one running process** —
      no crash, no restart of the process.
- [ ] Inspect console logs: handler name, `chat_id`, and stage appear; **no secret value**
      appears anywhere.

> Not performed in this verification environment (no phone / real token).
> The logging expectation is covered by `tests/test_logging.py`, and the
> multiple-reset path is covered by `tests/test_bot.py`, but the on-device
> checks still need a human with a live bot token.

## 3. Scope guards (YAGNI)
- [x] No Gemini/ADK/Bouncer/media/TTS code, imports, or dependencies present.
      Evidence: no `google.adk`, `PIL`/`pillow`, media, or TTS imports anywhere;
      the only Gemini reference is the `gemini_api_key` placeholder that
      `requirements.md` explicitly allows ("declared for later features, unused now").
- [x] Only the gateway + state scaffold is implemented.
- [x] `.env` is **not** committed (`git status` clean of it; `git check-ignore .env`
      confirms ignore) — `.gitignore:2:.env`. `.env.example` unchanged
      (no diff against the initial commit).

## 4. Resilience checks
- [x] Every Telegram send goes through the guarded helper.
      Evidence: the single `bot.send_message` call site is inside
      `safe_send_message` (`app/bot.py:33`); callers use it at `bot.py:59` and
      `bot.py:81`.
- [x] A simulated send failure does not crash the process (unit test) and logs a
      friendly path. (`tests/test_bot.py::test_safe_send_swallows_*`,
      `test_dispatch_safely_sends_friendly_message_on_failure`)
- [x] A global `Application` error handler is registered.
      (`app/bot.py:119`; `tests/test_bot.py::test_build_application_registers_error_handler`)

## 5. Docs & spec reconciliation
- [x] `README.md` documents `scripts/test`, `scripts/hooks`, install, and run.
      (Added in this validation pass — prior drift, plan task 8.1.)
- [x] `SPECS/TECH.md` updated to name `python-telegram-bot` and Pydantic contracts
      (with user approval). (Added in this validation pass — prior drift, plan task 8.2.)
- [x] Any drift between implementation and the feature spec is recorded and the
      spec/`SPECS` files updated on approval. See "Implementation deviations" in
      `plan.md`, including `dispatch_safely()`, `@logged` stage logging, the
      `httpx` log-level fix, the `kind="other"` handling, and the intentional
      omission of `pytest-asyncio`.

## 6. Merge criteria
- [x] All automated gates green. (`./scripts/test`: 48 passed; `./scripts/hooks`: clean)
- [ ] Manual phone test passed (including the repeated reset test in one process).
- [x] Scope guards hold; no secrets committed.
- [x] Code-reviewer raised no blocking findings.
      (Review findings were addressed in commit `e6d04fe`; this pass found no
      remaining code defects.)
- [ ] PR opened against `main` (not merged by the build agent).
      Not yet opened (`gh pr list` empty); branch is pushed and in sync with origin.
