# TECH — Constraints and Architecture

## Runtime
Python 3.11+, running locally in Codio during development.

## Stack (use these, do not substitute)
- Telegram API via **`python-telegram-bot`** (v21+) using LONG POLLING, not webhooks. Rationale: long polling reaches outbound to Telegram over HTTPS, works behind NAT/firewall on localhost, needs no public IP, SSL cert, or ngrok. Do not implement a webhook server.
- Transports and contracts: Pydantic v2 models at the boundaries. `Settings` (loaded from `.env`, secrets held as `SecretStr`), `ChatInput` (a normalised, typed view of every inbound update, with `chat_id` normalised to `int`), and `SessionState` (per-chat stage, plus `interview: InterviewState | None`). Untrusted input is parsed/validated at the edge, never trusted raw.
  - **Interview contracts (Feature 3):** `InterviewQuestion(text)`, `InterviewAnswer(question, answer)`, `TraitSummary(summary, traits=[])`, and `InterviewState(answers, pending_question, summary)` with `record(answer)` pairing the pending question (fail loud if there is none). `SessionState.interview` is `None` until the interview starts; `InMemorySessionStore.reset` clears it.
- Google ADK (Agent Development Kit) to structure the pipeline as discrete specialized agents in a clear state machine, not ad-hoc chained API calls.
  - **In use (Feature 2):** the Bouncer is a real ADK `LlmAgent` (`gemini-3.1-flash-lite`, multimodal) behind a typed interface `Bouncer.inspect(image_path: Path) -> BouncerVerdict` (`app/bouncer.py`); `google-adk` is pinned in `requirements.txt`. The verdict is parsed via a Pydantic `output_schema` (schema, not regex). Unit tests inject a fake runner, so no network is used.
  - **In use (Feature 3):** the Interviewer is a real ADK `LlmAgent` (`gemini-3.1-flash-lite`) behind a typed interface `Interviewer` with `next_question(history) -> InterviewQuestion` and `summarise(history) -> TraitSummary` (`app/interviewer.py`). Both outputs are parsed via a Pydantic `output_schema` (schema, not regex). The fixed interview length is `TOTAL_QUESTIONS = 6`. The API key is held privately and never logged; `LazyInterviewer` builds the ADK delegate on first use (startup needs no key). Unit tests inject fake `question_runner`/`summary_runner` seams, so no network is used.
  - **In use (Feature 4):** the Converter is a real ADK `LlmAgent` (`gemini-3.1-flash-image`) behind a typed interface `Converter.generate_avatar(portrait_path: Path, summary: TraitSummary) -> bytes` (`app/converter.py`). It returns the raw image bytes and does **no** filesystem work (it never sees `chat_id`); the orchestrator writes those bytes to `avatar_dest(chat_id)` (`<chat dir>/avatar.png` or `avatar.jpg`, matching decoded PNG/JPEG). The API key is held privately and never logged, and `LazyConverter` builds the ADK delegate on first use. Unit tests inject a fake `runner`, so no network is used.
  - **Orchestrator seam (Features 3–4):** `app/orchestrator.py` owns the `INTERVIEW`-stage routing (`start_interview`, `handle_interview_update`) and drives progression: one question at a time, commit-only-on-success, `INTERVIEW -> CONVERTING` after the final answer. The `CONVERTING`-stage step (`handle_converting_update`) generates one avatar, writes then sends it, advances `CONVERTING -> SCRIPTING` on success, and deletes the generated file; any failure leaves the stage at `CONVERTING` for a retry. The photo send is the injected typed `SendPhotoFn = Callable[[int, Path, str], Awaitable[bool]]` built in `bot.py` over `safe_send_photo` (mirroring `safe_send_message`). `bot.dispatch` only delegates: it routes updates at `CONVERTING` to the step (retry/self-heal) and runs it once after the interview acknowledgement. `build_application` registers a catch-all `MessageHandler(filters.COMMAND, ...)` **last**, so unknown commands reach the dispatcher without shadowing `/start`/`/restart`. Nothing runs after `SCRIPTING` yet.
- Gemini 3.1 models:
  - Bouncer, Interviewer, Scripter: gemini-3.1-flash-lite
  - Converter: gemini-3.1-flash-image
  - Narrator: gemini-3.1-flash-tts-preview

## Secrets
Read TELEGRAM_BOT_TOKEN and GEMINI_API_KEY from .env via python-dotenv (`load_dotenv()`, then a Pydantic `Settings` model validates). Never hard-code, never log secret values. .env is git-ignored; .env.example documents the keys.

