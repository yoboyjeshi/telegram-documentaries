# Validation — Feature 2: The Bouncer

How we know Feature 2 is correct and can be merged.

Verification date: **2026-10-11**. Branch: `feature/2026-10-11-bouncer`.
Status: **pre-implementation** (checklist authored with the spec; boxes are ticked
during the verifier pass). Deviations from the plan are recorded in `plan.md`
("Implementation deviations"). Items needing a human/phone or a PR are left
unchecked and flagged.

## 1. Automated gates
- [ ] `scripts/test` passes (all unit tests green, **no network**).
- [ ] `scripts/hooks` passes (`ruff` clean, `mypy` clean).
- [ ] Tests were written first (Red) and cover, at minimum:
  - [ ] Adapter photo contract: photo -> `kind="photo"` + largest `file_id` + `int` chat_id; text/other/foreign unchanged. (`tests/test_adapter.py`)
  - [ ] Media namespacing: per-chat dirs differ; `photo_dest` sits inside the chat dir; `purge_chat_media` removes only that chat's files, is idempotent, and tolerates a missing dir. (`tests/test_media.py`)
  - [ ] Retry edge: `BOUNCER_CHECK -> AWAITING_PHOTO` legal; unrelated backward moves still raise `IllegalTransitionError`. (`tests/test_states.py`)
  - [ ] `BouncerVerdict` schema; `refusal_text` deterministic and drawn from `REFUSAL_LINES`; `AdkBouncer` uses `gemini-3.1-flash-lite`, raises without a key, never logs it, and delegates to an injected runner. (`tests/test_bouncer.py`)
  - [ ] Pure logic: wrong-stage photo -> redirect (stage unchanged); human -> `INTERVIEW` + `INTERVIEW_READY_TEXT` (**no question**); non-human -> `AWAITING_PHOTO` + refusal. (`tests/test_handlers.py`)
  - [ ] Orchestration resilience: download failure and bouncer failure each -> friendly retry, stage `AWAITING_PHOTO`, no raise. (`tests/test_photo_flow.py`)
  - [ ] Reset purge: `/start` and `/restart` purge the chat's temp media and end at `AWAITING_PHOTO`. (`tests/test_handlers.py`)
  - [ ] Wiring: a `PHOTO` handler is registered; dispatch routes photo vs text-at-awaiting-photo. (`tests/test_bot.py`)
  - [ ] Logging hygiene: neither `GEMINI_API_KEY` nor image bytes appear in logs. (`tests/test_logging.py`, `tests/test_bouncer.py`)

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

## 3. Scope guards (YAGNI)
- [ ] **No** Interviewer / Converter / Scripter / Narrator code; **no interview questions**.
- [ ] No new pipeline stages; no webhook, DB, or user accounts.
- [ ] Only the Bouncer path is added on top of Feature 1.

## 4. Contracts & boundaries
- [ ] `ChatInput` parses photos at the edge; the adapter performs **no** download.
- [ ] `BouncerVerdict` is a Pydantic v2 model; the ADK path uses a **schema**, not regex.
- [ ] Malformed input (foreign update, missing `file_id`) is handled without raising.

## 5. Resilience & temp-media hygiene
- [ ] Telegram photo download wrapped; failure -> friendly retry, no crash.
- [ ] Gemini/ADK call wrapped; failure -> friendly retry, no crash.
- [ ] Temp photos are namespaced by `chat_id`; `/start` and `/restart` purge the chat's dir and touch no other chat.
- [ ] The bot never requires a process reboot to recover.

## 6. Secrets
- [ ] `GEMINI_API_KEY` read from `.env` only; never logged, never in tests.
- [ ] Image bytes never logged.
- [ ] `.env` remains untracked/ignored; `.env.example` unchanged (or updated with no real values).

## 7. Docs & spec reconciliation
- [ ] `README.md` documents the photo flow and the new dependency.
- [ ] `SPECS/TECH.md` reflects that ADK is now in use and names the Bouncer model/interface.
- [ ] `SPECS/ROADMAP.md` Feature 2 status updated.
- [ ] Resolved `google-adk` version and any deviations recorded in `plan.md`.

## 8. Merge criteria
- [ ] All automated gates green (`scripts/test`, `scripts/hooks`).
- [ ] Manual phone test passed (selfie accepted; object/pet refused without crashing; reset purges temp media).
- [ ] Scope guards hold; no secrets committed.
- [ ] Code-reviewer raised no blocking findings.
- [ ] PR opened against `main` (not merged by the build agent).
