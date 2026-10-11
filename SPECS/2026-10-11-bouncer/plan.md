# Plan — Feature 2: The Bouncer

**Method:** Red/Green TDD. For each task group: write the failing test first (**Red**), implement the minimum to pass (**Green**), then tidy. Run the dev scripts **`scripts/test`** (pytest) and **`scripts/hooks`** (ruff + mypy) after each group. Keep business logic free of logging (use decorators).

**Builds on:** Feature 1 (`app/` gateway, `Stage` machine, `InMemorySessionStore`, `@logged`/`@guard_errors`, `safe_send_message`/`dispatch_safely`).

**Target layout (changes in **bold**)**
```
app/
  __init__.py
  main.py             # entrypoint (unchanged)
  config.py           # Settings (add: raise clear error when gemini key missing at bouncer use)
  logging_config.py   # @logged / @guard_errors (unchanged)
  states.py           # Stage machine  **+ BOUNCER_CHECK -> AWAITING_PHOTO retry edge**
  session_store.py    # InMemorySessionStore (unchanged)
  telegram_adapter.py # ChatInput        **+ kind="photo" + file_id**
  media.py            # **NEW** chat-namespaced temp paths + purge
  bouncer.py          # **NEW** BouncerVerdict, refusal pool, Bouncer protocol, AdkBouncer
  photo_flow.py       # **NEW** async orchestration: gate -> download -> inspect -> apply verdict
  handlers.py         # **+ photo prompts, verdict application, reset purge + advance**
  bot.py              # **+ PHOTO handler wiring + injected deps**
tests/
  conftest.py
  test_config.py
  test_states.py      # **+ retry edge**
  test_session_store.py
  test_handlers.py    # **+ reset purge/advance, photo prompts**
  test_adapter.py     # **+ photo contract**
  test_logging.py
  test_media.py       # **NEW**
  test_bouncer.py     # **NEW**
  test_photo_flow.py  # **NEW** orchestration + resilience
  test_bot.py         # **+ photo wiring**
scripts/
  test                # runs pytest
  hooks               # runs ruff + mypy
pyproject.toml
requirements.txt      # **+ google-adk (pinned to resolved major)**
```

---

## Task group 1 — Dependency & config seam
1.1 Install `google-adk`; add it to `requirements.txt` pinned to the resolved major; record the exact version.
1.2 Confirm `import google.adk` works and `python -c "import app.main"` stays side-effect-free.
1.3 **Red:** `tests/test_config.py` — a helper (e.g. `Settings.require_gemini_key()`) raises a clear, secret-free error when `GEMINI_API_KEY` is missing, and returns the value when present.
1.4 **Green:** add the accessor to `app/config.py` (keep `gemini_api_key` optional at load time, so the bot still starts without it).
1.5 Run `scripts/test` + `scripts/hooks`.

## Task group 2 — Temp media: namespacing + purge (Red/Green)
2.1 **Red:** `tests/test_media.py` — `chat_media_dir(chat_id, root=tmp)` is `<root>/<chat_id>`; two chats get different dirs; `photo_dest` lives inside the chat dir; `purge_chat_media` removes only that chat's files, is idempotent, and tolerates a missing dir; purging chat A never touches chat B.
2.2 **Green:** `app/media.py` — `MEDIA_ROOT`, `chat_media_dir`, `photo_dest`, `purge_chat_media`.
2.3 Run `scripts/test` + `scripts/hooks`.

## Task group 3 — Telegram adapter: photo contract (Red/Green)
3.1 **Red:** `tests/test_adapter.py` — a photo update maps to `ChatInput(kind="photo", file_id=<largest>, chat_id=int)`; a text message still maps to `kind="text"`; a message with neither text nor photo maps to `kind="other"` (+ WARNING); a foreign update maps to `None`.
3.2 **Green:** `app/telegram_adapter.py` — add `file_id` to `ChatInput` and the photo branch (largest `PhotoSize`; no download).
3.3 Run `scripts/test` + `scripts/hooks`.

## Task group 4 — State machine: Bouncer retry edge (Red/Green)
4.1 **Red:** `tests/test_states.py` — `AWAITING_PHOTO -> BOUNCER_CHECK` and `BOUNCER_CHECK -> INTERVIEW` stay legal; **`BOUNCER_CHECK -> AWAITING_PHOTO` becomes legal**; an unrelated backward move (e.g. `INTERVIEW -> AWAITING_PHOTO`) still raises `IllegalTransitionError`; reset-to-`GREETING` stays legal.
4.2 **Green:** `app/states.py` — allow the single retry edge in `can_transition`.
4.3 Run `scripts/test` + `scripts/hooks`.

## Task group 5 — Bouncer contract, refusals, ADK impl (Red/Green)
5.1 **Red:** `tests/test_bouncer.py` — `BouncerVerdict(is_human=...)` schema; `REFUSAL_LINES` is non-empty and `refusal_text(chat_id)` is deterministic and always from the pool; `AdkBouncer` uses `BOUNCER_MODEL == "gemini-3.1-flash-lite"`; constructing it without a key raises a clear error and **never logs the key**; `inspect` delegates to an injected runner and returns its `BouncerVerdict` (no network).
5.2 **Green:** `app/bouncer.py` — `BouncerVerdict`, `BOUNCER_MODEL`, `REFUSAL_LINES`, `refusal_text`, `Bouncer` protocol, `AdkBouncer` (lazy `google.adk` import; ADK `LlmAgent` with `output_schema=BouncerVerdict`; image passed as a multimodal `Part`; injectable runner seam for tests).
5.3 Run `scripts/test` + `scripts/hooks`.

