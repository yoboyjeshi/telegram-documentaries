# Requirements — Feature 2: The Bouncer

## Context
- **Roadmap position:** `SPECS/ROADMAP.md` feature **2 of 8** ("The Bouncer").
- **Constitution:** honours `SPECS/MISSION.md` (never crash; clean full reset including temp-media purge; secrets only in `.env`; one session per chat; YAGNI) and `SPECS/TECH.md` (Google **ADK** agents; typed contracts; **gemini-3.1-flash-lite** multimodal; temp-media hygiene; resilience; long polling; Red/Green TDD).
- **Branch:** `feature/2026-10-11-bouncer`
- **Spec folder:** `SPECS/2026-10-11-bouncer/`
- **Builds on:** Feature 1 (`app/` gateway, state machine, session store, logging, resilience).

## Goal
Accept a Telegram **photo**, verify with the **Bouncer agent** that it shows a human face, and advance the chat to `INTERVIEW` when it does — a non-human / no-face photo gets a humorous refusal and the chat stays awaiting another photo. The whole path must obey the existing state machine, keep secrets and image bytes out of the logs, purge temp media on reset, and never crash. **No interview questions yet.**

## In scope
1. **Photo intake:** handle Telegram PHOTO updates; download the image to a **chat-namespaced temp path keyed by `chat_id`**.
2. **Bouncer agent** (Google ADK, `gemini-3.1-flash-lite`, multimodal) inspects the image and returns a typed verdict: human face present, or not.
3. **State wiring** (existing `Stage` machine):
   - `/start` **and** `/restart` reset the chat, **purge that chat's temp media**, and advance `GREETING -> AWAITING_PHOTO` (the greeting asks for a photo).
   - A photo is accepted **only** at `AWAITING_PHOTO`.
   - **Human:** `AWAITING_PHOTO -> BOUNCER_CHECK -> INTERVIEW`; reply that they're in — **ask no question**.
   - **Non-human / no face:** `AWAITING_PHOTO -> BOUNCER_CHECK -> AWAITING_PHOTO`; humorous refusal; ask for another photo.
   - **Photo at the wrong stage:** gentle redirect, no download, no crash, stage unchanged.
4. **Secrets:** read `GEMINI_API_KEY` through the existing `Settings`; **never** log the key or the image bytes.
5. **Resilience:** the Telegram photo download and the Gemini call are each wrapped; on failure the user is notified and offered a retry, the chat stays at `AWAITING_PHOTO`, and the process never crashes (reuse `safe_send_message`, `dispatch_safely`, `@guard_errors`).
6. **Text at `AWAITING_PHOTO`:** gently prompt for a photo (do not confuse the user with an echo at this stage).
7. **Red/Green unit tests** for the pure logic; **all external calls mocked** (no network, no real Gemini, no real Telegram).

## Out of scope (YAGNI — later features)
- Interviewer / questions & answers, Converter, Scripter, Narrator; any image generation or TTS. **No interview questions.**
- No new pipeline stages; no webhook, DB, user accounts, rate limiting, i18n.
- No caption handling, no multi-photo batching, no client-side image resizing/validation beyond what the model needs.

## Decisions (confirmed with user)
- **Bouncer = real Google ADK `LlmAgent`** (`gemini-3.1-flash-lite`, multimodal) behind a small typed interface `Bouncer.inspect(path) -> BouncerVerdict`. Unit tests mock the interface; LLM behaviour is exercised by manual phone testing (`TECH.md`).
- **`/start` and `/restart` advance to `AWAITING_PHOTO`.** This deliberately changes Feature 1's post-reset stage (`GREETING`); the affected Feature 1 tests are updated. The greeting text now asks for a photo.
- **Refusal text = canned deterministic lines.** A fixed, warm, PG pool; chosen deterministically from `chat_id` (same chat == same line). No extra API call. Never mocks the user's appearance.
- **Text at `AWAITING_PHOTO` prompts for a photo.**
- **Backward compatibility:** the `/start`/`/restart` stage change is intentional and user-approved; **no** legacy shims or toggles.

