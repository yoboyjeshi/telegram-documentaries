# Validation — Feature 2: The Bouncer

How we know Feature 2 is correct and can be merged.

Verification date: **2026-10-11**; docs reconciled **2026-10-17**. Branch: `feature/2026-10-11-bouncer`.
Status: **automated verification complete** — all automated gates green (95
unit tests) and coverage in place. Post-verification fixes landed on the branch
(see "Post-verification commits" below). The **manual phone test** and the
**PR merge** remain for a human (marked unchecked below); the phone test has
since been reported passing by the user, but it was not re-verified here.
Deviations are recorded in `plan.md` ("Implementation deviations").

## 1. Automated gates
- [x] `scripts/test` passes (all unit tests green, **no network**). — 95 passed.
- [x] `scripts/hooks` passes (`ruff` clean, `mypy` clean). — 12 source files.
- [x] Tests were written first (Red) and cover, at minimum:
  - [x] Adapter photo contract: photo -> `kind="photo"` + largest `file_id` + `int` chat_id; text/other/foreign unchanged. (`tests/test_adapter.py`)
  - [x] Media namespacing: per-chat dirs differ; `photo_dest` sits inside the chat dir; `purge_chat_media` removes only that chat's files, is idempotent, and tolerates a missing dir. (`tests/test_media.py`)
  - [x] Retry edge: `BOUNCER_CHECK -> AWAITING_PHOTO` legal; unrelated backward moves still raise `IllegalTransitionError`. (`tests/test_states.py`)
  - [x] `BouncerVerdict` schema; `refusal_text` deterministic and drawn from `REFUSAL_LINES`; `AdkBouncer` uses `gemini-3.1-flash-lite`, raises without a key, never logs it, and delegates to an injected runner. (`tests/test_bouncer.py`)
  - [x] Pure logic: wrong-stage photo -> redirect (stage unchanged); human -> `INTERVIEW` + `INTERVIEW_READY_TEXT` (**no question**); non-human -> `AWAITING_PHOTO` + refusal. (`tests/test_handlers.py`)
  - [x] Orchestration resilience: download failure and bouncer failure each -> friendly retry, stage `AWAITING_PHOTO`, no raise. (`tests/test_photo_flow.py`)
  - [x] Reset purge: `/start` and `/restart` purge the chat's temp media and end at `AWAITING_PHOTO`. (`tests/test_handlers.py`)
  - [x] Wiring: a `PHOTO` handler is registered; dispatch routes photo vs text-at-awaiting-photo. (`tests/test_bot.py`)
  - [x] Logging hygiene: neither `GEMINI_API_KEY` nor image bytes appear in logs. (`tests/test_logging.py`, `tests/test_bouncer.py`, `tests/test_photo_flow.py`)
  - [x] Post-verification regressions: global `_on_error` logs the real exception via `context.error` (no `NoneType: None`); the photo-size fallback logs (no silent swallow); a Bouncer failure logs type + message only and never image bytes; a pre-existing `GOOGLE_API_KEY` warns without logging either key value. (`tests/test_bot.py`, `tests/test_adapter.py`, `tests/test_bouncer.py`, `tests/test_photo_flow.py`)

## 2. Manual phone test (long polling)
With a real bot token + `GEMINI_API_KEY` in `.env`, run `python -m app.main`, then from a phone:
- [ ] Send `/start` -> greeting that **asks for a photo**.
- [ ] Send a **selfie with a visible face** -> the bot advances to the INTERVIEW stage and replies that they're in (no question asked yet).
- [ ] Send a **non-human photo** (object/pet/scenery) -> **humorous refusal** + asks for another photo; stage stays awaiting a photo.
- [ ] Send a **photo before `/start`** (or at another stage) -> gentle redirect, no crash.
- [ ] Send **text while awaiting a photo** -> prompts for a photo.
- [ ] Send a photo, then `/restart` -> session resets to awaiting-a-photo and the chat's temp image is purged on disk.
- [ ] Simulate a failure (e.g. temporarily unset `GEMINI_API_KEY`) -> friendly message + retry offered, **no crash**; set it back and retry succeeds.
- [ ] Inspect logs: handler/stage/`chat_id` and bouncer outcome appear; **no `GEMINI_API_KEY` and no image bytes** anywhere.