## Task group 6 — Pure photo logic (Red/Green)
6.1 **Red:** `tests/test_handlers.py` — `handle_photo_wrong_stage` returns a gentle redirect and leaves the stage unchanged; `apply_bouncer_verdict(human)` sets `INTERVIEW` and returns `INTERVIEW_READY_TEXT` (contains no question mark / asks nothing); `apply_bouncer_verdict(non-human)` sets `AWAITING_PHOTO` and returns a refusal from the pool.
6.2 **Green:** `app/handlers.py` — `AWAITING_PHOTO_PROMPT`, `PHOTO_REDIRECT_TEXT`, `INTERVIEW_READY_TEXT`, `bouncer_failure_text`, `handle_photo_wrong_stage`, `apply_bouncer_verdict`.
6.3 Run `scripts/test` + `scripts/hooks`.

## Task group 7 — Async orchestration + resilience (Red/Green)
7.1 **Red:** `tests/test_photo_flow.py` (fake download + fake bouncer):
   - photo at `AWAITING_PHOTO` -> sets `BOUNCER_CHECK`, downloads to the chat-namespaced path, inspects, and applies the verdict;
   - human -> `INTERVIEW`; non-human -> `AWAITING_PHOTO` + refusal;
   - photo at a wrong stage -> redirect, **no** download, stage unchanged;
   - **download raises** -> friendly retry reply, stage back to `AWAITING_PHOTO`, no raise;
   - **bouncer raises** -> friendly retry reply, stage back to `AWAITING_PHOTO`, no raise;
   - missing key / `file_id` None -> friendly branch, no raise.
7.2 **Green:** `app/photo_flow.py` — `async def handle_photo_update(store, incoming, *, media_root, bouncer, download) -> str`.
7.3 Run `scripts/test` + `scripts/hooks`.

## Task group 8 — Wiring + full reset purge (Red/Green)
8.1 **Red:** `tests/test_handlers.py` — `handle_start`/`handle_restart` purge the chat's temp media, end at `AWAITING_PHOTO`, and their text mentions sending a photo.
8.2 **Green:** `app/handlers.py` — thread a `media_root` param through `handle_start`/`handle_restart` (default `MEDIA_ROOT`).
8.3 **Red:** `tests/test_bot.py` — a `filters.PHOTO` handler is registered; `dispatch` routes a photo to the photo flow and a text-at-`AWAITING_PHOTO` to the prompt; deps (bouncer, media root, download) are injectable for tests.
8.4 **Green:** `app/bot.py` — register the PHOTO handler, add the text-at-awaiting-photo branch, and inject deps into `build_application`.
8.5 Run `scripts/test` + `scripts/hooks`.

## Task group 9 — Docs & spec reconciliation
9.1 Update `README.md` (photo usage), `SPECS/TECH.md` (ADK now in use; Bouncer model/interface; `google-adk` dependency), `SPECS/ROADMAP.md` (Feature 2 status).
9.2 Record the resolved `google-adk` version and any deviations in this spec's `plan.md`/`validation.md`.
9.3 Run `scripts/test` + `scripts/hooks`.

---

## Guardrails
- **Scope guard:** no Interviewer/Converter/Scripter/Narrator code; **no interview questions**; no new stages.
- **Secrets:** `GEMINI_API_KEY` and image bytes are never logged, never in tests, never committed (`.env` stays ignored).
- **Mock all externals** in unit tests: no network, no real ADK/Gemini, no real Telegram download.
- Keep it simple and general; no config toggles or speculative abstractions.

## Implementation deviations
- **`google-adk` resolved version: `2.11.0`** (pinned exactly in `requirements.txt`); installed and importable in the dev environment. `requirements.txt` was also put in alphabetical order.
- **`LazyBouncer` added** (`app/bouncer.py`): `build_application` constructs the ADK agent lazily on first photo, via `Settings.require_gemini_key`. This keeps the bot's *startup* free of any Gemini key requirement while still failing friendly (not crashing) at first use if the key is absent. Task group 1.4's intent ("keep `gemini_api_key` optional at load time") is preserved.
- **`file_id is None` guard lives in `app/photo_flow.py`** as defence-in-depth. In the real path the adapter already maps a photo with a missing `file_id` to `kind="other"` (WARNING, never raises), so this branch is only reachable when `ChatInput` is built directly (as unit tests do). It returns the friendly download-failure text and recovers the stage, matching the "never crash" contract.
- **`handle_photo_wrong_stage` and the reset handlers keep a `store` parameter** even where unused, to keep the handler signature uniform and call sites simple.
- **ADK app/session identifiers are module constants** (`BOUNCER_APP_NAME`, user/session id `"bouncer"`) — no per-chat session is needed for a stateless single-shot inspection.
- **`AdkBouncer` sets `GOOGLE_API_KEY` from the supplied key via `os.environ.setdefault(...)`** immediately before the lazy ADK imports, so the ADK/Gemini client can authenticate without the key ever being logged.
- No scope drift: no Interviewer/Converter/Scripter/Narrator code, no interview questions, no new stages.