## Agents as a state machine
The Interviewer agent doubles as the orchestrator.
- Bouncer (agent, flash-lite multimodal): inspect uploaded image; confirm a human face. Reject non-human/absent-face with a humorous refusal and ask for another photo.
- Interviewer (agent + orchestrator, flash-lite): maintain multi-turn state on the per-chat session; ask 6 (`TOTAL_QUESTIONS`) pointed questions one at a time; synthesise a `TraitSummary`; drive the `INTERVIEW -> CONVERTING` handoff (`app/interviewer.py`, `app/orchestrator.py`).
- Converter (agent, flash-image): takes the original portrait plus the interview summary; generates one hybrid-animal avatar image (`app/converter.py`). The `CONVERTING` step in `app/orchestrator.py` writes the returned bytes to the chat's temp dir and sends them as a Telegram photo with a canned documentary caption (`CONVERTER_CAPTION`, plain text) via `safe_send_photo`, then advances to `SCRIPTING`.
- Scripter (agent, flash-lite): authoritative British documentary-narrator persona; synthesize insights into ONE dramatic paragraph, max about 90 words, plain text only (no markdown, no stage directions, no emojis).
- Narrator (integration, NOT an agent, flash-tts-preview): render the Scripter paragraph to an audio file; send as a Telegram voice/audio message.

## State machine stages (per chat)
GREETING to AWAITING_PHOTO to BOUNCER_CHECK to INTERVIEW (5 to 7 Q) to CONVERTING to SCRIPTING to NARRATING to DONE.
/start and /restart from ANY stage cancel in-progress work, purge that chat's temp media, and re-ask for a photo (they advance to AWAITING_PHOTO).
Besides reset-to-GREETING, the only non-ordered move allowed is the Bouncer retry edge **BOUNCER_CHECK -> AWAITING_PHOTO** (a rejected photo returns the chat to awaiting a photo); every other non-ordered move stays illegal (`IllegalTransitionError`, fail loud).

## Resilience (point-bearing, do not skip)
1. /restart and /start full reset: cancel active session, purge all temp files for that chat (photo, PNG, audio), re-init orchestrator memory to AWAITING_PHOTO (the reset reply asks for a photo). Must work mid-interview, mid-generation, any stage. No process reboot ever.
2. Try/except around every external call (every Telegram send/receive and every Gemini call). On failure: friendly message, offer retry, keep the process alive. During the interview a Gemini failure returns a friendly retry and leaves the interview state **exactly** as it was (commit-only-on-success); during conversion a Gemini-image failure or a Telegram photo-send failure (`safe_send_photo`) returns a friendly retry, leaves the stage at `CONVERTING`, removes any generated file, and lets the next update retry. Converter and Telegram send failures log operation/chat ID/exception type only, never arbitrary exception text or tracebacks. Earlier-stage logging remains unchanged and is part of Feature 7 review.
3. Edge cases (build these): photo sent during interview (gently redirect, no state change, no download); blank/whitespace answer (re-ask the current question, not recorded); unknown slash-command during interview (nudge to answer or `/restart`, not recorded); text sent during Bouncer/awaiting-photo (prompt for a photo); Gemini timeout/error (caught, user notified, retry offered).
4. Temp-media hygiene: namespace each chat's media by chat_id (portrait `photo.jpg`, generated avatar `avatar.png` or `avatar.jpg`); delete on completion AND on reset (`purge_chat_media` removes the whole chat directory).

## Testing
Gates: `scripts/test` (pytest) and `scripts/hooks` (ruff + mypy). Red/Green TDD via write-tests for pure logic (state transitions, prompt builders, reset logic). Mock external calls in unit tests; keep secrets out of tests. LLM calls exercised by manual phone testing.

## Out of scope
No web frontend, no webhook server, no database (in-memory per-chat state plus temp files is sufficient), no user accounts.

## Feature 4 hardening implementation
- `ConversionTasks` owns per-chat tasks; reset invalidates a monotonic session
  version before cancellation. `InMemorySessionStore.reset` also invalidates.
- The live Application always injects this registry. Sequential handlers and
  `concurrent_updates=1` are retained. The direct dispatch seam may be awaited
  without a registry for deterministic callers; it is not the live entry point.
- Session ownership is checked before work, after generation and after sending.
  Old tasks cannot send a new photo, commit a transition or clean newer media.
- `post_stop` cancels tracked tasks before Telegram shutdown; cancellation is
  not converted to a retry. A third-party coroutine that suppresses cancellation
  indefinitely cannot be forcibly terminated by asyncio; reset still invalidates
  its results. SDK calls must remain cooperative.
- Pillow is the one added dependency. Accept decoded PNG/JPEG only, with matching
  MIME, at most 10 MiB and 20 million pixels. Keep original bytes, no re-encoding.
- Generation deadline: 120 seconds. Photo-send deadline: 30 seconds.
- Filesystem failures use the same safe retry path. Finally cleanup only acts on
  the owning session. Cleanup failure after delivery does not roll back SCRIPTING.
- No Scripter/Narrator work. No model-ID changes. MISSION.md unchanged.
