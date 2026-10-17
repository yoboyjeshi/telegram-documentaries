# Plan — Feature 3: The Interviewer (and orchestrator)

**Method:** Red/Green TDD. For each task group: write the failing test first (**Red**), implement the minimum to pass (**Green**), then tidy. Run the dev scripts **`scripts/test`** (pytest) and **`scripts/hooks`** (ruff + mypy) after each group. Keep business logic free of logging (use decorators).

**Builds on:** Feature 1 (`Stage` machine, `InMemorySessionStore`, `@logged`/`@guard_errors`, `safe_send_message`/`dispatch_safely`) and Feature 2 (`ChatInput`, `handle_photo_update`, `apply_bouncer_verdict`, `LazyBouncer`).

**Target layout (changes in **bold**)**
```
app/
  __init__.py
  main.py             # entrypoint (unchanged)
  config.py           # Settings  **+ public ensure_google_api_key()**
  logging_config.py   # @logged / @guard_errors (unchanged)
  states.py           # Stage machine  **+ InterviewAnswer, TraitSummary, InterviewState; SessionState.interview**
  session_store.py    # InMemorySessionStore  **reset() also clears interview**
  telegram_adapter.py # ChatInput (unchanged)
  media.py            # chat-namespaced temp paths + purge (unchanged)
  bouncer.py          # **use config.ensure_google_api_key (drop private helper)**
  interviewer.py      # **NEW** InterviewQuestion, Interviewer protocol, AdkInterviewer, LazyInterviewer, prompts, TOTAL_QUESTIONS
  orchestrator.py     # **NEW** start_interview + handle_interview_update (INTERVIEW-stage routing)
  photo_flow.py       # **+ accept interviewer; human verdict -> start_interview**
  handlers.py         # **+ interview texts; apply_bouncer_verdict human -> None; INTERVIEW_READY_TEXT removed**
  bot.py              # **+ LazyInterviewer; route INTERVIEW-stage updates to the orchestrator**
tests/
  conftest.py
  test_config.py      # **+ ensure_google_api_key**
  test_states.py      # **+ interview contracts / progression helpers**
  test_session_store.py # **+ reset clears interview**
  test_bouncer.py     # **update the two key-helper tests to the shared location**
  test_handlers.py    # **+ interview texts; update verdict tests; drop "asks_no_question"**
  test_interviewer.py # **NEW** schema/keys/runner delegation/lazy build
  test_orchestrator.py# **NEW** progression, re-ask, redirect, completion, resilience
  test_photo_flow.py  # **+ human verdict starts the interview**
  test_bot.py         # **+ INTERVIEW routing**
scripts/
  test / hooks
```

---

## Task group 1 — Shared Google-key helper (Red/Green)
1.1 **Red:** `tests/test_config.py` — `ensure_google_api_key(key)` sets `GOOGLE_API_KEY` when absent (no warning); keeps a pre-existing *different* value and warns (no key value in the log); is a no-op warn-free when the pre-existing value is *identical*.
1.2 **Green:** `app/config.py` — add `ensure_google_api_key`; `app/bouncer.py` uses it and drops `_ensure_google_api_key`.
1.3 Update the two Feature 2 key tests in `tests/test_bouncer.py` to target the shared helper (no shim left in `bouncer`).
1.4 Run `scripts/test` + `scripts/hooks`.

## Task group 2 — Session model: interview state (Red/Green)
2.1 **Red:** `tests/test_states.py` — `TraitSummary(summary=..., traits=[...])` schema; `InterviewAnswer(question, answer)`; `InterviewState` defaults (empty answers, no pending question, no summary) and that two instances don't share the list; `record("a")` pairs the pending question, appends, clears pending, and **raises when there is no pending question** (fail loud).
2.2 **Green:** `app/states.py` — add `TraitSummary`, `InterviewAnswer`, `InterviewState`; add `interview: InterviewState | None = None` to `SessionState`.
2.3 **Red:** `tests/test_session_store.py` — `reset(chat_id)` clears a populated `interview` (stage back to `GREETING`).
2.4 **Green:** `app/session_store.py` — `reset` clears `interview`.
2.5 Run `scripts/test` + `scripts/hooks`.

