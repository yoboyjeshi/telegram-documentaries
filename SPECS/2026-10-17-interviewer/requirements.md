# Requirements — Feature 3: The Interviewer (and orchestrator)

## Context
- **Roadmap position:** `SPECS/ROADMAP.md` feature **3 of 8** ("The Interviewer (+ orchestrator)").
- **Constitution:** honours `SPECS/MISSION.md` (never crash; clean full reset incl. temp-media purge and interview state; one session per chat; secrets only in `.env`; YAGNI) and `SPECS/TECH.md` (ADK agents; typed contracts at the boundaries; **gemini-3.1-flash-lite**; resilience; per-chat state; Red/Green TDD).
- **Branch:** `feature/2026-10-17-interviewer`
- **Spec folder:** `SPECS/2026-10-17-interviewer/`
- **Builds on:** Feature 1 (gateway, `Stage` machine, `InMemorySessionStore`, `@logged`/`@guard_errors`, `safe_send_message`/`dispatch_safely`) and Feature 2 (photo intake, `apply_bouncer_verdict`, `INTERVIEW` entry).

## Goal
When the Bouncer advances a chat to `INTERVIEW`, an **Interviewer ADK agent** (`gemini-3.1-flash-lite`) runs a **6-question, one-at-a-time** interview about the user's quirks, daily habits, and personality. Each answer is recorded on the **per-chat session**; after the final question the agent synthesises a **concise typed trait summary** and the chat advances to `CONVERTING` with an acknowledgement — **no Converter/image generation yet**. The interview flow lives in a small **orchestrator** seam that later stages hang off. The whole path obeys the state machine, never logs answers/keys/image bytes, resets cleanly from mid-interview, and never crashes.

## In scope
1. **Interview entry:** when the Bouncer verdict is human, the chat advances to `INTERVIEW` **and the bot immediately asks question 1**. A rejected photo keeps today's behaviour (refusal, back to `AWAITING_PHOTO`).
2. **Interviewer agent:** a real Google ADK `LlmAgent` behind a typed interface:
   - `async def next_question(history: Sequence[InterviewAnswer]) -> InterviewQuestion` — one pointed question, mindful of what was already asked.
   - `async def summarise(history: Sequence[InterviewAnswer]) -> TraitSummary` — a concise synthesis of the collected answers.
   The model's output is parsed via a Pydantic `output_schema` (**schema, not regex**). Unit tests inject fake runners (no network).
3. **Session model extension:** store interview progress on the existing per-chat `SessionState` — the completed Q/A pairs, the currently pending question, and (at the end) the trait summary. One interview per chat.
4. **Progression (pure logic):** ask question 1..N one at a time, advancing exactly one step per valid answer; the count is a **fixed constant `TOTAL_QUESTIONS = 6`** (within the 5–7 range).
5. **Summary + handoff:** after the Nth answer, produce the `TraitSummary`, store it, advance `INTERVIEW -> CONVERTING` (the existing ordered step), and reply with a friendly acknowledgement. **Do not build the Converter.**
6. **Edge cases (constitution):**
   - A **photo sent mid-interview** → gentle redirect; stage and current question unchanged; no download, no crash.
   - A **blank/whitespace answer** → re-ask the current question; not recorded; no advance.
   - An **unknown slash-command mid-interview** (e.g. `/foo`, neither `/start` nor `/restart`) → a short nudge (`INTERVIEW_UNKNOWN_COMMAND_TEXT`) inviting the user to answer the question or use `/restart`; **not** recorded as an answer; the current question is unchanged.
   - `/start` and `/restart` mid-interview → full reset and temp-media purge (existing behaviour), now **also clearing the interview state**.
7. **Resilience:** every Gemini call and every Telegram send is wrapped. A Gemini failure mid-interview → friendly "try again" message, interview state unchanged, process alive (reuse `@guard_errors`, `safe_send_message`, `dispatch_safely`).
8. **Orchestrator seam:** a minimal `app/orchestrator.py` owns the `INTERVIEW`-stage routing (for now only that stage) and delegates to the interviewer; `bot.dispatch` just calls it. Later features (Converter, Scripter, Narrator) extend this module.
9. **Red/Green unit tests** for the pure logic (progression 1..N, answer collection, summary assembly, reset clearing state, photo-mid-interview). **All external calls mocked.**

## Out of scope (YAGNI — later features)
- Converter, Scripter, Narrator; any image generation or TTS. **Nothing happens after reaching `CONVERTING`.**
- No new pipeline stages (`CONVERTING` already exists). No inline keyboards/menus, no skip/back/re-ask commands, no answer editing, no question randomisation beyond the model, no persistence/DB, no i18n.
- No caption handling; a photo mid-interview is only redirected.

## Decisions (confirmed with user)
- **The ADK agent drives it all:** it generates each next question (given the history) *and* the final trait summary.
- **Fixed question count = 6** (`TOTAL_QUESTIONS` constant), inside the 5–7 requirement.
- **`TraitSummary` is a typed Pydantic schema** (mocked in tests), stored on the session for the Converter later.
- **Minimal `app/orchestrator.py`** owns the `INTERVIEW`-stage routing; `bot.py` delegates to it.
- **Photo mid-interview = gentle redirect, no state change.**
- **Blank/whitespace text = re-ask the current question, not recorded.**
- **No backward-compatibility shims.** Deliberate changes to Feature 2 surface:
  - `INTERVIEW_READY_TEXT` is replaced by `INTERVIEW_INTRO` (the entry reply now *does* ask a question). The Feature 2 test `test_interview_ready_text_asks_no_question` is removed.
  - `apply_bouncer_verdict(...)` returns the refusal text for a non-human verdict and **`None`** for a human one (the caller then starts the interview). Feature 2 tests for it are updated.
