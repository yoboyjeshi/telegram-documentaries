# Plan — Feature 4: The Converter

**Method:** Red/Green TDD. For each task group: write the failing test first (**Red**), implement the minimum to pass (**Green**), then tidy. Run the dev scripts **`scripts/test`** (pytest) and **`scripts/hooks`** (ruff + mypy) after each group. Keep business logic free of logging (use decorators).

**Builds on:** Feature 1 (`Stage` machine, `InMemorySessionStore`, `@logged`/`@guard_errors`, `safe_send_message`/`dispatch_safely`) and Feature 2 (`photo_dest`, chat-namespaced media, `purge_chat_media`) and Feature 3 (`app/orchestrator.py`; `INTERVIEW -> CONVERTING` handoff with a stored `TraitSummary`).

**Target layout (changes in **bold**)**
```
app/
  __init__.py
  main.py             # entrypoint (unchanged)
  config.py           # Settings (unchanged; ensure_google_api_key reused)
  logging_config.py   # @logged / @guard_errors (unchanged)
  states.py           # Stage machine (unchanged; CONVERTING -> SCRIPTING already ordered)
  session_store.py    # InMemorySessionStore (unchanged)
  telegram_adapter.py # ChatInput (unchanged)
  media.py            # **+ avatar_dest()**
  bouncer.py          # (unchanged)
  interviewer.py      # (unchanged)
  converter.py        # **NEW** Converter protocol, AdkConverter, LazyConverter, CONVERTER_MODEL, prompt
  orchestrator.py     # **+ handle_converting_update (CONVERTING-stage step)**
  photo_flow.py       # (unchanged)
  handlers.py         # **+ converter texts**
  bot.py              # **+ safe_send_photo; LazyConverter; route CONVERTING; run converter on interview handoff**
tests/
  conftest.py
  test_converter.py   # **NEW** model/keys/runner delegation/lazy build/bytes extraction
  test_orchestrator.py# **+ success, missing portrait, converter failure, send failure, retry, cleanup**
  test_media.py       # **+ avatar_dest; purge removes avatar**
  test_handlers.py    # **+ converter texts**
  test_bot.py         # **+ CONVERTING routing, post-interview handoff, safe_send_photo, /restart purge**
scripts/
  test / hooks
```

---

## Task group 1 — Media path for the generated image (Red/Green)
1.1 **Red:** `tests/test_media.py` — `avatar_dest(chat_id)` is chat-namespaced (`<root>/<chat_id>/avatar.png`); `purge_chat_media(chat_id)` removes a directory that contains both `photo.jpg` and `avatar.png`.
1.2 **Green:** `app/media.py` — add `avatar_dest(chat_id, root=None) -> Path` returning `chat_media_dir(...)/"avatar.png"`. (`purge_chat_media` already `rmtree`s the chat dir — no change.)
1.3 Run `scripts/test` + `scripts/hooks`.

## Task group 2 — Converter contract, prompt, ADK impl (Red/Green)
2.1 **Red:** `tests/test_converter.py` — `CONVERTER_MODEL == "gemini-3.1-flash-image"`; constructing `AdkConverter` without a key raises `ConfigurationError` and never logs the key; `generate_avatar(path, summary)` delegates to an injected `runner` and returns its bytes (no network); a failing runner propagates so the orchestrator can catch it; `LazyConverter` builds its delegate once and is key-free at construction (mirrors `LazyBouncer`/`LazyInterviewer`); extracting the inline image bytes from a fake ADK event stream returns the image bytes and raises on an empty/no-image stream.
2.2 **Green:** `app/converter.py` — `Converter` protocol (`generate_avatar`), `AdkConverter` (lazy `google.adk` import; one `LlmAgent(name="converter", model=CONVERTER_MODEL, instruction=CONVERTER_INSTRUCTION)`; message = portrait image part + prompt built from the `TraitSummary`; scan final events for the first inline-data image part), `LazyConverter`, `CONVERTER_MODEL`, `CONVERTER_INSTRUCTION`.
2.3 Run `scripts/test` + `scripts/hooks`.

