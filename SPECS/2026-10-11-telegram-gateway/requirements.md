# Requirements — Feature 1: Telegram Gateway

## Context
- **Roadmap position:** `SPECS/ROADMAP.md` feature **1 of 8** ("Telegram Gateway"). It is the foundation every later feature builds on.
- **Constitution:** honours `SPECS/MISSION.md` (never crash; clean full reset; secrets only in `.env`; one session per chat; YAGNI) and `SPECS/TECH.md` (Telegram via **long polling**, no webhook; state-machine design; typed contracts; Red/Green TDD).
- **Branch:** `feature/2026-10-11-telegram-gateway`
- **Spec folder:** `SPECS/2026-10-11-telegram-gateway/`

## Goal
Stand up the Telegram transport and the per-chat state scaffold so later features (Bouncer, Interviewer, Converter, Scripter, Narrator) can be added without rework. Nothing is generative yet — this is plumbing plus the state skeleton.

## In scope
1. Connect to Telegram via **long polling** using **`python-telegram-bot`**, reading `TELEGRAM_BOT_TOKEN` from `.env` loaded with **`python-dotenv`**.
2. `/start` handler: greet the user and reset **that chat's** session to `GREETING`.
3. `/restart` handler: identical effect — greet and reset to `GREETING`.
4. **Echo** handler: on any non-command text message, reply with the same text (so the pipe can be confirmed on a phone).
5. Per-chat, in-memory **session store** keyed by `chat_id` (typed as `int`).
6. **State-machine scaffold:** a `Stage` enum with the eight stages, per-chat current-stage tracking, and pure **transition helpers**.
7. **Resilience:** every Telegram send is wrapped so a failure notifies/logs and never crashes the process.

## Out of scope (YAGNI — deferred to later features)
- Bouncer, Interviewer, Converter, Scripter, Narrator; any **Gemini**/ADK calls.
- Media download/upload, temp-file handling, image generation, TTS.
- Database or any persistence (in-memory is sufficient).
- Webhook server, web frontend, user accounts, rate limiting, i18n.

## Decisions (confirmed with user)
- **Telegram library:** `python-telegram-bot` (v21+), async, **long polling** via `Application.run_polling()`.
- **Contracts:** **Pydantic v2** models at boundaries; `SecretStr` for secrets. (Refines `TECH.md`'s "a Python Telegram library".)
- **Layout:** `app/` package; entrypoint `python -m app.main` (matches README).
- **Tooling:** `pytest`, `ruff`, `mypy`; dev scripts `scripts/test` and `scripts/hooks`; `requirements.txt` + `pyproject.toml`.
- **Env loading:** `python-dotenv` `load_dotenv()` at startup, then a Pydantic `Settings` model validates.
- **Backward compatibility:** none required (greenfield); no legacy shims or toggles.

## Typed contracts (parse at the edge)
- **`Settings`** — `telegram_bot_token: SecretStr` (required), `gemini_api_key: SecretStr | None` (declared for later features, unused now). A missing token **fails loud** at startup with a clear message; the value is **never** logged or printed.
- **`ChatInput`** — normalized view of an inbound update: `chat_id: int`, `kind: Literal["command", "text", "other"]`, `text: str | None`, `command: str | None`. Produced by a single `extract_chat_input(update)` adapter. **`chat_id` is normalized to `int`** so it can be used as a session key consistently (guards the string-vs-int lookup class of bug).
- **`Stage`** — `str` enum, in pipeline order: `GREETING, AWAITING_PHOTO, BOUNCER_CHECK, INTERVIEW, CONVERTING, SCRIPTING, NARRATING, DONE`.
- **`SessionState`** — `chat_id: int`, `stage: Stage = Stage.GREETING`.
- **Failure behaviour:** an update that yields no usable `ChatInput` (foreign/unsupported type) is **ignored** and logged at `WARNING` — it never raises into the polling loop.

## Behaviour details
- A single `GREETING_TEXT` constant is returned by both `/start` and `/restart`.
- `reset(chat_id)` is always legal from **any** stage and returns the chat to `GREETING`.
- The store creates a fresh `GREETING` state on first access for an unknown `chat_id`.
- Sessions are **isolated per chat**; a reset in one chat must not affect another.
- Ordered transitions are validated by pure helpers; an **illegal pipeline transition raises `IllegalTransitionError`** (fail loud, covered by tests). Reset-to-`GREETING` is always permitted.
- Echo replies with the received text verbatim.

## Logging
- Standard-library `logging`, configured once at startup.
- A decorator (`@logged`) wraps handlers and logs the handler name, `chat_id`, and stage before/after — **never** secret values or full message bodies.
- A `@guard_errors` decorator wraps handlers: on exception it logs the traceback and returns a friendly message instead of propagating. A global `Application` error handler is registered as a backstop.

## Testing
- **Red/Green TDD.** Tests are written first and fail until implemented.
- Pure logic (state init, reset, transitions, store isolation, pure handlers) is unit-tested with **no network** and **no real Telegram**.
- Telegram sends are mocked (`AsyncMock`); secrets are kept out of tests.
- Run `scripts/test` (pytest) and `scripts/hooks` (ruff + mypy) as the gates.

## Docs to reconcile during validation
- `README.md`: add `scripts/test` / `scripts/hooks` usage and confirm run instructions.
- `SPECS/TECH.md`: name `python-telegram-bot` and the Pydantic contract approach (refinement of the current wording).

## Open questions
- None blocking — library, contracts, layout, and tooling resolved with the user above.
