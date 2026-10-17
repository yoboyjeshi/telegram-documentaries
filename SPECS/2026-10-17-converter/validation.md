# Validation — Feature 4: The Converter

How we know Feature 4 is correct and can be merged.

Verification date: **pending**. Branch: `feature/2026-10-17-converter`.
Status: **drafted — awaiting user review before implementation.**

## 1. Automated gates
- [ ] `scripts/test` passes (all unit tests green, **no network**). — observed count: _TBD_.
- [ ] `scripts/hooks` passes (`ruff` clean, `mypy` clean).
- [ ] Tests were written first (Red) and cover, at minimum:
  - [ ] `CONVERTER_MODEL == "gemini-3.1-flash-image"`; `AdkConverter` raises without a key and never logs it; delegates to an injected runner; `LazyConverter` builds once and is key-free at construction. (`tests/test_converter.py`)
  - [ ] Inline image bytes are extracted from a fake ADK event stream; an empty/no-image stream raises. (`tests/test_converter.py`)
  - [ ] Success: `generate_avatar` called with the portrait + summary; `avatar_dest` written then deleted; photo sent with `CONVERTER_CAPTION`; stage `CONVERTING -> SCRIPTING`; returns `None`. (`tests/test_orchestrator.py`)
  - [ ] Missing portrait file (and separately, missing summary) → `CONVERTER_MISSING_PORTRAIT_TEXT`; converter **not** called; no send; stage stays `CONVERTING`. (`tests/test_orchestrator.py`)
  - [ ] Gemini failure (runner raises) → `CONVERTER_FAILURE_TEXT`; no advance; no send; no raise; no leftover file. (`tests/test_orchestrator.py`)
  - [ ] Send failure (`send_photo` returns `False`, and separately raises) → `CONVERTER_FAILURE_TEXT`; no advance; stage stays `CONVERTING`; no raise. (`tests/test_orchestrator.py`)
  - [ ] Retry after failure advances to `SCRIPTING`. (`tests/test_orchestrator.py`)
  - [ ] `safe_send_photo` sends with a caption and returns `True`; a send error is swallowed (returns `False`, never raises). (`tests/test_bot.py`)
  - [ ] After the interview completes, the Converter runs in the same turn: `send_photo` called with the caption, stage becomes `SCRIPTING`. (`tests/test_bot.py`)
  - [ ] An update while `CONVERTING` re-runs the Converter (retry). (`tests/test_bot.py`)
  - [ ] `/restart` while `CONVERTING` resets to `AWAITING_PHOTO` and purges the avatar + portrait. (`tests/test_bot.py`)
  - [ ] `media.avatar_dest` is chat-namespaced; `purge_chat_media` removes the avatar. (`tests/test_media.py`)
  - [ ] Pure texts exist and the caption is plain text (no markdown/emoji). (`tests/test_handlers.py`)
  - [ ] Logging hygiene: no `GEMINI_API_KEY`, no image bytes, no caption/summary text. (`tests/test_converter.py`, `tests/test_orchestrator.py`)

## 2. Manual phone test (long polling)
With a real bot token + `GEMINI_API_KEY` in `.env`, run `python -m app.main`, then from a phone:
- [ ] Send `/start`, a selfie, answer all 6 questions → after the final answer the bot sends the completion message, then a **generated hybrid-animal avatar photo** with a one-line documentary caption.
- [ ] The avatar is recognisably a hybrid of the user's portrait and an animal consistent with the interview traits.
- [ ] After the photo arrives, the chat is at **`SCRIPTING`** and **nothing further happens**.
- [ ] Inspect the chat temp dir: `photo.jpg` and `avatar.png` are chat-namespaced; `avatar.png` is **deleted** after a successful send.
- [ ] Temporarily unset `GEMINI_API_KEY` (or force a model error) at `CONVERTING` → friendly retry, **no crash**, stage stays `CONVERTING`; restore and send a message → the avatar is generated and sent.
- [ ] Delete the portrait temp file then trigger the step → `CONVERTER_MISSING_PORTRAIT_TEXT` asking for `/restart`; **no crash**.
- [ ] `/restart` at `CONVERTING` → session resets to awaiting-a-photo; the chat temp dir (portrait + any avatar) is purged.
- [ ] Inspect logs: stage/outcome lines appear; **no image bytes, no caption/summary text, no key**.

> On-device checks need a human; the automated suite covers the pure logic and orchestration with mocked models and sends.

## 3. Scope guards (YAGNI)
- [ ] **No** Scripter / Narrator / TTS code; **nothing happens after `SCRIPTING`**.
- [ ] **Exactly one** image is generated per conversion; no variants, no regeneration command.
- [ ] No new pipeline stages; no menus/inline keyboards; no DB.

## 4. Contracts & boundaries
- [ ] The `Converter` interface is typed; the real `AdkConverter` holds secrets privately and accepts an injected runner for tests.
- [ ] The photo send goes through the new `safe_send_photo`; the orchestrator treats a non-delivered send as the retry path.
- [ ] Empty/malformed model output or a missing image surfaces as a caught converter failure (friendly retry), never a crash.

## 5. Resilience & state
- [ ] Every Gemini image call and every Telegram photo send is wrapped; failure → friendly retry, state unchanged at `CONVERTING`, no crash.
- [ ] `@guard_errors` / `dispatch_safely` remain the backstop.
- [ ] `/start` and `/restart` fully reset from **mid-`CONVERTING`**, purging the portrait and the generated avatar; other chats are untouched.
- [ ] The generated image is deleted on success; the bot never requires a process reboot to recover.

## 6. Secrets & privacy
- [ ] `GEMINI_API_KEY` read from `.env` only; **never** logged, never in tests.
- [ ] Image bytes, the caption, and the trait summary are **never** logged.
- [ ] `.env` remains untracked/ignored; `.env.example` unchanged.

## 7. Docs & spec reconciliation
- [ ] `README.md` documents the converter flow and updated status.
- [ ] `SPECS/TECH.md` reflects the Converter in use, the `CONVERTING` step in `app/orchestrator.py`, the converter contract, and `safe_send_photo`.
- [ ] `SPECS/ROADMAP.md` Feature 3 status (merged) and Feature 4 status updated.
- [ ] Any deviations recorded in `plan.md`.
- [ ] `SPECS/MISSION.md` principle #1 wording reconciled **only if the user approves**. (No change made; awaiting approval.)

## 8. Merge criteria
- [ ] All automated gates green (`scripts/test`, `scripts/hooks`).
- [ ] Manual phone test passed (avatar arrives after the interview; missing-portrait and failure paths behave; reset purges media; no crash).
- [ ] Scope guards hold; no secrets committed.
- [ ] Code review raised no blocking findings.
- [ ] PR opened against `main` (not merged by the build agent).