## Task group 3 — Interviewer contract, prompts, ADK impl (Red/Green)
3.1 **Red:** `tests/test_interviewer.py` — `INTERVIEWER_MODEL == "gemini-3.1-flash-lite"`; `5 <= TOTAL_QUESTIONS <= 7` and `== 6`; `InterviewQuestion(text=...)` schema; constructing `AdkInterviewer` without a key raises `ConfigurationError` and never logs the key; `next_question`/`summarise` delegate to injected `question_runner`/`summary_runner` and return their typed results (no network); a failing runner propagates so the orchestrator can catch it; `LazyInterviewer` builds its delegate once and is key-free at construction (mirrors `LazyBouncer`).
3.2 **Green:** `app/interviewer.py` — `InterviewQuestion`, `Interviewer` protocol (`next_question`, `summarise`), `AdkInterviewer` (lazy `google.adk` import; two `LlmAgent`s with `output_schema=InterviewQuestion`/`TraitSummary`; history formatted to text; distinct session ids; injectable runners), `LazyInterviewer`, `INTERVIEWER_MODEL`, `TOTAL_QUESTIONS`, `QUESTION_INSTRUCTION`/`SUMMARY_INSTRUCTION`.
3.3 Run `scripts/test` + `scripts/hooks`.

## Task group 4 — Pure handler texts + verdict change (Red/Green)
4.1 **Red:** `tests/test_handlers.py` — the new texts exist and read well (`INTERVIEW_INTRO`, `INTERVIEW_PHOTO_REDIRECT_TEXT`, `INTERVIEW_REPROMPT_TEXT`, `INTERVIEW_COMPLETE_TEXT`, `INTERVIEW_FAILURE_TEXT`, `INTERVIEW_UNKNOWN_COMMAND_TEXT`); `apply_bouncer_verdict(human)` sets `INTERVIEW` and returns `None`; `apply_bouncer_verdict(non-human)` stays a refusal from the pool; drop `test_interview_ready_text_asks_no_question`.
4.2 **Green:** `app/handlers.py` — add the texts, remove `INTERVIEW_READY_TEXT`, change the human branch to set `INTERVIEW` and return `None`.
4.3 Run `scripts/test` + `scripts/hooks`.

## Task group 5 — Orchestrator: progression, completion, resilience (Red/Green)
5.1 **Red:** `tests/test_orchestrator.py` (fake `Interviewer`; `tmp_path` store):
   - `start_interview` sets a fresh `InterviewState`, asks Q1 (empty history passed), stores it as `pending_question`, and the reply contains the intro **and** Q1; stage stays `INTERVIEW`;
   - answering advances 1..N one step at a time, recording each `InterviewAnswer` (question = the pending one);
   - on the Nth answer the fake summary is stored and the stage becomes `CONVERTING`, reply is `INTERVIEW_COMPLETE_TEXT`;
   - a blank/whitespace answer re-asks (`INTERVIEW_REPROMPT_TEXT`) and does **not** record or advance;
   - a photo mid-interview returns `INTERVIEW_PHOTO_REDIRECT_TEXT`, no state change (stage + pending question unchanged);
   - self-heal: text at `INTERVIEW` with no interview state asks Q1;
   - an **unknown command** at `INTERVIEW` returns `INTERVIEW_UNKNOWN_COMMAND_TEXT`, records nothing, and leaves the current question unchanged;
   - `next_question` raises → `INTERVIEW_FAILURE_TEXT`, state unchanged (same pending question), no raise;
   - `summarise` raises → `INTERVIEW_FAILURE_TEXT`, not advanced, answered history unchanged, no raise.
5.2 **Green:** `app/orchestrator.py` — `async def start_interview(store, chat_id, *, interviewer) -> str` and `async def handle_interview_update(store, incoming, *, interviewer) -> str` (handles `text`, `photo`, and unknown `command` kinds), with the commit-only-on-success rule and wrapped Gemini calls (log type + message only).
5.3 Run `scripts/test` + `scripts/hooks`.

## Task group 6 — Photo flow starts the interview (Red/Green)
6.1 **Red:** `tests/test_photo_flow.py` — a **human** verdict now sets `INTERVIEW` and the reply is the intro + Q1 (fake interviewer); non-human unchanged; existing download/bouncer failure tests stay green.
6.2 **Green:** `app/photo_flow.py` — accept `interviewer`; when `apply_bouncer_verdict` returns `None`, call `orchestrator.start_interview`.
6.3 Run `scripts/test` + `scripts/hooks`.

## Task group 7 — Bot wiring: route INTERVIEW updates (Red/Green)
7.1 **Red:** `tests/test_bot.py` — a `LazyInterviewer` is built and injected; a **text** while `INTERVIEW` is routed to the orchestrator (answer recorded / progressed); a **photo** while `INTERVIEW` gets the redirect (**no** download); an **unknown command** while `INTERVIEW` gets `INTERVIEW_UNKNOWN_COMMAND_TEXT` and does **not** advance; existing AWAITING_PHOTO/command routing stays green; deps remain injectable.
7.2 **Green:** `app/bot.py` — build `LazyInterviewer(settings.require_gemini_key)`; in `dispatch`, before the generic text/photo branches, route `INTERVIEW`-stage `text`/`photo`/`command` updates to `orchestrator.handle_interview_update`; thread `interviewer` through `dispatch`/`dispatch_safely`/`build_application` and into `handle_photo_update`.
7.3 Run `scripts/test` + `scripts/hooks`.