## Task group 3 — Pure handler texts (Red/Green)
3.1 **Red:** `tests/test_handlers.py` — `CONVERTER_CAPTION`, `CONVERTER_FAILURE_TEXT`, `CONVERTER_MISSING_PORTRAIT_TEXT` exist; the caption is one short line and is plain text (no markdown/emoji characters).
3.2 **Green:** `app/handlers.py` — add the three constants.
3.3 Run `scripts/test` + `scripts/hooks`.

## Task group 4 — Orchestrator: the CONVERTING step (Red/Green)
4.1 **Red:** `tests/test_orchestrator.py` (fake `Converter`; fake `send_photo` returning `True`; `tmp_path` store set to `CONVERTING` with a stored `TraitSummary` and a portrait file):
   - success: `generate_avatar` is called with the portrait path + summary; `avatar_dest` is written then deleted; `send_photo(chat_id, avatar_dest, CONVERTER_CAPTION)` is called; stage becomes `SCRIPTING`; returns `None`;
   - missing portrait file → returns `CONVERTER_MISSING_PORTRAIT_TEXT`; the converter is **not** called; `send_photo` is **not** called; stage stays `CONVERTING`;
   - missing summary (no interview) → same missing-portrait behaviour;
   - converter raises → `CONVERTER_FAILURE_TEXT`; no send; no advance; no raise; `avatar_dest` not left behind;
   - `send_photo` returns `False` → `CONVERTER_FAILURE_TEXT`; no advance; stage `CONVERTING`;
   - `send_photo` raises → `CONVERTER_FAILURE_TEXT`; no advance; no raise;
   - retry: after a failure, a second call with a working converter/send advances to `SCRIPTING`.
4.2 **Green:** `app/orchestrator.py` — `async def handle_converting_update(store, chat_id, *, converter, send_photo, media_root=None) -> str | None` implementing the order above with commit-only-on-success, wrapped Gemini + send calls (log type + message only), and cleanup on failure.
4.3 Run `scripts/test` + `scripts/hooks`.

## Task group 5 — Bot wiring: send photos, route CONVERTING, run on handoff (Red/Green)
5.1 **Red:** `tests/test_bot.py`:
   - `safe_send_photo` sends a photo with a caption and returns `True`; a send error is swallowed (returns `False`, never raises);
   - a `LazyConverter` is built and injected; deps remain injectable;
   - when the interview completes (fake interviewer → summary), the reply is `INTERVIEW_COMPLETE_TEXT` **and** the converter runs: `send_photo` is called with the caption and the stage becomes `SCRIPTING`;
   - an update arriving while `CONVERTING` re-runs the converter (retry → advances on success);
   - `/restart` while `CONVERTING` resets to `AWAITING_PHOTO` and purges the avatar (plus portrait);
   - existing AWAITING_PHOTO/INTERVIEW/command routing stays green.
5.2 **Green:** `app/bot.py` — add `safe_send_photo`; build a `LazyConverter(settings.require_gemini_key)`; in `dispatch`, add a `CONVERTING` branch (retry path) and, after the interview reply is sent, run the Converter step when the stage is now `CONVERTING` (guard against double-running on the same turn); thread `converter` + a `send_photo` closure (`make_send_photo(bot)`) through `dispatch`/`dispatch_safely`/`build_application`.
5.3 Run `scripts/test` + `scripts/hooks`.

## Task group 6 — Docs & spec reconciliation
6.1 Update `README.md` (converter usage; status), `SPECS/TECH.md` (Converter now in use; the `CONVERTING` step in `app/orchestrator.py`; converter contract; `safe_send_photo`; image-bytes hygiene), `SPECS/ROADMAP.md` (Feature 3 → merged; Feature 4 status).
6.2 Record any deviations in this spec's `plan.md`/`validation.md`; optionally reconcile `SPECS/MISSION.md` principle #1 wording **only if the user approves**.
6.3 Run `scripts/test` + `scripts/hooks`.

---

## Guardrails
- **Scope guard:** no Scripter/Narrator, no narration, no TTS, **nothing after `SCRIPTING`**; no new stages; **exactly one** image.
- **Secrets & privacy:** `GEMINI_API_KEY` never logged; image bytes, the caption, and the trait summary never logged; `.env` stays ignored.
- **Mock all externals** in unit tests: no network, no real ADK/Gemini, no real Telegram.
- Keep it simple and general; no config toggles, menus, or speculative abstractions.

## Implementation deviations
_To be filled in during implementation (any deviation from this plan recorded here)._

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
