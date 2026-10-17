# Requirements — Feature 4: The Converter

## Context
- **Roadmap position:** `SPECS/ROADMAP.md` feature **4 of 8** ("The Converter").
- **Constitution:** honours `SPECS/MISSION.md` (never crash; clean full reset incl. temp-media purge; one session per chat; secrets only in `.env`; YAGNI) and `SPECS/TECH.md` (ADK agents; typed contracts at the boundaries; **gemini-3.1-flash-image** for the Converter; resilience; per-chat state; Red/Green TDD).
- **Branch:** `feature/2026-10-17-converter`
- **Spec folder:** `SPECS/2026-10-17-converter/`
- **Builds on:** Feature 1 (gateway, `Stage` machine, `InMemorySessionStore`, `@logged`/`@guard_errors`, `safe_send_message`/`dispatch_safely`) and Feature 2 (photo intake saves the portrait to a chat-namespaced temp path via `photo_dest`) and Feature 3 (`app/orchestrator.py` owns stage routing; on completion the Interviewer stores a `TraitSummary` on `SessionState.interview` and advances `INTERVIEW -> CONVERTING` — **the handoff that currently goes nowhere**).

## Goal
When the Interviewer advances a chat to `CONVERTING`, a **Converter ADK agent** (`gemini-3.1-flash-image`) takes the **user's original portrait** (the chat-namespaced temp file already saved by the Bouncer) plus the **`TraitSummary`** from the Interviewer, and **generates exactly ONE hybrid-animal avatar** — the person reimagined as a hybrid of an animal that matches their interview traits. The bot **saves the image to a chat-namespaced temp path**, **sends it to the Telegram chat as a photo with one short documentary-tone caption (plain text, no markdown, no emojis)**, then **advances the stage to `SCRIPTING` and stops**. The step lives in the existing `app/orchestrator.py` seam. Every Gemini image call and every Telegram photo send is wrapped: a failure notifies and offers a retry, never crashes, and leaves the state at `CONVERTING`. Reset purges all temp media **including the generated image**. **Nothing beyond `SCRIPTING` is built** (no Scripter, no narration, no TTS).

## In scope
1. **Trigger / handoff:** when the Interviewer completes and the chat becomes `CONVERTING`, the bot **immediately runs the Converter step** (instead of stopping at the acknowledgement). Any update that arrives **while the chat is already at `CONVERTING`** re-runs the step (the retry path / self-heal).
2. **Converter agent:** a real Google ADK agent (`gemini-3.1-flash-image`) behind a typed interface:
   - `async def generate_avatar(portrait_path: Path, summary: TraitSummary) -> bytes` — returns the raw generated image bytes.
   The ADK agent is imported **lazily** (like the Bouncer/Interviewer) so importing the module needs no model, network, or key. The API key is held privately and **never logged**. Unit tests inject a fake runner (no network).
3. **Inputs:** the original portrait at the existing chat-namespaced path (`photo_dest(chat_id)`) and the `TraitSummary` stored on `SessionState.interview.summary`.
4. **Action — exactly ONE image:** send the portrait + a prompt built from the trait summary to the model; **one** image comes back. Save it to a chat-namespaced temp path (`avatar_dest(chat_id)`).
5. **Send as a photo + caption:** send the saved image back to the chat as a Telegram **photo** with a single short documentary-tone caption (`CONVERTER_CAPTION`), **plain text only** (no markdown/emoji).
6. **Advance then stop:** on success, advance `CONVERTING -> SCRIPTING` (the existing ordered step) and **do nothing further**. No narration text, no TTS, no extra reply beyond the photo + caption.
7. **Orchestrator wiring:** add the `CONVERTING`-stage step to `app/orchestrator.py`; `bot.dispatch` routes to it (both the post-interview handoff and retry updates at `CONVERTING`). The orchestrator receives the photo-send capability as an injected callable (mirroring how `photo_flow` receives the download callable).
8. **Resilience (reuse existing patterns, do not reinvent):**
   - Gemini image failure → friendly `CONVERTER_FAILURE_TEXT` ("try again"), **state unchanged at `CONVERTING`**, no crash (wrap + log type/message only).
   - Telegram photo-send failure → same retry path; **state unchanged at `CONVERTING`**, no crash.
   - **Missing portrait file** (or missing trait summary) → friendly `CONVERTER_MISSING_PORTRAIT_TEXT` asking the user to `/restart`; **no crash**, state stays `CONVERTING`.
   - Reuse `@logged` / `@guard_errors` / a new `safe_send_photo` alongside the existing `safe_send_message`.
9. **Reset / temp-media hygiene:** `/start` and `/restart` still fully reset the chat **and purge its temp media, including the generated avatar** (existing `purge_chat_media` removes the chat directory). The generated image is also **deleted on successful completion**.
10. **Red/Green unit tests** for the pure logic and orchestration (success path, missing portrait, Gemini failure, send failure, reset purge). **All external calls (Gemini + Telegram) mocked.**

