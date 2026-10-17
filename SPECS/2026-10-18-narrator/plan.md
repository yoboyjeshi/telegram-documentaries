# Feature 6 approved implementation and validation plan

Status: APPROVED by BRIJESH DUTT on 2026-10-18. Branch feature/2026-10-18-narrator, main baseline 0dec595.
Implementation authorized; pause at one PR for Codio phone testing, never merge.

## Implementation after approval

Each slice starts with failing offline tests, then the smallest implementation to
pass. Record Red/Green evidence, not just the final test count.

1. Establish the 439-test baseline and clean ruff/mypy gates in this checkout.
   Check google-genai SDK schema/version compatibility and FFmpeg/libopus availability.
   Do not install system packages without explicit permission.
2. Add typed audio contracts and validation in app/narrator.py and app/audio_format.py.
   Test supported PCM/WAV, MIME parsing, malformed containers, size/duration limits,
   strict types and validation of injected results.
3. Add lazy direct Gemini TTS integration: fixed exact model, Charon voice,
   AUDIO response, stored paragraph only, no ADK, no SDK automatic retries.
   Mock the actual async SDK request/response boundary, not only the high-level protocol.
4. Add bounded cancellable FFmpeg codec seam in app/audio_encoding.py. Offline fixtures
   exercise real local encoding/decoding when the documented codec is available;
   process fakes exercise nonzero exit, malformed output, timeout, cancellation,
   failed termination and pipe size caps. Codec tests must not silently skip in final gates.
5. Add attempt-owned media paths, safe_send_voice and handle_narrating_update.
   Cover send-confirmation-before-DONE, expiry guards, safe errors and file cleanup.
6. Wire same-task Scripter continuation, NARRATING-only retries and terminal DONE.
   Reuse reset/shutdown lifecycle; preserve old injected test seams. Add no new registry.
7. Update README, TECH and ROADMAP, record validation results, run all project gates
   and obtain independent focused review. MISSION.md byte-identical; no broad cleanup.
8. Check staged diff, .env exclusions and every new commit tree; publish exactly one
   Feature 6 PR. Supply Codio setup/phone checklist and stop, no assistant merge.

## Automated acceptance matrix (all Gemini and Telegram calls mocked)

| Area | Required evidence |
| --- | --- |
| Happy path | Stored paragraph passed to exact TTS model, valid artifact saved during send, one voice delivery, NARRATING -> DONE once, artifact removed |
| Integration contract | Direct async SDK request/config, lazy credentials, no ADK, no model substitution, no automatic provider retries; malformed/text-only/multiple-audio responses rejected |
| Format validation | Empty/wrong-type/odd PCM; missing/wrong MIME; contradictory parameters; truncated/mismatched WAV; oversized input/output; excessive duration; broken OGG/Opus; no bad artifact sent |
| Missing input | None/empty/whitespace -> /restart notice, zero provider calls, unchanged NARRATING |
| Failure paths | TTS/encoder/filesystem/Telegram error or timeout; safe retry, unchanged state and narration; failed retry notice bounded and not repeated |
| Privacy | Sentinel key/narration/audio/exception text absent from logs and tracebacks for provider, validation, encoder, write, send and notification failures |
| Deadline defence | Provider or sender suppresses timeout cancellation and returns late: expiry still rejects result; no DONE |
| Reset | /start and /restart during TTS, encoding and voice delivery: cancel, purge, clear narration, AWAITING_PHOTO, no stale state/notification/new send |
| Late provider | Cancellation-resistant fake returns after reset/shutdown; no stale artifact creation, send or state write |
| Replacement session | Old task's finally/error path cannot unlink, overwrite or advance replacement-session work |
| Concurrency | Duplicate active updates cause one TTS task; independent chats progress; cancelling one does not cancel another |
| Routing | Real registered Telegram handlers (mock transport), not just direct dispatch: text/photo/unknown command at NARRATING retries only Narrator; resets take priority |
| Handoff | Converter -> Scripter -> Narrator uses one registry start/version; SCRIPTING retry can finish pipeline; NARRATING retry never re-scripts |
| Shutdown | Existing post_stop closes registry and cancels Narrator/encoder; no post-shutdown send/commit |
| DONE | Later text/photo/non-reset commands cause no TTS, script, conversion, download or stage advance; resets still work |
| Regression | All original 439 tests retained and passing plus new tests; earlier isolation tests can omit Narrator injection, while live-wiring tests mock it explicitly |

Use asyncio Events and registry completion signals for deterministic race tests,
not sleeps/polling. Distinguish reset-before-send from already-accepted delivery;
do not assert that cancellation recalls a Telegram message. Include fixtures for
cancellation-resistant send completion with no stale DONE transition.

## Gates

- scripts/test: full baseline plus Narrator suite, all external network denied.
- scripts/hooks: ruff check app tests and mypy app.
- Real offline codec smoke/decode, SDK-schema compatibility tests, independent review.
- Compare MISSION.md bytes against baseline.
- Verify .env ignored, absent from index and every Feature 6 commit; inspect only
  filenames/status, never secret contents. No secrets or real audio in test fixtures.
- Record actual counts/results only after execution. No Feature 6 test result is
  claimed by this proposal; known historical baseline is 439 passing tests.

## Codio phone checklist after PR, before user merge

1. Verify FFmpeg/libopus availability and install approved dependencies; .env remains
   Codio-local and untracked. Start the bot using the existing long-polling command.
2. Full run: selfie, six answers, avatar, narration text, then a playable voice message.
   Listen for complete, intelligible narration and chosen delivery style.
3. Send another text/photo/unknown command after completion: no extra generation.
4. Run /restart, then finish a second full pipeline in the same process, no reboot.
5. Try /start and /restart during narration. Confirm reset reply and fresh photo intake;
   no late old-session voice send initiated after reset, no stale stage advance.
6. Where feasible, exercise a controlled failure and text retry: script/image are not
   resent; successful narration finishes. Automated tests cover forced faults that
   cannot be reliably triggered manually on a phone.
7. User reports phone-test result. PAUSE until that result; assistant never merges.