## Typed contracts (parse at the edge, prefer schemas over regex)
- **`ChatInput` (extended).** `kind: Literal["command", "text", "photo", "other"]`; new optional `file_id: str | None = None`. A photo update yields `kind="photo"` with the **largest** `PhotoSize.file_id`; `chat_id` stays normalised to `int`. The adapter stays pure — it does **not** download.
- **`BouncerVerdict` (Pydantic v2).** `is_human: bool`; `reason: str | None = None` (short, log-only rationale — never shown raw to the user).
- **`Bouncer` (interface).** `async def inspect(image_path: Path) -> BouncerVerdict`.
- **Download callable (injected).** `async def download(file_id: str, dest: Path) -> Path`. The real implementation uses PTB `get_file` + `download_to_drive`; tests inject a fake.
- **Media path schema.** Root `<system temp>/telegram_documents_media`-style dir; per-chat dir `root/<chat_id>/`; photo file `<chat dir>/photo.jpg` (single slot per chat; a new photo overwrites the previous one to bound disk use).
- **Failure behaviour.** A foreign update (no message) is ignored + `WARNING`. A malformed photo (missing `file_id`) is treated as unsupported (`WARNING`), never raises. A missing `GEMINI_API_KEY` or model error is a **bouncer failure**: friendly message + retry, no crash. Fail loud in logs, friendly to the user.

## Behaviour details
- **Transitions:** the ordered steps plus **one explicit retry edge `BOUNCER_CHECK -> AWAITING_PHOTO`** (Bouncer rejection). Every other non-ordered move stays illegal (`IllegalTransitionError`, fail loud). Reset-to-`GREETING` is always permitted.
- **Reset purge:** `/start` and `/restart` delete the chat's temp directory (idempotent; a missing dir is fine) **and** isolate per chat (purging one chat never touches another).
- **Verdict application (pure):** human -> `set_stage(INTERVIEW)`; non-human -> `set_stage(AWAITING_PHOTO)` + refusal.
- **`INTERVIEW_READY_TEXT`** acknowledges the accepted photo and says the interview is coming — it asks **no** question (Feature 3 owns that).

## Logging
- `@logged` continues to log handler name, `chat_id`, and stage; the Bouncer outcome (`is_human`) is logged at `INFO` with `chat_id`.
- **Never** log the `GEMINI_API_KEY`, the raw image bytes, or secret values (the `httpx` INFO suppression from Feature 1 already keeps Telegram URLs — which embed the bot token — out of logs).
- Download/bouncer failures log the traceback at `ERROR`; the user still gets a friendly, non-technical message.

## Testing
- **Red/Green TDD.** Tests first, fail until implemented.
- **Mock everything external:** the ADK/Gemini call and the Telegram download. No network in unit tests; secrets stay out of tests.
- Unit-test areas: adapter photo contract; media namespacing + purge isolation; the retry transition's legality; `BouncerVerdict` schema + refusal determinism + `AdkBouncer` wiring (no network); pure stage-gating (accept vs redirect); verdict handling (human vs non-human); async orchestration resilience (download failure, bouncer failure, missing key -> friendly + retry, no crash); reset purges temp media; logging hygiene (no key, no bytes).
- Gates: `scripts/test` (pytest) and `scripts/hooks` (ruff + mypy).

## Dependencies
- Add **`google-adk`** to `requirements.txt` (pinned to the major resolved at install) and install it. Record the exact resolved version during validation.

## Docs to reconcile during validation
- `README.md` (photo usage), `SPECS/TECH.md` (ADK now actually used; Bouncer model/interface), `SPECS/ROADMAP.md` (Feature 2 status), and this spec folder.

## Open questions
- None blocking — ADK approach, `/start` stage change, refusal source, and text-at-awaiting-photo behaviour were resolved with the user before writing this spec.