## Out of scope (YAGNI — later features)
- **Scripter and Narrator:** no narration text, no TTS, **no `SCRIPTING`/`NARRATING` work**. Nothing happens after the stage reaches `SCRIPTING`.
- No new pipeline stages (`CONVERTING`/`SCRIPTING` already exist); no image variants/galleries, no regeneration command, no menus/inline keyboards, no caption editing, no persistence/DB, no i18n, no prompt tuning UI.
- No change to the portrait intake or interview flow beyond triggering the new step on handoff.

## Decisions (confirmed with user)
- **Exactly ONE image** is generated and sent per conversion.
- **Model:** `gemini-3.1-flash-image` via Google ADK (`CONVERTER_MODEL` constant).
- **Trigger:** runs on the `INTERVIEW -> CONVERTING` handoff, and re-runs on any update while at `CONVERTING` (the retry path).
- **Missing portrait (or missing summary):** send `CONVERTER_MISSING_PORTRAIT_TEXT` asking the user to `/restart`; do not crash; stay at `CONVERTING`.
- **Caption:** a single short documentary-tone line (`CONVERTER_CAPTION`), **plain text** (no markdown/emoji). It is a **canned constant** (deterministic and testable, matching the Bouncer's canned refusals) — **no extra model call**.
- **Converter contract:** `generate_avatar(portrait_path, summary) -> bytes`; the **orchestrator writes the returned bytes** to `avatar_dest(chat_id)` (the agent does no filesystem work and never sees `chat_id`, mirroring the Bouncer returning a verdict).
- **Generated-image lifecycle:** written to `avatar_dest(chat_id)` (`<chat dir>/avatar.png`); **deleted on successful completion**; removed by `purge_chat_media` on `/start`/`/restart`.
- **Photo send:** a new `safe_send_photo` helper (mirrors `safe_send_message`: catches `Exception`, never `BaseException`, returns a bool); the orchestrator treats a non-delivered send as the retry path. The injected send callable is threaded from `bot.py`.
- **No backward-compatibility shims.** `INTERVIEW_COMPLETE_TEXT` remains the interview acknowledgement; it is now followed by the Converter step in the same turn.
- **No new dependencies** (`google-adk` is already pinned at `==2.11.0`; `google.genai` is already used by the Bouncer/Interviewer).

## Typed contracts (parse at the edge, prefer schemas over regex)
- **`TraitSummary` (existing, Pydantic v2):** consumed as input; no change.
- **`Converter` (interface):** `async def generate_avatar(portrait_path: Path, summary: TraitSummary) -> bytes` — the real `AdkConverter` holds the key privately and accepts an injected `runner: Callable[[Path, TraitSummary], Awaitable[bytes]]` seam for tests.
- **`SendPhotoFn`:** `Callable[[int, Path, str], Awaitable[bool]]` — `(chat_id, image_path, caption) -> delivered?`, built in `bot.py` over `safe_send_photo`. Returning `False` (or raising) is a send failure.
- **Return value of the Converter step:** `str | None`. `None` means the photo + caption were delivered and the stage advanced to `SCRIPTING`; a string is the text to send to the user (missing-portrait prompt or retry prompt) with the stage left at `CONVERTING`.
- **Failure behaviour:** a missing `GEMINI_API_KEY` or a model/empty-output error is a **converter failure** (friendly retry, state unchanged, no crash). Fail loud in logs (type + message only), friendly to the user.

## Behaviour details
- **Transitions:** only the existing ordered moves. `CONVERTING -> SCRIPTING` is the ordered step; there are **no new edges**. Reset-to-`GREETING` stays legal from any stage (purges media).
- **Handoff:** when `handle_interview_update` completes the interview it sets `CONVERTING` and returns `INTERVIEW_COMPLETE_TEXT`; `bot.dispatch` sends that acknowledgement and then runs the Converter step in the same turn.
- **Retry / self-heal:** an update that arrives while the stage is `CONVERTING` triggers the Converter step again. A successful conversion advances to `SCRIPTING`; a failed one leaves `CONVERTING` so the user's next message retries. On success the step returns `None` (nothing extra to send beyond the photo).
- **Missing media guard:** if the portrait file does not exist (or the summary is absent), return `CONVERTER_MISSING_PORTRAIT_TEXT` **without** calling the model or sending a photo; stage stays `CONVERTING`.
- **Order of operations on success:** read portrait + summary → generate bytes → write `avatar_dest` → send photo with caption → advance to `SCRIPTING` → delete `avatar_dest` → return `None`. The advance happens only **after** a successful send (commit-only-on-success, mirroring the Interviewer).
- **Cleanup on failure:** the generated file (if any) is removed so a retry starts clean; the stage is left at `CONVERTING`.

## Logging
- `@logged` continues to log handler name, `chat_id`, and stage.
- Conversion progress is logged as **stage/outcome only** (e.g. `converter done chat_id=... bytes=N`), never the image bytes, the caption, or the trait-summary text.
- **Never** log the `GEMINI_API_KEY`, key values, image bytes, or secret values.
- Gemini/parse failures log the exception **type + message only** (a full traceback can embed the request payload / image bytes) at `ERROR`; the user gets a friendly, non-technical message.

## Testing
- **Red/Green TDD.** Tests first, fail until implemented.
- **Mock everything external:** the Converter ADK/Gemini calls (injected fake runner returning bytes) and Telegram sends (fake `send_photo`). No network; secrets stay out of tests.
- Unit-test areas:
  - `CONVERTER_MODEL == "gemini-3.1-flash-image"`; constructing `AdkConverter` without a key raises `ConfigurationError` and never logs the key; `generate_avatar` delegates to the injected runner; `LazyConverter` builds once and is key-free at construction.
  - Success: fake converter + fake send → `avatar_dest` written then deleted, the photo is sent with `CONVERTER_CAPTION`, stage advances `CONVERTING -> SCRIPTING`, reply is `None`.
  - Missing portrait file → `CONVERTER_MISSING_PORTRAIT_TEXT`, converter **not** called, no send, stage stays `CONVERTING`.
  - Gemini failure (runner raises) → `CONVERTER_FAILURE_TEXT`, no advance, no send, no crash; the generated file is not left behind.
  - Send failure (send returns `False`, and separately raises) → `CONVERTER_FAILURE_TEXT`, no advance, stage stays `CONVERTING`, no crash.
  - Retry after failure succeeds (second update advances to `SCRIPTING`).
  - `bot.dispatch`: after the interview completes, the Converter runs and `send_photo` is called with the caption; an update at `CONVERTING` re-runs it; `/restart` at `CONVERTING` purges the avatar and resets; deps remain injectable.
  - `safe_send_photo`: swallows a send error (returns `False`), never raises; `send_photo` is not called unless the avatar exists.
  - `media.avatar_dest` path is chat-namespaced; `purge_chat_media` removes the avatar.
  - Pure texts (`CONVERTER_CAPTION`, `CONVERTER_FAILURE_TEXT`, `CONVERTER_MISSING_PORTRAIT_TEXT`) exist, are plain text, and read well.
  - Logging hygiene: no key, no image bytes, no caption/summary text.
- Gates: `scripts/test` (pytest) and `scripts/hooks` (ruff + mypy).

## Dependencies
- **None new.** `google-adk==2.11.0` is already pinned; `google.genai` is already used. No `requirements.txt` change expected.

## Docs to reconcile during validation
- `README.md` (converter usage/status), `SPECS/TECH.md` (Converter now in use; the `CONVERTING` step in `app/orchestrator.py`; converter contract; `safe_send_photo`; image-bytes hygiene), `SPECS/ROADMAP.md` (Feature 3 status already merged; Feature 4 status), and this spec folder. Optionally `SPECS/MISSION.md` principle #1 wording ("return to greeting") **on user approval only**.

## Open questions
- None. Scope, model, image count, missing-portrait behaviour, caption style, and resilience were all fixed by the user.

## Approved hardening addendum (2026-10-18)
This section supersedes conflicting details in the original draft above.
- Keep the existing Feature 4 build and branch. No rebuild or later agents.
- Only conversion is a cancellable per-chat background task in the live app;
  update handlers stay sequential. Registry deduplicates work and invalidates
  session ownership before cancellation. Late work cannot send/advance/clean
  a new session. Both reset commands use this path; shutdown cancels tasks.
- Preserve CancelledError. The image-generation and photo-send deadlines are
  120 seconds and 30 seconds respectively. Ordinary failures offer retry while
  retaining CONVERTING. Missing portrait/summary asks for /restart.
- Raw exception text is NOT safe logging. Converter/send errors record metadata
  only, including filesystem failures; no traceback or payload text.
- PNG/JPEG bytes are decoded/validated with Pillow, MIME checked at the ADK
  boundary, and saved with matching extension. Reject corrupt, unsupported or
  oversized output (10 MiB / 20 million pixels). Pillow is an explicit addition
  to the original no-new-dependencies plan, needed for actual decoding.
- Finally cleanup covers failed writes, sends, generation and cancellation,
  but only while the task owns the session. Reset purges the whole chat folder.
  An unlink failure logs safely; delivered photos never roll back for cleanup.
- Existing 196 test cases retained: invalid byte-string fixtures upgraded to
  real images and the old raw-error logging assertion strengthened. 28 added
  cases cover the approved hardening. Real model/Telegram phone checks pending.
- Telegram cannot recall an already accepted send. Provider calls that ignore
  cancellation cannot be forcibly stopped, but their late results are invalid.
