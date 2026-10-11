# TECH — Constraints and Architecture

## Runtime
Python 3, running locally in Codio during development.

## Stack (use these, do not substitute)
- Telegram API via **`python-telegram-bot`** (v21+) using LONG POLLING, not webhooks. Rationale: long polling reaches outbound to Telegram over HTTPS, works behind NAT/firewall on localhost, needs no public IP, SSL cert, or ngrok. Do not implement a webhook server.
- Transports and contracts: Pydantic v2 models at the boundaries. `Settings` (loaded from `.env`, secrets held as `SecretStr`), `ChatInput` (a normalised, typed view of every inbound update, with `chat_id` normalised to `int`), and `SessionState` (per-chat stage). Untrusted input is parsed/validated at the edge, never trusted raw.
- Google ADK (Agent Development Kit) to structure the pipeline as discrete specialized agents in a clear state machine, not ad-hoc chained API calls.
- Gemini 3.1 models:
  - Bouncer, Interviewer, Scripter: gemini-3.1-flash-lite
  - Converter: gemini-3.1-flash-image
  - Narrator: gemini-3.1-flash-tts-preview

## Secrets
Read TELEGRAM_BOT_TOKEN and GEMINI_API_KEY from .env via python-dotenv (`load_dotenv()`, then a Pydantic `Settings` model validates). Never hard-code, never log secret values. .env is git-ignored; .env.example documents the keys.

## Agents as a state machine
The Interviewer agent doubles as the orchestrator.
- Bouncer (agent, flash-lite multimodal): inspect uploaded image; confirm a human face. Reject non-human/absent-face with a humorous refusal and ask for another photo.
- Interviewer (agent + orchestrator, flash-lite): maintain multi-turn state; ask 5 to 7 pointed questions; then drive remaining stages.
- Converter (agent, flash-image): take original portrait plus interview summary; generate a hybrid-animal avatar image.
- Scripter (agent, flash-lite): authoritative British documentary-narrator persona; synthesize insights into ONE dramatic paragraph, max about 90 words, plain text only (no markdown, no stage directions, no emojis).
- Narrator (integration, NOT an agent, flash-tts-preview): render the Scripter paragraph to an audio file; send as a Telegram voice/audio message.

## State machine stages (per chat)
GREETING to AWAITING_PHOTO to BOUNCER_CHECK to INTERVIEW (5 to 7 Q) to CONVERTING to SCRIPTING to NARRATING to DONE.
/start and /restart from ANY stage cancel in-progress work, delete temp media, reset to GREETING.

## Resilience (point-bearing, do not skip)
1. /restart and /start full reset: cancel active session, purge all temp files for that chat (photo, PNG, audio), re-init orchestrator memory to GREETING. Must work mid-interview, mid-generation, any stage. No process reboot ever.
2. Try/except around every external call (every Telegram send/receive and every Gemini call). On failure: friendly message, offer retry, keep the process alive.
3. Edge cases (build these): photo sent during interview (gently redirect, no crash); text sent during Bouncer/awaiting-photo (prompt for a photo); Gemini timeout/error (caught, user notified, retry offered).
4. Temp-media hygiene: namespace each chat's media by chat_id; delete on completion AND on reset.

## Testing
Gates: `scripts/test` (pytest) and `scripts/hooks` (ruff + mypy). Red/Green TDD via write-tests for pure logic (state transitions, prompt builders, reset logic). Mock external calls in unit tests; keep secrets out of tests. LLM calls exercised by manual phone testing.

## Out of scope
No web frontend, no webhook server, no database (in-memory per-chat state plus temp files is sufficient), no user accounts.
