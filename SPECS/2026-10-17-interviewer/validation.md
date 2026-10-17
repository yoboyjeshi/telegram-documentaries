# Validation — Feature 3: The Interviewer (and orchestrator)

How we know Feature 3 is correct and can be merged.

Verification date: **2026-10-17 (automated gates)**. Branch: `feature/2026-10-17-interviewer`.
Status: **spec approved (2026-10-17) — implemented; automated gates green; manual phone test pending.**

## 1. Automated gates
- [x] `scripts/test` passes (all unit tests green, **no network**). — observed count: **151**.
- [x] `scripts/hooks` passes (`ruff` clean, `mypy` clean).
- [x] Tests were written first (Red) and cover, at minimum:
  - [x] Shared key helper: sets when absent; keeps+warns on a *different* pre-existing value; no warning when identical; never logs a key. (`tests/test_config.py`)
  - [x] `InterviewState`/`InterviewAnswer`/`TraitSummary` schemas; `record` pairs the pending question and **fails loud** with no pending question. (`tests/test_states.py`)
  - [x] `reset` clears interview state (stage back to `GREETING`). (`tests/test_session_store.py`)
  - [x] `INTERVIEWER_MODEL == "gemini-3.1-flash-lite"`; `TOTAL_QUESTIONS == 6` and within 5–7; `AdkInterviewer` raises without a key, never logs it, delegates to injected runners; `LazyInterviewer` builds once. (`tests/test_interviewer.py`)
  - [x] Pure texts + `apply_bouncer_verdict` (human → `None` + `INTERVIEW`; non-human → refusal). (`tests/test_handlers.py`)
  - [x] Progression 1..N advancing one step per answer; answers collected as Q/A pairs. (`tests/test_orchestrator.py`)
  - [x] Summary assembly stores the `TraitSummary` and advances `INTERVIEW -> CONVERTING` with `INTERVIEW_COMPLETE_TEXT`. (`tests/test_orchestrator.py`)
  - [x] Blank/whitespace answer re-asks, does not record/advance. (`tests/test_orchestrator.py`)
  - [x] Unknown command mid-interview → `INTERVIEW_UNKNOWN_COMMAND_TEXT` nudge, records nothing, stays on the current question. (`tests/test_orchestrator.py`, `tests/test_bot.py`)
  - [x] Unknown commands reach the handler through PTB's **real handler registry** (catch-all registered last, `/start`/`/restart` still matched first). (`tests/test_bot.py`)
  - [x] Other update kinds (sticker/document) mid-interview → `INTERVIEW_OTHER_REDIRECT_TEXT`, no state change, reachable via `bot.dispatch`. (`tests/test_orchestrator.py`, `tests/test_bot.py`)
  - [x] Photo mid-interview → gentle redirect, stage + pending question unchanged. (`tests/test_orchestrator.py`, `tests/test_bot.py`)
  - [x] Interviewer failure (`next_question` / `summarise` raises) → friendly retry, state unchanged, no raise. (`tests/test_orchestrator.py`)
  - [x] Human verdict starts the interview (intro + Q1). (`tests/test_photo_flow.py`)
  - [x] Bot routing: INTERVIEW text → orchestrator; INTERVIEW photo → redirect; INTERVIEW unknown command → nudge. (`tests/test_bot.py`)
  - [x] Logging hygiene: no `GEMINI_API_KEY` (verified on the real `_run_adk` path), no raw answers (including `ValidationError` payloads), no summary text, no image bytes. (`tests/test_interviewer.py`, `tests/test_orchestrator.py`)
  - [x] Commit-only-on-success: after a failed `next_question`, a retry records exactly one answer against the single unchanged pending question. (`tests/test_orchestrator.py`)

## 2. Manual phone test (long polling)
With a real bot token + `GEMINI_API_KEY` in `.env`, run `python -m app.main`, then from a phone:
- [ ] Send `/start`, then a selfie → the bot advances to `INTERVIEW` **and asks question 1**.
- [ ] Answer each question → the bot asks the next, one at a time, until **6** questions are answered.
- [ ] After the final answer → a friendly completion message; the chat is now at `CONVERTING` (nothing further happens yet).
- [ ] Send a **photo mid-interview** → gentle redirect; the current question is unchanged; no crash.
- [ ] Send an **empty/whitespace** message mid-interview → the same question is re-asked.
- [ ] Send an **unknown command** (e.g. `/foo`) mid-interview → a short nudge to answer the question or use `/restart`; the current question is unchanged.
- [ ] `/restart` mid-interview → session resets to awaiting-a-photo and the interview state + temp media are cleared.
- [ ] Temporarily unset `GEMINI_API_KEY` mid-interview → friendly retry, **no crash**, state preserved; restore and retry succeeds.
- [ ] Inspect logs: handler/`chat_id`/stage and interview **counts** appear; **no answer text, no summary text, no key, no image bytes**.

> On-device checks need a human; the automated suite covers the pure logic and orchestration with mocked models.

## 3. Scope guards (YAGNI)
- [x] **No** Converter / Scripter / Narrator code; **nothing happens after `CONVERTING`**.
- [x] No new pipeline stages; no menus/inline keyboards; no DB.
- [x] Only the interview path is added on top of Features 1–2.

## 4. Contracts & boundaries
- [x] `InterviewQuestion` / `TraitSummary` / `InterviewAnswer` / `InterviewState` are Pydantic v2 models; the ADK outputs use **schemas, not regex**.
- [x] The `Interviewer` interface is typed; the real `AdkInterviewer` holds secrets privately and accepts injected runners for tests.
- [x] Malformed/empty model output surfaces as a caught interviewer failure (friendly retry), never a crash.

## 5. Resilience & state
- [x] Every Gemini call is wrapped; failure → friendly retry, interview state unchanged, no crash.
- [x] Every Telegram send reuses `safe_send_message`; `dispatch_safely`/`@guard_errors` remain the backstop.
- [x] `/start` and `/restart` fully reset from **mid-interview**, clearing interview state and purging that chat's temp media; other chats are untouched.
- [x] The bot never requires a process reboot to recover.

## 6. Secrets & privacy
- [x] `GEMINI_API_KEY` read from `.env` only; **never** logged, never in tests.
- [x] Raw answers, the trait summary, and image bytes are **never** logged.
- [x] `.env` remains untracked/ignored; `.env.example` unchanged.

## 7. Docs & spec reconciliation
- [x] `README.md` documents the interview flow and updated status.
- [x] `SPECS/TECH.md` reflects the Interviewer in use, the `app/orchestrator.py` seam, and the interview contracts.
- [x] `SPECS/ROADMAP.md` Feature 3 status updated.
- [x] Any deviations recorded in `plan.md`.
- [ ] `SPECS/MISSION.md` principle #1 wording reconciled **only if the user approves**. (No change made; awaiting approval.)

## 8. Merge criteria
- [x] All automated gates green (`scripts/test`, `scripts/hooks`).
- [ ] Manual phone test passed (interview completes in 6 questions; photo-mid-interview redirected; reset clears state; no crash).
- [x] Scope guards hold; no secrets committed.
- [ ] Code review raised no blocking findings.
- [ ] PR opened against `main` (not merged by the build agent).