> Not performed in this verification environment (no phone / live Gemini here);
> covered for the pure logic by unit tests, but the on-device checks need a human.
> The user has since reported the phone test passing, but this box is left for the
> human to tick.

## 3. Scope guards (YAGNI)
- [x] **No** Interviewer / Converter / Scripter / Narrator code; **no interview questions**.
- [x] No new pipeline stages; no webhook, DB, or user accounts.
- [x] Only the Bouncer path is added on top of Feature 1.

## 4. Contracts & boundaries
- [x] `ChatInput` parses photos at the edge; the adapter performs **no** download.
- [x] `BouncerVerdict` is a Pydantic v2 model; the ADK path uses a **schema**, not regex.
- [x] Malformed input (foreign update, missing `file_id`) is handled without raising.

## 5. Resilience & temp-media hygiene
- [x] Telegram photo download wrapped; failure -> friendly retry, no crash.
- [x] Gemini/ADK call wrapped; failure -> friendly retry, no crash.
- [x] Temp photos are namespaced by `chat_id`; `/start` and `/restart` purge the chat's dir and touch no other chat.
- [x] The bot never requires a process reboot to recover.

## 6. Secrets
- [x] `GEMINI_API_KEY` read from `.env` only; never logged, never in tests.
- [x] Image bytes never logged.
- [x] `.env` remains untracked/ignored; `.env.example` unchanged (or updated with no real values).

## 7. Docs & spec reconciliation
- [x] `README.md` documents the photo flow and the new dependency.
- [x] `SPECS/TECH.md` reflects that ADK is now in use and names the Bouncer model/interface.
- [x] `SPECS/ROADMAP.md` Feature 2 status updated.
- [x] Resolved `google-adk` version and any deviations recorded in `plan.md`. — **`google-adk==2.11.0`**.

## 8. Merge criteria
- [x] All automated gates green (`scripts/test`, `scripts/hooks`).
- [ ] Manual phone test passed (selfie accepted; object/pet refused without crashing; reset purges temp media).
- [x] Scope guards hold; no secrets committed.
- [x] Code review raised no blocking findings. (Self-review by the build orchestrator: the code-reviewer subagent was unavailable, so the same review scope — correctness, state edge, media hygiene, secret handling, boundary validation, test gaps, ruff/mypy — was performed inline; no blocking findings.)
- [ ] PR opened against `main` (not merged by the build agent).

## 9. Post-verification commits
- **`192e8d7`** — *Log the real exception in the global error handler.* The PTB
  `_on_error` handler logged `exc_info=True` outside an `except` block, so
  unhandled update errors surfaced as the useless `NoneType: None`; it now logs
  `exc_info=context.error`. A regression test asserts the record carries the real
  exception. (Surfaced while phone-testing: two polling instances caused a
  `Conflict` that could not be read.)
- **`82afd69`** — *Log photo-size fallback and harden Bouncer failure logging.*
  The adapter's photo-size fallback no longer swallows silently; Bouncer failures
  log exception **type + message only**, so image bytes chained into the
  ADK/Gemini payload cannot reach the log; `_ensure_google_api_key` warns when a
  pre-existing `GOOGLE_API_KEY` takes precedence (without logging either value).
  New edge-case tests cover empty/all-`None` photo sizes, the real key-logging
  path, and a long image-bytes sentinel.
- **`074f520` + `ec93724`** — net-zero `# noqa: BLE001` churn: an unused directive
  was dropped and then restored (ruff's `BLE001` **is** enabled, so the directive
  is required for the intentional blind except around the Bouncer call). The final
  tree matches the hardened design above.
- **H1/H2 RCA sweep (lost traceback / silent fallback class):** no other siblings
  found. Every other `exc_info=True` / `logger.exception` site
  (`app/photo_flow.py`, `app/telegram_adapter.py`, `app/logging_config.py`,
  `app/bot.py`) is inside an `except` block, so its traceback is captured
  correctly; the only outside-`except` logger is `_on_error`, now fixed by passing
  `context.error` explicitly. The two silent-fallback paths found (adapter photo
  size, pre-existing `GOOGLE_API_KEY`) were both made observable in `82afd69`.
- **Reconciliation:** automated gates re-run green on this tree — `scripts/test`
  **95 passed**, `scripts/hooks` clean (ruff + mypy, 12 source files). The
  manual-phone-test and PR-merge boxes in section 8 are deliberately left
  unchecked: they belong to the human/build agent, not this verification.