## Task group 8 — Docs & spec reconciliation
8.1 Update `README.md` (interviewer usage; status), `SPECS/TECH.md` (Interviewer now in use; `app/orchestrator.py`; interview contracts; resilience note), `SPECS/ROADMAP.md` (Feature 3 status).
8.2 Record any deviations in this spec's `plan.md`/`validation.md`; optionally reconcile `SPECS/MISSION.md` principle #1 wording **only if the user approves**.
8.3 Run `scripts/test` + `scripts/hooks`.

---

## Guardrails
- **Scope guard:** no Converter/Scripter/Narrator, no image generation, no TTS, **nothing after `CONVERTING`**; no new stages.
- **Secrets & privacy:** `GEMINI_API_KEY` never logged; answer text and the trait summary are never logged (counts only); image bytes never logged; `.env` stays ignored.
- **Mock all externals** in unit tests: no network, no real ADK/Gemini, no real Telegram.
- Keep it simple and general; no config toggles, menus, or speculative abstractions.

## Implementation deviations

- **TG4 + TG5 + TG6 gates run together.** Changing `apply_bouncer_verdict` to
  return `str | None` made `mypy` fail on `app/photo_flow.py` (the sole caller),
  so the `photo_flow` consumer change (TG6) had to land with TG4/TG5 to keep
  `scripts/hooks` green. The intended work of all three groups was implemented;
  only the gate-run points were merged. No behavioural deviation.
- **Deliberate Feature 2 surface changes (per the approved spec).**
  `INTERVIEW_READY_TEXT` was removed (the entry reply now asks question 1), and
  `apply_bouncer_verdict` returns `None` for a human verdict (the caller starts
  the interview) and the refusal text otherwise. The Feature 2 test
  `test_interview_ready_text_asks_no_question` was deleted and the verdict tests
  updated.
- **Shared Google-key helper extracted.** `_ensure_google_api_key` moved from
  `app/bouncer.py` to a public `ensure_google_api_key` in `app/config.py`, now
  used by both the Bouncer and the Interviewer. The two Feature 2 key tests were
  retargeted to the shared helper; no shim was left behind in `bouncer`.
- **`start_interview` failure path.** The spec describes entry as "asks Q1,
  replies with `INTERVIEW_INTRO` + Q1" but does not state what to do if that very
  first Gemini call fails. Chosen behaviour (consistent with the self-heal and
  commit-only-on-success rules): return `INTERVIEW_FAILURE_TEXT`, leave a fresh
  `InterviewState` with no pending question, and let the next text self-heal by
  asking Q1 again. Covered by a test.
- **Commit-only-on-success.** An answer is recorded only *after* the model call
  succeeds. A failed `next_question`/`summarise` leaves the interview exactly as
  it was (same single pending question, no recorded answer), so a retry records
  exactly once against the unchanged question. Covered by a re-entrancy test.
- **`record(answer)` records the answer verbatim.** Blank/whitespace text is
  rejected before `record` is called; non-blank answers are stored as received.
- **`handle_interview_update` branches by kind.** Commands get
  `INTERVIEW_UNKNOWN_COMMAND_TEXT`; any other non-text/non-photo kind (sticker,
  document, unsupported media) gets the new `INTERVIEW_OTHER_REDIRECT_TEXT`.
  `bot.dispatch` routes `other`-kind updates at `INTERVIEW` to the orchestrator,
  but **no PTB handler matches sticker/document updates in the current handler
  registry**, so that branch is a **defensive default only** -- exercised by unit
  tests that call `dispatch` directly, not reachable from real Telegram today.
  (It would become reachable if a later feature registered such a handler.)
- **Bot registry catch-all (code-review fix; blocking finding).**
  `build_application` registers `MessageHandler(filters.COMMAND, _handle)`
  **last**, so unknown commands (`/foo`) reach `dispatch` -> orchestrator.
  Previously PTB dropped them, making the mid-interview nudge unreachable from
  real Telegram. `/start` and `/restart` keep their dedicated `CommandHandler`s
  and are still matched first. Covered by tests that drive PTB's real handler
  registry.
- **Validation-failure logging (code-review fix).** `_log_interviewer_failure`
  logs a `pydantic.ValidationError` as its type alone, because its message
  embeds `input_value` (the raw model output, which can carry answer/summary
  text). Other failures log a length-capped message (`_FAILURE_MESSAGE_MAX`).
