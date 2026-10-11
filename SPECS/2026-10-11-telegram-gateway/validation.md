# Validation — Feature 1: Telegram Gateway

How we know Feature 1 is correct and can be merged.

## 1. Automated gates
- [ ] `scripts/test` passes (all unit tests green, no network access).
- [ ] `scripts/hooks` passes (`ruff` clean, `mypy` clean).
- [ ] Tests were written first (Red) and demonstrably cover:
  - [ ] `Stage` enum: eight members in pipeline order.
  - [ ] Transition helpers: ordered step allowed, reset-to-`GREETING` allowed, illegal jump raises `IllegalTransitionError`.
  - [ ] Session init: unknown `chat_id` → `GREETING`.
  - [ ] `/start` and `/restart` reset the chat to `GREETING` (from any stage).
  - [ ] Per-chat isolation: resetting chat A does not affect chat B.
  - [ ] `handle_text` returns the text verbatim.
  - [ ] `extract_chat_input`: command vs text vs unsupported; `chat_id` is `int`.
  - [ ] `safe_send_message` swallows a `TelegramError` and does not propagate.

## 2. Manual phone test (long polling)
With a real bot token in `.env`, run `python -m app.main`, then from a phone:
- [ ] Send `/start` → the bot replies with the greeting.
- [ ] Send any plain text → the bot echoes it back.
- [ ] Send `/restart` → the bot greets again / resets to `GREETING`.
- [ ] Repeat `/start`, text, `/restart` **multiple times in one running process** — no crash, no restart of the process.
- [ ] Inspect console logs: handler name, `chat_id`, and stage appear; **no secret value** appears anywhere.

## 3. Scope guards (YAGNI)
- [ ] No Gemini/ADK/Bouncer/media/TTS code, imports, or dependencies present.
- [ ] Only the gateway + state scaffold is implemented.
- [ ] `.env` is **not** committed (`git status` clean of it; `git check-ignore .env` confirms ignore). `.env.example` unchanged.

## 4. Resilience checks
- [ ] Every Telegram send goes through the guarded helper.
- [ ] A simulated send failure does not crash the process (unit test) and logs a friendly path.
- [ ] A global `Application` error handler is registered.

## 5. Docs & spec reconciliation
- [ ] `README.md` documents `scripts/test`, `scripts/hooks`, install, and run.
- [ ] `SPECS/TECH.md` updated to name `python-telegram-bot` and Pydantic contracts (with user approval).
- [ ] Any drift between implementation and the feature spec is recorded and the spec/`SPECS` files updated on approval.

## 6. Merge criteria
- [ ] All automated gates green.
- [ ] Manual phone test passed (including the repeated reset test in one process).
- [ ] Scope guards hold; no secrets committed.
- [ ] Code-reviewer raised no blocking findings.
- [ ] PR opened against `main` (not merged by the build agent).