- **Small shared helper:** extract the Google-API-key env helper from `app/bouncer.py` (`_ensure_google_api_key`) to a public `ensure_google_api_key` in `app/config.py`, used by both agents (warn only when a *different* value is already set, never logging any key).
- **Unknown slash-command mid-interview:** **do not** silently ignore it. Reply with a short nudge — `INTERVIEW_UNKNOWN_COMMAND_TEXT` ("That isn't a command I recognise — just answer the question, or use /restart to start over.") — and stay on the current question. It is **not** recorded as an answer.

## Typed contracts (parse at the edge, prefer schemas over regex)
- **`InterviewQuestion` (Pydantic v2):** `text: str` — the agent's single question (validated schema).
- **`TraitSummary` (Pydantic v2):** `summary: str`; `traits: list[str] = []` — concise, stored for later stages.
- **`InterviewAnswer` (Pydantic v2):** `question: str`; `answer: str` — one completed Q/A pair.
- **`InterviewState` (Pydantic v2):** `answers: list[InterviewAnswer] = []`; `pending_question: str | None = None`; `summary: TraitSummary | None = None`. `record(answer)` pairs the pending question with the answer and clears the pending slot (fail loud if there is no pending question).
- **`SessionState` (extended):** `interview: InterviewState | None = None`.
- **`Interviewer` (interface):** the two async methods above; the real `AdkInterviewer` holds the key privately and accepts injected `question_runner`/`summary_runner` seams for tests.
- **Failure behaviour:** a missing `GEMINI_API_KEY` or a model/parse error is an **interviewer failure** (friendly retry, state unchanged, no crash). Fail loud in logs (type + message only), friendly to the user.

## Behaviour details
- **Transitions:** only the existing ordered moves. `INTERVIEW -> CONVERTING` is the ordered step; there are **no new edges**. Reset-to-`GREETING` stays legal from any stage.
- **Entry:** human verdict → `INTERVIEW` → `start_interview` sets a fresh `InterviewState`, asks Q1, replies with `INTERVIEW_INTRO` + Q1.
- **Self-heal:** if a text arrives at `INTERVIEW` but no interview/pending question exists (e.g. a prior Q1 call failed), the orchestrator asks Q1 rather than erroring.
- **Progress:** on a valid answer, generate the next question using the updated history; only **commit** the answer/pending question once the model call succeeds (a failed call leaves the interview exactly as it was so the user can retry).
- **Completion:** on the Nth answer, generate the summary; on success store it and advance to `CONVERTING`, replying with `INTERVIEW_COMPLETE_TEXT`.
- **Reset:** `store.reset(chat_id)` returns the stage to `GREETING` **and clears `interview`**; `handle_start`/`handle_restart` keep purging that chat's temp media and end at `AWAITING_PHOTO`.

## Logging
- `@logged` continues to log handler name, `chat_id`, and stage.
- Interview progress is logged as **counts and stage only** (e.g. `interview answered=3/6`), never the raw answer text (personal) and never the summary text.
- **Never** log the `GEMINI_API_KEY`, key values, image bytes, or secret values.
- Gemini/parse failures log the exception **type + message only** (a full traceback can embed the request payload) at `ERROR`; the user gets a friendly, non-technical message.

## Testing
- **Red/Green TDD.** Tests first, fail until implemented.
- **Mock everything external:** the Interviewer ADK/Gemini calls (injected fake runners) and Telegram sends. No network; secrets stay out of tests.
- Unit-test areas: `InterviewState`/`InterviewAnswer`/`TraitSummary` schemas + `record` fail-loud; `TOTAL_QUESTIONS` is 6 and within 5–7; `AdkInterviewer` key handling (raises without a key, never logs it, delegates to injected runners, lazy build); pure texts; progression 1..N advancing on each answer; answer collection; summary assembly + `INTERVIEW -> CONVERTING` handoff; blank answer re-asks without advancing; photo mid-interview redirects with no state change; interviewer failure → friendly + retry + state unchanged + no crash; reset clears interview state and purges media; bot routing (INTERVIEW text → orchestrator, INTERVIEW photo → redirect, INTERVIEW unknown command → nudge); logging hygiene (no key/answers/bytes).
- Gates: `scripts/test` (pytest) and `scripts/hooks` (ruff + mypy).

## Dependencies
- **None new.** `google-adk` is already pinned (`==2.11.0`); no `requirements.txt` change expected.

## Docs to reconcile during validation
- `README.md` (interviewer usage/status), `SPECS/TECH.md` (Interviewer now in use; orchestrator module; interview contracts), `SPECS/ROADMAP.md` (Feature 3 status), and this spec folder. Optionally `SPECS/MISSION.md` principle #1 wording ("return to greeting") **on user approval only**.

## Open questions
- None. The unknown-command behaviour was resolved with the user (nudge + stay on the current question).
